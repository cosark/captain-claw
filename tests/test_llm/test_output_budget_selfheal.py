"""A tier whose output budget is bigger than the server's context window made
every call fail: lithos-metal started without ``--max-context`` (32768) and an
agent asking for ``max_tokens=65536`` got "Prompt (9655 tokens) plus output
budget (65536) exceeds context capacity (32768)". The server answers 200 and
sends that error mid-stream, so the stream collector swallowed it and the agent
reported "The model returned an empty response."

The error now heals like the other request-shape 400s — the server names its
capacity and the prompt size, so the request is retried with the budget that
fits — and a stream that errors before producing anything surfaces the error
instead of an empty reply.
"""

from __future__ import annotations

import sys
import types

import pytest

import captain_claw.llm as llm_mod
from captain_claw.llm import (
    LiteLLMProvider,
    LLMError,
    Message,
    _acompletion_tolerant,
    _heal_request_kwargs,
    _is_healable_request_error,
    _is_max_tokens_rejected_error,
    _output_budget_that_fits,
)

_LITHOS = (
    "litellm.MidStreamFallbackError: litellm.APIConnectionError: "
    "APIConnectionError: OpenAIException - Prompt (9655 tokens) plus output "
    "budget (65536) exceeds context capacity (32768); reduce messages or "
    "max_completion_tokens."
)
_VLLM_OLD = (
    "This model's maximum context length is 32768 tokens. However, you "
    "requested 75191 tokens (9655 in the messages, 65536 in the completion). "
    "Please reduce the length of the messages or completion."
)
_VLLM_MID = (
    "'max_tokens' or 'max_completion_tokens' is too large: 65536. This model's "
    "maximum context length is 32768 tokens and your request has 9655 input "
    "tokens (65536 > 32768 - 9655)."
)
# vLLM 0.11+ (checked against 0.19): the prompt count is exact unless the
# tokenizer stopped early ("at least"), and the budget alone can be too big.
_VLLM_CURRENT = (
    "This model's maximum context length is 32768 tokens. However, you "
    "requested 30000 output tokens and your prompt contains 9655 input tokens, "
    "for a total of 39655 tokens. Please reduce the length of the input prompt "
    "or the number of requested output tokens. (parameter=input_tokens, value=9655)"
)
_VLLM_AT_LEAST = (
    "This model's maximum context length is 32768 tokens. However, you "
    "requested 30000 output tokens and your prompt contains at least 2769 input "
    "tokens, for a total of at least 32769 tokens. Please reduce the length of "
    "the input prompt or the number of requested output tokens. "
    "(parameter=input_tokens, value=2769)"
)
_VLLM_BUDGET_TOO_BIG = (
    "max_tokens=65536cannot be greater than max_model_len=max_total_tokens=32768. "
    "Please request fewer output tokens. (parameter=max_tokens, value=65536)"
)
_OPENAI_FUNCTIONS = (
    "This model's maximum context length is 32768 tokens. However, you "
    "requested 75191 tokens (9000 in the messages, 655 in the functions, 65536 "
    "in the completion). Please reduce the length of the messages, functions, "
    "or completion."
)
_SGLANG = (
    "Requested token count exceeds the model's maximum context length of 32768 "
    "tokens. You requested a total of 75191 tokens: 9655 tokens from the input "
    "messages and 65536 tokens for the completion."
)
_TGI = (
    "Input validation error: `inputs` tokens + `max_new_tokens` must be <= "
    "32768. Given: 9655 `inputs` tokens and 65536 `max_new_tokens`"
)
_FITS = 32768 - 9655 - llm_mod._OUTPUT_BUDGET_MARGIN


# --- Parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [_LITHOS, _VLLM_OLD, _VLLM_MID, _VLLM_CURRENT, _OPENAI_FUNCTIONS, _SGLANG, _TGI],
)
def test_each_server_phrasing_yields_the_budget_that_fits(message):
    assert _output_budget_that_fits(message.lower()) == _FITS
    assert _is_healable_request_error(message.lower())


