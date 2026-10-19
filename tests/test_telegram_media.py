"""Voice notes, audio files and videos sent to an agent on Telegram.

Telegram sends each of these as its own message type (``voice``, ``audio``,
``video``, ``video_note``, ``animation``) — never as a document — and the
bridge used to drop them without a word. Now:

* the user's own voice note is transcribed and the transcript is the message
  (the user sees it, as on WhatsApp); a miss is told, never silent;
* a forwarded voice note and an audio file reach the agent as files, with the
  hint that names the audio tool;
* a video, round video message or GIF reaches the agent as a file that is
  analyzed before the turn;
* a file over the Bot API's 20 MB download limit is told plainly.
"""

from __future__ import annotations

import types

import httpx
import pytest

from captain_claw.telegram_bridge import TelegramBridge, TelegramMessage
from captain_claw.web import chat_handler as ch
from captain_claw.web import telegram as tg

USER = "4242"


def _update(update_id, **msg):
    base = {"message_id": update_id, "date": 1,
            "from": {"id": int(USER), "is_bot": False, "first_name": "Kos"},
            "chat": {"id": int(USER), "type": "private"}}
    return {"update_id": update_id, "message": {**base, **msg}}


# ── the bridge ──────────────────────────────────────────────────────


async def test_the_bridge_keeps_sound_and_video_messages():
    updates = [
        _update(1, voice={"file_id": "v1", "mime_type": "audio/ogg", "duration": 7, "file_size": 9000}),
        _update(2, audio={"file_id": "a1", "file_name": "interview.m4a", "mime_type": "audio/mp4"},
                caption="from the meeting"),
        _update(3, video={"file_id": "vid", "mime_type": "video/mp4", "file_size": 30_000_000}),
        _update(4, video_note={"file_id": "vn", "length": 240, "duration": 12}),
        _update(5, animation={"file_id": "gif", "mime_type": "video/mp4"},
                document={"file_id": "gif", "file_name": "funny.mp4"}),
        _update(6, voice={"file_id": "v2"}, forward_origin={"type": "user", "date": 1}),
    ]
    bridge = TelegramBridge(token="123:abc")
    bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True, "result": updates})))
    got = {m.message_id: m for m in await bridge.get_updates()}
    assert set(got) == {1, 2, 3, 4, 5, 6}  # none dropped
    assert (got[1].media_kind, got[1].media_file_id, got[1].media_duration) == ("voice", "v1", 7)
    assert got[2].media_kind == "audio" and got[2].media_file_name == "interview.m4a"
    assert got[2].text == "from the meeting"
    assert got[3].media_kind == "video" and got[3].media_size == 30_000_000
    assert got[4].media_kind == "video_note"
    assert got[5].media_kind == "animation" and got[5].document_file_id == ""  # not twice
    assert got[6].forwarded and not got[1].forwarded


