"""Workflow tests for the research app, with the Activities mocked.

Four things here are worth more than the rest of the suite combined, because each
corresponds to a demo beat that would otherwise fail live:

  - `test_fan_out_creates_real_backlog` — the whole premise. If all N research
    Activities do not land on the Task Queue together, no backlog forms, the Worker
    Controller has nothing to react to, and the pool never scales.
  - `test_pause_waits_then_the_signal_completes_it` — the review pause, which is
    what lets the pool reach zero underneath a live Workflow.
  - `test_review_pause_starts_a_bounded_timer` — an attendee who never comes back
    must not leave a Workflow parked forever.
  - `test_durability_credit_is_recorded` — the number that goes on the screen.

Note there is no time-skipping anywhere in this file, and it is not a preference:
the test server rejects Worker Versioning outright, and every Workflow here
requires it. See the note above `test_review_pause_starts_a_bounded_timer`.
"""

import asyncio
import contextlib
import time

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer

import runtime
from llm import Usage
from research_types import Answer, Finding, ResearchPlan, SubQuestion
from research_workflow import ResearchWorkflow

from .conftest import TASK_QUEUE, make_current

TOKENS_PER_FINDING = 200  # 100 in + 100 out
PLAN_TOKENS = 50          # the planner call, which also has to be counted
SYNTH_TOKENS = 100


def _mocks(*, n_subs=3, resumed=(), retried=(), plan_texts=None):
    """Mocked Activities registered under the real names."""
    calls: dict = {"plan": [], "research": [], "synth": 0}

    @activity.defn(name="plan_research")
    async def plan(question: str) -> ResearchPlan:
        calls["plan"].append(question)
        texts = plan_texts or [f"sub {i}" for i in range(n_subs)]
        return ResearchPlan(
            sub_questions=[SubQuestion(index=i, text=t) for i, t in enumerate(texts)],
            usage=Usage(input_tokens=25, output_tokens=25),
        )

    @activity.defn(name="research_subquestion")
    async def research(sub: SubQuestion) -> Finding:
        calls["research"].append(sub.index)
        return Finding(
            index=sub.index,
            question=sub.text,
            summary=f"found {sub.index}",
            usage=Usage(input_tokens=100, output_tokens=100),
            # attempt>1 is what a real interruption looks like: Temporal re-ran the
            # Activity. Gemini's grounded request is atomic, so `resumed` remains
            # false and the durability credit cannot key on it.
            attempt=2 if sub.index in retried else 1,
            resumed=sub.index in resumed,
        )

    @activity.defn(name="synthesize")
    async def synth(question: str, findings: list[Finding]) -> Answer:
        calls["synth"] += 1
        return Answer(
            text=f"answer from {len(findings)} findings",
            usage=Usage(input_tokens=50, output_tokens=50),
        )

    return [plan, research, synth], calls


@pytest_asyncio.fixture(loop_scope="module")
async def research_worker(env, settings):
    """Production's Worker construction, with mocked Activities.

    The ordering is load-bearing: the Worker has to be POLLING before
    `set_current_version` can resolve its build ID, otherwise the server answers
    "build ID not found in Worker Deployment". Same order as conftest's `worker`.
    """

    @contextlib.asynccontextmanager
    async def build(acts):
        w = runtime.build_worker(env.client, [ResearchWorkflow], acts, settings)
        async with w:
            await make_current(env.client, settings.deployment_name, settings.build_id)
            yield w

    return build


async def _wait_for_stage(handle, stage: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await handle.query("progress")
        if last["stage"] == stage:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached stage {stage!r}; last was {last}")


# --- happy path ------------------------------------------------------------


async def test_plan_research_synthesize_then_accept(env, research_worker, wf_id):
    acts, calls = _mocks(n_subs=4)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "a question", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        answer = await handle.result()

    assert answer.text == "answer from 4 findings"
    assert len(calls["research"]) == 4
    assert calls["synth"] == 1


async def test_query_reports_progress_and_token_usage(env, research_worker, wf_id):
    acts, _ = _mocks(n_subs=3)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        state = await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    assert state["done"] == 3 and state["total"] == 3
    assert state["awaiting_review"] is True
    assert state["draft"]
    # 3 findings + one synthesis + THE PLANNER. Planning spend used to be dropped
    # on the floor; asserting the exact total is what keeps it counted.
    assert state["usage"]["total_tokens"] == 3 * TOKENS_PER_FINDING + 100 + 50


def _all_keys(obj) -> set[str]:
    """Every dict key in a nested payload. KEYS ONLY — deliberately not values."""
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _all_keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _all_keys(v)}
    return set()