def test_litellm_repeating_the_message_does_not_count_the_prompt_twice():
    from litellm.exceptions import APIConnectionError, MidStreamFallbackError

    inner = APIConnectionError(
        message="OpenAIException - " + _LITHOS.split("OpenAIException - ")[1],
        llm_provider="openai",
        model="qwen",
    )
    wrapped = MidStreamFallbackError(
        message=str(inner), model="qwen", llm_provider="openai", original_exception=inner
    )
    msg = str(wrapped).lower()
    assert msg.count("prompt (9655 tokens)") >= 2  # the shape that double-counted
    assert _output_budget_that_fits(msg) == _FITS


def test_an_unreported_prompt_size_is_estimated_high_from_the_request():
    kwargs = {"model": "openai/qwen", "max_tokens": 65536,
              "messages": [{"role": "user", "content": "x" * 30000}]}
    estimate = llm_mod._estimate_prompt_tokens(kwargs)
    assert estimate >= 30000 // 4  # errs high: at least the prose count
    fits = 32768 - estimate - llm_mod._OUTPUT_BUDGET_MARGIN
    assert _output_budget_that_fits(_VLLM_BUDGET_TOO_BIG.lower(), kwargs) == fits
    # "at least n" is a floor, never the size.
    at_least = dict(kwargs, max_tokens=30000)
    assert _output_budget_that_fits(_VLLM_AT_LEAST.lower(), at_least) == fits
    healed, stripped, _ = _heal_request_kwargs(kwargs, _VLLM_BUDGET_TOO_BIG.lower())
    assert healed["max_tokens"] == fits and stripped == ["max_tokens"]


def test_prompt_alone_filling_the_window_is_not_healable():
    # A smaller output budget can't help — compaction has to.
    msg = (
        "this model's maximum context length is 32768 tokens. however, your "
        "request has 40000 input tokens. please reduce the length of the input "
        "messages."
    )
    assert _output_budget_that_fits(msg) is None
    almost_full = "prompt (32700 tokens) plus output budget (4096) exceeds context capacity (32768)"
    assert _output_budget_that_fits(almost_full) is None


@pytest.mark.parametrize(
    "message",
    ["context length exceeded", "rate limit exceeded", "internal server error"],
)
def test_unrelated_errors_do_not_match(message):
    assert _output_budget_that_fits(message) is None


def test_vllm_overflow_is_not_mistaken_for_the_max_tokens_rename():
    # Names both keys, but wants a smaller budget — not max_completion_tokens.
    assert not _is_max_tokens_rejected_error(_VLLM_MID.lower())
    assert not _is_max_tokens_rejected_error(_LITHOS.lower())


# --- Healing the request ----------------------------------------------------


def test_heal_shrinks_max_tokens():
    healed, stripped, _ = _heal_request_kwargs(
        {"model": "openai/qwen", "max_tokens": 65536}, _LITHOS.lower()
    )
    assert healed["max_tokens"] == _FITS
    assert stripped == ["max_tokens"]


def test_heal_shrinks_max_completion_tokens_when_that_is_the_key(monkeypatch):
    monkeypatch.setattr(llm_mod, "_MAX_COMPLETION_TOKENS_MODELS", set())
    healed, stripped, _ = _heal_request_kwargs(
        {"model": "openai/qwen", "max_completion_tokens": 65536}, _VLLM_MID.lower()
    )
    assert healed["max_completion_tokens"] == _FITS
    assert "max_tokens" not in healed
    assert stripped == ["max_completion_tokens"]
    assert llm_mod._MAX_COMPLETION_TOKENS_MODELS == set()


def test_an_overflow_quoted_back_from_the_conversation_is_not_healed():
    # A different 400 that echoes request text containing an old overflow error
    # names a budget this request didn't send.
    msg = ("invalid request: field 'messages.3.content' "
           "input: '" + _LITHOS + "'").lower()
    _, stripped, _ = _heal_request_kwargs({"model": "openai/qwen", "max_tokens": 8192}, msg)
    assert stripped == []