# ── the handler ─────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    sent: list[str] = []
    turns: list[dict] = []
    downloads: list[str] = []

    async def _send(server, chat_id, text, **kw):
        sent.append(text)

    async def _agent_for(server, message):
        return types.SimpleNamespace(session=types.SimpleNamespace(id="sess1"), provider=None)

    async def _turn(server, chat_id, text, reply_to=None, **kw):
        turns.append({"text": text, **kw})

    class _Bridge:
        async def download_file(self, file_id, dest):
            if file_id == "huge":
                raise RuntimeError("getFile failed: {'ok': False, 'description': 'Bad Request: file is too big'}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"OggS")
            downloads.append(str(dest))
            return dest

    cfg = types.SimpleNamespace(resolved_workspace_path=lambda: tmp_path)
    monkeypatch.setattr("captain_claw.config.get_config", lambda: cfg)
    monkeypatch.setattr(tg, "_tg_send", _send)
    monkeypatch.setattr(tg, "_tg_get_or_create_agent", _agent_for)
    monkeypatch.setattr(tg, "_tg_process_with_typing", _turn)
    monkeypatch.setattr(ch, "_transcription_usable", lambda: True)
    server = types.SimpleNamespace(
        _telegram_bridge=_Bridge(), agent=object(), _approved_telegram_users={USER: {}},
        _broadcast=lambda _m: None,
    )
    return types.SimpleNamespace(server=server, sent=sent, turns=turns, downloads=downloads,
                                 monkeypatch=monkeypatch)


def _msg(**kw):
    return TelegramMessage(update_id=1, message_id=1, chat_id=int(USER), user_id=int(USER),
                           username="kos", first_name="Kos", text=kw.pop("text", ""), **kw)


async def test_a_voice_note_is_transcribed_and_becomes_the_message(env):
    async def transcribe(path, mime):
        assert path.endswith(".ogg")
        return "remind me to call Ana at five", ""

    env.monkeypatch.setattr(tg, "_tg_transcribe", transcribe)
    await tg._handle_telegram_message(env.server, _msg(media_kind="voice", media_file_id="v1",
                                                       media_mime="audio/ogg"))
    assert env.sent[0].startswith("🎙 Transcribing")
    assert env.sent[1] == '🎙 Transcription:\n\n"remind me to call Ana at five"'
    (turn,) = env.turns
    assert turn["text"].endswith("\nremind me to call Ana at five")
    assert "transcript of the user's Telegram voice note" in turn["text"]
    assert "[Attached file:" not in turn["text"]  # named in a note, not attached
    assert env.downloads[0] in turn["text"]


async def test_a_voice_note_that_cant_be_transcribed_is_told(env):
    async def transcribe(path, mime):
        return "", "speech-to-text isn't set up — connect Soniox in Flight Deck"

    env.monkeypatch.setattr(tg, "_tg_transcribe", transcribe)
    await tg._handle_telegram_message(env.server, _msg(media_kind="voice", media_file_id="v1"))
    assert "Couldn't transcribe that — speech-to-text isn't set up" in env.sent[-1]
    assert env.turns == []


async def test_a_forwarded_voice_note_and_an_audio_file_are_files(env):
    async def never(path, mime):
        raise AssertionError("not the user's own voice — no automatic transcript")

    env.monkeypatch.setattr(tg, "_tg_transcribe", never)
    await tg._handle_telegram_message(env.server, _msg(media_kind="voice", media_file_id="v2",
                                                       forwarded=True))
    await tg._handle_telegram_message(env.server, _msg(
        media_kind="audio", media_file_id="a1", media_file_name="interview.m4a",
        text="what did they decide?"))
    fwd, audio = env.turns
    assert "[Attached file:" in fwd["text"] and "forwarded voice note" in fwd["text"]
    assert "I've sent an audio file." in fwd["text"]
    assert audio["text"].endswith("what did they decide?")
    assert "interview" in audio["text"] and ".m4a]" in audio["text"]
    assert "transcribe it with the audio tool" in audio["text"]
    assert not fwd.get("analyze_videos") and not audio.get("analyze_videos")


@pytest.mark.parametrize("kind", ["video", "video_note", "animation"])
async def test_videos_are_attached_and_analyzed(env, kind):
    await tg._handle_telegram_message(env.server, _msg(media_kind=kind, media_file_id="vid",
                                                       media_mime="video/mp4"))
    (turn,) = env.turns
    (video,) = turn["analyze_videos"]
    assert video.endswith(".mp4") and f"[Attached file: {video}]" in turn["text"]
    assert "analyzed automatically" in turn["text"] and turn["text"].endswith("I've sent a video.")


async def test_too_big_is_told_and_a_caption_still_goes_through(env):
    await tg._handle_telegram_message(env.server, _msg(
        media_kind="video", media_file_id="vid", media_size=50 * 1024 * 1024))
    assert "only lets bots download files up to 20 MB" in env.sent[-1] and env.turns == []
    await tg._handle_telegram_message(env.server, _msg(
        media_kind="video", media_file_id="huge", text="summarize this talk"))
    assert "up to 20 MB" in env.sent[-1]
    (turn,) = env.turns
    assert turn["text"] == "summarize this talk" and not turn.get("analyze_videos")


def test_media_file_names_keep_a_known_extension():
    stamp = "20261019-120000"
    assert tg._tg_media_filename(_msg(media_kind="voice", media_file_id="x"), stamp) == \
        "tg-voice-20261019-120000.ogg"
    assert tg._tg_media_filename(_msg(media_kind="audio", media_file_id="x",
                                      media_file_name="My Song!.flac"), stamp) == \
        "tg-audio-My_Song_-20261019-120000.flac"
    assert tg._tg_media_filename(_msg(media_kind="video", media_file_id="x",
                                      media_mime="video/quicktime"), stamp).endswith(".mov")
    assert tg._tg_media_filename(_msg(media_kind="videonote", media_file_id="x"), stamp) \
        .endswith(".bin")


async def test_the_turn_runs_the_video_analysis_before_the_agent_answers(monkeypatch):
    from captain_claw.session import Session

    sent: list[str] = []
    analyzed: list[list[str]] = []
    asked: list[str] = []

    async def _send(server, chat_id, text, **kw):
        sent.append(text)

    async def _analysis(agent, content, videos, send):
        analyzed.append(list(videos))
        return f"[Automatic analysis of attached video]\nA cat jumps.\n\n---\n{content}"

    async def _complete(text):
        asked.append(text)
        return "A cat jumping onto a table."

    class _Bridge:
        async def send_chat_action(self, **kw):
            return None

    monkeypatch.setenv("STREAM_NARRATION", "off")
    monkeypatch.setattr(tg, "_tg_send", _send)
    monkeypatch.setattr(ch, "_prefix_video_analysis", _analysis)
    agent = types.SimpleNamespace(
        session=Session(id="s1", name="d"), complete=_complete, narration_callback=None,
        provider=None, last_usage={}, total_usage={},
    )
    server = types.SimpleNamespace(
        _telegram_bridge=_Bridge(), _telegram_user_locks={}, _orchestrator=None,
        _broadcast=lambda _m: None,
    )
    await tg._tg_process_with_typing(
        server, 1, "[Attached file: /w/clip.mp4]\nI've sent a video.", 7,
        user_agent=agent, user_id=USER, analyze_videos=["/w/clip.mp4"],
    )
    assert analyzed == [["/w/clip.mp4"]]
    assert asked[0].startswith("[Automatic analysis of attached video]\nA cat jumps.")
    assert asked[0].endswith("I've sent a video.")
    assert "🎬 Watching the video…" in sent and "A cat jumping onto a table." in sent


async def test_telegrams_refusal_reason_survives_the_download(tmp_path):
    bridge = TelegramBridge(token="123:abc")
    bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"ok": False, "error_code": 400,
                                            "description": "Bad Request: file is too big"})))
    with pytest.raises(RuntimeError, match="file is too big"):
        await bridge.download_file("huge", tmp_path / "x.mp4")
    bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(502, text="bad gateway")))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        await bridge.download_file("x", tmp_path / "x.mp4")
