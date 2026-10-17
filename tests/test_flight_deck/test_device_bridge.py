"""Device bridge (handheld voice client): helpers and the agent-bus filter."""

from __future__ import annotations

import asyncio
import io
import json
import math
import time
import wave

import pytest

from captain_claw.flight_deck import device_bridge as db


def test_wav_header_round_trips():
    pcm = (b"\x01\x00\xff\x7f") * 800
    blob = db.wav_from_pcm(pcm, 16000)
    with wave.open(io.BytesIO(blob)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16000)
        assert w.readframes(w.getnframes()) == pcm


def test_speakable_reads_markdown_as_sentences():
    text = ("## Weather\n**Sunny**, 24°C — see [the forecast](https://x.example/f).\n"
            "- Wind: light\n- Rain: none\n\n| a | b |\n|---|---|\n| 1 | 2 |\nhttps://raw.example/u")
    out = db.speakable(text)
    assert "**" not in out and "#" not in out and "http" not in out and "---" not in out
    assert "the forecast" in out
    assert "Wind: light." in out and "Rain: none." in out


def test_speakable_cuts_at_a_sentence_end():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    out = db.speakable(text, limit=300)
    assert len(out) <= 300 and out.endswith(".")


def test_screen_text_keeps_structure_without_marks():
    out = db.screen_text("# Title\n**bold** and `code`\n- one\n- two")
    assert out == "Title\nbold and code\n• one\n• two"
    assert db.screen_text("Đakovo i đak") == "Djakovo i đak"


def test_fit_jpeg_letterboxes_to_the_screen():
    from PIL import Image

    src = io.BytesIO()
    Image.new("RGBA", (300, 600), (255, 0, 0, 128)).save(src, "PNG")
    jpeg = db.fit_jpeg(src.getvalue(), 240, 135)
    im = Image.open(io.BytesIO(jpeg))
    assert im.format == "JPEG" and im.size == (240, 135)
    assert not im.info.get("progressive")
    assert im.getpixel((5, 67)) == (0, 0, 0) or sum(im.getpixel((5, 67))) < 30  # letterbox
    assert im.getpixel((120, 67))[0] > 100  # the picture, centred


def test_fit_jpeg_stays_under_the_frame_limit():
    import os

    from PIL import Image

    noisy = Image.frombytes("RGB", (480, 270), os.urandom(480 * 270 * 3))
    src = io.BytesIO()
    noisy.save(src, "PNG")
    assert len(db.fit_jpeg(src.getvalue(), 240, 135)) <= db.MAX_JPEG_BYTES


@pytest.mark.parametrize("aspect", [16 / 9, 4 / 3, 1.0, 3 / 4, 9 / 16])
def test_round_fit_is_the_largest_picture_inside_the_circle(aspect):
    w, h = db.round_fit(466, 466, aspect)
    assert math.hypot(w / 2, h / 2) <= 233          # corners inside the circle
    assert math.hypot(w / 2 + 1, h / 2 + 1) > 233   # and no room to spare
    assert abs(w / h - aspect) < 0.02


def _picture(size, color=(255, 0, 0)) -> bytes:
    from PIL import Image

    src = io.BytesIO()
    Image.new("RGB", size, color).save(src, "PNG")
    return src.getvalue()


@pytest.mark.parametrize("size", [(400, 300), (300, 500), (200, 200)])
def test_fit_jpeg_on_a_round_screen_keeps_the_corners_in_view(size):
    from PIL import Image

    jpeg = db.fit_jpeg(_picture(size), 466, 466, max_bytes=db.MAX_CHUNKED_JPEG_BYTES, round_screen=True)
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    assert im.size == (466, 466)
    x0, y0, x1, y1 = im.point(lambda v: 255 if v > 100 else 0).getbbox()
    for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        assert math.hypot(x - 233, y - 233) <= 234   # the picture's corners show
    assert abs((x1 - x0) / (y1 - y0) - size[0] / size[1]) < 0.03


