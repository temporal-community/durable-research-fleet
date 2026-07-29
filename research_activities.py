"""The research Activities — the app layer. `runtime.py` is untouched by this file.

    plan_research        one call, JSON-schema constrained      ~15s
    research_subquestion one call + server-side web search   1-5 min   <- fans out
    synthesize           one call over the collected findings   ~1 min

Those durations are MEASURED, and they are why the timeouts look generous: server-side
web search fetches and filters pages inside one HTTP request. A 120s ceiling produced
four consecutive APITimeoutErrors on the first live run.

The heartbeat runs on a TIMER, not between `pause_turn` rounds. One round can exceed
`heartbeat_timeout`, and the server cannot tell "still thinking" from "instance scaled
away" — so a healthy Activity got timed out and retried, burning tokens already spent.
The timer also means the last heartbeat holds partial research for the retry to
continue from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any

from temporalio import activity

import llm
from research_types import Answer, Checkpoint, Finding, ResearchPlan, SubQuestion

logger = logging.getLogger("research")

# Comfortably below the Workflow's heartbeat_timeout (see research_workflow.py).
# The invariant to preserve is:
#   HEARTBEAT_INTERVAL_SECONDS < HEARTBEAT_TIMEOUT < START_TO_CLOSE
HEARTBEAT_INTERVAL_SECONDS = 15.0

# The FAN-OUT WIDTH — bounds spend and the instance count together, since one slot per
# instance means N sub-questions occupy N Serverless Workers. That is the number the
# room watches, so it is an env var like RESEARCH_EFFORT rather than a constant: a
# `gcloud run worker-pools update --update-env-vars MAX_SUBQUESTIONS=3` retunes the
# demo with no rebuild.
#
# Measured: the planner ALWAYS returns the top of the range, even for a yes/no lookup
# ("Did AWS Lambda raise its 15-minute timeout?" planned 6). So this value, not the
# question, is what decides the width.
MAX_SUBQUESTIONS = max(1, int(os.environ.get("MAX_SUBQUESTIONS", "6")))
MIN_SUBQUESTIONS = min(3, MAX_SUBQUESTIONS)

# Cap on the partial text carried in a heartbeat. Heartbeat details ride every
# heartbeat, so this is not a place to park an unbounded string.
CHECKPOINT_SUMMARY_CHARS = 4_000

# How many sources the final answer carries. A measured run consulted 436 unique
# pages; showing them all buries the answer on a phone screen.
MAX_SOURCES_SHOWN = 25

# The demo's latency knob, and an env var so it is tunable on the day with no
# rebuild. `high` ran a single sub-question past the original 120s timeout.
RESEARCH_EFFORT = os.environ.get("RESEARCH_EFFORT", "medium").strip()

# Planning is schema-constrained and short; `high` here would spend thinking tokens
# on something the schema already pins down.
PLAN_EFFORT = os.environ.get("PLAN_EFFORT", "medium").strip()
SYNTHESIS_EFFORT = os.environ.get("SYNTHESIS_EFFORT", "high").strip()


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

# Byte-identical across every sub-question in a burst so the rest read it from cache
# at ~0.1x input price — the biggest cost lever here. Never interpolate into it.
# LENGTH IS FUNCTIONAL: Opus 5 silently stops caching a prefix under 512 tokens
# (cache_creation_input_tokens just stays 0). Guarded by
# `test_research_prompt_is_cacheable`. Do not trim for tidiness.
SYSTEM_RESEARCH = [
    {
        "type": "text",
        "text": """You are a research analyst working on one narrow sub-question that forms part of a larger investigation. Another analyst will combine your answer with several others, so your job is depth on your specific sub-question rather than breadth across the whole topic. Do not try to answer the broader question you can infer around it.