def test_heal_is_a_noop_when_the_budget_already_fits():
    _, stripped, _ = _heal_request_kwargs(
        {"model": "openai/qwen", "max_tokens": 1024}, _LITHOS.lower()
    )
    assert stripped == []


async def test_tolerant_retries_a_rejected_budget(monkeypatch):
    seen: list[dict] = []

    async def fake_acompletion(**kwargs):
        seen.append(kwargs)
        if kwargs["max_tokens"] > _FITS:
            raise Exception(_VLLM_OLD)
        return "ok"

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(acompletion=fake_acompletion))

    out = await _acompletion_tolerant({"model": "openai/qwen", "max_tokens": 65536})
    assert out == "ok"
    assert [kw["max_tokens"] for kw in seen] == [65536, _FITS]


# --- Streaming --------------------------------------------------------------


async def _raising_stream(exc: Exception):
    if False:  # make this an async generator
        yield  # pragma: no cover
    raise exc


async def _content_stream():
    yield {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}
    yield {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


async def _partial_then_raising_stream(exc: Exception):
    yield {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]}
    raise exc


def _local_provider() -> LiteLLMProvider:
    return LiteLLMProvider(
        provider="openai",
        model="nvidia/Qwen3.8-27B-NVFP4",
        base_url="http://127.0.0.1:8000/v1",
        max_tokens=65536,
    )


async def test_complete_with_callback_heals_the_lithos_mid_stream_error(monkeypatch):
    provider = _local_provider()
    sent: list[int] = []

    async def fake_tolerant(kwargs, prov=None):
        sent.append(kwargs["max_tokens"])
        if len(sent) == 1:
            return _raising_stream(Exception(_LITHOS))
        return _content_stream()

    monkeypatch.setattr(llm_mod, "_acompletion_tolerant", fake_tolerant)

    resp = await provider.complete_with_callback(
        [Message(role="user", content="hi")], on_chunk=lambda _c: None
    )
    assert sent == [65536, _FITS]
    assert resp.content == "hello"


async def test_complete_streaming_heals_the_lithos_mid_stream_error(monkeypatch):
    provider = _local_provider()
    sent: list[int] = []

    async def fake_tolerant(kwargs, prov=None):
        sent.append(kwargs["max_tokens"])
        if len(sent) == 1:
            return _raising_stream(Exception(_LITHOS))
        return _content_stream()

    monkeypatch.setattr(llm_mod, "_acompletion_tolerant", fake_tolerant)

    chunks = [c async for c in provider.complete_streaming([Message(role="user", content="hi")])]
    assert sent == [65536, _FITS]
    assert "".join(chunks) == "hello"


async def test_stream_error_before_any_output_surfaces_instead_of_empty_reply():
    provider = _local_provider()
    with pytest.raises(Exception, match="connection reset"):
        await provider._collect_streaming_response(
            _raising_stream(Exception("connection reset by peer"))
        )


async def test_stream_error_after_partial_output_keeps_the_partial_answer():
    provider = _local_provider()
    collected = await provider._collect_streaming_response(
        _partial_then_raising_stream(Exception("connection reset by peer"))
    )
    choice = collected["choices"][0]
    assert choice["message"]["content"] == "hel"
    assert choice["finish_reason"] == "interrupted"


async def test_complete_with_callback_reports_the_server_error(monkeypatch):
    provider = _local_provider()

    async def fake_tolerant(kwargs, prov=None):
        return _raising_stream(Exception("upstream exploded"))

    monkeypatch.setattr(llm_mod, "_acompletion_tolerant", fake_tolerant)

    with pytest.raises(LLMError, match="upstream exploded"):
        await provider.complete_with_callback(
            [Message(role="user", content="hi")], on_chunk=lambda _c: None
        )
