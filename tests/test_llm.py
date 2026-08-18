"""Offline tests for the Gemini seam — no server, network, or API key."""

import inspect

import pytest

import llm


class _Obj:
    """Small attribute-based stand-in for SDK response models."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _usage(prompt=0, response=0, cached=0, tool=0, thoughts=0):
    return _Obj(
        prompt_token_count=prompt,
        response_token_count=response,
        cached_content_token_count=cached,
        tool_use_prompt_token_count=tool,
        thoughts_token_count=thoughts,
    )


def _chunk(url, title=""):
    return _Obj(web=_Obj(uri=url, title=title))


def _candidate(
    text="answer",
    *,
    finish_reason="STOP",
    chunks=(),
    queries=(),
    thought_text=None,
):
    parts = []
    if thought_text is not None:
        parts.append(_Obj(text=thought_text, thought=True))
    if text is not None:
        parts.append(_Obj(text=text, thought=False))
    return _Obj(
        content=_Obj(parts=parts),
        finish_reason=finish_reason,
        grounding_metadata=_Obj(
            grounding_chunks=list(chunks), web_search_queries=list(queries)
        ),
    )


def _resp(*candidates, usage=None, block_reason=None):
    return _Obj(
        candidates=list(candidates) or [_candidate()],
        usage_metadata=usage if usage is not None else _usage(),
        prompt_feedback=_Obj(block_reason=block_reason),
    )


class _Fake:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.models = self
        self.aio = self

    async def generate_content(self, **kw):
        self.calls.append(kw)
        return self.response


class _AnthropicFake:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = self
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _claude_response(text, *, stop_reason="end_turn", searches=0):
    content = [_Obj(type="text", text=text)]
    if searches:
        content.append(
            _Obj(
                type="web_search_tool_result",
                content=[_Obj(url="https://claude.example", title="Claude source")],
            )
        )
    return _Obj(
        content=content,
        stop_reason=stop_reason,
        usage=_Obj(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=2,
            server_tool_use=_Obj(web_search_requests=searches),
        ),
    )


@pytest.fixture
def fake(monkeypatch):
    def install(response):
        f = _Fake(response)
        monkeypatch.setattr(llm, "client", lambda: f)
        return f

    return install


async def test_text_sources_and_search_count_are_extracted(fake):
    fake(
        _resp(
            _candidate(
                "grounded answer",
                chunks=[
                    _chunk("https://a.example", "A"),
                    _chunk("https://b.example", "B"),
                    _chunk("https://a.example", "duplicate"),
                ],
                queries=["first query", "second query"],
                thought_text="private reasoning",
            ),
            usage=_usage(prompt=120, response=25, cached=20, tool=30, thoughts=15),
        )
    )

    out = await llm.complete(
        system="s", prompt="p", tools=[llm.WEB_SEARCH_TOOL], effort="medium"
    )

    assert out.text == "grounded answer"
    assert "private reasoning" not in out.text
    assert [s.url for s in out.sources] == [
        "https://a.example",
        "https://b.example",
    ]
    assert out.usage.web_searches == 2
    assert out.usage.input_tokens == 130  # prompt minus cache, plus tool results
    assert out.usage.output_tokens == 40  # visible response plus thinking
    assert out.usage.cache_read_tokens == 20
    assert out.usage.total_tokens == 190
    assert out.rounds == 1


async def test_prompt_block_is_non_retryable(fake):
    fake(_resp(_candidate(text=None), block_reason="PROHIBITED_CONTENT"))
    with pytest.raises(llm.RefusalError, match="prompt:PROHIBITED_CONTENT"):
        await llm.complete(system="s", prompt="p")


async def test_response_safety_finish_is_non_retryable(fake):
    fake(_resp(_candidate(text=None, finish_reason="SAFETY")))
    with pytest.raises(llm.RefusalError, match="response:SAFETY"):
        await llm.complete(system="s", prompt="p")


async def test_max_tokens_returns_the_partial_response(fake):
    fake(_resp(_candidate("partial", finish_reason="MAX_TOKENS")))
    out = await llm.complete(system="s", prompt="p")
    assert out.text == "partial"


async def test_request_shape_for_structured_output(fake):
    f = fake(_resp(_candidate('{"sub_questions": ["a"]}')))
    schema = {
        "type": "object",
        "properties": {"sub_questions": {"type": "array", "items": {"type": "string"}}},
        "required": ["sub_questions"],
        "additionalProperties": False,
    }

    await llm.complete(
        system="system",
        prompt="prompt",
        json_schema=schema,
        effort="medium",
        max_tokens=4321,
    )

    sent = f.calls[0]
    config = sent["config"]
    assert sent["model"] == "gemini-3.6-flash"
    assert sent["contents"] == "prompt"
    assert config.system_instruction == "system"
    assert config.max_output_tokens == 4321
    assert llm._enum_name(config.thinking_config.thinking_level) == "MEDIUM"
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema
    assert config.tools is None
    for banned in ("temperature", "top_p", "top_k"):
        assert getattr(config, banned) is None


async def test_google_search_is_passed_as_a_server_side_tool(fake):
    f = fake(_resp(_candidate()))
    await llm.complete(system="s", prompt="p", tools=[llm.WEB_SEARCH_TOOL])
    tools = f.calls[0]["config"].tools
    assert len(tools) == 1
    assert tools[0].google_search is not None
    assert tools[0].code_execution is None


async def test_invalid_thinking_level_fails_before_an_api_call(fake):
    f = fake(_resp(_candidate()))
    with pytest.raises(ValueError, match="unsupported Gemini thinking level"):
        await llm.complete(system="s", prompt="p", effort="max")
    assert not f.calls


def test_missing_api_key_has_a_clear_lazy_error(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is not set"):
        llm.client()


def test_usage_arithmetic():
    a = llm.Usage(input_tokens=1_000_000)
    b = llm.Usage(output_tokens=1_000_000)
    assert (a + b).total_tokens == 2_000_000
    assert llm.Usage(cache_read_tokens=1_000_000).total_tokens == 1_000_000


def test_usage_carries_no_money():
    u = llm.Usage(input_tokens=10, output_tokens=10)
    assert not hasattr(u, "usd")
    assert not [n for n in dir(llm) if "USD" in n or "PRICE" in n.upper()]


def test_usage_from_a_response_missing_usage_is_zero():
    assert llm.Usage.from_response(_Obj()).total_tokens == 0


def test_retries_are_left_to_temporal():
    src = inspect.getsource(llm.client)
    assert "HttpRetryOptions(attempts=1)" in src


async def test_claude_preserves_pause_turn_resume_and_cache_control(monkeypatch):
    fake = _AnthropicFake(
        [
            _claude_response("first ", stop_reason="pause_turn", searches=1),
            _claude_response("second"),
        ]
    )
    monkeypatch.setattr(llm, "anthropic_client", lambda: fake)
    progress = []

    out = await llm.complete(
        provider="anthropic",
        system="stable shared prefix",
        prompt="research this",
        web_search=True,
        cache_system=True,
        on_progress=progress.append,
    )

    assert out.text == "first second"
    assert out.rounds == 2
    assert out.usage.web_searches == 1
    assert [s.url for s in out.sources] == ["https://claude.example"]
    assert progress[0].text == "first " and progress[0].rounds == 1
    assert fake.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert fake.calls[0]["tools"] == [llm.ANTHROPIC_WEB_SEARCH_TOOL]
    # The second request carries the completed assistant/tool turn, which is the
    # provider-side continuation that MAX_PAUSE_RESUMES bounds.
    assert len(fake.calls[1]["messages"]) == 2


async def test_claude_pause_turn_loop_is_bounded(monkeypatch):
    fake = _AnthropicFake(
        [
            _claude_response(f"round {i} ", stop_reason="pause_turn")
            for i in range(llm.MAX_PAUSE_RESUMES + 1)
        ]
    )
    monkeypatch.setattr(llm, "anthropic_client", lambda: fake)

    out = await llm.complete(
        provider="anthropic", system="s", prompt="p", web_search=True
    )

    assert len(fake.calls) == llm.MAX_PAUSE_RESUMES + 1
    assert out.rounds == llm.MAX_PAUSE_RESUMES + 1


def test_provider_selection_is_explicit_and_defaults_to_gemini():
    assert llm.normalize_provider(None) == "gemini"
    assert llm.normalize_provider(" ANTHROPIC ") == "anthropic"
    with pytest.raises(ValueError, match="unsupported research provider"):
        llm.normalize_provider("auto")
