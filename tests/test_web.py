"""Tests for the web tier's guards. No server, no Temporal, no API key.

These cover the controls that bound spend on a page anyone in the room can reach.
`terraform/web.tf` names the per-IP rate limit as one of four reasons the public
endpoint is acceptable, so a limiter that can be bypassed is not a hygiene issue —
it is the reason the endpoint is safe, absent.
"""

import inspect
import pathlib
import time

import web


class _Req:
    """Minimal stand-in: `_client_ip` only touches `.headers` and `.client`."""

    def __init__(self, forwarded=None, peer="10.0.0.1"):
        self.headers = {} if forwarded is None else {"x-forwarded-for": forwarded}
        self.client = type("C", (), {"host": peer})() if peer else None


def test_client_ip_never_trusts_the_first_forwarded_hop(monkeypatch):
    """REGRESSION. Google's front end APPENDS to whatever X-Forwarded-For the client
    sent, so the FIRST entry is attacker-chosen. Reading it let a caller rotate the
    header for a fresh rate-limit bucket per request, making the limit inert and
    unbounded Gemini spend reachable by anyone with the URL.
    """
    monkeypatch.setattr(web, "_TRUST_XFF", True)
    monkeypatch.setattr(web, "XFF_HOPS_FROM_END", 1)

    assert web._client_ip(_Req("1.2.3.4, 203.0.113.9")) == "203.0.113.9"
    # Whitespace and empty hops must not produce a distinct bucket.
    assert web._client_ip(_Req("1.2.3.4 , , 203.0.113.9 ")) == "203.0.113.9"
    # A rotating forged prefix must map to ONE bucket, not many.
    seen = {web._client_ip(_Req(f"9.9.9.{i}, 203.0.113.9")) for i in range(20)}
    assert seen == {"203.0.113.9"}


def test_client_ip_honours_the_hop_count_behind_a_load_balancer(monkeypatch):
    """Behind an external ALB the appended tail is `<client>, <forwarding-rule IP>`,
    so the caller is second-to-last. Taking the last would give every attendee the
    load balancer's address and put the whole room in one bucket.
    """
    monkeypatch.setattr(web, "_TRUST_XFF", True)
    monkeypatch.setattr(web, "XFF_HOPS_FROM_END", 2)
    assert web._client_ip(_Req("1.2.3.4, 203.0.113.9, 34.1.1.1")) == "203.0.113.9"


def test_client_ip_ignores_the_header_when_not_behind_a_proxy(monkeypatch):
    """Locally nothing appends the header, so an inbound one is pure client input."""
    monkeypatch.setattr(web, "_TRUST_XFF", False)
    assert web._client_ip(_Req("9.9.9.9", peer="127.0.0.1")) == "127.0.0.1"


def test_client_ip_falls_back_to_the_peer(monkeypatch):
    monkeypatch.setattr(web, "_TRUST_XFF", True)
    assert web._client_ip(_Req(None, peer="198.51.100.7")) == "198.51.100.7"
    assert web._client_ip(_Req("", peer="198.51.100.7")) == "198.51.100.7"
    assert web._client_ip(_Req(None, peer=None)) == "unknown"


def test_rate_limit_blocks_the_fourth_ask(monkeypatch):
    monkeypatch.setattr(web, "_asks", {})
    ip = "203.0.113.1"
    allowed = [web._rate_limited(ip) for _ in range(web.ASKS_PER_MINUTE_PER_IP + 1)]
    assert allowed[:-1] == [False] * web.ASKS_PER_MINUTE_PER_IP
    assert allowed[-1] is True


def test_rate_limiter_evicts_idle_buckets(monkeypatch):
    """The limiter used to prune timestamps within a bucket but never remove keys,
    so a stream of requests with varying addresses grew `_asks` without bound until
    the instance was OOM-killed. Eviction is what makes it survive being hammered.
    """
    monkeypatch.setattr(web, "_asks", {})
    stale = time.monotonic() - 3600
    web._asks.update({f"10.0.0.{i}": [stale] for i in range(500)})

    web._rate_limited("203.0.113.2")

    assert len(web._asks) == 1, "idle buckets must be swept"
    assert "203.0.113.2" in web._asks


