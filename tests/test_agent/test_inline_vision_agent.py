"""An agent whose own model sees images gets the turn's pictures inline."""

from __future__ import annotations

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from captain_claw.agent import Agent  # noqa: E402
from captain_claw.config import get_config, set_config  # noqa: E402
from captain_claw.llm import LLMProvider, LLMResponse, ToolCall  # noqa: E402
from captain_claw.llm import vision  # noqa: E402
from captain_claw.session import Session  # noqa: E402
from captain_claw.tools.image_ocr import ImageVisionTool  # noqa: E402
from captain_claw.tools.registry import ToolRegistry  # noqa: E402

MODEL = "openai/qwen-local-27b"


class _Stub(LLMProvider):
    provider = "openai"
    model = MODEL
    base_url = "http://127.0.0.1:8000"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        return self.responses.pop(0) if self.responses else LLMResponse(content="ok")

    async def complete_streaming(self, messages, tools=None, temperature=None, max_tokens=None):
        if False:
            yield ""

    def count_tokens(self, text):
        return len((text or "").split()) or 1


class _SessionManager:
    async def save_session(self, session):
        return None

    async def get_todo_summary(self, *args, **kwargs):
        return []


@pytest.fixture
def vision_config(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "_REJECTED_STORE", tmp_path / "vision_rejected.json")
    monkeypatch.setattr(vision, "_rejected_loaded", False)
    vision._VISION_REJECTED.clear()
    old_cfg = get_config().model_copy(deep=True)
    cfg = old_cfg.model_copy(deep=True)
    cfg.model.provider = "openai"
    cfg.model.model = "qwen-local-27b"
    cfg.model.vision = True
    cfg.model.allowed = []
    cfg.context.max_tokens = 100_000
    set_config(cfg)
    yield cfg
    set_config(old_cfg)
    vision._VISION_REJECTED.clear()


def _agent(provider) -> Agent:
    agent = Agent(provider=provider)
    agent._initialized = True
    agent.session = Session(id="s1", name="default")
    agent.session_manager = _SessionManager()
    agent.tools = ToolRegistry()
    agent._build_env_now_text = lambda: ""
    return agent


def _picture(path) -> str:
    Image.new("RGB", (1600, 1200), (10, 120, 200)).save(path)
    return str(path)


def _images_of(messages):
    return [(m.role, [i.path for i in (m.images or [])]) for m in messages if m.images]


def test_only_the_current_turns_picture_goes_and_its_tokens_are_budgeted(tmp_path, vision_config):
    old, new = _picture(tmp_path / "old.png"), _picture(tmp_path / "new.png")
    agent = _agent(_Stub())
    s = agent.session
    s.add_message("user", f"what is this?\n[Attached image: {old}]", origin="human")
    s.add_message("assistant", "A blue square.", origin="model")
    s.add_message("user", f"and this one?\n[Attached image: {new}]", origin="human")

    messages = agent._build_messages(tool_messages_from_index=2, query="and this one?")

    assert _images_of(messages) == [("user", [new])]
    window = agent.last_context_window
    cost = vision.estimate_image_tokens(1280, 960, "openai", MODEL)
    assert window["inline_images"] == 1 and window["image_tokens"] == cost
    assert window["sections"]["images"] == cost
    assert window["prompt_tokens"] >= cost
    # The old turn's marker stays as text for the model.
    assert any(f"[Attached image: {old}]" in m.content for m in messages if m.role == "user")


def test_the_picture_rides_on_the_message_that_names_it_beside_the_context_block(
        tmp_path, vision_config):
    pic = _picture(tmp_path / "p.png")
    agent = _agent(_Stub())
    agent._build_env_now_text = lambda: "System environment:\n- Current date: today"
    agent.session.add_message("user", f"what is this?\n[Attached image: {pic}]", origin="human")

    messages = agent._build_messages(tool_messages_from_index=0, query="what is this?")

    [carrier] = [m for m in messages if m.images]
    assert f"[Attached image: {pic}]" in carrier.content
    # The context block still opens the same user message.
    assert carrier.content.startswith("[INTERNAL CONTEXT")
    assert [m.role for m in messages].count("user") == 1


