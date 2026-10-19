"""The ``audio`` tool (Soniox speech-to-text and text-to-speech):

* the registry shows it only while a Soniox key is usable — the agent's own
  ``SONIOX_API_KEY``, or a fresh "connected" from Flight Deck;
* ``transcribe`` returns the text (timed segments on request) and names the
  input with "Source:", never "Path:" — so no bridge sends the user's own
  recording back;
* ``speak`` saves an MP3 under ``saved/media/<session>/`` and reports it on a
  "Path:" line, so every chat surface delivers it like pocket_tts audio;
* Soniox calls go through :mod:`captain_claw.soniox_client` (mocked here).
"""

from __future__ import annotations

import time

import httpx
import pytest

from captain_claw import platform_adapter as pa
from captain_claw import soniox_api, soniox_client
from captain_claw.session import Session
from captain_claw.soniox_api import SonioxError, Transcript
from captain_claw.tools.audio import AudioTool, timed_segments
from captain_claw.tools.registry import ToolRegistry

TOKENS = [
    {"text": "Hello", "start_ms": 0}, {"text": " there.", "start_ms": 500},
    {"text": " Second", "start_ms": 16000}, {"text": " part.", "start_ms": 16500},
]


@pytest.fixture()
def fake_soniox(monkeypatch):
    calls: dict[str, list] = {"transcribe": [], "speak": []}

    async def transcribe(audio, **kw):
        calls["transcribe"].append((audio, kw))
        return Transcript(text="Hello there. Second part.", tokens=TOKENS)

    async def speak(text, **kw):
        calls["speak"].append((text, kw))
        return b"ID3-soniox-mp3"

    monkeypatch.setattr(soniox_client, "transcribe", transcribe)
    monkeypatch.setattr(soniox_client, "speak", speak)
    return calls


# ── visibility ──────────────────────────────────────────────────────


class TestVisibility:
    @pytest.fixture()
    def registry(self):
        reg = ToolRegistry()
        reg.register(AudioTool(), metadata={"requires_soniox": True})
        return reg

    def _names(self, reg):
        return set(reg.list_tools())

    def test_hidden_without_any_key(self, registry, monkeypatch):
        monkeypatch.delenv("SONIOX_API_KEY", raising=False)
        monkeypatch.setattr(soniox_client, "_fd_base", lambda: "")
        assert "audio" not in self._names(registry)

    def test_shown_with_the_agents_own_key(self, registry, monkeypatch):
        monkeypatch.setenv("SONIOX_API_KEY", "snx_own_key_1234")
        assert "audio" in self._names(registry)

    def test_under_flight_deck_it_follows_the_owners_connection(self, registry, monkeypatch):
        monkeypatch.delenv("SONIOX_API_KEY", raising=False)
        monkeypatch.setattr(soniox_client, "_fd_base", lambda: "http://fd.test")
        monkeypatch.setattr(soniox_client, "_CONNECTED", (False, 0.0))
        assert "audio" not in self._names(registry)
        monkeypatch.setattr(soniox_client, "_CONNECTED", (True, time.time()))
        assert "audio" in self._names(registry)
        # A stale "connected" no longer counts.
        monkeypatch.setattr(soniox_client, "_CONNECTED", (True, time.time() - 3600))
        assert "audio" not in self._names(registry)

    async def test_the_per_turn_refresh_asks_flight_deck(self, monkeypatch):
        monkeypatch.delenv("SONIOX_API_KEY", raising=False)
        monkeypatch.setattr(soniox_client, "_fd_base", lambda: "http://fd.test")
        monkeypatch.setattr(soniox_client, "_fd_headers", lambda: {"X-Agent-Auth": "tok"})
        monkeypatch.setattr(soniox_client, "_CONNECTED", (False, 0.0))
        answer = {"connected": True}
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{
            **kw, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json=answer))}))
        assert await soniox_client.available() is True and soniox_client.soniox_usable() is True
        monkeypatch.setattr(soniox_client, "_CONNECTED", (True, 0.5))  # expired → asks again
        answer["connected"] = False
        assert await soniox_client.available() is False and soniox_client.soniox_usable() is False


