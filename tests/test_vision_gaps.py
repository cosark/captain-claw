"""Vision gaps closed after native vision shipped (#99 / #100 / #104).

* Telegram: a model that doesn't see images gets the photo described first
  (as the chat path does), while the mail guard keeps reading the user's own
  words — never the description;
* the flow vision step (``/api/vision``) answers for such an agent through the
  vision model / peer fallback instead of sending the path as text;
* a server's refusal of image input outlives an agent restart, for a day;
* HEIC / AVIF / TIFF uploads are stored as JPEG.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import types

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from captain_claw import garbled_reply  # noqa: E402
from captain_claw.llm import LLMResponse, vision  # noqa: E402
from captain_claw.web import chat_handler  # noqa: E402
from captain_claw.web import telegram as tg  # noqa: E402

CHAT = 777


# ── Telegram ─────────────────────────────────────────────────────────────────


class _Bridge:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, *, reply_to_message_id=None):
        self.sent.append(text)

    async def send_chat_action(self, chat_id, action):
        pass


class _Agent:
    def __init__(self):
        self.session = None
        self.provider = None
        self.last_usage = self.total_usage = {}
        self.narration_callback = None
        self.got: list[str] = []

    async def complete(self, text):
        self.got.append(text)
        return "It is a red square."


@pytest.fixture
def tg_env(monkeypatch):
    server = types.SimpleNamespace(
        _telegram_bridge=_Bridge(), _telegram_user_locks={}, _orchestrator=None,
        _broadcast=lambda _event: None,
    )

    async def _not_garbled(reply, message=""):
        return False

    async def _no_steps(provider, response):
        return []

    monkeypatch.setattr(garbled_reply, "is_garbled", _not_garbled)
    monkeypatch.setattr(tg, "extract_next_steps", _no_steps)
    authority: list[str] = []
    real_interactive = tg.mail_authority.interactive
    monkeypatch.setattr(tg.mail_authority, "interactive",
                        lambda text: authority.append(text) or real_interactive(text))
    return types.SimpleNamespace(server=server, authority=authority)


@pytest.mark.asyncio
async def test_telegram_describes_the_photo_for_a_model_that_cannot_see(tg_env, monkeypatch):
    async def describe(agent, paths, prompt, send=None):
        return [(p, "a red square on white") for p in paths]

    monkeypatch.setattr(chat_handler, "describe_images_without_vision", describe)
    agent = _Agent()
    user_text = "[Attached image: /tmp/tg-photo.jpg]\nwhat is this?"

    await tg._tg_process_with_typing(tg_env.server, CHAT, user_text, 1, user_agent=agent,
                                     user_id="u1", describe_images=["/tmp/tg-photo.jpg"])

    [sent] = agent.got
    assert sent.startswith("[Automatic analysis of attached image tg-photo.jpg]\na red square on white")
    assert sent.endswith(user_text)
    # The mail guard reads the user's own words — a photographed page can't
    # pass for an instruction.
    assert tg_env.authority == [user_text]
    assert tg_env.server._telegram_bridge.sent[-1] == "It is a red square."


@pytest.mark.asyncio
async def test_telegram_sends_the_text_as_it_is_when_nothing_needs_describing(tg_env):
    agent = _Agent()
    await tg._tg_process_with_typing(tg_env.server, CHAT, "hello", 1, user_agent=agent, user_id="u1")
    assert agent.got == ["hello"]


# ── /api/vision (flow vision step) ───────────────────────────────────────────


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _Provider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, *a, **k):
        self.calls.append(messages)
        return LLMResponse(content="three apples")


def _api_vision(agent, body):
    from captain_claw.web_server import WebServer

    return WebServer._api_vision(types.SimpleNamespace(agent=agent), _Request(body))


def _agent_that_sees(sees: bool):
    return types.SimpleNamespace(provider=_Provider(), sees_images=lambda: sees)


@pytest.mark.asyncio
async def test_flow_vision_step_uses_the_fallback_for_a_model_that_cannot_see(monkeypatch):
    seen = []

    async def describe(agent, paths, prompt, send=None):
        seen.append((paths, prompt))
        return [(paths[0], "image_vision results from p.png (3.0 KB, model: eyes):\n\nthree apples")]

    monkeypatch.setattr(chat_handler, "describe_images_without_vision", describe)
    agent = _agent_that_sees(False)
    resp = await _api_vision(agent, {"image": "/tmp/p.png", "prompt": "count the apples"})
    assert json.loads(resp.text) == {"success": True, "content": "three apples"}
    assert seen == [(["/tmp/p.png"], "count the apples")]
    assert agent.provider.calls == []  # the text model never got a path to guess from


@pytest.mark.asyncio
async def test_flow_vision_step_says_so_when_nothing_can_see(monkeypatch):
    async def nothing(agent, paths, prompt, send=None):
        return [(paths[0], None)]

    monkeypatch.setattr(chat_handler, "describe_images_without_vision", nothing)
    resp = await _api_vision(_agent_that_sees(False), {"image": "/tmp/p.png"})
    assert resp.status == 503 and "no vision model" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_flow_vision_step_shows_a_seeing_model_the_picture(tmp_path):
    pic = tmp_path / "p.png"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(pic)
    agent = _agent_that_sees(True)
    resp = await _api_vision(agent, {"image": str(pic), "prompt": "count"})
    assert json.loads(resp.text) == {"success": True, "content": "three apples"}
    [messages] = agent.provider.calls
    assert messages[-1].images == [str(pic)]


# ── Refusal memory ───────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "vision_rejected.json"
    monkeypatch.setattr(vision, "_REJECTED_STORE", path)
    monkeypatch.setattr(vision, "_rejected_loaded", False)
    vision._VISION_REJECTED.clear()
    yield path
    vision._VISION_REJECTED.clear()


def _restart(monkeypatch):
    """What a new agent process starts with: nothing in memory."""
    vision._VISION_REJECTED.clear()
    monkeypatch.setattr(vision, "_rejected_loaded", False)


def test_a_refusal_survives_a_restart(store, monkeypatch):
    vision.remember_vision_rejected("openai", "openai/qwen-x", "http://127.0.0.1:8000")
    assert "openai|http://127.0.0.1:8000|qwen-x" in json.loads(store.read_text())
    _restart(monkeypatch)
    assert vision.vision_rejected("openai", "openai/qwen-x", "http://127.0.0.1:8000")
    assert vision.resolve_sees_images("openai", "openai/qwen-x", "http://127.0.0.1:8000", "on") is False


def test_a_refusal_is_forgotten_after_a_day(store, monkeypatch):
    store.write_text(json.dumps({"openai|http://127.0.0.1:8000|qwen-x": time.time() - 25 * 3600}))
    assert not vision.vision_rejected("openai", "openai/qwen-x", "http://127.0.0.1:8000")
    vision.remember_vision_rejected("ollama", "text:7b", "http://127.0.0.1:11434")
    _restart(monkeypatch)
    stored = json.loads(store.read_text())
    assert list(stored) == ["ollama|http://127.0.0.1:11434|text:7b"]  # the stale one is dropped


def test_another_agents_refusals_are_kept_when_saving(store):
    store.write_text(json.dumps({"openai|http://other:8080|m": time.time()}))
    vision.remember_vision_rejected("openai", "openai/qwen-x", "http://127.0.0.1:8000")
    assert set(json.loads(store.read_text())) == {
        "openai|http://other:8080|m", "openai|http://127.0.0.1:8000|qwen-x"}


# ── HEIC / AVIF / TIFF uploads ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tiff_upload_is_stored_as_jpeg(tmp_path):
    from captain_claw.web.rest_file_upload import picture_as_jpeg

    tiff = tmp_path / "scan.tiff"
    Image.new("RGB", (320, 240), (20, 60, 220)).save(tiff, format="TIFF")
    path, size = await picture_as_jpeg(tiff)
    assert path.suffix == ".jpg" and path.stat().st_size == size > 0
    assert Image.open(path).format == "JPEG" and not tiff.exists()
    assert vision.encode_image(str(path)) is not None


@pytest.mark.asyncio
async def test_an_unreadable_picture_is_kept_as_uploaded(tmp_path):
    from captain_claw.web.rest_file_upload import picture_as_jpeg

    bad = tmp_path / "photo.heic"
    bad.write_bytes(b"not a picture at all")
    assert await picture_as_jpeg(bad) is None
    assert bad.read_bytes() == b"not a picture at all"


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sips"), reason="macOS sips only")
def test_heic_converts_on_macos_without_pillow_heif(tmp_path):
    import subprocess

    from captain_claw.flight_deck.whatsapp_inbound import to_jpeg

    png, heic = tmp_path / "in.png", tmp_path / "photo.heic"
    Image.new("RGB", (160, 120), (30, 160, 60)).save(png)
    subprocess.run(["sips", "-s", "format", "heic", str(png), "--out", str(heic)],
                   capture_output=True, check=False)
    if not heic.is_file():
        pytest.skip("this macOS can't write HEIC")
    out = to_jpeg(heic.read_bytes())
    assert out and out[:2] == b"\xff\xd8"