Method:
- Search the web before you answer. Do not answer from memory, even when you are confident: your training data may be stale, and the entire point of this task is current information.
- Prefer primary sources over commentary about them. An official statistic, regulatory filing, dataset, standards document or first-party announcement beats a news article summarising it, which in turn beats an aggregator summarising the article.
- When a question is time-sensitive, establish how current your sources are and say so explicitly. A number with no date attached is not usable by the analyst who reads your answer.
- Cross-check any figure that matters against a second independent source. If the two disagree, report both, and say which you find more credible and why. Do not silently pick one, and do not average them into a made-up middle.
- Recency and quality are different axes. A newer source is not automatically better: a blog post from this week does not override an official dataset from last quarter. Say which you are relying on.
- Watch for low-quality content: SEO farms, sites that recycle each other's numbers, and AI-generated summaries with no original reporting. Three sites repeating one original claim is one source, not three. Trace a figure to where it actually came from.
- If a source you need is paywalled or unreachable, say so rather than substituting a weaker source silently.
- Be specific about scope. Most real questions are implicitly bounded by place, time period, population or jurisdiction, and an answer for the wrong scope is simply wrong. State the scope you researched.
- Normalise units and currencies, and state which you used. Where a currency figure matters, note the period, because comparing across years without saying so is misleading.
- If the sub-question rests on a false or outdated premise, say that directly and answer what the asker evidently wanted to know instead.
- If the honest answer is that the evidence is thin, contested, or does not exist, say that plainly. A well-evidenced "this is genuinely uncertain, and here is the range of published estimates" is a good answer. A confident answer built on one weak source is not.
- Distinguish what your sources establish from what you are inferring, and mark inferences as inferences.

Output:
- 150 to 250 words of plain prose. No headings, no bullet lists, no tables: your answer gets quoted inside a longer synthesised report, and structure fights that.
- Lead with the answer to the sub-question in your first sentence. Supporting evidence, figures and caveats come after it.
- Attach concrete specifics wherever you have them: numbers, dates, named organisations, named places. Vague summary is the failure mode to avoid.
- Do not pad to reach the word count. If the honest answer is short, keep it short.
- Never invent a statistic, a quotation, a date, or a source. If you did not find it, say you did not find it.""",
        # Cache the prefix. See the comment above.
        "cache_control": {"type": "ephemeral"},
    }
]

SYSTEM_PLAN = """You break a research question into independent sub-questions that can be investigated in parallel.

Good sub-questions are:
- Independent. Researching one must not require the answer to another, because they run simultaneously.
- Narrow enough that a focused web search can answer them well.
- Collectively sufficient. Together they should cover what someone would need to answer the original question properly, including the parts the asker did not think to ask about.
- Non-overlapping. Two sub-questions that would return the same sources are one sub-question.

Prefer concrete, searchable phrasing over abstract framing. If the question is time-sensitive, make at least one sub-question explicitly about the current state or the most recent data."""

SYSTEM_SYNTHESIS = """You are writing the final report answering a research question, using findings that several analysts gathered in parallel.

## Structure

- Open with a direct answer to the question in the first two or three sentences, before any heading. Do not restate the question and do not preface it with what you are about to do.
- Then organise the body under `## ` headings. Choose headings from the material, not from a template: the shape of the answer should follow the shape of the evidence. Four to seven sections is usual.
- Use a final section for what remains uncertain, contested, or unresolved. Name it honestly ("What is still unclear", "Where sources disagree") rather than hedging in every paragraph.
- 800 to 1200 words total. Short paragraphs. Use `- ` bullets only where the content is genuinely a list of parallel items.

## Citations

- Cite with bracketed numbers that refer to the numbered source list in the prompt: `[3]`, or `[3][7]` for several.
- Use ONLY numbers that appear in that list. Never invent a number, never cite a range like `[1-4]`, and never cite a source you were not given.
- Cite the specific claims a reader would want to check: figures, dates, quotes, and anything contested. Do not decorate every sentence.
- Do not append a bibliography or a "Sources" section. The application renders the source list itself.

## Judgement