def test_jpeg_frames_split_a_picture_into_parts():
    data = bytes(range(256)) * 118  # 30208 bytes
    frames = db.jpeg_frames(data)
    assert [k for k, _ in frames] == [db.BIN_JPEG_PART, db.BIN_JPEG_PART, db.BIN_JPEG]
    assert all(len(p) <= db.MAX_JPEG_BYTES for _, p in frames)
    assert b"".join(p for _, p in frames) == data
    assert db.jpeg_frames(data[:500]) == [(db.BIN_JPEG, data[:500])]
    assert db.jpeg_frames(data[:db.MAX_JPEG_BYTES]) == [(db.BIN_JPEG, data[:db.MAX_JPEG_BYTES])]


def test_picture_options_default_to_the_stick():
    assert db._picture_options({}) == {"screen": (240, 135), "round_screen": False, "chunked": False}
    assert db._picture_options({"sw": "466", "sh": "466", "round": "1", "img": "chunks"}) == {
        "screen": (466, 466), "round_screen": True, "chunked": True}


class _WS:
    def __init__(self):
        self.sent: list = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        self.sent.append(bytes(data))


def _conn(speak=False):
    return db._DeviceConn(_WS(), {"id": "u1"}, "stick", lang="", speak=speak, screen=(240, 135))


def _watch():
    return db._DeviceConn(_WS(), {"id": "u1"}, "watch", lang="", speak=False, screen=(466, 466),
                          round_screen=True, chunked=True)


def _noisy_png(size) -> bytes:
    import os

    from PIL import Image

    src = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(src, "PNG")
    return src.getvalue()


@pytest.mark.asyncio
async def test_a_big_picture_reaches_a_chunked_device_in_parts(monkeypatch):
    conn = _watch()

    async def _fetch(path):
        return _noisy_png((600, 450))

    monkeypatch.setattr(conn, "_fetch_from_agent", _fetch)
    await conn._send_image({"path": "/w/a.png", "name": "a.png"})
    head, frames = conn.ws.sent[0], conn.ws.sent[1:]
    assert head["type"] == "image" and (head["w"], head["h"]) == (466, 466)
    assert db.MAX_JPEG_BYTES < head["size"] <= db.MAX_CHUNKED_JPEG_BYTES
    assert [f[0] for f in frames] == [db.BIN_JPEG_PART] * (len(frames) - 1) + [db.BIN_JPEG]
    assert all(len(f) <= db.MAX_JPEG_BYTES + 1 for f in frames)
    jpeg = b"".join(f[1:] for f in frames)
    assert len(jpeg) == head["size"] and jpeg[:2] == b"\xff\xd8"


@pytest.mark.asyncio
async def test_a_device_without_chunks_gets_one_small_frame(monkeypatch):
    conn = _conn()

    async def _fetch(path):
        return _noisy_png((600, 450))

    monkeypatch.setattr(conn, "_fetch_from_agent", _fetch)
    await conn._send_image({"path": "/w/a.png", "name": "a.png"})
    assert _types(conn) == ["image", "bin"]
    frame = conn.ws.sent[1]
    assert frame[0] == db.BIN_JPEG and len(frame) - 1 == conn.ws.sent[0]["size"] <= db.MAX_JPEG_BYTES


@pytest.mark.asyncio
async def test_two_pictures_parts_never_interleave(monkeypatch):
    conn = _watch()

    async def _fetch(path):
        await asyncio.sleep(0)
        return _noisy_png((600, 450))

    monkeypatch.setattr(conn, "_fetch_from_agent", _fetch)
    await asyncio.gather(conn._send_image({"path": "a", "name": "a"}), conn._send_image({"path": "b", "name": "b"}))
    kinds = ["image" if isinstance(m, dict) else m[0] for m in conn.ws.sent]
    first_end = kinds.index(db.BIN_JPEG)
    assert kinds[0] == "image" and kinds[first_end + 1] == "image"
    assert "image" not in kinds[1:first_end] and kinds[-1] == db.BIN_JPEG


def test_the_agent_hears_which_screen_it_writes_for():
    assert "240×135 pixel screen" in _conn().system_context()
    assert "small round 466 px screen" in _watch().system_context()
    assert "{screen}" not in _watch().system_context()