def test_image_tokens_come_off_the_history_budget(tmp_path, vision_config):
    pic = _picture(tmp_path / "p.png")
    agent = _agent(_Stub())
    agent.session.add_message("user", "plain question", origin="human")
    agent._build_messages(tool_messages_from_index=0, query="q")
    plain_budget = agent.last_context_window["history_budget_tokens"]

    agent = _agent(_Stub())
    agent.session.add_message("user", f"plain question [Attached image: {pic}]", origin="human")
    agent._build_messages(tool_messages_from_index=0, query="q")
    window = agent.last_context_window
    assert plain_budget - window["history_budget_tokens"] == window["image_tokens"] > 0


def test_a_model_that_does_not_see_gets_no_images_and_the_tool_policy(tmp_path, vision_config):
    vision_config.model.vision = "auto"  # an unknown local model: not seen as multimodal
    pic = _picture(tmp_path / "p.png")
    agent = _agent(_Stub())
    agent.session.add_message("user", f"look [Attached image: {pic}]", origin="human")
    messages = agent._build_messages(tool_messages_from_index=0, query="look")
    assert _images_of(messages) == []
    assert "you MUST use a tool to look at it" in messages[0].content
    assert agent.last_context_window["image_tokens"] == 0


def test_the_system_prompt_says_so_when_the_model_sees(tmp_path, vision_config):
    agent = _agent(_Stub())
    agent.session.add_message("user", "hi", origin="human")
    system = agent._build_messages(query="hi")[0].content
    assert "You can see images." in system
    assert "you MUST use a tool to look at it" not in system


def test_a_tiny_window_falls_back_to_the_description(tmp_path, vision_config):
    vision_config.context.max_tokens = 4000  # 30% = 1200 < one 1280px picture
    agent = _agent(_Stub())
    assert agent.sees_images() is False


def test_an_allowed_models_switch_is_its_own(vision_config):
    from captain_claw.config import ModelConfig

    vision_config.model.allowed = [ModelConfig.AllowedModelConfig(
        id="flash", provider="openai", model="qwen-flash-next", vision=False)]
    agent = _agent(_Stub())
    agent._refresh_runtime_model_details(source="session", model_id="flash")
    assert agent._vision_switch() is False
    agent._refresh_runtime_model_details(source="config", model_id="")
    assert agent._vision_switch() is True


@pytest.mark.asyncio
async def test_image_vision_hands_the_picture_to_the_agents_own_model(tmp_path, vision_config):
    pic = _picture(tmp_path / "chart.png")
    provider = _Stub([
        LLMResponse(content="", tool_calls=[ToolCall(id="c1", name="image_vision",
                                                     arguments={"path": pic, "prompt": "what trend?"})]),
        LLMResponse(content="The trend is up."),
    ])
    agent = _agent(provider)
    registry = ToolRegistry()
    registry.register(ImageVisionTool())
    agent.tools = registry

    result = await agent.complete(f"read the chart at {pic}")

    assert result.startswith("The trend is up.")
    stored = next(m for m in agent.session.messages if m.get("tool_call_id") == "c1")
    assert vision.IMAGE_MARKER_RE.search(stored["content"]).group(2).endswith("chart.png")
    assert "Answer from it: what trend?" in stored["content"]
    second_call = provider.calls[1]
    [(role, paths)] = _images_of(second_call)
    assert role == "tool" and paths[0].endswith("chart.png")


@pytest.mark.asyncio
async def test_image_vision_called_inside_another_tool_still_describes(tmp_path, vision_config,
                                                                       monkeypatch):
    """Only the model's own call looks inline; screen_capture / desktop_action /
    video_vision call image_vision for a description and keep getting one —
    from the agent's own model (through its provider) when it sees images."""
    import litellm

    pic = _picture(tmp_path / "p.png")
    provider = _Stub([LLMResponse(content="a blue rectangle")])
    agent = _agent(provider)

    def _backend(**kwargs):
        raise AssertionError("litellm called directly")

    monkeypatch.setattr(litellm, "completion", _backend)
    result = await ImageVisionTool().execute(path=pic, prompt="describe", _agent=agent)
    assert result.success and "a blue rectangle" in result.content
    assert not result.images
    assert len(provider.calls) == 1 and provider.calls[0][-1].images