# ── transcribe ──────────────────────────────────────────────────────


class TestTranscribe:
    async def test_returns_the_text_and_names_the_source_not_a_path(self, tmp_path, fake_soniox):
        note = tmp_path / "voice-note.ogg"
        note.write_bytes(b"OggS....")
        r = await AudioTool().execute(action="transcribe", path=str(note), language_hints=["hr", "en"])
        assert r.success, r.error
        assert "Hello there. Second part." in r.content
        assert f"Source: {note}" in r.content
        assert not any(line.strip().lower().startswith("path:") for line in r.content.splitlines())
        (audio, kw), = fake_soniox["transcribe"]
        assert audio == b"OggS...." and kw["mime"] == "audio/ogg" and kw["language_hints"] == ["hr", "en"]

    async def test_timestamps(self, tmp_path, fake_soniox):
        note = tmp_path / "meeting.m4a"
        note.write_bytes(b"....")
        r = await AudioTool().execute(action="transcribe", path=str(note), timestamps=True)
        # 15 s windows: "Second" starts at 16 s, after "there." closed the first.
        assert "[00:00] Hello there.\n[00:16] Second part." in r.content

    def test_timed_segments_break_at_sentences_after_the_window(self):
        long = [{"text": "A.", "start_ms": 0}, {"text": " B.", "start_ms": 12000},
                {"text": " C.", "start_ms": 30000}]
        assert timed_segments(long, every=10) == ["[00:00] A.", "[00:12] B.", "[00:30] C."]
        # No sentence end for a long time: cut at a word after 3 windows.
        ramble = [{"text": f" w{i}", "start_ms": i * 5000} for i in range(10)]
        assert [line[:7] for line in timed_segments(ramble, every=10)] == ["[00:00]", "[00:30]"]

    async def test_refuses_what_isnt_audio_and_missing_files(self, tmp_path, fake_soniox):
        doc = tmp_path / "notes.pdf"
        doc.write_bytes(b"%PDF")
        r = await AudioTool().execute(action="transcribe", path=str(doc))
        assert not r.success and "Not an audio or video file" in r.error
        r = await AudioTool().execute(action="transcribe", path=str(tmp_path / "gone.ogg"))
        assert not r.success
        assert fake_soniox["transcribe"] == []

    async def test_silence(self, tmp_path, monkeypatch):
        async def transcribe(audio, **kw):
            return Transcript(text="", tokens=[])

        monkeypatch.setattr(soniox_client, "transcribe", transcribe)
        note = tmp_path / "quiet.wav"
        note.write_bytes(b"RIFF")
        r = await AudioTool().execute(action="transcribe", path=str(note))
        assert r.success and "No speech was detected" in r.content

    async def test_soniox_failure_is_reported(self, tmp_path, monkeypatch):
        async def transcribe(audio, **kw):
            raise SonioxError(404, "Soniox isn't connected — add a Soniox API key in Connections")

        monkeypatch.setattr(soniox_client, "transcribe", transcribe)
        note = tmp_path / "v.ogg"
        note.write_bytes(b"OggS")
        r = await AudioTool().execute(action="transcribe", path=str(note))
        assert not r.success and "Connections" in r.error

    async def test_a_long_transcript_is_saved_to_a_file(self, tmp_path, monkeypatch):
        async def transcribe(audio, **kw):
            return Transcript(text="word " * 9000, tokens=[])

        monkeypatch.setattr(soniox_client, "transcribe", transcribe)
        note = tmp_path / "podcast.mp3"
        note.write_bytes(b"ID3")
        r = await AudioTool().execute(action="transcribe", path=str(note),
                                      _saved_base_path=tmp_path / "saved", _session_id="s1")
        assert r.success and "the full transcript is saved at" in r.content
        (saved,) = (tmp_path / "saved" / "media" / "s1").glob("podcast-transcript-*.txt")
        assert saved.read_text().count("word") == 9000


# ── speak ───────────────────────────────────────────────────────────