async def test_no_money_anywhere_in_the_query(env, research_worker, wf_id):
    """The `progress` payload is the only thing both UIs read, so the no-cost
    decision is enforceable in exactly one place. Two attempts at a dollar figure
    both produced a wrong number — see the comment on the Query.

    Asserts on the payload's KEYS, never on its text. An earlier version of this
    test searched the serialized JSON for "$" and "usd", which passes here only
    because the mocked findings are bland: a *real* answer about serverless cost
    quotes "$0.104/hour" in its own prose, and the live parked run tripped exactly
    that. Model output is content, not schema — the invariant is that this app
    publishes no cost FIELD, not that the answer never says the word.
    """
    acts, _ = _mocks(n_subs=2)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        state = await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    banned = ("usd", "cost", "price", "dollar", "spend")
    offenders = {k for k in _all_keys(state) if any(b in k.lower() for b in banned)}
    assert not offenders, f"currency-denominated keys in the Query payload: {offenders}"
    # The token counters that replaced it must still be there.
    assert {"total_tokens", "cache_read_tokens", "web_searches"} <= set(state["usage"])


# --- partial failure ------------------------------------------------------


async def test_one_refused_subquestion_still_produces_a_report(
    env, research_worker, wf_id
):
    """REGRESSION. The sub-questions are independent — that is the whole premise of
    the fan-out — so one permanent failure must not discard the siblings.

    `asyncio.gather` without `return_exceptions` cancelled the entire fan-out on the
    first failure. Since `RefusalError` is non-retryable, one sub-question tripping a
    safety classifier threw away every finding already paid for, produced no draft,
    and left `stage` on "researching" so the phone span forever with no error.
    """
    calls: dict = {"research": []}

    @activity.defn(name="plan_research")
    async def plan(question: str) -> ResearchPlan:
        return ResearchPlan(
            sub_questions=[SubQuestion(index=i, text=f"sub {i}") for i in range(3)],
            usage=Usage(),
        )

    @activity.defn(name="research_subquestion")
    async def research(sub: SubQuestion) -> Finding:
        calls["research"].append(sub.index)
        if sub.index == 1:
            raise ApplicationError("declined by safety classifiers", non_retryable=True)
        return Finding(index=sub.index, question=sub.text, summary=f"found {sub.index}")

    @activity.defn(name="synthesize")
    async def synth(question: str, findings: list[Finding]) -> Answer:
        return Answer(text=f"report from {len(findings)}", sources=[])

    async with research_worker([plan, research, synth]):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        state = await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    # Two of three survived, and the report was written from them.
    assert state["done"] == 2 and state["total"] == 3
    assert state["draft"] == "report from 2"
    # The failure is REPORTED, not hidden: a short report must be explained.
    assert state["failed"] == ["sub 1"]


async def test_every_subquestion_failing_fails_the_workflow(
    env, research_worker, wf_id
):
    """A partial answer is worth having; no answer is not. With nothing to
    synthesise the Workflow must fail loudly rather than draft from an empty list.
    """

    @activity.defn(name="plan_research")
    async def plan(question: str) -> ResearchPlan:
        return ResearchPlan(
            sub_questions=[SubQuestion(index=i, text=f"sub {i}") for i in range(2)],
            usage=Usage(),
        )

    @activity.defn(name="research_subquestion")
    async def research(sub: SubQuestion) -> Finding:
        raise ApplicationError("nope", non_retryable=True)

    @activity.defn(name="synthesize")
    async def synth(question: str, findings: list[Finding]) -> Answer:
        raise AssertionError("synthesize must not be called with no findings")

    async with research_worker([plan, research, synth]):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()


# --- the premise: a burst must actually queue ------------------------------


