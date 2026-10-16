"""Tools hand their pictures to an agent whose own model sees images.

A tool called by the model on such an agent gets ``_look_inline`` (set only in
``_execute_tool_with_guard``) and returns its picture as ``ToolResult.images``
instead of a separate vision model's description; the tool loop marks it and
the next call carries it as an image part. Without the flag every tool behaves
as before. A tool that needs a description from inside another tool
(desktop_action, video_vision) asks the agent's own provider when it sees.
"""

from __future__ import annotations

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from captain_claw.agent import Agent  # noqa: E402
from captain_claw.config import get_config, set_config  # noqa: E402
from captain_claw.llm import LLMProvider, LLMResponse, ToolCall  # noqa: E402
from captain_claw.llm import vision  # noqa: E402
from captain_claw.session import Session  # noqa: E402
from captain_claw.tools import browser as browser_mod  # noqa: E402
from captain_claw.tools import screen_capture as screen_mod  # noqa: E402
from captain_claw.tools.image_ocr import ImageVisionTool  # noqa: E402
from captain_claw.tools.registry import Tool, ToolRegistry, ToolResult  # noqa: E402

MODEL = "openai/qwen-local-27b"


def _picture(path, size=(800, 600)) -> str:
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return str(path)


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
def sees(request):
    """Config for an agent whose model sees images (param False: it doesn't)."""
    vision._VISION_REJECTED.clear()
    old_cfg = get_config().model_copy(deep=True)
    cfg = old_cfg.model_copy(deep=True)
    cfg.model.provider, cfg.model.model = "openai", "qwen-local-27b"
    cfg.model.vision = getattr(request, "param", True)
    cfg.model.allowed = []
    cfg.context.max_tokens = 100_000
    set_config(cfg)
    yield cfg
    set_config(old_cfg)
    vision._VISION_REJECTED.clear()


def _agent(provider=None) -> Agent:
    agent = Agent(provider=provider or _Stub())
    agent._initialized = True
    agent.session = Session(id="s1", name="default")
    agent.session_manager = _SessionManager()
    agent.tools = ToolRegistry()
    agent._build_env_now_text = lambda: ""
    return agent


class _Recorder(Tool):
    parameters = {"type": "object", "properties": {}, "required": []}
    description = "records its arguments"

    def __init__(self, name):
        self.name = name
        self.seen: list[dict] = []

    async def execute(self, **kwargs) -> ToolResult:
        self.seen.append(kwargs)
        return ToolResult(success=True, content="done")


# ── Who gets the flag ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("sees", [True, False], indirect=True)
async def test_only_picture_tools_on_a_seeing_agent_get_the_flag(sees):
    agent = _agent()
    tools = {name: _Recorder(name) for name in ("browser", "cv", "screen_capture", "web_fetch")}
    for tool in tools.values():
        agent.tools.register(tool)
    for name in tools:
        await agent._execute_tool_with_guard(name, {}, interaction_label="t")
    flagged = {name for name, tool in tools.items() if tool.seen[0].get("_look_inline")}
    assert flagged == ({"browser", "cv", "screen_capture"} if sees.model.vision else set())


# ── browser ──────────────────────────────────────────────────────────────────


class _Page:
    pass


class _BrowserSession:
    current_url = "http://example.test/"

    async def screenshot(self, path, full_page=False):
        Image.new("RGB", (640, 480), (20, 60, 220)).save(path, format="JPEG")
        return path.read_bytes()

    async def get_title(self):
        return "Example"

    async def ensure_page(self):
        return _Page()


@pytest.fixture
def browser(monkeypatch):
    tool = browser_mod.BrowserTool()
    monkeypatch.setattr(browser_mod, "has_playwright", lambda: True)
    monkeypatch.setattr(tool, "_get_session", lambda key="default": _BrowserSession())
    monkeypatch.setattr(browser_mod.AccessibilityExtractor, "extract_tree",
                        staticmethod(lambda page, **kw: _async("button 'Buy'")))
    monkeypatch.setattr(browser_mod.AccessibilityExtractor, "find_interactive_elements",
                        staticmethod(lambda page, **kw: _async([])))
    monkeypatch.setattr(tool, "_detect_login_hint", lambda url, items: _async(""))
    vision_calls = []

    async def analyze(data, prompt=""):
        vision_calls.append(prompt)
        return "a blue page (from the vision model)"

    monkeypatch.setattr(browser_mod.BrowserVision, "analyze_screenshot", staticmethod(analyze))
    tool.vision_calls = vision_calls
    return tool


async def _async(value):
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("action,extra", [("screenshot", {}), ("observe", {"goal": "buy"}),
                                          ("act", {"goal": "buy"})])
async def test_browser_hands_the_screenshot_to_a_seeing_model(browser, tmp_path, action, extra):
    result = await browser.execute(action, _look_inline=True, _runtime_base_path=str(tmp_path), **extra)
    assert result.success and len(result.images) == 1
    assert result.images[0].endswith(".jpg") and vision.encode_image(result.images[0]) is not None
    assert "attached below" in result.content
    assert browser.vision_calls == []  # no second model asked


@pytest.mark.asyncio
@pytest.mark.parametrize("action,extra", [("screenshot", {}), ("observe", {"goal": "buy"}),
                                          ("act", {"goal": "buy"})])
