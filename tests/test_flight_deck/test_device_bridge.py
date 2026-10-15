"""Device bridge (handheld voice client): helpers and the agent-bus filter."""

from __future__ import annotations

import asyncio
import io
import json
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


class _WS:
    def __init__(self):
        self.sent: list = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        self.sent.append(bytes(data))


def _conn(speak=False):
    return db._DeviceConn(_WS(), {"id": "u1"}, "stick", lang="", speak=speak, screen=(240, 135))


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
