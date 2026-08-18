"""The provider seam for Gemini and Claude.

MUST NOT import ``temporalio`` — that keeps it unit-testable without a server and
keeps retry ownership in one place. Three details are load-bearing:

1. Both providers use their server-side web-search tools. There is no local scraper.
2. Both SDKs are configured for one HTTP attempt. Temporal's Activity RetryPolicy is
   the only retry layer, so independent backoff loops cannot multiply.
3. Claude's ``pause_turn`` is a real checkpoint boundary. Gemini's grounded
   GenerateContent call is atomic, so it has no equivalent partial response to
   resume; timer heartbeats still provide liveness for both providers.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from google import genai
from google.genai import types

logger = logging.getLogger("llm")

# Provider selection is request-scoped and recorded in Workflow history. Do not
# turn this into an environment switch: one deployment intentionally serves both.
DEFAULT_PROVIDER = "gemini"
PROVIDERS = frozenset({"gemini", "anthropic"})

# Model overrides remain deployment settings; the PROVIDER does not.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5").strip()
# Backwards-compatible name used by existing Gemini-focused tests and callers.
MODEL = GEMINI_MODEL

# Gemini performs the search, retrieval and grounding on Google's servers. There
# is no local tool loop and no separate search API key.
WEB_SEARCH_TOOL = types.Tool(google_search=types.GoogleSearch())
ANTHROPIC_WEB_SEARCH_TOOL: dict[str, str] = {
    "type": "web_search_20260209",
    "name": "web_search",
}

# One grounded request can legitimately take minutes. This is a per-request
# ceiling and must remain below the Activity's start_to_close timeout.
HTTP_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "300"))
ANTHROPIC_HTTP_TIMEOUT_SECONDS = float(
    os.environ.get("CLAUDE_TIMEOUT_SECONDS", "300")
)

# Claude only. Each pause_turn creates a completed server-tool round that can be
# checkpointed. Gemini makes one atomic grounded call and never uses this limit.
MAX_PAUSE_RESUMES = 2

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS_ENABLED = os.environ.get("CLAUDE_FALLBACKS", "true").strip().lower() == "true"

_THINKING_LEVELS = {"minimal", "low", "medium", "high"}
_NON_RETRYABLE_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
}


class RefusalError(Exception):
    """The selected provider blocked or declined the prompt or response.

    This is non-retryable because an identical request is expected to be blocked
    again and would only consume another Activity attempt.
    """


def normalize_provider(provider: str | None) -> str:
    """Validate the request-scoped provider at the shared boundary."""
    value = (provider or DEFAULT_PROVIDER).strip().lower()
    if value not in PROVIDERS:
        allowed = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"unsupported research provider {provider!r}; choose {allowed}")
    return value


@dataclass(frozen=True)
class Usage:
    """Token and search usage for one or more calls. No pricing lives here."""

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
        """Map Gemini usage metadata without double-counting cached tokens."""
        u = getattr(response, "usage_metadata", None)
        if u is None:
            return cls()

        cached = getattr(u, "cached_content_token_count", 0) or 0
        prompt = getattr(u, "prompt_token_count", 0) or 0
        tool = getattr(u, "tool_use_prompt_token_count", 0) or 0
        generated = (
            getattr(u, "response_token_count", None)
            or getattr(u, "candidates_token_count", 0)
            or 0
        )
        thoughts = getattr(u, "thoughts_token_count", 0) or 0

        searches = 0
        for candidate in getattr(response, "candidates", None) or []:
            grounding = getattr(candidate, "grounding_metadata", None)
            searches += len(getattr(grounding, "web_search_queries", None) or [])

        # Gemini's prompt count includes cached content. Split that bucket so the
        # UI can show cache hits without total_tokens counting them twice. Tool
        # result and thinking tokens are included in the closest existing buckets.
        return cls(
            input_tokens=max(0, prompt - cached) + tool,
            output_tokens=generated + thoughts,
            cache_read_tokens=cached,
            web_searches=searches,
        )

    @classmethod
    def from_anthropic_response(cls, response: Any) -> "Usage":
        """Map Claude usage, including prompt caching and server web searches."""
        u = getattr(response, "usage", None)
        if u is None:
            return cls()
        server = getattr(u, "server_tool_use", None)
        return cls(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            web_searches=(getattr(server, "web_search_requests", 0) or 0)
            if server
            else 0,
        )


@dataclass(frozen=True)
class Source:
    """One web page Gemini used to ground its response."""

    url: str
    title: str = ""


@dataclass
class LLMResult:
    text: str = ""
    sources: list[Source] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # Gemini always reports one. Claude increments this at pause_turn boundaries.
    rounds: int = 1


_client: genai.Client | None = None
_anthropic_client: Any | None = None


def client() -> genai.Client:
    """Return a process-wide Gemini client so HTTP connections stay warm."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or api_key == "unset":
            raise RuntimeError(
                "GEMINI_API_KEY is not set. The research app needs it; the hello "
                "app does not, so `make verify SCALE=1` still works without it."
            )
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(HTTP_TIMEOUT_SECONDS * 1000),
                # One attempt total: Temporal owns retries and backoff.
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