async def test_fan_out_creates_real_backlog(env, research_worker, wf_id):
    """The demo's entire premise, asserted from history.

    All N research Activities must be SCHEDULED before the first one completes.
    That simultaneity is what produces sync-match failures and Task Queue backlog,
    which is the only thing the Worker Controller scales on. A Workflow that
    awaited each sub-question in turn would look identical from the outside and
    would never scale the pool past one instance.

    Note the Worker runs one Activity at a time (MAX_CONCURRENT_ACTIVITIES=1), so
    this is about scheduling, not parallel execution — and that is exactly the
    production shape.
    """
    acts, _ = _mocks(n_subs=5)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    # ActivityTaskCompleted carries no activity name, only a scheduled_event_id, so
    # resolve names first. Without this the *planner's* completion closes the gate
    # before any research is scheduled and the count is always zero.
    events = [e async for e in env.client.get_workflow_handle(wf_id).fetch_history_events()]
    name_of: dict[int, str] = {
        e.event_id: e.activity_task_scheduled_event_attributes.activity_type.name
        for e in events
        if e.HasField("activity_task_scheduled_event_attributes")
    }

    scheduled = 0
    for e in events:
        if e.HasField("activity_task_scheduled_event_attributes"):
            if name_of.get(e.event_id) == "research_subquestion":
                scheduled += 1
        elif e.HasField("activity_task_completed_event_attributes"):
            done_id = e.activity_task_completed_event_attributes.scheduled_event_id
            if name_of.get(done_id) == "research_subquestion":
                break  # first research completion — stop counting

    assert scheduled == 5, (
        "all sub-questions must hit the Task Queue together, or no backlog forms "
        f"and the pool never scales (counted {scheduled})"
    )


async def test_research_activity_timeouts_reach_the_server(env, research_worker, wf_id):
    """The serverless-critical settings must land in history, not merely exist in
    source — a refactor that drops heartbeat_timeout makes scale-in undetectable
    for the full 10-minute start_to_close.
    """
    import research_workflow as rw

    acts, _ = _mocks(n_subs=1)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    attrs = [
        e.activity_task_scheduled_event_attributes
        async for e in env.client.get_workflow_handle(wf_id).fetch_history_events()
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name
        == "research_subquestion"
    ]
    assert len(attrs) == 1
    a = attrs[0]
    assert a.start_to_close_timeout.seconds == rw.RESEARCH_START_TO_CLOSE_SECONDS
    assert a.heartbeat_timeout.seconds == rw.RESEARCH_HEARTBEAT_TIMEOUT_SECONDS
    assert a.heartbeat_timeout.seconds < a.start_to_close_timeout.seconds
    # A refusal must not be retried — an identical request gets declined again.
    assert "RefusalError" in list(a.retry_policy.non_retryable_error_types)
    # Slow enough backoff to ride out a 429 when the room fans out at once.
    assert a.retry_policy.maximum_interval.seconds >= 60


# --- the review pause -----------------------------------------------------


async def test_pause_waits_then_the_signal_completes_it(env, research_worker, wf_id):
    """While parked here the Workflow holds no Workflow Task and no Activity Task,
    which is what lets Cloud Run scale the pool to zero with a live Workflow still
    in flight. The Signal is then the sync-match failure that wakes it.
    """
    acts, calls = _mocks(n_subs=2)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        state = await _wait_for_stage(handle, "awaiting_review")
        assert state["draft"], "a draft must exist before the pause"

        # Still parked a moment later — it is genuinely waiting, not racing through.
        await asyncio.sleep(0.4)
        assert (await handle.query("progress"))["stage"] == "awaiting_review"

        await handle.signal("review", args=["accept", ""])
        await handle.result()

    assert calls["plan"] == ["q"], "accepting must not trigger a second plan"
    assert calls["synth"] == 1


async def test_refine_triggers_a_second_fan_out(env, research_worker, wf_id):
    """"Dig deeper" is a fresh plan seeded with the attendee's note, so the pool
    scales up a second time after having been at zero. One round only — a refine
    doubles the spend for that question.
    """
    acts, calls = _mocks(n_subs=2)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["refine", "the mortgage rate angle"])
        answer = await handle.result()

    assert len(calls["plan"]) == 2
    assert "the mortgage rate angle" in calls["plan"][1]
    assert len(calls["research"]) == 4  # two rounds of two
    assert calls["synth"] == 2
    assert answer.text == "answer from 4 findings"