@pytest.mark.asyncio
async def test_a_round_screen_keeps_capital_dj():
    conn = _watch()
    conn.pending = True
    await conn.on_bus({"type": "agent", "text": "Đuro je potvrdio."})
    assert conn.ws.sent[0]["text"] == "Đuro je potvrdio."


def _types(conn):
    return [m["type"] if isinstance(m, dict) else "bin" for m in conn.ws.sent]


@pytest.mark.asyncio
async def test_replies_from_other_surfaces_are_not_shown():
    conn = _conn()
    await conn.on_bus({"type": "agent", "text": "a web turn's answer"})
    await conn.on_bus({"type": "status", "status": "ready"})
    assert conn.ws.sent == []


@pytest.mark.asyncio
async def test_the_device_turn_reply_is_shown_and_spoken():
    conn = _conn(speak=True)
    conn.pending = True
    await conn.on_bus({"type": "status", "status": "thinking"})
    await conn.on_bus({"type": "narration", "text": "Looking it up…"})
    await conn.on_bus({"type": "agent", "text": "**Sunny**, 24°C."})
    await conn.on_bus({"type": "status", "status": "ready"})
    assert _types(conn) == ["state", "status", "reply", "state"]
    assert conn.ws.sent[2]["text"] == "Sunny, 24°C."
    assert conn.ws.sent[3]["state"] == "ready" and conn.pending is False
    assert conn._audio_q.get_nowait() == ("tts", "Sunny, 24°C.")


@pytest.mark.asyncio
async def test_proactive_results_arrive_unless_flight_deck_delivers_them():
    conn = _conn()
    await conn.on_bus({"type": "agent", "text": "Reminder: call Ana", "proactive": True})
    await conn.on_bus({"type": "agent", "text": "sent to WhatsApp", "proactive": True, "fd_delivers": True})
    assert [m["text"] for m in conn.ws.sent] == ["Reminder: call Ana"]
    assert conn.ws.sent[0]["proactive"] is True


@pytest.mark.asyncio
async def test_a_busy_agent_is_a_short_error():
    conn = _conn()
    conn.pending = True
    await conn.on_bus({"type": "error", "text": "busy", "retryable": True})
    assert conn.ws.sent == [{"type": "error", "text": "The agent is busy — try again in a moment."}]
    assert conn.pending is False


@pytest.mark.asyncio
async def test_turn_media_trailing_the_reply_is_delivered(monkeypatch):
    conn = _conn()
    shown: list = []

    async def _fake_image(item):
        shown.append(item["name"])

    monkeypatch.setattr(conn, "_send_image", _fake_image)
    media = {"type": "media", "items": [
        {"kind": "image", "path": "/w/saved/media/s/a.png", "name": "a.png"},
        {"kind": "audio", "path": "/w/saved/media/s/b.mp3", "name": "b.mp3"},
    ]}
    await conn.on_bus(media)  # no device turn yet: ignored
    assert shown == [] and conn._audio_q.empty()

    conn.last_reply_at = time.monotonic()  # the reply just arrived, turn closed
    await conn.on_bus(media)
    await asyncio.sleep(0)
    assert shown == ["a.png"]
    assert conn._audio_q.get_nowait()[0] == "file"


@pytest.mark.asyncio
async def test_stream_pcm_frames_audio_and_keeps_samples_whole():
    conn = _conn()

    async def _chunks():
        yield b"\x00" * (db.PCM_CHUNK + 3)
        yield b"\x01" * 2

    await conn._stream_pcm(_chunks())
    assert conn.ws.sent[0] == {"type": "audio_start", "rate": db.TTS_RATE}
    frames = [m for m in conn.ws.sent if isinstance(m, bytes)]
    assert all(f[0] == db.BIN_AUDIO for f in frames)
    assert len(frames[0]) - 1 == db.PCM_CHUNK
    assert all((len(f) - 1) % 2 == 0 for f in frames)
    assert conn.ws.sent[-1] == {"type": "audio_end"}


@pytest.mark.asyncio
async def test_stop_audio_cancels_the_sound_and_keeps_the_worker():
    conn = _conn()
    started = asyncio.Event()

    async def _slow(_text):
        started.set()
        await asyncio.sleep(30)

    conn._play_tts = _slow
    conn.start_audio_worker()
    await conn._audio_q.put(("tts", "a long answer"))
    await asyncio.wait_for(started.wait(), 1)
    await conn.stop_audio()
    assert not conn._audio_worker.done()
    started.clear()
    await conn._audio_q.put(("tts", "next"))
    await asyncio.wait_for(started.wait(), 1)
    await conn.close()


