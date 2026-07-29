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
    unbounded Claude spend reachable by anyone with the URL.
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
