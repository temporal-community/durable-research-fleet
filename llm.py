"""The Claude seam. Everything that knows about the Anthropic API lives here.

MUST NOT import `temporalio` — that keeps it unit-testable without a server and keeps
the retry story in one place. Progress is reported via an injected `on_progress`
callback, which the Activity wires to `activity.heartbeat`.

Three load-bearing facts:

1. Web search is a SERVER-side tool. `web_search_20260209` runs on Anthropic's
   infrastructure, so researching a question is ONE API call, not a client-side tool
   loop. No scraper, no search API key.
2. `max_retries=0`. Temporal is the only retry layer; two backoff loops multiply into
   latency nobody can reason about.
3. `pause_turn` is the checkpoint boundary — a server-tool loop that hits its
   iteration cap returns it instead of finishing, and each boundary is where we
   heartbeat so a scale-in kill resumes from the last completed round.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import anthropic

logger = logging.getLogger("llm")

# --------------------------------------------------------------------------
# Model and pricing
# --------------------------------------------------------------------------

MODEL = "claude-opus-5"

# Server-side web search. `_20260209` is the variant documented as supported on
# Opus 5 and carries dynamic filtering (Claude writes code to filter results
# before they reach the context window), so we must NOT also declare
# code_execution — a second execution environment confuses the model.
# A newer `web_search_20260318` exists in the SDK; untested on this path.
WEB_SEARCH_TOOL: dict[str, str] = {"type": "web_search_20260209", "name": "web_search"}

# No pricing table, deliberately: a hardcoded rate card goes stale silently and the
# only symptom is a wrong number on a projector. Tokens and searches are the units.

# A safety classifier declining on stage would end the demo, so a refusal is re-run
# on the recommended fallback inside the same call. "default" lets it route by
# refusal category. Kill switch: CLAUDE_FALLBACKS=false
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS_ENABLED = os.environ.get("CLAUDE_FALLBACKS", "true").strip().lower() == "true"

# Per-ROUND, not per Activity: `complete()` makes up to MAX_PAUSE_RESUMES + 1 calls.
# The BUDGET MUST CLOSE — rounds x this must stay under the Activity's
# start_to_close, or a slow-but-healthy call is killed by the Activity timeout
# instead of surfacing as retryable. 120s here caused four live timeouts.
# Guarded by test_the_timeout_budget_closes.
HTTP_TIMEOUT_SECONDS = float(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "300"))

# Low on purpose so the worst case fits the budget above: 3 x 300s < 1200s.
MAX_PAUSE_RESUMES = 2


class RefusalError(Exception):
    """Claude declined and no fallback accepted it.

    NOT retryable: an identical refused request is declined again and burns tokens.
    """


# --------------------------------------------------------------------------
# Usage accounting — tokens, cache reads and searches. No money.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """Token spend for one or more calls. Plain dataclass so Temporal carries it."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            web_searches=self.web_searches + other.web_searches,
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @classmethod
    def from_response(cls, response: Any) -> "Usage":
        u = getattr(response, "usage", None)
        if u is None:
            return cls()
        server = getattr(u, "server_tool_use", None)
        return cls(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            web_searches=getattr(server, "web_search_requests", 0) or 0 if server else 0,
        )


@dataclass(frozen=True)
class Source:
    """One page Claude actually consulted, for citation in the final answer."""

    url: str
    title: str = ""


@dataclass
class LLMResult:
    text: str = ""
    sources: list[Source] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # How many pause_turn boundaries this call crossed. Recorded so a resumed
    # Activity can report how much work it kept rather than redid.
    rounds: int = 1


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    """Process-wide async client, reused so connections stay warm."""
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. The research app needs it; the hello "
                "app does not, so `make verify SCALE=1` still works without it."
            )
        _client = anthropic.AsyncAnthropic(
            timeout=HTTP_TIMEOUT_SECONDS,
            # See the module docstring: Temporal owns retry.
            max_retries=0,
        )
    return _client