- Write for someone who has not seen the individual findings and does not know they exist. Never refer to "the findings", "the research above", "sub-question 3", or the analysts. The seams should be invisible.
- Where the findings disagree, say so explicitly and give your reading of which is better supported. Do not average conflicting numbers into a single confident figure.
- Distinguish what is well established from what is uncertain. If the honest answer is "it depends", say what it depends on.
- Keep specifics: numbers, dates, and named organisations are the parts a reader can act on. Drop generic filler."""


# --------------------------------------------------------------------------
# Heartbeating
# --------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _heartbeating(state: Checkpoint, interval: float | None = None):
    """Heartbeat `state` on a timer for as long as the block runs.

    `state` is mutated by the caller, so each tick carries the freshest progress.
    `asyncio.create_task` copies the current context, which is how the background
    task still sees the Activity context that `activity.heartbeat` needs.

    The interval is resolved from the module global at CALL time, not captured as a
    default argument — otherwise tests could not shorten it, and an untestable
    heartbeat is how you find out on stage that it never fired.
    """
    interval = HEARTBEAT_INTERVAL_SECONDS if interval is None else interval

    async def tick() -> None:
        while True:
            await asyncio.sleep(interval)
            activity.heartbeat(state)

    task = asyncio.create_task(tick())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _resume_from() -> Checkpoint | None:
    """The checkpoint from a previous attempt, if this is a retry.

    Heartbeat details come back without type information, so a dataclass arrives
    as a plain dict — handle both rather than assuming.
    """
    details = activity.info().heartbeat_details
    if not details:
        return None
    raw = details[-1]
    if isinstance(raw, Checkpoint):
        return raw
    if isinstance(raw, dict):
        return Checkpoint(
            rounds_done=raw.get("rounds_done", 0) or 0,
            partial_summary=raw.get("partial_summary", "") or "",
            tokens_so_far=raw.get("tokens_so_far", 0) or 0,
        )
    return None


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------


@activity.defn
async def plan_research(question: str) -> ResearchPlan:
    """Split the question into independent, parallel-researchable sub-questions.

    Returns the plan AND its usage. Returning a bare list dropped the planning
    spend — one Opus 5 call per question, missing from the cost counter.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "sub_questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["sub_questions"],
        # Required by the structured-outputs subset. Note the subset does NOT
        # support minItems/maxItems, so the count is asked for in the prompt and
        # enforced below rather than in the schema.
        "additionalProperties": False,
    }

    state = Checkpoint()
    async with _heartbeating(state):
        result = await llm.complete(
            system=SYSTEM_PLAN,
            prompt=(
                f"Research question:\n\n{question}\n\n"
                f"Break this into between {MIN_SUBQUESTIONS} and {MAX_SUBQUESTIONS} "
                "independent sub-questions."
            ),
            max_tokens=4_000,
            effort=PLAN_EFFORT,
            json_schema=schema,
        )

    # The schema guarantees the shape, so a parse failure here means something
    # upstream changed — fail loudly rather than silently researching nothing.
    payload = json.loads(result.text)
    texts = [t.strip() for t in payload["sub_questions"] if t and t.strip()]
    if not texts:
        raise ValueError("planner returned no sub-questions")

    texts = texts[:MAX_SUBQUESTIONS]
    activity.logger.info(
        "planned %d sub-questions (%d tokens)", len(texts), result.usage.total_tokens
    )
    return ResearchPlan(
        sub_questions=[SubQuestion(index=i, text=t) for i, t in enumerate(texts)],
        usage=result.usage,
    )


