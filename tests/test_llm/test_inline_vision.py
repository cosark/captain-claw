"""The agent's own model sees images: capability, selection, wire format, self-heal."""

from __future__ import annotations

import base64
import copy
import io
import json

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from captain_claw.llm import (  # noqa: E402
    LiteLLMProvider,
    Message,
    OllamaProvider,
    _convert_messages_for_ollama,
    _convert_messages_for_openai_style,
    _acompletion_tolerant,
    _convert_messages_for_responses_api,
    _estimate_prompt_tokens,
    _heal_request_kwargs,
)
from captain_claw.llm import vision  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_vision_state(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "_REJECTED_STORE", tmp_path / "vision_rejected.json")
    monkeypatch.setattr(vision, "_rejected_loaded", False)
    vision._VISION_REJECTED.clear()
    vision._ENCODE_CACHE.clear()
    yield
    vision._VISION_REJECTED.clear()
    vision._ENCODE_CACHE.clear()


def _picture(path, size=(3000, 2000), fmt="PNG"):
    Image.new("RGB", size, (200, 30, 30)).save(path, format=fmt)
    return str(path)


# ── Capability ───────────────────────────────────────────────────────


@pytest.mark.parametrize("provider,model,switch,expected", [
    # Local builds of open models need the switch: same names ship with and
    # without their vision tower.
    ("openai", "openai/nvidia/Qwen3.8-27B-NVFP4", "auto", False),
    ("openai", "openai/nvidia/Qwen3.8-27B-NVFP4", "on", True),
    ("openai", "openai/nvidia/Qwen3.8-27B-NVFP4", True, True),  # YAML `on`
    ("openai", "openai/Qwen3.8-27B-3bit-MLX-TextOnly", "auto", False),
    # Families multimodal in every build.
    ("anthropic", "anthropic/claude-opus-4-8", "auto", True),
    ("openai", "openai/gpt-5.5", "auto", True),
    ("gemini", "gemini/gemini-3-flash", "auto", True),
    ("openrouter", "openrouter/qwen/qwen2.5-vl-72b-instruct", "auto", True),
    ("openai", "openai/deepseek-v4-flash", "auto", False),
    ("openai", "openai/gpt-oss-120b", "auto", False),
    # Ollama keeps sending images to every model unless told otherwise.
    ("ollama", "qwen3-vl:235b-cloud", "auto", True),
    ("ollama", "deepseek-v4-pro:cloud", "auto", True),
    ("ollama", "deepseek-v4-pro:cloud", "off", False),
    ("anthropic", "anthropic/claude-opus-4-8", False, False),
    # Text-only runtimes never, whatever the switch says.
    ("claude-cli", "sonnet", "on", False),
    ("litert", "gemma-4", "on", False),
])
def test_resolve_sees_images(provider, model, switch, expected):
    assert vision.resolve_sees_images(provider, model, "", switch) is expected


def test_a_refusal_wins_over_the_switch_for_that_server_only():
    vision.remember_vision_rejected("openai", "openai/qwen-x", "http://127.0.0.1:8000/")
    assert vision.resolve_sees_images("openai", "openai/qwen-x", "http://127.0.0.1:8000", "on") is False
    assert vision.resolve_sees_images("openai", "openai/qwen-x", "http://other:8000", "on") is True


@pytest.mark.parametrize("value,expected", [
    (True, "on"), (False, "off"), ("ON", "on"), ("yes", "on"), ("0", "off"),
    ("auto", "auto"), ("", "auto"), (None, "auto"), ("maybe", "auto"),
])
def test_normalize_vision_switch(value, expected):
    assert vision.normalize_vision_switch(value) == expected


# ── Encoding and cost ────────────────────────────────────────────────


def test_encode_downscales_caches_and_converts_bmp(tmp_path):
    big = vision.encode_image(_picture(tmp_path / "big.png"), max_pixels=1280)
    assert (big.width, big.height) == (1280, 853) and big.mime == "image/jpeg"
    assert vision.encode_image(str(tmp_path / "big.png"), max_pixels=1280) is big  # cached

    small = vision.encode_image(_picture(tmp_path / "s.bmp", (100, 80), "BMP"), max_pixels=1280)
    assert small.mime == "image/jpeg" and (small.width, small.height) == (100, 80)
    assert Image.open(io.BytesIO(base64.b64decode(small.data))).format == "JPEG"

    assert vision.encode_image(str(tmp_path / "missing.png")) is None
    (tmp_path / "notes.txt").write_text("hi")
    assert vision.encode_image(str(tmp_path / "notes.txt")) is None


def test_image_token_estimates_follow_each_provider():
    assert vision.estimate_image_tokens(1280, 960, "openai", "openai/nvidia/Qwen3.8") == 46 * 35 + 2
    assert vision.estimate_image_tokens(1280, 960, "anthropic", "anthropic/claude-x") == 1639
    assert vision.estimate_image_tokens(1280, 960, "gemini", "gemini/gemini-3") == 258 * 4
    assert vision.estimate_image_tokens(1280, 960, "openai", "openai/gpt-5.5") == 85 + 170 * 4


