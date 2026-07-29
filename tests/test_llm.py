"""Tests for the Claude seam — no server, no network, no API key.

These cover the three behaviours that would otherwise only show up on stage:
a `pause_turn` that silently truncates the research, a `refusal` that arrives as a
successful HTTP 200, and a failed web search whose result block is shaped
differently from a successful one.
"""

import pytest

import llm


# --- fakes -----------------------------------------------------------------


class _Obj:
    """Stand-in for an SDK model object; attribute access is all llm.py uses."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _usage(i=0, o=0, cr=0, cw=0, searches=0):
    return _Obj(
        input_tokens=i,
        output_tokens=o,
        cache_read_input_tokens=cr,
        cache_creation_input_tokens=cw,
        server_tool_use=_Obj(web_search_requests=searches),
    )


def _resp(content, stop_reason="end_turn", usage=None, stop_details=None):
    return _Obj(
        content=content,
        stop_reason=stop_reason,
        usage=usage if usage is not None else _usage(),
        stop_details=stop_details,
    )


def _text(s):
    return _Obj(type="text", text=s)


def _search_ok(*pairs):
    return _Obj(
        type="web_search_tool_result",
        content=[_Obj(type="web_search_result", url=u, title=t) for u, t in pairs],
    )


def _search_err(code="max_uses_exceeded"):
    # On failure `content` is a single error OBJECT, not a list. Code that assumes
    # a list here crashes on a perfectly ordinary API response.
    return _Obj(type="web_search_tool_result", content=_Obj(error_code=code))


class _Fake:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []
        self.beta = _Obj(messages=self)

    async def create(self, **kw):
        self.calls.append(kw)
        # Repeat the last response once exhausted, so a runaway loop shows up as a
        # bound being hit rather than an IndexError.
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


@pytest.fixture
def fake(monkeypatch):
    def install(*responses):
        f = _Fake(*responses)
        monkeypatch.setattr(llm, "client", lambda: f)
        return f

    return install


# --- pause_turn ------------------------------------------------------------


async def test_pause_turn_is_resumed_and_usage_accumulates(fake):
    """A server-tool turn that hits its iteration cap returns pause_turn instead of
    finishing. Not resuming it silently truncates the research — no error, just a
    short answer.
    """
    f = fake(
        _resp([_text("first half ")], stop_reason="pause_turn", usage=_usage(i=100, o=10)),
        _resp([_text("second half")], usage=_usage(i=50, o=20)),
    )

    seen = []
    out = await llm.complete(
        system="s", prompt="p", on_progress=seen.append, tools=[llm.WEB_SEARCH_TOOL]
    )

    assert out.text == "first half second half"
    assert out.rounds == 2
    assert out.usage.input_tokens == 150 and out.usage.output_tokens == 30

    # The checkpoint hook fires at the boundary, and must carry the PARTIAL RESULT
    # rather than a round number. Passing only an index made the Activity's
    # checkpoint contentless, which silently disabled resume entirely — see
    # test_a_completed_round_populates_the_checkpoint.
    assert len(seen) == 1
    partial = seen[0]
    assert partial.text == "first half "
    assert partial.rounds == 1
    assert partial.usage.total_tokens == 110

    # The resume must re-send the original user turn plus the partial assistant turn,
    # and must NOT append an extra user message (which derails the server tool loop).
    assert [m["role"] for m in f.calls[1]["messages"]] == ["user", "assistant"]


async def test_a_second_pause_turn_keeps_the_earlier_rounds(fake):
    """REGRESSION. The resume message list must be APPENDED to, not rebuilt.

    Rebuilding it as [user, assistant(this round)] is correct for the first resume
    and silently wrong for the second: round 1's assistant turn — and its
    web_search_tool_result blocks — disappear, so Claude re-runs searches it already
    paid for and continues from a different point, while the local text has still
    accumulated all three rounds. The answer that reaches the report is spliced.
    """
    f = fake(
        _resp([_text("one ")], stop_reason="pause_turn"),
        _resp([_text("two ")], stop_reason="pause_turn"),
        _resp([_text("three")]),
    )

    out = await llm.complete(system="s", prompt="p", tools=[llm.WEB_SEARCH_TOOL])

    assert out.text == "one two three"
    assert out.rounds == 3
    assert len(f.calls) == 3

    # Round 3 must carry the user turn plus BOTH prior assistant turns.
    roles = [m["role"] for m in f.calls[2]["messages"]]
    assert roles == ["user", "assistant", "assistant"], roles
    # And round 2's list is the strict prefix of round 3's — nothing was dropped.
    assert f.calls[2]["messages"][:2] == f.calls[1]["messages"]


async def test_pause_turn_is_bounded(fake):
    """A pathological question must not loop forever burning tokens."""
    fake(_resp([_text("x")], stop_reason="pause_turn"))
    out = await llm.complete(system="s", prompt="p")
    assert out.rounds == llm.MAX_PAUSE_RESUMES + 1


# --- refusal ---------------------------------------------------------------


async def test_refusal_raises_before_content_is_read(fake):
    """Opus 5 safety classifiers decline with HTTP 200 and empty-or-partial
    content. Reading content[0] unconditionally is the bug this guards.
    """
    fake(
        _resp([], stop_reason="refusal", stop_details=_Obj(category="cyber")),
    )
    with pytest.raises(llm.RefusalError, match="cyber"):
        await llm.complete(system="s", prompt="p")


async def test_refusal_with_no_stop_details_still_raises(fake):
    """stop_details is informational and can be absent even on a refusal, so the
    branch must key off stop_reason alone.
    """
    fake(_resp([], stop_reason="refusal", stop_details=None))
    with pytest.raises(llm.RefusalError):
        await llm.complete(system="s", prompt="p")


# --- sources ---------------------------------------------------------------


async def test_sources_are_extracted_and_deduplicated(fake):
    fake(
        _resp(
            [
                _search_ok(("https://a.example", "A"), ("https://b.example", "B")),
                _search_ok(("https://a.example", "A again")),
                _text("answer"),
            ]
        )
    )
    out = await llm.complete(system="s", prompt="p", tools=[llm.WEB_SEARCH_TOOL])
    assert [s.url for s in out.sources] == ["https://a.example", "https://b.example"]
    assert out.text == "answer"


async def test_failed_search_does_not_crash_extraction(fake):
    fake(_resp([_search_err(), _text("answered without search")]))
    out = await llm.complete(system="s", prompt="p", tools=[llm.WEB_SEARCH_TOOL])
    assert out.sources == []
    assert out.text == "answered without search"


async def test_web_search_count_is_captured(fake):
    """Number of searches performed is a dashboard metric, so it has to survive
    the usage mapping.
    """
    fake(_resp([_text("x")], usage=_usage(i=1, o=1, searches=7)))
    out = await llm.complete(system="s", prompt="p")
    assert out.usage.web_searches == 7


# --- request shaping -------------------------------------------------------


async def test_request_shape(monkeypatch):
    f = _Fake(_resp([_text("x")]))
    monkeypatch.setattr(llm, "client", lambda: f)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    await llm.complete(
        system="s", prompt="p", json_schema=schema, effort="medium", max_tokens=4321
    )
    sent = f.calls[0]

    assert sent["model"] == "claude-opus-5"
    assert sent["max_tokens"] == 4321
    # Adaptive is the only supported thinking mode on Opus 5; budget_tokens is a 400.
    assert sent["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in str(sent["thinking"])
    assert sent["output_config"]["effort"] == "medium"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    # Sampling params are removed on Opus 5 and 400 if sent.
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in sent


async def test_fallbacks_are_enabled_by_default(monkeypatch):
    """An audience member typing something spicy must not end the demo. A refusal
    is re-run on Anthropic's recommended fallback inside the same call.
    """
    monkeypatch.setattr(llm, "FALLBACKS_ENABLED", True)
    f = _Fake(_resp([_text("x")]))
    monkeypatch.setattr(llm, "client", lambda: f)

    await llm.complete(system="s", prompt="p")
    assert f.calls[0]["fallbacks"] == "default"
    assert llm.FALLBACK_BETA in f.calls[0]["betas"]


async def test_fallbacks_can_be_switched_off(monkeypatch):
    """CLAUDE_FALLBACKS=false is the stage kill switch if the beta misbehaves."""
    monkeypatch.setattr(llm, "FALLBACKS_ENABLED", False)
    f = _Fake(_resp([_text("x")]))
    monkeypatch.setattr(llm, "client", lambda: f)

    await llm.complete(system="s", prompt="p")
    assert "fallbacks" not in f.calls[0]
    assert f.calls[0]["betas"] == []


async def test_code_execution_is_never_declared_alongside_web_search(monkeypatch):
    """The _20260209 web tools run code execution internally. Declaring it
    separately gives the model two execution environments and confuses it.
    """
    f = _Fake(_resp([_text("x")]))
    monkeypatch.setattr(llm, "client", lambda: f)
    await llm.complete(system="s", prompt="p", tools=[llm.WEB_SEARCH_TOOL])
    types = {t["type"] for t in f.calls[0]["tools"]}
    assert types == {"web_search_20260209"}


# --- usage accounting ------------------------------------------------------


def test_usage_arithmetic():
    a = llm.Usage(input_tokens=1_000_000, output_tokens=0)
    b = llm.Usage(input_tokens=0, output_tokens=1_000_000)
    assert (a + b).total_tokens == 2_000_000
    # Cache reads count toward total_tokens like any other bucket. They used to be
    # the interesting case because they were 10x cheaper; now they matter because a
    # cache read is research this app did not have to do again.
    cached = llm.Usage(cache_read_tokens=1_000_000)
    assert cached.total_tokens == 1_000_000


def test_usage_carries_no_money():
    """Tokens are the unit. Pricing was removed on 2026-07-29 — see llm.py.

    A hardcoded rate card goes stale silently, and the only symptom is a wrong
    number on a projector. This keeps it from creeping back in.
    """
    u = llm.Usage(input_tokens=10, output_tokens=10)
    assert not hasattr(u, "usd")
    assert not [n for n in dir(llm) if "USD" in n or "PRICE" in n.upper()]


def test_usage_from_a_response_missing_usage_is_zero():
    assert llm.Usage.from_response(_Obj()).total_tokens == 0


def test_retries_are_left_to_temporal():
    """Two independent backoff loops multiply into latency nobody can reason
    about, and a second retry layer is the thing this demo argues against.
    """
    import inspect

    assert "max_retries=0" in inspect.getsource(llm.client)
