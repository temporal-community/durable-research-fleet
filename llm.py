"""The Gemini seam. Everything that knows about the Google Gen AI SDK lives here.

MUST NOT import ``temporalio`` — that keeps it unit-testable without a server and
keeps retry ownership in one place. Three details are load-bearing:

1. Google Search grounding is a server-side tool. Research remains one Gemini API
   request, not a client-side search or scraping loop.
2. The SDK is configured for one HTTP attempt. Temporal's Activity RetryPolicy is
   the only retry layer, so independent backoff loops cannot multiply.
3. A grounded GenerateContent call returns atomically. There is no partial-response
   boundary at which model output can be checkpointed; timer heartbeats provide
   liveness while completed sibling
   Activities remain durable in Workflow history.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from google import genai
from google.genai import types

logger = logging.getLogger("llm")

# Stable GA model as of 2026-07-21. Keep the environment override so a model
# migration does not require rebuilding the Worker image.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()

# Gemini performs the search, retrieval and grounding on Google's servers. There
# is no local tool loop and no separate search API key.
WEB_SEARCH_TOOL = types.Tool(google_search=types.GoogleSearch())

# One grounded request can legitimately take minutes. This is a per-request
# ceiling and must remain below the Activity's start_to_close timeout.
HTTP_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "300"))

_THINKING_LEVELS = {"minimal", "low", "medium", "high"}
_NON_RETRYABLE_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
}


class RefusalError(Exception):
    """Gemini blocked the prompt or response.

    This is non-retryable because an identical request is expected to be blocked
    again and would only consume another Activity attempt.
    """


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
    # Retained in the transport shape used by the UI. GenerateContent is one
    # atomic round, so Gemini results always report 1.
    rounds: int = 1


_client: genai.Client | None = None


def client() -> genai.Client:
    """Return a process-wide client so HTTP connections stay warm."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
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


async def complete(
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
        model=MODEL,
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