# ── Selection ────────────────────────────────────────────────────────


def test_plan_takes_attachments_first_then_the_newest_tool_pictures(tmp_path):
    a1, a2 = _picture(tmp_path / "a1.png"), _picture(tmp_path / "a2.png")
    t1, t2, t3 = (_picture(tmp_path / f"t{i}.png") for i in (1, 2, 3))
    plan = vision.plan_inline_images([
        (0, f"look [Attached image: {a1}] and [Attached image: {a2}]"),
        (2, f"shot [Tool image: {t1}]"),
        (4, f"shot [Tool image: {t2}]"),
        (6, f"shot [Tool image: {t3}]"),
    ], max_images=4, max_tool_images=2)
    assert {pos: [i.path for i in imgs] for pos, imgs in plan.images.items()} == {
        0: [a1, a2], 4: [t2], 6: [t3]}
    assert plan.skipped == [t1] and plan.skipped_attached == []
    assert plan.tokens == 4 * vision.estimate_image_tokens(1280, 853)


def test_plan_keeps_the_users_copy_of_a_path_and_reports_what_it_drops(tmp_path):
    a = _picture(tmp_path / "a.png")
    b = _picture(tmp_path / "b.png")
    plan = vision.plan_inline_images([
        (0, f"[Attached image: {a}] [Attached image: {tmp_path / 'gone.png'}]"),
        (2, f"[Tool image: {a}]"),
        (3, f"[Tool image: {b}]"),
    ], max_tool_images=2)
    assert [i.path for i in plan.images[0]] == [a]
    assert [i.path for i in plan.images[3]] == [b] and 2 not in plan.images
    assert plan.skipped_attached == [str(tmp_path / "gone.png")]

    capped = vision.plan_inline_images([(0, f"[Attached image: {a}] [Attached image: {b}]")],
                                       token_cap=vision.estimate_image_tokens(1280, 853) + 10)
    assert [i.path for i in capped.images[0]] == [a] and capped.skipped_attached == [b]


# ── Wire format ──────────────────────────────────────────────────────


def _turn(tmp_path):
    pic = vision.encode_image(_picture(tmp_path / "p.png"))
    call = {"id": "c1", "type": "function", "function": {"name": "image_vision", "arguments": "{}"}}
    return pic, [
        Message("system", "sys"),
        Message("user", f"what is it? [Attached image: {pic.path}]", images=[pic]),
        Message("assistant", "", tool_calls=[call, {**call, "id": "c2"}]),
        Message("tool", "first", tool_call_id="c1", tool_name="image_vision", images=[pic]),
        Message("tool", "second", tool_call_id="c2", tool_name="image_vision"),
        Message("assistant", "done"),
    ]


def test_openai_style_sends_image_parts_and_tool_pictures_after_the_run(tmp_path):
    pic, messages = _turn(tmp_path)
    wire = _convert_messages_for_openai_style(messages)
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "tool", "tool", "user", "assistant"]
    assert wire[1]["content"][0] == {"type": "text", "text": messages[1].content}
    assert wire[1]["content"][1] == {"type": "image_url", "image_url": {"url": pic.data_url}}
    assert wire[3]["content"] == "first" and wire[4]["content"] == "second"
    assert wire[5]["content"][0]["text"] == f"{vision.TOOL_IMAGES_LEAD} [Tool image: {pic.path}]"
    assert wire[5]["content"][1]["image_url"]["url"] == pic.data_url
    # Without images nothing changes.
    plain = _convert_messages_for_openai_style([Message("user", "hi")])
    assert plain == [{"role": "user", "content": "hi"}]


def test_ollama_sends_the_picked_images_and_no_markers_for_an_agent(tmp_path):
    pic, messages = _turn(tmp_path)
    wire = _convert_messages_for_ollama(messages, legacy_markers=False)
    assert [(m["role"], len(m.get("images") or [])) for m in wire] == [
        ("system", 0), ("user", 1), ("assistant", 0), ("tool", 0), ("tool", 0), ("user", 1), ("assistant", 0)]
    assert wire[1]["images"] == [pic.data]
    # An agent that picked nothing this call sends nothing — not the old marker.
    marker_only = [Message("user", f"[Attached image: {pic.path}]"), Message("assistant", "a"),
                   Message("user", "and now?")]
    assert not any(m.get("images") for m in _convert_messages_for_ollama(marker_only, legacy_markers=False))
    # Callers that build their own messages keep the marker inlining.
    assert _convert_messages_for_ollama(marker_only)[0].get("images")


def test_responses_api_sends_input_image_parts(tmp_path):
    pic, messages = _turn(tmp_path)
    _instructions, items = _convert_messages_for_responses_api(messages)
    kinds = [(i["type"], i.get("role")) for i in items]
    assert kinds == [("message", "user"), ("function_call", None), ("function_call", None),
                     ("function_call_output", None), ("function_call_output", None),
                     ("message", "user"), ("message", "assistant")]
    assert items[0]["content"][1] == {"type": "input_image", "image_url": pic.data_url}
    assert items[5]["content"][1]["type"] == "input_image"