def test_passcode_is_case_insensitive(monkeypatch):
    """Phone keyboards auto-capitalise the first letter of a text field, so a
    lowercase passcode announced from the stage arrives capitalised for most of the
    room. A case-sensitive check would reject the majority on their first attempt,
    which reads as a broken demo rather than a typo.

    Uses a throwaway value on purpose — the real passcode lives in
    terraform.tfvars, which is gitignored, and must not be committed here.
    """
    monkeypatch.setattr(web, "DEMO_PASSCODE", "testcode")
    for supplied in ("testcode", "Testcode", "TESTCODE", "tEsTcOdE", "  Testcode  "):
        assert web._passcode_ok(supplied), supplied
    for supplied in ("testcode1", "testcod", "", None, "wrong"):
        assert not web._passcode_ok(supplied), supplied


def test_no_passcode_configured_means_open(monkeypatch):
    monkeypatch.setattr(web, "DEMO_PASSCODE", "")
    assert web._passcode_ok(None)
    assert web._passcode_ok("anything")


def test_provider_is_chosen_in_the_ui_and_claude_is_default():
    page = (pathlib.Path(__file__).parent.parent / "web" / "index.html").read_text()
    claude = '<option value="anthropic" selected>'
    gemini = '<option value="gemini">'
    assert gemini in page and claude in page
    assert page.index(claude) < page.index(gemini)
    assert "JSON.stringify({ question, provider" in page

    src = inspect.getsource(web.ask)
    assert 'body.get("provider")' in src
    assert '"provider": provider' in src
    assert "provider not in PROVIDERS" in src


def test_every_endpoint_that_spends_money_checks_the_passcode():
    """REGRESSION, found reviewing the Code Exchange submission. `/api/ask` was
    guarded by both the passcode and the per-IP limit; `/api/run/{id}/decision` was
    guarded by neither — yet a `refine` decision starts a SECOND fan-out and costs the
    same as a fresh question. Knowing a run id was enough to spend Gemini tokens
    repeatedly, bypassing both controls.

    Asserted on the source because exercising it needs a Temporal client: the guards
    have to run BEFORE the Signal is sent, and a test that mocked the client away
    would pass with the checks in the wrong order.
    """
    src = inspect.getsource(web.decide)
    assert "_passcode_ok" in src, "the review Signal must check the passcode"
    assert "_rate_limited" in src, "a refine starts a second fan-out; bound it"

    # Order matters: both guards precede the Signal, or they guard nothing.
    assert src.index("_passcode_ok") < src.index('signal("review"')
    assert src.index("_rate_limited") < src.index('signal("review"')


def test_the_passcode_comparison_is_constant_time():
    """`==` on a secret is the wrong pattern to publish in a repo people copy from,
    and it is what static scanners flag. The timing signal on a short shared passcode
    over HTTPS is not a practical attack — this is about what the example teaches.
    """
    src = inspect.getsource(web._passcode_ok)
    assert "compare_digest" in src


def test_there_is_no_run_listing_endpoint():
    """Run ids are `research-<uuid4 hex>`, so /api/run/{id} is capability-based —
    you can only read a run whose link you were given. An endpoint enumerating those
    ids on a public Service would make every attendee's question readable by a
    stranger, so it was removed rather than left as an unused ops convenience.
    """
    paths = {getattr(r, "path", None) for r in web.app.routes}
    assert "/api/runs" not in paths


def test_only_a_missing_run_is_a_404():
    """REGRESSION. A Query needs a Worker with a free workflow-task slot, and this
    platform routinely has none — the pool sits at zero until the WCI reacts, and
    during fan-out every instance is inside a multi-minute research Activity. Those
    Queries fail with "Timeout expired".

    Mapping every exception to 404 made the page treat a healthy run as gone about
    30 seconds in, which is exactly the scale-from-zero window the demo is about.
    Only NOT_FOUND may be 404; everything else is 503 so the page keeps polling.
    """
    src = inspect.getsource(web.run_state)
    assert "RPCStatusCode.NOT_FOUND" in src, "404 must be gated on NOT_FOUND"
    assert "status_code=503" in src, "transient query failures must be 503"
    # And the page must not treat 503 as terminal.
    page = (pathlib.Path(__file__).parent.parent / "web" / "index.html").read_text()
    assert "res.status === 503" in page
    assert "runFailed" in page


# --- parked-run cache --------------------------------------------------------------------


def _state(stage="awaiting_review"):
    return {"stage": stage, "awaiting_review": stage == "awaiting_review", "draft": "d"}


def test_only_a_parked_run_is_cached(monkeypatch):
    """A running run's counters move; serving them stale would freeze the progress bar."""
    monkeypatch.setattr(web, "_review_cache", {})
    web._review_cache_put("r1", _state("researching"))
    assert web._review_cache_get("r1") is None
    web._review_cache_put("r1", _state())
    assert web._review_cache_get("r1")["stage"] == "awaiting_review"