async def test_only_one_refine_round_is_allowed(env, research_worker, wf_id):
    """MAX_REVIEW_ROUNDS is the spend ceiling for a single question."""
    acts, calls = _mocks(n_subs=1)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["refine", "more"])
        # After the refine round the Workflow finishes without pausing again.
        await handle.result()
        # Query INSIDE the worker context: a Query needs a live poller, and outside
        # it the server answers "no poller seen for task queue recently".
        state = await handle.query("progress")

    assert len(calls["plan"]) == 2
    assert state["stage"] == "done"
    assert state["review_rounds"] == 1


async def test_unknown_decision_is_ignored(env, research_worker, wf_id):
    """A malformed Signal must not wake the Workflow into an undefined state."""
    acts, _ = _mocks(n_subs=1)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["nonsense", ""])
        await asyncio.sleep(0.4)
        assert (await handle.query("progress"))["stage"] == "awaiting_review"
        await handle.signal("review", args=["accept", ""])
        await handle.result()


# --- durability accounting ------------------------------------------------


async def test_durability_credit_is_recorded(env, research_worker, wf_id):
    """The number that goes on the dashboard.

    A Finding arrives with `resumed=True` when its Activity picked up partial work
    from a heartbeat, i.e. a previous attempt was interrupted. At that moment the
    tokens already banked in *sibling* findings are what an agent without durable
    state would have had to spend again, because it would have restarted the whole
    task.

    ASSERTED AS AN INVARIANT, NOT AN EXACT NUMBER — do not re-tighten this. An
    earlier version assumed one Activity slot meant findings land in scheduling
    order, so index 1 being resumed implied exactly one banked sibling. That passed
    for a while and then returned 400 instead of 200, because completion order is
    not guaranteed. The credit is legitimately order-dependent; only its bounds are
    stable.
    """
    n = 3
    acts, _ = _mocks(n_subs=n, resumed={1})
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        state = await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    dur = state["durability"]
    # interruptions is order-independent; the credit amount is not (if the
    # interrupted sub-question lands first, nothing was banked yet to protect).
    assert dur["interruptions"] == 1
    assert 0 <= dur["saved_subquestions"] <= n - 1
    # The plan is always banked before any finding, so the floor is never zero.
    assert dur["saved_tokens"] >= PLAN_TOKENS, "the plan is banked before any finding"
    assert dur["saved_tokens"] <= PLAN_TOKENS + (n - 1) * TOKENS_PER_FINDING
    # No money on the wire. An earlier version priced saved tokens at output rates
    # and reported more "saved" than the whole run cost — never on a projector.
    assert "saved_usd" not in dur


async def test_credit_fires_on_a_RETRY_not_only_on_a_resume(
    env, research_worker, wf_id
):
    """REGRESSION, and the most consequential one in this suite.

    The credit originally keyed on `resumed`, but Gemini's server-side search
    grounding finishes atomically inside one request, so `resumed` is never true.

    The consequence was severe and silent: the durability counter would have read 0
    all the way through the one demo moment it exists for. Keying on Temporal's
    Activity `attempt` fixes it, because that is what actually increments when
    pool-level scale-in kills an Activity.

    Every sub-question is marked retried, which makes the expectation
    ORDER-INDEPENDENT: whichever order the findings land in, the k-th to land has k
    siblings already banked, so the total is 0+1+...+(n-1) = n(n-1)/2. Marking a
    single one would be flaky — if the interrupted sub-question happened to complete
    first there was genuinely nothing banked yet to protect.
    """
    n = 3
    # Interrupted, but with NO checkpoint to resume from — the real-world shape.
    acts, _ = _mocks(n_subs=n, retried=set(range(n)))
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        state = await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    assert all(not f["resumed"] for f in state["findings"]), (
        "this test's whole point is that nothing resumed from a checkpoint"
    )
    assert all(f["attempt"] == 2 for f in state["findings"])
    dur = state["durability"]
    assert dur["interruptions"] == n
    assert dur["saved_subquestions"] == n * (n - 1) // 2, (
        "a retried sub-question must earn credit even with no checkpoint to resume "
        "from — otherwise the counter reads 0 during the demo"
    )


