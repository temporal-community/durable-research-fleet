"""Activity-level tests for the research app. No server, no network, no API key.

The heartbeat tests are the important ones. On Cloud Run, scale-in is decided at
the pool level and does not know which instance is mid-Activity, so these two
properties are what stand between the demo and a silently-restarted research task:

  - the heartbeat fires on a TIMER while one long Gemini request is in flight;
  - completed sibling Activities stay committed in Workflow history even if one
    in-flight atomic request is interrupted and retried.
"""

import asyncio
import inspect

import pytest
from temporalio.testing import ActivityEnvironment

import llm
import research_activities as ra
from research_types import Finding, SubQuestion


def _result(text="ok", sources=(), tokens=0, rounds=1):
    return llm.LLMResult(
        text=text,
        sources=[llm.Source(url=u, title=t) for u, t in sources],
        usage=llm.Usage(input_tokens=tokens, output_tokens=tokens),
        rounds=rounds,
    )


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace llm.complete, recording the kwargs it was called with."""

    def install(fn):
        calls = []

        async def wrapper(**kw):
            calls.append(kw)
            return await fn(**kw) if inspect.iscoroutinefunction(fn) else fn(**kw)

        monkeypatch.setattr(llm, "complete", wrapper)
        return calls

    return install


# --- planning --------------------------------------------------------------


async def test_plan_research_returns_subquestions(stub_llm):
    stub_llm(lambda **kw: _result('{"sub_questions": ["one", "two", "three"]}'))
    plan = await ActivityEnvironment().run(ra.plan_research, "big question")
    assert [s.text for s in plan.sub_questions] == ["one", "two", "three"]
    assert [s.index for s in plan.sub_questions] == [0, 1, 2]


async def test_plan_research_reports_its_own_token_spend(stub_llm):
    """REGRESSION. `plan_research` first returned a bare `list[SubQuestion]`, so the
    planning call's tokens were discarded — one real model call per question,
    invisible in the token counter. The first live run showed a completed plan with
    `tokens: 0`.
    """
    stub_llm(lambda **kw: _result('{"sub_questions": ["a", "b", "c"]}', tokens=250))
    plan = await ActivityEnvironment().run(ra.plan_research, "q")
    assert plan.usage.total_tokens == 500, "planning spend must be reported upward"


def test_fan_out_width_is_tunable_without_a_rebuild(monkeypatch):
    """The fan-out width IS the Serverless Worker count one question lights up, so it
    has to be settable on the pool rather than compiled in.

    Measured 2026-07-29: the planner always returns the TOP of the range it is given —
    even "Did AWS Lambda raise its 15-minute timeout?" planned 6 sub-questions. So the
    wording of the question does not control the width; this value does.
    """
    import importlib

    monkeypatch.setenv("MAX_SUBQUESTIONS", "3")
    importlib.reload(ra)
    assert ra.MAX_SUBQUESTIONS == 3
    assert ra.MIN_SUBQUESTIONS == 3

    # MIN must clamp, or a width of 2 asks the planner for "between 3 and 2".
    monkeypatch.setenv("MAX_SUBQUESTIONS", "2")
    importlib.reload(ra)
    assert (ra.MIN_SUBQUESTIONS, ra.MAX_SUBQUESTIONS) == (2, 2)

    monkeypatch.delenv("MAX_SUBQUESTIONS")
    importlib.reload(ra)
    assert ra.MAX_SUBQUESTIONS == 6


async def test_plan_research_caps_the_fan_out(stub_llm):
    """The fan-out is the spend AND the instance count for one question, so the
    ceiling has to be enforced here rather than trusted to the prompt — the
    structured-output subset does not support maxItems.
    """
    many = [f"q{i}" for i in range(20)]
    stub_llm(lambda **kw: _result(f'{{"sub_questions": {many!r}}}'.replace("'", '"')))
    plan = await ActivityEnvironment().run(ra.plan_research, "q")
    assert len(plan.sub_questions) == ra.MAX_SUBQUESTIONS


async def test_plan_research_rejects_an_empty_plan(stub_llm):
    stub_llm(lambda **kw: _result('{"sub_questions": []}'))
    with pytest.raises(ValueError, match="no sub-questions"):
        await ActivityEnvironment().run(ra.plan_research, "q")


async def test_planner_uses_a_schema_and_lower_effort(stub_llm):
    calls = stub_llm(lambda **kw: _result('{"sub_questions": ["a"]}'))
    await ActivityEnvironment().run(ra.plan_research, "q")
    assert calls[0]["json_schema"]["additionalProperties"] is False
    # Planning is constrained by the schema; spending `high` effort on it is waste.
    assert calls[0]["effort"] == ra.PLAN_EFFORT
    assert not calls[0].get("tools")


# --- research: heartbeating ------------------------------------------------


async def test_research_heartbeats_on_a_timer_during_one_long_call(
    monkeypatch, stub_llm
):
    """The bug this guards: failing to heartbeat during one long API request means a
    request longer than heartbeat_timeout looks like a dead Worker, and a
    healthy Activity is killed and retried — burning the tokens it already spent.
    """
    monkeypatch.setattr(ra, "HEARTBEAT_INTERVAL_SECONDS", 0.05)

    async def slow(**kw):
        await asyncio.sleep(0.4)  # one atomic GenerateContent request
        return _result("done")

    stub_llm(slow)

    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *a: beats.append(a)

    await env.run(ra.research_subquestion, SubQuestion(index=0, text="q"))

    # ~8 expected; assert a floor so the test is not brittle under load.
    assert len(beats) >= 3, f"expected timer heartbeats during the call, got {beats}"


async def test_research_heartbeats_when_cancelled(monkeypatch, stub_llm):
    """Graceful shutdown cancels in-flight Activities. A final heartbeat records
    the interruption even though Gemini exposes no partial response to resume.
    """
    monkeypatch.setattr(ra, "HEARTBEAT_INTERVAL_SECONDS", 10)  # no timer beats

    async def hangs(**kw):
        await asyncio.sleep(30)
        return _result()

    stub_llm(hangs)

    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *a: beats.append(a)

    task = asyncio.create_task(
        env.run(ra.research_subquestion, SubQuestion(index=3, text="q"))
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert beats, "cancellation must emit a final heartbeat"


# --- research: atomic response --------------------------------------------


async def test_research_is_not_marked_resumed(stub_llm):
    """GenerateContent is atomic, so a finding never claims partial-call resume."""
    calls = stub_llm(lambda **kw: _result("answer"))
    finding = await ActivityEnvironment().run(
        ra.research_subquestion, SubQuestion(index=0, text="the question")
    )
    assert finding.resumed is False
    assert "interrupted" not in calls[0]["prompt"]
    assert "the question" in calls[0]["prompt"]


# --- research: the fan-out unit carries usage through ---------------------


async def test_finding_carries_usage_and_sources(stub_llm):
    stub_llm(
        lambda **kw: _result(
            "summary",
            sources=[("https://a.example", "A")],
            tokens=500,
            rounds=3,
        )
    )
    f = await ActivityEnvironment().run(
        ra.research_subquestion, SubQuestion(index=2, text="q")
    )
    assert f.index == 2 and f.summary == "summary"
    assert [s.url for s in f.sources] == ["https://a.example"]
    assert f.usage.total_tokens == 1000
    assert f.rounds == 3


async def test_research_declares_web_search_and_a_tunable_effort(stub_llm):
    """Effort is an env var, not a constant: it is the biggest lever on how long the
    room waits, and the first live run showed `high` + web search is slow enough to
    blow a 120s timeout. Tunable on the day without a rebuild.
    """
    calls = stub_llm(lambda **kw: _result())
    await ActivityEnvironment().run(ra.research_subquestion, SubQuestion(0, "q"))
    assert calls[0]["tools"] == [llm.WEB_SEARCH_TOOL]
    assert calls[0]["effort"] == ra.RESEARCH_EFFORT
    assert ra.RESEARCH_EFFORT in ("minimal", "low", "medium", "high")


# --- synthesis -------------------------------------------------------------


async def test_synthesize_unions_sources_in_order(stub_llm):
    stub_llm(lambda **kw: _result("final"))
    findings = [
        Finding(
            index=1,
            question="second",
            summary="B",
            sources=[llm.Source("https://b.example"), llm.Source("https://shared.example")],
        ),
        Finding(
            index=0,
            question="first",
            summary="A",
            sources=[llm.Source("https://a.example"), llm.Source("https://shared.example")],
        ),
    ]
    answer = await ActivityEnvironment().run(ra.synthesize, "q", findings)

    # Ordered by finding index, deduplicated, first-seen order preserved.
    assert [s.url for s in answer.sources] == [
        "https://a.example",
        "https://shared.example",
        "https://b.example",
    ]
    assert answer.text == "final"


async def test_synthesis_prompt_is_ordered_by_index(stub_llm):
    calls = stub_llm(lambda **kw: _result("final"))
    findings = [
        Finding(index=1, question="second", summary="B"),
        Finding(index=0, question="first", summary="A"),
    ]
    await ActivityEnvironment().run(ra.synthesize, "q", findings)
    prompt = calls[0]["prompt"]
    assert prompt.index("first") < prompt.index("second")


# --- citations ------------------------------------------------------------
#
# The report cites sources by number. That only works if the model is SHOWN the
# numbered list before it writes, and if the list it was shown is the same list the
# page renders. The first version of `synthesize` built the prompt from the findings
# and unioned the sources afterwards, so citation was structurally impossible: there
# were no numbers to cite. These four tests pin the ordering that fixed it.


async def test_synthesis_prompt_carries_numbered_sources(stub_llm):
    """The model cannot cite what it was never shown."""
    calls = stub_llm(lambda **kw: _result("final"))
    findings = [
        Finding(
            index=0,
            question="first",
            summary="A",
            sources=[llm.Source("https://a.example", "Ay")],
        ),
    ]
    await ActivityEnvironment().run(ra.synthesize, "q", findings)
    prompt = calls[0]["prompt"]
    assert "[1]" in prompt
    assert "https://a.example" in prompt
    # And the per-finding hint that says which numbers back THIS material.
    assert "Sources for this" in prompt


async def test_synthesis_sources_are_capped_before_the_prompt(stub_llm):
    """A citation to a source the cap removed is a footnote pointing at nothing.

    The cap must therefore apply BEFORE the list reaches the model, not after.
    """
    calls = stub_llm(lambda **kw: _result("final"))
    over = ra.MAX_SOURCES_SHOWN + 12
    findings = [
        Finding(
            index=0,
            question="q",
            summary="s",
            sources=[llm.Source(f"https://s{i}.example") for i in range(over)],
        ),
    ]
    answer = await ActivityEnvironment().run(ra.synthesize, "q", findings)
    prompt = calls[0]["prompt"]

    assert len(answer.sources) == ra.MAX_SOURCES_SHOWN
    # No number beyond the cap is ever offered to the model.
    assert f"[{ra.MAX_SOURCES_SHOWN}]" in prompt
    assert f"[{ra.MAX_SOURCES_SHOWN + 1}]" not in prompt
    # And nothing past the cap is described at all.
    assert f"https://s{over - 1}.example" not in prompt


async def test_citation_numbering_matches_returned_sources(stub_llm):
    """The numbers offered and the numbers rendered come from one list."""
    calls = stub_llm(lambda **kw: _result("final"))
    findings = [
        Finding(index=0, question="a", summary="A", sources=[llm.Source("https://a.example")]),
        Finding(index=1, question="b", summary="B", sources=[llm.Source("https://b.example")]),
    ]
    answer = await ActivityEnvironment().run(ra.synthesize, "q", findings)
    prompt = calls[0]["prompt"]

    for n in range(1, len(answer.sources) + 1):
        assert f"[{n}]" in prompt
    assert f"[{len(answer.sources) + 1}]" not in prompt
    # Each finding is told the global number of its own source, not a local one.
    assert prompt.index("[1]") < prompt.index("[2]")


def test_synthesis_prompt_asks_for_sections_and_citations():
    """Guards the two properties the new UI depends on structurally.

    The outline rail is built from `##` headings and the citation links from `[n]`.
    If this prompt drifts back to "no headings" (as it read until 2026-07-29), the
    left rail silently has nothing to show and every citation disappears.
    """
    s = ra.SYSTEM_SYNTHESIS
    assert "## " in s, "the report must be sectioned or the outline is empty"
    assert "[3]" in s, "the citation format must be shown by example"
    assert "bibliography" in s.lower(), "the app renders sources; the model must not"


def test_research_prompt_uses_no_provider_specific_cache_marker():
    """Gemini implicit caching needs a common prefix, not a manual cache marker."""
    assert isinstance(ra.SYSTEM_RESEARCH, str)
    assert "cache_control" not in ra.SYSTEM_RESEARCH


def test_research_prompt_is_a_constant_not_interpolated():
    """Any per-request variation in the prefix invalidates the cache for every
    later sub-question in the burst.
    """
    text = ra.SYSTEM_RESEARCH
    assert "{" not in text and "%s" not in text


# --- the invariant --------------------------------------------------------


def test_heartbeat_interval_is_under_the_workflow_timeout():
    import research_workflow as rw

    assert (
        ra.HEARTBEAT_INTERVAL_SECONDS
        < rw.RESEARCH_HEARTBEAT_TIMEOUT_SECONDS
        < rw.RESEARCH_START_TO_CLOSE_SECONDS
    )


def test_the_timeout_budget_closes():
    """REGRESSION. The first live run failed with four consecutive APITimeoutErrors
    because the 120s HTTP timeout was far too tight for server-side web search.

    The Gemini seam makes one atomic call, so its per-request timeout must stay below
    the Activity's start_to_close budget. Otherwise a slow-but-healthy request is
    killed by the Activity timeout instead of surfacing as a retryable failure.
    """
    import research_workflow as rw

    assert llm.HTTP_TIMEOUT_SECONDS < rw.RESEARCH_START_TO_CLOSE_SECONDS


def test_http_timeout_is_generous_enough_for_web_search():
    """Google Search grounding can issue queries and fetch sources inside one
    request. The original 120s ceiling was measured to be too short.
    """
    assert llm.HTTP_TIMEOUT_SECONDS >= 240


def test_liveness_is_decoupled_from_call_duration():
    """The reason a 20-minute Activity is still safe: the heartbeat runs on a timer,
    so a dead Worker is detected in ~60s no matter how long the call legitimately
    takes. If these ever became coupled, either liveness detection gets slow or long
    healthy calls get killed.
    """
    import research_workflow as rw

    assert ra.HEARTBEAT_INTERVAL_SECONDS < rw.RESEARCH_HEARTBEAT_TIMEOUT_SECONDS
    assert rw.RESEARCH_HEARTBEAT_TIMEOUT_SECONDS < llm.HTTP_TIMEOUT_SECONDS