def anthropic_client() -> Any:
    """Return a process-wide Claude client, with Temporal owning all retries."""
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "Claude research requires the `anthropic` package; install "
                "requirements.txt."
            ) from exc
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key or api_key == "unset":
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Claude research needs it; Gemini "
                "and the hello app do not."
            )
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=ANTHROPIC_HTTP_TIMEOUT_SECONDS,
            max_retries=0,
        )
    return _anthropic_client


def _enum_name(value: Any) -> str:
    """Normalize SDK enums and test doubles to their wire-level names."""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).rsplit(".", 1)[-1].upper()


def _blocked_reason(response: Any) -> str | None:
    prompt_feedback = getattr(response, "prompt_feedback", None)
    prompt_reason = _enum_name(getattr(prompt_feedback, "block_reason", None))
    if prompt_reason and prompt_reason not in {"BLOCK_REASON_UNSPECIFIED", "NONE"}:
        return f"prompt:{prompt_reason}"

    for candidate in getattr(response, "candidates", None) or []:
        finish_reason = _enum_name(getattr(candidate, "finish_reason", None))
        if finish_reason in _NON_RETRYABLE_FINISH_REASONS:
            return f"response:{finish_reason}"
    return None


def _extract(response: Any) -> tuple[str, list[Source]]:
    """Extract visible text and deduplicated grounding sources."""
    text_parts: list[str] = []
    sources: list[Source] = []
    seen: set[str] = set()

    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", False):
                text_parts.append(text)

        grounding = getattr(candidate, "grounding_metadata", None)
        for chunk in getattr(grounding, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            url = getattr(web, "uri", None)
            if url and url not in seen:
                seen.add(url)
                sources.append(Source(url=url, title=getattr(web, "title", "") or ""))

    return "".join(text_parts), sources


async def _complete_gemini(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 16_000,
    effort: str = "high",
    tools: Iterable[Any] | None = None,
    json_schema: dict[str, Any] | None = None,
) -> LLMResult:
    """Run one Gemini GenerateContent request with optional search or JSON schema."""
    thinking_level = effort.strip().lower()
    if thinking_level not in _THINKING_LEVELS:
        allowed = ", ".join(sorted(_THINKING_LEVELS))
        raise ValueError(f"unsupported Gemini thinking level {effort!r}; choose {allowed}")

    config: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
        "thinking_config": types.ThinkingConfig(thinking_level=thinking_level),
    }
    if tools:
        config["tools"] = list(tools)
    if json_schema is not None:
        config["response_mime_type"] = "application/json"
        config["response_json_schema"] = json_schema

    response = await client().aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(**config),
    )

    blocked = _blocked_reason(response)
    if blocked:
        raise RefusalError(f"blocked by Gemini safety controls ({blocked})")

    text, sources = _extract(response)
    return LLMResult(
        text=text,
        sources=sources,
        usage=Usage.from_response(response),
        rounds=1,
    )