@pytest.mark.asyncio
async def test_device_tokens_must_be_personal_access_tokens(monkeypatch):
    monkeypatch.setenv("FD_AUTH_ENABLED", "true")
    assert await db._device_user("") is None
    assert await db._device_user("eyJhbGciOi.jwt.like") is None


@pytest.mark.asyncio
async def test_auth_off_deck_needs_no_token(monkeypatch):
    monkeypatch.setenv("FD_AUTH_ENABLED", "false")
    user = await db._device_user("")
    assert user and user["id"] == "local"


def test_activity_text_is_one_drawable_line():
    assert db.activity_text("📄 3 of 27 — Reading: a.pdf") == "3 of 27 — Reading: a.pdf"
    assert db.activity_text("🛡️ Scale guard: redirecting\nfrom read") == "Scale guard: redirecting from read"
    assert db.activity_text("Calling LLM (deepseek)...") == "Calling LLM (deepseek)…"


@pytest.mark.asyncio
async def test_activity_reaches_the_device_during_its_turn_only():
    conn = _conn()
    await conn.on_bus({"type": "activity", "text": "Searching: weather", "tool": "web_search", "phase": "tool"})
    assert conn.ws.sent == []  # not our turn

    conn.pending = True
    await conn.on_bus({"type": "status", "status": "Using web_search..."})       # the activity says more
    await conn.on_bus({"type": "activity", "text": "Searching: weather", "tool": "web_search", "phase": "tool"})
    await conn.on_bus({"type": "activity", "text": "Searching: weather", "tool": "web_search", "phase": "tool"})
    await conn.on_bus({"type": "status", "status": "Calling LLM (deepseek-v4-flash)"})
    await conn.on_bus({"type": "status", "status": "Using playbook: travel"})
    await conn.on_bus({"type": "activity", "text": "", "phase": "done"})
    await conn.on_bus({"type": "status", "status": "thinking"})
    assert [m.get("text") for m in conn.ws.sent if m["type"] == "activity"] == [
        "Searching: weather", "Calling LLM (deepseek-v4-flash)", "Using playbook: travel"]
    assert conn.ws.sent[0] == {"type": "activity", "text": "Searching: weather", "tool": "web_search"}


@pytest.mark.asyncio
async def test_agent_thinking_is_forwarded_as_activity():
    from captain_claw.flight_deck import glasses_bridge as gb

    seen: list = []
    ch = gb._ChannelState(channel_id="t-activity")

    async def _cb(payload):
        seen.append(payload)

    ch.callback_subscribers.append(_cb)
    await gb._forward_agent_msg(ch, {"type": "thinking", "text": "Reading: a.md", "tool": "read", "phase": "tool"})
    assert seen and seen[0]["type"] == "activity" and seen[0]["text"] == "Reading: a.md"
    assert not ch.recent  # live only, never replayed


def test_voice_choice_maps_to_soniox_voices(monkeypatch):
    monkeypatch.delenv("DEVICE_VOICE_MALE", raising=False)
    monkeypatch.delenv("DEVICE_VOICE_FEMALE", raising=False)
    assert db.voice_name("male") == "Adrian"
    assert db.voice_name("Female") == "Emma"
    monkeypatch.setenv("DEVICE_VOICE_FEMALE", "Grace")
    assert db.voice_name("female") == "Grace"
    assert db.voice_name("Nina") == "Nina"
    assert db.voice_name("") == ""
    assert db.voice_name("x;rm -rf/") == "xrm-rf"


@pytest.mark.asyncio
async def test_agent_link_drops_are_a_state():
    conn = _conn()
    await conn.on_bus({"type": "status", "status": "agent_reconnecting"})
    await conn.on_bus({"type": "status", "status": "agent_connected"})
    states = [m["state"] for m in conn.ws.sent if m["type"] == "state"]
    assert states == ["agent_offline", "connected"]