def _extract(response: Any, into: LLMResult) -> None:
    """Pull text and consulted sources out of one response, appending to `into`."""
    seen = {s.url for s in into.sources}
    for block in response.content or []:
        kind = getattr(block, "type", None)

        if kind == "text":
            into.text += getattr(block, "text", "") or ""

        elif kind == "web_search_tool_result":
            content = getattr(block, "content", None)
            # On success `content` is a LIST of results; on failure it is a single
            # error object. Branching on that is required, not defensive padding.
            if not isinstance(content, list):
                code = getattr(content, "error_code", "unknown")
                logger.warning("web_search failed: %s", code)
                continue
            for result in content:
                url = getattr(result, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    into.sources.append(
                        Source(url=url, title=getattr(result, "title", "") or "")
                    )


async def complete(
    *,
    system: str | list[dict[str, Any]],
    prompt: str,
    max_tokens: int = 16_000,
    effort: str = "high",
    tools: Iterable[dict[str, Any]] | None = None,
    json_schema: dict[str, Any] | None = None,
    on_progress: Callable[["LLMResult"], None] | None = None,
) -> LLMResult:
    """One logical Claude turn, resumed across `pause_turn` boundaries.

    `json_schema` constrains the reply to that shape, so the planner shares this
    one code path (fallbacks, refusal handling, usage accounting) instead of
    having its own. Note it cannot be combined with citations, which is fine
    because the schema path never passes tools.

    `on_progress(partial)` is called at each `pause_turn` boundary with the
    accumulated result so far — text, sources, usage and round count.

    It receives the whole partial rather than just a round number, and that
    matters: the caller writes it into an Activity heartbeat, so a retry after a
    scale-in interruption can continue from the research already done. An earlier
    version passed only the round index, which meant the checkpoint carried no
    content, `resumed` was structurally always False, and the durability credit
    could never fire. Unit tests missed it because they injected checkpoints
    directly instead of producing one.

    Raises RefusalError if the request was declined and no fallback accepted it.
    """
    output_config: dict[str, Any] = {"effort": effort}
    if json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}

    request: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        # Adaptive thinking is the only supported mode on Opus 5 and is on by
        # default; stated explicitly so the intent is visible. `display` is left
        # at its default ("omitted") because we never surface reasoning.
        "thinking": {"type": "adaptive"},
        "output_config": output_config,
    }
    if tools:
        request["tools"] = list(tools)

    betas: list[str] = []
    if FALLBACKS_ENABLED:
        betas.append(FALLBACK_BETA)
        request["fallbacks"] = "default"

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    out = LLMResult()

    for round_index in range(MAX_PAUSE_RESUMES + 1):
        response = await client().beta.messages.create(
            # A COPY: `messages` grows on every pause_turn resume, and handing the
            # SDK the live list would let a later append mutate a request already
            # sent. It also keeps each round's payload independently inspectable.
            messages=list(messages), betas=betas, **request
        )

        # Check stop_reason BEFORE touching content: a refusal can arrive as a
        # successful HTTP 200 with content empty (declined before any output) or
        # partial (declined mid-stream).
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise RefusalError(f"declined by safety classifiers (category={category})")

        out.usage = out.usage + Usage.from_response(response)
        _extract(response, out)
        out.rounds = round_index + 1

        if response.stop_reason != "pause_turn":
            return out

        # The server-tool loop hit its iteration cap. Re-send the original turn
        # plus the partial assistant turn and it picks up where it left off — no
        # extra user message, which would derail it.
        #
        # This is the checkpoint boundary: a completed round is the smallest unit
        # of research that can survive an interruption, because a single in-flight
        # Claude call produces nothing until it returns.
        if on_progress is not None:
            # A SNAPSHOT, not `out` itself. `out` keeps accumulating across rounds,
            # so handing it over would give the callback an object that silently
            # changes under it — fine for a callback that copies immediately, a
            # trap for one that stores the reference.
            on_progress(
                LLMResult(
                    text=out.text,
                    sources=list(out.sources),
                    usage=out.usage,
                    rounds=out.rounds,
                )
            )
        logger.info("pause_turn: resuming round %d", out.rounds + 1)
        # APPEND. Rebuilding the list as [user, assistant(this round)] looks right
        # and is only correct for the FIRST resume: on the second it drops round 1's
        # assistant turn along with its web_search_tool_result blocks, so Claude
        # resumes from a conversation that no longer contains the searches it already
        # ran — re-issuing them, and continuing from a different point than it left
        # off, while `out.text` has still accumulated all three rounds locally. The
        # result was a spliced answer with duplicated passages.
        messages.append({"role": "assistant", "content": response.content})

    logger.warning("hit MAX_PAUSE_RESUMES=%d; returning partial", MAX_PAUSE_RESUMES)
    return out