def test_anthropic_request_keeps_image_parts_under_cache_control(tmp_path):
    pic, messages = _turn(tmp_path)
    provider = LiteLLMProvider(provider="anthropic", model="claude-opus-4-8", api_key="x")
    kwargs = provider._request_kwargs(messages=messages[:2])
    user = kwargs["messages"][1]
    assert [p["type"] for p in user["content"]] == ["text", "image_url"]
    assert user["content"][-1].get("cache_control") == {"type": "ephemeral"}


def test_prompt_estimate_counts_an_image_allowance_not_its_base64(tmp_path):
    pic, messages = _turn(tmp_path)
    wire = _convert_messages_for_openai_style(messages)
    estimate = _estimate_prompt_tokens({"messages": wire})
    text_only = _estimate_prompt_tokens({"messages": vision.text_view_of_payload(wire)})
    assert estimate == text_only + 2 * vision.IMAGE_TOKEN_ALLOWANCE
    assert estimate < len(json.dumps(wire)) // 3


# ── Self-heal ────────────────────────────────────────────────────────


class _Provider:
    provider = "openai"
    model = "openai/nvidia/Qwen3.8-27B-NVFP4"
    base_url = "http://127.0.0.1:8000"


@pytest.mark.parametrize("error", [
    "Error code: 400 - image input is not supported - hint: if this is unexpected, "
    "you may need to provide the mmproj",
    "Model does not support images. Please use a model that does.",
    "No endpoints found that support image input",
    "Invalid content type. image_url is only supported by certain models.",
    "this model is missing data required for image input",
    "Input should be a valid string: messages.1.content expected a string, got an array",
])
def test_a_refused_image_is_stripped_and_remembered(tmp_path, error):
    _pic, messages = _turn(tmp_path)
    kwargs = {"model": _Provider.model, "messages": _convert_messages_for_openai_style(messages)}
    provider = _Provider()
    healed, stripped, _ = _heal_request_kwargs(kwargs, error.lower(), provider)
    assert stripped == ["images"]
    assert not vision.has_image_parts(healed["messages"])
    assert vision.has_image_parts(kwargs["messages"])  # the original is untouched
    assert vision.NOT_RECEIVED_NOTE in json.dumps(healed["messages"], ensure_ascii=False)
    assert provider.sees_images is False
    assert vision.resolve_sees_images("openai", _Provider.model, _Provider.base_url, "on") is False


def test_other_errors_and_text_requests_are_left_alone(tmp_path):
    _pic, messages = _turn(tmp_path)
    with_images = {"model": "m", "messages": _convert_messages_for_openai_style(messages)}
    text_only = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    for kwargs, error in ((with_images, "rate limit exceeded"),
                          (text_only, "model does not support images")):
        _healed, stripped, _ = _heal_request_kwargs(kwargs, error, _Provider())
        assert "images" not in stripped
    assert not vision._VISION_REJECTED


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self.is_success = status < 400
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    async def post(self, url, json=None, headers=None, **kw):
        self.bodies.append(copy.deepcopy(json))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_a_refused_picture_is_described_by_the_fallback_in_the_same_call(tmp_path, monkeypatch):
    import litellm

    pic, messages = _turn(tmp_path)
    sent = []

    async def fake_acompletion(**kwargs):
        sent.append(kwargs["messages"])
        if len(sent) == 1:
            raise RuntimeError("Error code: 400 - image input is not supported - hint: "
                               "you may need to provide the mmproj")
        return {"choices": [{"message": {"content": "a red picture"}}]}

    described = []

    async def describe(paths):
        described.append(paths)
        return f"[Automatic analysis of attached image {paths[0]}] a red rectangle"

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = _Provider()
    provider.describe_refused_images = describe
    await _acompletion_tolerant(
        {"model": provider.model, "messages": _convert_messages_for_openai_style(messages)}, provider)

    # The user's attachment and the tool picture are each described where they were.
    assert described == [[pic.path], [pic.path]]
    retried = json.dumps(sent[1], ensure_ascii=False)
    assert "a red rectangle" in retried and vision.NOT_RECEIVED_NOTE not in retried
    assert not vision.has_image_parts(sent[1])


@pytest.mark.asyncio
async def test_ollama_retries_without_images_when_the_model_refuses_them(tmp_path):
    pic, messages = _turn(tmp_path)
    provider = OllamaProvider(model="text-only:7b")
    provider.sees_images = True

    async def describe(paths):
        return "described by the vision model"

    provider.describe_refused_images = describe
    provider.client = _Client([
        _Response(400, '{"error":"this model is missing data required for image input"}'),
        _Response(200, {"message": {"role": "assistant", "content": "a text answer"}, "done": True}),
    ])
    response = await provider.complete(messages[:2])
    assert response.content == "a text answer"
    first, second = provider.client.bodies
    assert first["messages"][1]["images"] and "images" not in second["messages"][1]
    assert second["messages"][1]["content"].endswith("described by the vision model")
    assert provider.sees_images is False
    assert vision.vision_rejected("ollama", "text-only:7b", provider.base_url)