def test_a_run_leaving_review_is_evicted(monkeypatch):
    monkeypatch.setattr(web, "_review_cache", {})
    web._review_cache_put("r1", _state())
    web._review_cache_put("r1", _state("synthesizing"))
    assert web._review_cache_get("r1") is None


def test_the_cache_expires(monkeypatch):
    monkeypatch.setattr(web, "_review_cache", {})
    monkeypatch.setattr(web, "REVIEW_CACHE_TTL_SECONDS", 0.0)
    web._review_cache_put("r1", _state())
    time.sleep(0.01)
    assert web._review_cache_get("r1") is None


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(web, "_review_cache", {})
    monkeypatch.setattr(web, "REVIEW_CACHE_MAX", 3)
    for i in range(10):
        web._review_cache_put(f"r{i}", _state())
    assert len(web._review_cache) == 3
    assert web._review_cache_get("r9") is not None  # newest survives, oldest evicted


async def test_a_decision_drops_the_cache_before_signalling(monkeypatch):
    """Dropping AFTER the Signal would let a poll in between re-cache the old state."""
    monkeypatch.setattr(web, "_review_cache", {})
    web._review_cache_put("r1", _state())
    order = []

    class Handle:
        async def signal(self, *a, **k):
            order.append(("signal", "r1" in web._review_cache))

    class Client:
        def get_workflow_handle(self, _):
            return Handle()

    async def fake_temporal():
        return Client()

    class Req:
        client = type("C", (), {"host": "1.1.1.1"})()
        headers = {}

        async def json(self):
            return {"decision": "accept"}

    monkeypatch.setattr(web, "temporal", fake_temporal)
    monkeypatch.setattr(web, "DEMO_PASSCODE", "")
    await web.decide("r1", Req())
    assert order == [("signal", False)], "the cache entry must already be gone at Signal time"


def test_the_web_default_provider_matches_the_workflow_default():
    """web.py cannot import llm, so the two constants are kept equal by this test. A drift
    would make the page and a provider-less request disagree about which model runs.
    """
    import llm

    assert web.DEFAULT_PROVIDER == llm.DEFAULT_PROVIDER
    assert web.PROVIDERS == llm.PROVIDERS


def test_the_page_names_no_single_compute_platform():
    """The same page is served in front of Cloud Run and AgentCore deployments, so its copy
    must not claim one. (It said "on Cloud Run workers" until the AgentCore target landed.)
    """
    page = (pathlib.Path(__file__).parent.parent / "web" / "index.html").read_text()
    for platform in ("Cloud Run", "AgentCore", "Lambda"):
        assert platform not in page, f"index.html names {platform}; keep the copy platform-neutral"


def test_fleet_payload_names_the_platform(monkeypatch):
    """`fleet run` exports FLEET_TARGET so the footer can say which platform is live."""
    src = inspect.getsource(web._read_fleet)
    assert '"target"' in src and "FLEET_TARGET" in src and '"namespace"' in src


# --------------------------------------------------------------- live activities
#
# The page must keep telling the truth while every Worker is gone, which is when a Query
# cannot answer. These build real PendingActivityInfo protos: no server, no Worker.

from google.protobuf.timestamp_pb2 import Timestamp  # noqa: E402
from temporalio.api.common.v1 import Payload, Payloads  # noqa: E402
from temporalio.api.enums.v1 import PendingActivityState  # noqa: E402
from temporalio.api.failure.v1 import Failure  # noqa: E402
from temporalio.api.workflow.v1 import PendingActivityInfo  # noqa: E402

NOW = 1_000_000.0


def _ts(seconds: float) -> Timestamp:
    t = Timestamp()
    t.FromNanoseconds(int(seconds * 1e9))
    return t


def _pa(activity_id="2", attempt=1, state="STARTED", beat_ago=None, started_ago=5.0,
        next_in=None, failure=None, checkpoint=False, worker="1@localhost-ab"):
    pa = PendingActivityInfo(
        activity_id=activity_id,
        attempt=attempt,
        state=getattr(PendingActivityState, f"PENDING_ACTIVITY_STATE_{state}"),
        last_worker_identity=worker,
    )
    pa.activity_type.name = "research_subquestion"
    if started_ago is not None:
        pa.last_started_time.CopyFrom(_ts(NOW - started_ago))
    if beat_ago is not None:
        pa.last_heartbeat_time.CopyFrom(_ts(NOW - beat_ago))
    if next_in is not None:
        pa.next_attempt_schedule_time.CopyFrom(_ts(NOW + next_in))
    if failure:
        pa.last_failure.CopyFrom(Failure(message=failure))
    if checkpoint:
        pa.heartbeat_details.CopyFrom(Payloads(payloads=[Payload(data=b'{"text":"partial"}')]))
    return pa