def _extract_anthropic(response: Any, into: LLMResult) -> None:
    """Append Claude text and consulted sources from one completed round."""
    seen = {s.url for s in into.sources}
    for block in getattr(response, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            into.text += getattr(block, "text", "") or ""
        elif kind == "web_search_tool_result":
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                code = getattr(content, "error_code", "unknown")
                logger.warning("Claude web_search failed: %s", code)
                continue
            for result in content:
                url = getattr(result, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    into.sources.append(
                        Source(url=url, title=getattr(result, "title", "") or "")
                    )


async def _complete_anthropic(
    *,
    system: str,
    prompt: str,
    max_tokens: int = 16_000,
    effort: str = "high",
    web_search: bool = False,
    json_schema: dict[str, Any] | None = None,
    cache_system: bool = False,
    on_progress: Callable[[LLMResult], None] | None = None,
) -> LLMResult:
    """Run one logical Claude turn, continuing across pause_turn boundaries."""
    output_config: dict[str, Any] = {"effort": effort}
    if json_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": json_schema}

    system_value: str | list[dict[str, Any]] = system
    if cache_system:
        # Anthropic-specific prompt caching belongs at this provider boundary. The
        # shared prompt remains a byte-identical string for Gemini implicit caching.
        system_value = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    request: dict[str, Any] = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system_value,
        "thinking": {"type": "adaptive"},
        "output_config": output_config,
    }
    if web_search:
        request["tools"] = [ANTHROPIC_WEB_SEARCH_TOOL]

    betas: list[str] = []
    if FALLBACKS_ENABLED:
        betas.append(FALLBACK_BETA)
        request["fallbacks"] = "default"

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    out = LLMResult()

    for round_index in range(MAX_PAUSE_RESUMES + 1):
        response = await anthropic_client().beta.messages.create(
            messages=list(messages), betas=betas, **request
        )

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise RefusalError(
                f"declined by Claude safety classifiers (category={category})"
            )

        out.usage = out.usage + Usage.from_anthropic_response(response)
        _extract_anthropic(response, out)
        out.rounds = round_index + 1

        if response.stop_reason != "pause_turn":
            return out

        if on_progress is not None:
            # Snapshot: `out` continues to mutate during later rounds.
            on_progress(
                LLMResult(
                    text=out.text,
                    sources=list(out.sources),
                    usage=out.usage,
                    rounds=out.rounds,
                )
            )
        logger.info("Claude pause_turn: resuming round %d", out.rounds + 1)
        # Preserve every previous assistant tool-result turn. Replacing this list
        # would make Claude repeat searches completed in an earlier round.
        messages.append({"role": "assistant", "content": response.content})

    logger.warning("hit MAX_PAUSE_RESUMES=%d; returning partial", MAX_PAUSE_RESUMES)
    return out


async def complete(
    *,
    provider: str = DEFAULT_PROVIDER,
    system: str,
    prompt: str,
    max_tokens: int = 16_000,
    effort: str = "high",
    tools: Iterable[Any] | None = None,
    web_search: bool = False,
    json_schema: dict[str, Any] | None = None,
    cache_system: bool = False,
    on_progress: Callable[[LLMResult], None] | None = None,
) -> LLMResult:
    """Dispatch one model turn to the provider recorded in the Workflow input."""
    selected = normalize_provider(provider)
    if selected == "anthropic":
        return await _complete_anthropic(
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            effort=effort,
            web_search=web_search or bool(tools),
            json_schema=json_schema,
            cache_system=cache_system,
            on_progress=on_progress,
        )

    gemini_tools = list(tools) if tools else ([WEB_SEARCH_TOOL] if web_search else None)
    return await _complete_gemini(
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
        effort=effort,
        tools=gemini_tools,
        json_schema=json_schema,
    )