class TestSpeak:
    async def test_saves_an_mp3_the_bridges_deliver(self, tmp_path, fake_soniox):
        saved = tmp_path / "saved"
        r = await AudioTool().execute(action="speak", text="Dobro jutro!", voice="emma", language="hr",
                                      _saved_base_path=saved, _session_id="s1")
        assert r.success, r.error
        (mp3,) = (saved / "media" / "s1").glob("speech-*.mp3")
        assert mp3.read_bytes() == b"ID3-soniox-mp3"
        assert f"Path: {mp3}" in r.content and "Voice: Emma" in r.content
        assert fake_soniox["speak"] == [("Dobro jutro!", {"voice": "Emma", "language": "hr"})]
        # Telegram & co. pick it up exactly like pocket_tts audio.
        session = Session(id="s1", name="t")
        session.add_message("user", "say good morning", origin="human")
        session.add_message("tool", r.content, tool_name="audio", origin="tool")
        assert pa.collect_turn_generated_audio_paths(session, 0, saved_root=saved, since=time.time() - 60) == [mp3]

    async def test_two_recordings_never_overwrite(self, tmp_path, fake_soniox):
        saved = tmp_path / "saved"
        for _ in range(2):
            r = await AudioTool().execute(action="speak", text="Hi", _saved_base_path=saved, _session_id="s1")
            assert r.success
        assert len(list((saved / "media" / "s1").glob("speech-*.mp3"))) == 2

    async def test_output_path_is_kept_under_saved_media(self, tmp_path, fake_soniox):
        saved = tmp_path / "saved"
        r = await AudioTool().execute(action="speak", text="Hi", output_path="../../etc/greeting",
                                      _saved_base_path=saved, _session_id="s1")
        assert r.success
        assert (saved / "media" / "s1" / "greeting.mp3").is_file()

    async def test_unknown_voice_and_empty_text(self, fake_soniox):
        r = await AudioTool().execute(action="speak", text="Hi", voice="Darth")
        assert not r.success and "Unknown voice" in r.error and "Adrian" in r.error
        r = await AudioTool().execute(action="speak", text="  ")
        assert not r.success
        assert fake_soniox["speak"] == []

    async def test_too_long(self, fake_soniox):
        r = await AudioTool().execute(action="speak", text="x" * (soniox_api.MAX_TTS_CHARS + 1))
        assert not r.success and "too long" in r.error


def test_voices_and_bad_action():
    import asyncio

    r = asyncio.run(AudioTool().execute(action="voices"))
    assert r.success and "Adrian" in r.content and "British: Oliver" in r.content
    r = asyncio.run(AudioTool().execute(action="dance"))
    assert not r.success


# ── the shared Soniox TTS client ────────────────────────────────────


async def test_synthesize_speaks_long_text_in_chunks(monkeypatch):
    seen = []
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b"MP3" + str(len(seen)).encode())

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    text = ("This is a sentence. " * 400).strip()
    audio = await soniox_api.synthesize("snx_key_00001234", text, voice="Emma", language="en")
    assert len(seen) == 2 and audio == b"MP31MP32"
    assert all(len(p["text"]) <= soniox_api.TTS_CHUNK_CHARS for p in seen)
    assert seen[0]["voice"] == "Emma" and seen[0]["audio_format"] == "mp3"


async def test_synthesize_refused_key(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{
        **kw, "transport": httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "bad key"}))}))
    with pytest.raises(SonioxError) as err:
        await soniox_api.synthesize("snx_bad_key_0000", "hi")
    assert err.value.refused


async def test_speak_goes_through_flight_deck(monkeypatch):
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    monkeypatch.setattr(soniox_client, "_fd_base", lambda: "http://fd.test")
    monkeypatch.setattr(soniox_client, "_fd_headers", lambda: {"X-Agent-Auth": "tok"})
    seen = []
    real = httpx.AsyncClient

    def fd(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ID3-from-fd", headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(fd)}))
    assert await soniox_client.speak("hello", voice="Emma") == b"ID3-from-fd"
    (req,) = seen
    assert req.url.path == "/fd/soniox/agent/speak" and req.url.host == "fd.test"
    assert b'"voice":"Emma"' in req.content.replace(b" ", b"")

