"""The research Workflow — plan, fan out, synthesise, then wait for a human.

`versioning_behavior` is mandatory: a Workflow declaring one is rejected outright if
its Worker is unversioned. PINNED keeps an in-flight execution on the Version it
started on — so **do not deploy a new BUILD_ID while any Workflow is awaiting
review**, because that Version's pool must still exist when it wakes.

Two scaling beats:

1. Fan-out. All N sub-question Activities are scheduled at once, and with one slot
   per instance N-1 cannot sync-match, so the Worker Controller scales out. This is
   the BACKLOG trigger.
2. Wake from zero. After the draft this Workflow blocks on `wait_condition`, holding
   no Task at all, so the pool scales to zero with a live Workflow in flight. The
   review Signal then enqueues a Workflow Task that cannot sync-match — the
   SYNC-MATCH-FAILURE trigger in isolation.

The draft survives that gap in event history. No database, which is the durability
argument made structurally rather than asserted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from llm import DEFAULT_PROVIDER, PROVIDERS, Usage
    from research_activities import plan_research, research_subquestion, synthesize
    from research_types import Answer, Finding, ResearchPlan, ResearchRequest, SubQuestion

# Invariant: HEARTBEAT_INTERVAL (15s) < HEARTBEAT_TIMEOUT < START_TO_CLOSE.
# 1200s leaves headroom around one Gemini request with a 300s HTTP ceiling.
# heartbeat_timeout stays 60s regardless — that is the point of heartbeating on a
# timer: liveness is decoupled from call duration.
RESEARCH_START_TO_CLOSE_SECONDS = 1200
RESEARCH_HEARTBEAT_TIMEOUT_SECONDS = 60

PLAN_START_TO_CLOSE_SECONDS = 300
SYNTHESIS_START_TO_CLOSE_SECONDS = 600
SHORT_HEARTBEAT_TIMEOUT_SECONDS = 60

# How long a draft waits for its human before it is accepted as final. Long
# enough to survive a conference coffee break; finite so an attendee who never
# comes back does not leave a Workflow alive forever.
REVIEW_TIMEOUT_SECONDS = 2 * 60 * 60

# One refine round only. A refine triggers a whole second fan-out, so this is the
# spend ceiling for a single question as well as a complexity ceiling.
MAX_REVIEW_ROUNDS = 1

# Slower than the hello app's on purpose: a room can create many concurrent Gemini
# calls, so 429s are expected. Temporal is the only retry layer.
RESEARCH_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=6,
    # A refusal is not worth retrying: an identical request gets declined again
    # and burns tokens doing it. Surfaced to the attendee instead.
    non_retryable_error_types=["RefusalError"],
)


@dataclass
class _State:
    """Everything the console reads, via the `progress` Query."""

    question: str = ""
    provider: str = DEFAULT_PROVIDER
    stage: str = "planning"
    sub_questions: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # Sub-questions that failed permanently — a refusal, or retries exhausted. The
    # page shows these so a short report is explained rather than just short.
    failed: list[str] = field(default_factory=list)
    draft: str = ""
    answer: str = ""
    sources: list[dict] = field(default_factory=list)
    review_rounds: int = 0

    usage: Usage = field(default_factory=Usage)
    # Work a non-durable agent would have redone. WORK, not money — see _bank_finding.
    saved_tokens: int = 0
    saved_subquestions: int = 0
    saved_searches: int = 0
    interruptions: int = 0


@workflow.defn(versioning_behavior=VersioningBehavior.PINNED)
class ResearchWorkflow:
    def __init__(self) -> None:
        self._s = _State()
        self._decision: str | None = None
        self._note: str = ""
        # Work already committed and safe. What a restart-from-scratch agent would
        # have to do all over again.
        self._banked = 0
        self._banked_subquestions = 0
        self._banked_searches = 0

    # ---------------------------------------------------------------- queries

    @workflow.query
    def progress(self) -> dict:
        """Plain-JSON view of the run. `web.py` reads this and nothing else."""
        s = self._s
        return {
            "question": s.question,
            "provider": s.provider,
            "stage": s.stage,
            "sub_questions": s.sub_questions,
            "findings": [
                {
                    "index": f.index,
                    "question": f.question,
                    "summary": f.summary,
                    "rounds": f.rounds,
                    "attempt": f.attempt,
                    "resumed": f.resumed,
                    "tokens": f.usage.total_tokens,
                }
                for f in sorted(s.findings, key=lambda f: f.index)
            ],
            "done": len(s.findings),
            "total": len(s.sub_questions),
            "failed": s.failed,
            "draft": s.draft,
            "answer": s.answer,
            "sources": s.sources,
            "review_rounds": s.review_rounds,
            "awaiting_review": s.stage == "awaiting_review",
            "usage": {
                "input_tokens": s.usage.input_tokens,
                "output_tokens": s.usage.output_tokens,
                "cache_read_tokens": s.usage.cache_read_tokens,
                "cache_write_tokens": s.usage.cache_write_tokens,
                "web_searches": s.usage.web_searches,
                "total_tokens": s.usage.total_tokens,
            },
            # WORK, not money. Two attempts at a dollar figure both put a wrong
            # number on a projector. No key here is denominated in currency —
            # guarded by `test_no_money_anywhere_in_the_query`.
            "durability": {
                "interruptions": s.interruptions,
                "saved_subquestions": s.saved_subquestions,
                "saved_searches": s.saved_searches,
                "saved_tokens": s.saved_tokens,
            },
        }

    # --------------------------------------------------------------- signals

    @workflow.signal
    def review(self, decision: str, note: str = "") -> None:
        """The attendee's verdict on the draft: "accept" or "refine".

        Arrives from `POST /r/{id}/decision`, possibly a long time after the draft
        was produced and against a pool that has since scaled to zero. Enqueuing
        this Workflow Task is what wakes it.
        """
        if decision not in ("accept", "refine"):
            workflow.logger.warning("ignoring unknown review decision %r", decision)
            return
        self._decision = decision
        self._note = note

    # ------------------------------------------------------------------ run

    @workflow.run
    async def run(self, request: ResearchRequest | str | dict) -> Answer:
        # A plain string remains accepted for CLI/backwards compatibility and uses
        # the public default. The web sends ResearchRequest-shaped JSON so provider
        # choice is explicit in Workflow input and durable event history.
        if isinstance(request, str):
            question = request
            provider = DEFAULT_PROVIDER
        elif isinstance(request, dict):
            question = str(request.get("question", ""))
            provider = str(request.get("provider", DEFAULT_PROVIDER))
        else:
            question = request.question
            provider = request.provider
        provider = provider.strip().lower()
        if provider not in PROVIDERS:
            raise ApplicationError(
                f"unsupported research provider {provider!r}", non_retryable=True
            )

        self._s.question = question
        self._s.provider = provider

        subs = await self._plan(question)
        await self._research(subs)
        answer = await self._draft()

        while self._s.review_rounds < MAX_REVIEW_ROUNDS:
            decision = await self._await_review()
            if decision == "accept":
                break

            self._s.review_rounds += 1
            self._s.stage = "refining"
            # A refine is a fresh plan seeded with what the attendee asked for,
            # so it produces a genuine second fan-out rather than one extra call.
            more = await self._plan(question, note=self._note)
            await self._research(more)
            answer = await self._draft()

        self._s.stage = "done"
        self._s.answer = answer.text
        return answer

    # -------------------------------------------------------------- helpers

    async def _plan(self, question: str, note: str = "") -> list[SubQuestion]:
        self._s.stage = "planning"
        prompt = question if not note else f"{question}\n\nFocus specifically on: {note}"
        plan: ResearchPlan = await workflow.execute_activity(
            plan_research,
            args=[prompt, self._s.provider],
            start_to_close_timeout=timedelta(seconds=PLAN_START_TO_CLOSE_SECONDS),
            heartbeat_timeout=timedelta(seconds=SHORT_HEARTBEAT_TIMEOUT_SECONDS),
            retry_policy=RESEARCH_RETRY,
        )
        # Planning is a real model call and has to be counted. Dropping it made the
        # dashboard read zero tokens for a question that had already done work.
        self._s.usage = self._s.usage + plan.usage
        self._banked += plan.usage.total_tokens

        # Keep indices unique across refine rounds so findings never collide.
        offset = len(self._s.sub_questions)
        subs = [
            SubQuestion(index=offset + i, text=s.text)
            for i, s in enumerate(plan.sub_questions)
        ]
        self._s.sub_questions.extend(s.text for s in subs)
        return subs

    async def _research(self, subs: list[SubQuestion]) -> None:
        """Fan out. This is the burst the Worker Controller reacts to.

        `return_exceptions=True` because the sub-questions are INDEPENDENT — that is
        the premise of the fan-out. Letting one failure propagate cancelled the whole
        gather and discarded every sibling finding already paid for, which is the
        opposite of the argument this demo makes. A refusal is the realistic case:
        `RefusalError` is non-retryable, so one sub-question tripping a safety
        classifier used to fail the entire question with the phone still showing
        "researching in parallel" and no error anywhere.

        A partial answer from five of six sub-questions is worth far more than
        nothing; the report is synthesised from whatever came back.
        """
        self._s.stage = "researching"
        results = await asyncio.gather(
            *(self._one(s) for s in subs), return_exceptions=True
        )
        for sub, result in zip(subs, results):
            if isinstance(result, BaseException):
                self._s.failed.append(sub.text)
                workflow.logger.warning(
                    "sub-question %d failed permanently: %s", sub.index, result
                )

        # Every sub-question failing is different from some failing: there is nothing
        # to write a report from, so say so rather than synthesising an empty draft.
        if not self._s.findings:
            self._s.stage = "failed"
            raise ApplicationError(
                "every sub-question failed; nothing to synthesise",
                non_retryable=True,
            )

    async def _one(self, sub: SubQuestion) -> None:
        finding: Finding = await workflow.execute_activity(
            research_subquestion,
            args=[sub, self._s.provider],
            start_to_close_timeout=timedelta(seconds=RESEARCH_START_TO_CLOSE_SECONDS),
            # The important one on this platform. Cloud Run decides scale-in at the
            # pool level and does not know which instance is mid-Activity, so an
            # Activity can be stopped at any moment. Without this the server waits
            # out the full start_to_close before retrying; with it a lost Worker is
            # detected in ~60s.
            heartbeat_timeout=timedelta(seconds=RESEARCH_HEARTBEAT_TIMEOUT_SECONDS),
            retry_policy=RESEARCH_RETRY,
        )
        self._bank_finding(finding)

    def _bank_finding(self, finding: Finding) -> None:
        """Record a completed finding and, if it was interrupted, what that saved.

        `attempt > 1` means this sub-question was interrupted and re-run — in
        practice by pool-level scale-in. At that moment `self._banked` is the token
        spend already safely committed to the plan and to *other* completed
        findings. An agent without durable state would have restarted the whole task
        and paid for all of it again. That difference is the number worth putting on
        a screen, and it is measured rather than modelled.

        KEYED ON `attempt`, NOT only `resumed`. Gemini's grounded GenerateContent
        request is atomic, so an interrupted call restarts and `resumed` remains
        false; Claude may resume from pause_turn. The durable sibling-work credit
        applies to either provider.
        """
        self._s.findings.append(finding)
        self._s.usage = self._s.usage + finding.usage

        if finding.attempt > 1 or finding.resumed:
            self._s.interruptions += 1
            self._s.saved_tokens += self._banked
            # Counted in the units the stage cares about: how much finished research
            # survived, not what it would have cost to repeat.
            self._s.saved_subquestions += self._banked_subquestions
            self._s.saved_searches += self._banked_searches
            workflow.logger.info(
                "sub-question %d survived an interruption (attempt %d, resumed=%s); "
                "%d sub-questions, %d searches and %d tokens of finished work did "
                "not have to be redone",
                finding.index,
                finding.attempt,
                finding.resumed,
                self._banked_subquestions,
                self._banked_searches,
                self._banked,
            )

        self._banked += finding.usage.total_tokens
        self._banked_subquestions += 1
        self._banked_searches += finding.usage.web_searches

    async def _draft(self) -> Answer:
        self._s.stage = "drafting"
        answer: Answer = await workflow.execute_activity(
            synthesize,
            args=[self._s.question, self._s.findings, self._s.provider],
            start_to_close_timeout=timedelta(seconds=SYNTHESIS_START_TO_CLOSE_SECONDS),
            heartbeat_timeout=timedelta(seconds=SHORT_HEARTBEAT_TIMEOUT_SECONDS),
            retry_policy=RESEARCH_RETRY,
        )
        self._s.usage = self._s.usage + answer.usage
        self._banked += answer.usage.total_tokens
        self._s.draft = answer.text
        self._s.sources = [{"url": s.url, "title": s.title} for s in answer.sources]
        return answer

    async def _await_review(self) -> str:
        """Block until the attendee decides, or the timeout accepts for them.

        While this awaits, the Workflow holds no Task of any kind — which is what
        lets the pool go to zero underneath a live Workflow.
        """
        self._s.stage = "awaiting_review"
        self._decision = None
        workflow.logger.info("draft ready; awaiting review (pool may scale to zero)")
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(seconds=REVIEW_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            workflow.logger.info("review timed out; accepting the draft as final")
            return "accept"
        return self._decision or "accept"