async def test_no_interruptions_means_no_credit(env, research_worker, wf_id):
    """The counter must stay honest at zero — no free credit for a clean run."""
    acts, _ = _mocks(n_subs=3)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        state = await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    assert state["durability"] == {
        "interruptions": 0,
        "saved_subquestions": 0,
        "saved_searches": 0,
        "saved_tokens": 0,
    }


# --- failure handling -----------------------------------------------------


async def test_a_permanently_failing_activity_fails_the_workflow(
    env, settings, wf_id
):
    """Retries must be bounded, or one bad sub-question pins a pool instance."""

    @activity.defn(name="plan_research")
    async def plan(question: str) -> ResearchPlan:
        return ResearchPlan(sub_questions=[SubQuestion(index=0, text="sub")])

    @activity.defn(name="research_subquestion")
    async def always_fails(sub: SubQuestion) -> Finding:
        raise ApplicationError("permanent")

    @activity.defn(name="synthesize")
    async def synth(question: str, findings: list[Finding]) -> Answer:
        return Answer(text="never reached")

    w = runtime.build_worker(
        env.client, [ResearchWorkflow], [plan, always_fails, synth], settings
    )
    async with w:
        await make_current(env.client, settings.deployment_name, settings.build_id)
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
            )


# --- replay --------------------------------------------------------------


async def test_history_replays_deterministically(env, research_worker, wf_id):
    """ResearchWorkflow is PINNED, and it can sit parked for hours across a coffee
    break. Non-determinism introduced by an edit during that window would strand a
    live execution, so this is load-bearing rather than routine.
    """
    acts, _ = _mocks(n_subs=3, resumed={2})
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")
        await handle.signal("review", args=["refine", "note"])
        await handle.result()

    history = await env.client.get_workflow_handle(wf_id).fetch_history()
    await Replayer(workflows=[ResearchWorkflow]).replay_workflow(history)


# --- the review timeout --------------------------------------------------
#
# TIME-SKIPPING IS UNAVAILABLE ON THIS PROJECT. `WorkflowEnvironment.start_time_skipping()`
# runs the Java test server, which answers every versioning call with
#
#     RPCError: Worker Versioning not yet supported in test server
#
# and `runtime.build_worker` always sets `use_worker_versioning=True` because
# Serverless Workers require it. So the obvious way to test a two-hour timer — skip
# the clock — cannot be used here at all.
#
# This is a harder constraint than the one in decisions/ D-3.5, which rejected
# time-skipping on the grounds that a skipped clock races past `heartbeat_timeout`.
# That was a reason to prefer `start_local`; this is a reason it is the only option.
#
# So the bound is asserted from history instead: entering the review pause must
# start a Timer of exactly REVIEW_TIMEOUT_SECONDS. That proves the pause is bounded
# and correctly configured without waiting two hours. The branch it guards (accept
# the draft on timeout) is three lines and is exercised for real in Phase 1.


async def test_review_pause_starts_a_bounded_timer(env, research_worker, wf_id):
    """An attendee who wanders off must not leave a Workflow parked forever.

    Asserted on the Timer in history rather than by letting it fire — see the note
    above on why time-skipping is unavailable here.
    """
    import research_workflow as rw

    acts, _ = _mocks(n_subs=2)
    async with research_worker(acts):
        handle = await env.client.start_workflow(
            ResearchWorkflow.run, "q", id=wf_id, task_queue=TASK_QUEUE
        )
        await _wait_for_stage(handle, "awaiting_review")

        timers = [
            e.timer_started_event_attributes
            async for e in handle.fetch_history_events()
            if e.HasField("timer_started_event_attributes")
        ]
        await handle.signal("review", args=["accept", ""])
        await handle.result()

    assert len(timers) == 1, "the review pause must be bounded by exactly one timer"
    assert timers[0].start_to_fire_timeout.seconds == rw.REVIEW_TIMEOUT_SECONDS
    # Long enough to survive a conference coffee break, finite so nothing leaks.
    assert 30 * 60 <= rw.REVIEW_TIMEOUT_SECONDS <= 24 * 60 * 60