async def test_browser_without_the_flag_is_unchanged(browser, tmp_path, action, extra):
    result = await browser.execute(action, _runtime_base_path=str(tmp_path), **extra)
    assert result.success and result.images == []
    if action == "screenshot":
        assert "Use image_vision or image_ocr on this path" in result.content
        assert browser.vision_calls == []
    else:
        assert "a blue page (from the vision model)" in result.content
        assert len(browser.vision_calls) == 1


# ── screen_capture ───────────────────────────────────────────────────────────


@pytest.fixture
def capture(monkeypatch, tmp_path):
    shot = tmp_path / "screen.png"

    async def fake_capture(**kwargs):
        _picture(shot)
        return shot

    monkeypatch.setattr(screen_mod, "capture_and_save", fake_capture)
    monkeypatch.setattr(screen_mod, "_IS_MACOS", True)
    described = []

    async def fake_vision(self, path, prompt="", **kwargs):
        described.append(prompt)
        return ToolResult(success=True, content="described by the vision model")

    monkeypatch.setattr(ImageVisionTool, "execute", fake_vision)
    return shot, described


@pytest.mark.asyncio
async def test_screen_capture_hands_over_the_capture(capture):
    shot, described = capture
    tool = screen_mod.ScreenCaptureTool()
    result = await tool.execute(prompt="what app is open?", _look_inline=True)
    assert result.images == [str(shot)] and "Answer from it: what app is open?" in result.content
    assert described == []

    plain = await tool.execute(prompt="what app is open?")
    assert plain.content == "described by the vision model" and not plain.images
    bare = await tool.execute()
    assert "Use image_vision or image_ocr tool on this path" in bare.content and not bare.images


# ── cv ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cv_annotate_shows_its_output_only_to_a_seeing_model(tmp_path):
    from captain_claw.tools import cv as cv_mod

    if not cv_mod._HAS_CV2:
        pytest.skip("OpenCV not installed")
    src = _picture(tmp_path / "in.png")
    tool = cv_mod.CvTool()
    args = {"path": src, "boxes": [[10, 10, 100, 80]], "labels": ["x"], "_runtime_base_path": str(tmp_path)}
    shown = await tool.execute(op="annotate", out=str(tmp_path / "a.png"), _look_inline=True, **args)
    assert shown.success and shown.images and shown.images[0].endswith("a.png")
    hidden = await tool.execute(op="annotate", out=str(tmp_path / "b.png"), **args)
    assert hidden.success and hidden.images == []


# ── descriptions from inside other tools ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_description_without_a_vision_model_asks_the_agents_own_provider(
        tmp_path, sees, monkeypatch):
    import litellm

    def _no_litellm(**kwargs):
        raise AssertionError("litellm called directly")

    monkeypatch.setattr(litellm, "completion", _no_litellm)
    provider = _Stub([LLMResponse(content='{"x": 120, "y": 40}')])
    agent = _agent(provider)
    pic = _picture(tmp_path / "screen.png")

    result = await ImageVisionTool().execute(path=pic, prompt="where is OK?", _agent=agent)

    assert result.success and '{"x": 120, "y": 40}' in result.content
    assert f"model: {MODEL}" in result.content
    [call] = provider.calls
    assert call[-1].content == "where is OK?" and len(call[-1].images) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sees", [False], indirect=True)
async def test_a_non_seeing_agent_keeps_the_litellm_fallback(tmp_path, sees, monkeypatch):
    import litellm

    def _backend(**kwargs):
        raise RuntimeError("litellm reached")

    monkeypatch.setattr(litellm, "completion", _backend)
    provider = _Stub()
    result = await ImageVisionTool().execute(path=_picture(tmp_path / "p.png"), prompt="describe",
                                             _agent=_agent(provider))
    assert result.success is False and "litellm reached" in (result.error or "")
    assert provider.calls == []


# ── the tool loop ────────────────────────────────────────────────────────────


class _ShotTool(Tool):
    name = "browser"
    description = "takes a screenshot"
    parameters = {"type": "object", "properties": {"action": {"type": "string"}}, "required": []}

    def __init__(self, folder):
        self.folder = folder
        self.n = 0

    async def execute(self, **kwargs) -> ToolResult:
        self.n += 1
        path = _picture(self.folder / f"shot{self.n}.png")
        return ToolResult(success=True, content=f"Path: {path}",
                          images=[path] if kwargs.get("_look_inline") else [])


@pytest.mark.asyncio
async def test_screenshots_ride_on_the_next_call_and_only_the_newest_stay(tmp_path, sees):
    shots = [LLMResponse(content="", tool_calls=[ToolCall(id=f"c{i}", name="browser",
                                                          arguments={"action": "screenshot"})])
             for i in (1, 2, 3)]
    provider = _Stub(shots + [LLMResponse(content="The page is red.")])
    agent = _agent(provider)
    agent.tools.register(_ShotTool(tmp_path))

    result = await agent.complete("look at the page three times")

    assert result.startswith("The page is red.")
    sent = [sorted(img.path.rsplit("/", 1)[-1] for m in call for img in (m.images or []))
            for call in provider.calls[:4]]
    # One more screenshot per call, up to max_tool_images (2): the oldest drops.
    assert sent == [[], ["shot1.png"], ["shot1.png", "shot2.png"], ["shot2.png", "shot3.png"]]
    stored = [m for m in agent.session.messages if m.get("role") == "tool"]
    assert all("[Tool image: " in m["content"] for m in stored)