@activity.defn
async def research_subquestion(sub: SubQuestion) -> Finding:
    """Research one sub-question with server-side web search.

    The fan-out unit: N of these hit the Task Queue at once, N-1 fail to sync-match
    against the current pool, and the Worker Controller scales up to meet them.
    """
    resume = _resume_from()
    state = Checkpoint(
        rounds_done=resume.rounds_done if resume else 0,
        partial_summary=resume.partial_summary if resume else "",
        tokens_so_far=resume.tokens_so_far if resume else 0,
    )

    prompt = f"Sub-question:\n\n{sub.text}"
    if resume and resume.partial_summary:
        # A previous attempt was interrupted — almost certainly by pool-level
        # scale-in. Hand back what it had already established so this attempt
        # extends it instead of re-running the same searches.
        activity.logger.info(
            "resuming sub-question %d from checkpoint (%d rounds, %d tokens already spent)",
            sub.index,
            resume.rounds_done,
            resume.tokens_so_far,
        )
        prompt += (
            "\n\nA previous attempt at this sub-question was interrupted before it "
            "finished. Here is what it had already established:\n\n"
            f"{resume.partial_summary}\n\n"
            "Continue from there. Verify anything that looks shaky, fill the gaps, "
            "and return the complete answer — but do not repeat searches whose "
            "results are already reflected above."
        )

    def checkpoint(partial: llm.LLMResult) -> None:
        """Record a completed research round so a retry can continue from it.

        Called at each `pause_turn` boundary. Writing the actual partial text here
        — not just a round counter — is what makes `resumed` meaningful and what
        the durability credit is computed from.

        Bounded, because heartbeat details ride every heartbeat and this is not a
        place to park an unbounded string.
        """
        state.rounds_done = partial.rounds
        state.partial_summary = partial.text.strip()[:CHECKPOINT_SUMMARY_CHARS]
        state.tokens_so_far = partial.usage.total_tokens
        activity.heartbeat(state)

    try:
        async with _heartbeating(state):
            result = await llm.complete(
                system=SYSTEM_RESEARCH,
                prompt=prompt,
                tools=[llm.WEB_SEARCH_TOOL],
                max_tokens=16_000,
                effort=RESEARCH_EFFORT,
                on_progress=checkpoint,
            )
    except asyncio.CancelledError:
        # Graceful shutdown during scale-in cancels in-flight Activities. Record
        # the freshest checkpoint on the way out so the retry — which will land on
        # a different instance — starts from here rather than from nothing.
        activity.heartbeat(state)
        activity.logger.warning(
            "sub-question %d cancelled mid-flight; checkpoint recorded", sub.index
        )
        raise

    return Finding(
        index=sub.index,
        question=sub.text,
        summary=result.text.strip(),
        sources=result.sources,
        usage=result.usage,
        rounds=result.rounds,
        # The interruption signal the footer actually keys on — see Finding.attempt.
        attempt=activity.info().attempt,
        resumed=bool(resume and resume.partial_summary),
    )


@activity.defn
async def synthesize(question: str, findings: list[Finding]) -> Answer:
    """Combine the findings into one report that CITES the sources by number.

    Order matters here, and it is the whole reason this function is shaped this way.

    The first version built the prompt from the findings, called the model, and only
    then unioned/deduped/capped the sources. That made citations impossible: the model
    never saw the list, so it had no numbers to cite. Worse, telling it to cite anyway
    would have produced references to sources the `MAX_SOURCES_SHOWN` cap then removed
    — a footnote pointing at nothing, on a projector.

    So: build the final numbered list FIRST, including the cap, then prompt with it,
    then return that same list. The numbers the model cites and the numbers the page
    renders come from one object, so they cannot drift apart.
    """
    ordered = sorted(findings, key=lambda f: f.index)

    # 1. The final source list. Deduped by URL, first-seen order so the earlier
    #    sub-questions lead, and capped BEFORE the model is told about it.
    #
    #    The cap is presentational — a measured run consulted 436 unique pages across
    #    six sub-questions, and rendering all of them buries the report. The full set
    #    survives in each Finding and in Workflow history.
    seen: dict[str, int] = {}
    sources: list[llm.Source] = []
    for f in ordered:
        for s in f.sources:
            if s.url not in seen and len(sources) < MAX_SOURCES_SHOWN:
                sources.append(s)
                seen[s.url] = len(sources)  # 1-based citation number

    # 2. The prompt. Each finding carries the global numbers of its own sources, so the
    #    model knows which numbers back which material rather than having to guess from
    #    content alone.
    parts = [f"Original question:\n\n{question}\n\nWhat the analysts found:\n"]
    for f in ordered:
        cites = [seen[s.url] for s in f.sources if s.url in seen]
        marks = "".join(f"[{n}]" for n in sorted(set(cites))) or "(no cited sources)"
        parts.append(f"\n--- On: {f.question}\n{f.summary}\nSources for this: {marks}\n")

    if sources:
        parts.append("\nNumbered sources — cite ONLY these numbers:\n")
        for n, s in enumerate(sources, start=1):
            parts.append(f"[{n}] {s.title or s.url} — {s.url}\n")

    prompt = "".join(parts)

    state = Checkpoint()
    async with _heartbeating(state):
        result = await llm.complete(
            system=SYSTEM_SYNTHESIS,
            prompt=prompt,
            max_tokens=16_000,
            effort=SYNTHESIS_EFFORT,
        )

    return Answer(text=result.text.strip(), sources=sources, usage=result.usage)


# Registered by both workers. A module-level list so adding an Activity does not
# mean editing two worker files.
RESEARCH_ACTIVITIES = [plan_research, research_subquestion, synthesize]