def _phase(pa, labels=None):
    return web.describe_pending_activities([pa], labels or {}, NOW)[0]


def test_a_heartbeating_first_attempt_is_running_and_quiet():
    row = _phase(_pa(beat_ago=3.0))
    assert row["phase"] == "running" and row["attempt"] == 1 and not row["worker_lost"]


def test_no_heartbeat_for_a_while_reads_as_silent_before_temporal_gives_up():
    row = _phase(_pa(beat_ago=34.0))
    assert row["phase"] == "silent" and row["silent_for"] == 34.0


def test_a_lost_worker_shows_a_retry_countdown_and_its_checkpoint():
    row = _phase(_pa(attempt=2, state="SCHEDULED", next_in=12.0,
                     failure="Activity heartbeat timeout", checkpoint=True))
    assert row["phase"] == "retry_wait"
    assert row["retry_in"] == 12.0
    assert row["worker_lost"] and row["has_checkpoint"]


def test_after_the_backoff_it_waits_for_a_worker_not_a_countdown():
    row = _phase(_pa(attempt=2, state="SCHEDULED", next_in=-3.0))
    assert row["phase"] == "retrying" and row["retry_in"] is None


def test_a_first_attempt_nobody_has_taken_is_queued():
    assert _phase(_pa(state="SCHEDULED", started_ago=None))["phase"] == "queued"


def test_an_ordinary_failure_is_not_called_a_lost_worker():
    row = _phase(_pa(attempt=2, state="SCHEDULED", next_in=5.0, failure="Error code: 429 rate limited"))
    assert not row["worker_lost"] and row["last_failure"].startswith("Error code: 429")


def test_long_failures_are_clipped_to_one_line():
    row = _phase(_pa(attempt=2, state="SCHEDULED", failure="x\n" * 400))
    assert "\n" not in row["last_failure"] and len(row["last_failure"]) <= 160


def test_rows_carry_their_sub_question_and_sort_by_it():
    pas = [_pa(activity_id="4"), _pa(activity_id="3")]
    labels = {"3": {"index": 0, "text": "first"}, "4": {"index": 1, "text": "second"}}
    rows = web.describe_pending_activities(pas, labels, NOW)
    assert [r["index"] for r in rows] == [0, 1] and rows[0]["text"] == "first"


async def test_labels_are_read_from_history_once_and_survive_a_bad_payload(monkeypatch):
    from temporalio.api.history.v1 import HistoryEvent

    def scheduled(activity_id, name):
        ev = HistoryEvent()
        a = ev.activity_task_scheduled_event_attributes
        a.activity_id = activity_id
        a.activity_type.name = name
        a.input.payloads.add(data=b"{}")
        return ev

    class Handle:
        calls = 0

        async def fetch_history_events(self):
            Handle.calls += 1
            for ev in (scheduled("1", "plan_research"), scheduled("2", "research_subquestion"),
                       scheduled("3", "research_subquestion")):
                yield ev

    class Converter:
        async def decode(self, payloads):
            if len(payloads) and payloads[0].data == b"{}" and Converter.n == 0:
                Converter.n += 1
                return [{"index": 0, "text": "why?"}]
            raise ValueError("undecodable")

        n = 0

    class FakeClient:
        data_converter = Converter()

    monkeypatch.setattr(web, "_labels", {})
    got = await web._activity_labels(FakeClient(), Handle(), "wf", {"1", "2", "3"})
    assert got["2"] == {"index": 0, "text": "why?"}
    assert got["3"] == {} and got["1"] == {}        # a bad payload costs a label, not the page
    await web._activity_labels(FakeClient(), Handle(), "wf", {"2"})
    assert Handle.calls == 1                       # labels are immutable, so history is read once


def test_the_page_polls_the_worker_free_endpoint():
    """The XSS guard (`test_the_page_never_uses_innerhtml`) covers this panel too: every
    row is built with createElement and textContent."""
    html = pathlib.Path("web/index.html").read_text()
    assert "/activities" in html and "renderLive" in html
