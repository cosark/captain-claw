"""audio — speech-to-text and text-to-speech with Soniox.

* ``transcribe``: an audio (or video) file → its text, optionally with
  timestamps. Voice notes, recordings, podcasts, meetings, a video's speech.
* ``speak``: text → an MP3 under ``saved/media/<session>/``. Chat bridges
  (WhatsApp, Telegram, the voice devices, the web UI) send or play it
  automatically, like ``pocket_tts`` audio.
* ``voices``: the Soniox voices to pick from.

Whose Soniox key: under Flight Deck the agent's OWNER's (their own, else the
team key an admin shared, else the deck's ``SONIOX_API_KEY``) — Flight Deck
holds it and does the Soniox calls (:mod:`captain_claw.soniox_client`).
Standalone, the agent's own ``SONIOX_API_KEY``.

The registry shows the tool only while a key is usable (metadata
``requires_soniox``, :func:`soniox_client.soniox_usable`), so an agent gets it
the moment its owner connects Soniox and never sees a tool that can't work.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from captain_claw import soniox_api, soniox_client
from captain_claw.logging import get_logger
from captain_claw.soniox_api import SonioxError
from captain_claw.tools.registry import Tool, ToolResult
from captain_claw.tools.write import WriteTool

log = get_logger(__name__)

# What Soniox's async API takes (it reads the container itself).
_AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".amr": "audio/amr", ".aiff": "audio/aiff", ".aif": "audio/aiff", ".wma": "audio/x-ms-wma",
    ".webm": "audio/webm", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".3gp": "audio/3gpp",
}
_MAX_AUDIO_BYTES = 500 * 1024 * 1024  # Flight Deck's limit for one upload
# The transcript comes back whole to the model up to this size; past it the
# full text is saved to a file and the reply carries the start and its path.
_INLINE_TRANSCRIPT_CHARS = 30000
_SEGMENT_SECONDS = 15.0


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def timed_segments(tokens: list[dict[str, Any]], every: float = _SEGMENT_SECONDS) -> list[str]:
    """``[mm:ss] text`` lines: tokens grouped into ~*every*-second segments.
    Once a segment is *every* seconds long, the next sentence starts a new
    one; speech with no sentence end is cut at a word after 3 × *every*."""
    lines: list[str] = []
    start: float | None = None
    buf = ""
    for tok in tokens:
        text = str(tok.get("text") or "")
        ms = tok.get("start_ms")
        if not text:
            continue
        at = float(ms) / 1000.0 if isinstance(ms, (int, float)) else (start or 0.0)
        if buf.strip() and start is not None:
            span = at - start
            sentence_done = buf.rstrip().endswith((".", "!", "?", "…"))
            if (span >= every and sentence_done) or (span >= 3 * every and text[:1].isspace()):
                lines.append(f"[{_clock(start)}] {buf.strip()}")
                start, buf = None, ""
        if start is None:
            start = at
        buf += text
    if buf.strip():
        lines.append(f"[{_clock(start or 0.0)}] {buf.strip()}")
    return lines


class AudioTool(Tool):
    """Transcribe audio files and speak text, with Soniox."""

    name = "audio"
    timeout_seconds = 900.0
    description = (
        "Speech-to-text and text-to-speech (Soniox). "
        "action='transcribe': turn an audio or video file (voice note, recording, meeting, "
        "podcast; .ogg .mp3 .m4a .wav .opus .webm .mp4 …) into text — pass 'path'; "
        "timestamps=true adds [mm:ss] segments. Languages are detected automatically; "
        "'language_hints' (e.g. ['hr','en']) helps. "
        "action='speak': turn 'text' into an MP3 voice recording, saved under saved/media — "
        "the chat sends or plays it to the user automatically, so don't attach it yourself. "
        "Pick 'voice' (see action='voices'; default Adrian) and the text's 'language' (ISO code, default en). "
        "action='voices': list the voices."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["transcribe", "speak", "voices"],
                "description": "transcribe a file, speak text, or list voices.",
            },
            "path": {
                "type": "string",
                "description": "transcribe: the audio/video file (saved/ workspace, vfs or absolute path).",
            },
            "timestamps": {
                "type": "boolean",
                "description": "transcribe: add [mm:ss] timed segments (default false).",
            },
            "language_hints": {
                "type": "array",
                "items": {"type": "string"},
                "description": "transcribe: ISO codes of the languages likely spoken (optional).",
            },
            "text": {
                "type": "string",
                "description": f"speak: the text to say (up to {soniox_api.MAX_TTS_CHARS} characters).",
            },
            "voice": {
                "type": "string",
                "description": "speak: a Soniox voice name, e.g. Adrian, Emma, Oliver, Sofia (default Adrian).",
            },
            "language": {
                "type": "string",
                "description": "speak: ISO code of the text's language, e.g. en, hr, de (default en).",
            },
            "output_path": {
                "type": "string",
                "description": "speak: optional file name; kept under saved/media/<session>/ as .mp3.",
            },
        },
        "required": ["action"],
    }

    async def execute(self, action: str = "", **kwargs: Any) -> ToolResult:
        action = str(action or "").strip().lower()
        if action == "voices":
            return self._voices()
        if action == "transcribe":
            return await self._transcribe(**kwargs)
        if action == "speak":
            return await self._speak(**kwargs)
        return ToolResult(success=False, error="action must be one of: transcribe, speak, voices")

    # ── voices ──────────────────────────────────────────────────────

    @staticmethod
    def _voices() -> ToolResult:
        lines = [f"{accent.title()}: {', '.join(names)}" for accent, names in soniox_api.VOICES.items()]
        return ToolResult(success=True, content=(
            "Soniox voices (every voice speaks every supported language; the group is its accent):\n"
            + "\n".join(lines)
            + f"\nDefault: {soniox_api.DEFAULT_VOICE}."
        ))

    # ── transcribe ──────────────────────────────────────────────────

    async def _transcribe(
        self, path: str = "", timestamps: Any = False, language_hints: Any = None, **kwargs: Any,
    ) -> ToolResult:
        path_str = str(path or "").strip()
        if not path_str:
            return ToolResult(success=False, error="transcribe needs 'path' — the audio or video file")
        from captain_claw.tools.document_extract import _resolve_readable_file

        file_path, error = await _resolve_readable_file(
            path_str, runtime_base_path=kwargs.get("_runtime_base_path"),
        )
        if error:
            return ToolResult(success=False, error=error)
        suffix = file_path.suffix.lower()
        mime = _AUDIO_MIME.get(suffix)
        if not mime:
            return ToolResult(success=False, error=(
                f"Not an audio or video file ('{suffix or 'no extension'}'). "
                f"Supported: {', '.join(sorted(_AUDIO_MIME))}."
            ))
        size = file_path.stat().st_size
        if size == 0:
            return ToolResult(success=False, error=f"{file_path.name} is empty (0 bytes)")
        if size > _MAX_AUDIO_BYTES:
            return ToolResult(success=False, error=(
                f"{file_path.name} is too large to transcribe ({size // (1024 * 1024)} MB, max "
                f"{_MAX_AUDIO_BYTES // (1024 * 1024)} MB) — cut it with ffmpeg first"
            ))
        hints = language_hints if isinstance(language_hints, list) else (
            [h for h in str(language_hints or "").replace(" ", "").split(",") if h])
        hints = [str(h).strip().lower()[:8] for h in hints if str(h).strip()][:8]
        try:
            out = await soniox_client.transcribe(
                file_path.read_bytes(), filename=file_path.name, mime=mime,
                language_hints=hints or None, language_identification=True, poll_max=600,
            )
        except SonioxError as exc:
            return ToolResult(success=False, error=f"Transcription failed: {exc}")
        if not out.text:
            return ToolResult(success=True, content=f"No speech was detected in {file_path.name}.")

        want_times = timestamps is True or str(timestamps).strip().lower() in ("true", "1", "yes")
        body = "\n".join(timed_segments(out.tokens)) if want_times and out.tokens else out.text
        # "Source:", never "Path:" — chat bridges send audio named on a Path:
        # line, and this is the user's own recording, not something to echo.
        header = f"Transcript of {file_path.name}\nSource: {file_path}\n"
        if len(body) <= _INLINE_TRANSCRIPT_CHARS:
            return ToolResult(success=True, content=f"{header}\n{body}")
        saved = self._save_text(body, file_path.stem, kwargs)
        return ToolResult(success=True, content=(
            f"{header}Characters: {len(body)} — the full transcript is saved at {saved}; "
            f"read it in parts with the read tool.\n\n{body[:_INLINE_TRANSCRIPT_CHARS]}\n[…]"
        ))

    @staticmethod
    def _save_text(text: str, stem: str, kwargs: dict[str, Any]) -> Path:
        saved_root = WriteTool._resolve_saved_root(kwargs)
        session_id = WriteTool._normalize_session_id(str(kwargs.get("_session_id", "")))
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = WriteTool._normalize_under_saved(f"media/{session_id}/{stem}-transcript-{stamp}.txt",
                                                  saved_root, session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    # ── speak ───────────────────────────────────────────────────────

    @staticmethod
    def _output_file(output_path: str, kwargs: dict[str, Any]) -> Path:
        saved_root = WriteTool._resolve_saved_root(kwargs)
        session_id = WriteTool._normalize_session_id(str(kwargs.get("_session_id", "")))
        requested = Path(str(output_path or "").strip()).name  # a name, never a directory
        if not requested:
            # A random tail: two recordings in the same second must not overwrite.
            requested = f"speech-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.mp3"
        resolved = WriteTool._normalize_under_saved(f"media/{session_id}/{requested}", saved_root, session_id)
        return resolved if resolved.suffix.lower() == ".mp3" else resolved.with_suffix(".mp3")

    async def _speak(
        self, text: str = "", voice: str = "", language: str = "", output_path: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        text_value = str(text or "").strip()
        if not text_value:
            return ToolResult(success=False, error="speak needs 'text'")
        if len(text_value) > soniox_api.MAX_TTS_CHARS:
            return ToolResult(success=False, error=(
                f"Text too long to speak ({len(text_value)} characters, max {soniox_api.MAX_TTS_CHARS}) "
                "— shorten it or split it into several recordings"
            ))
        chosen = str(voice or "").strip()
        voice_name = soniox_api.voice_for(chosen) if chosen else ""
        if chosen and not voice_name:
            return ToolResult(success=False, error=(
                f"Unknown voice '{chosen}'. Voices: {', '.join(soniox_api.all_voices())}"
            ))
        lang = str(language or "").strip().lower()[:8]
        try:
            audio = await soniox_client.speak(text_value, voice=voice_name, language=lang)
        except SonioxError as exc:
            return ToolResult(success=False, error=f"Speech failed: {exc}")
        target = self._output_file(output_path, kwargs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(audio)
        return ToolResult(success=True, content=(
            "Generated speech audio with Soniox.\n"
            f"Path: {target}\n"
            f"Voice: {voice_name or 'default'}\n"
            f"Language: {lang or 'default'}\n"
            "Format: mp3\n"
            f"Size: {len(audio)} bytes\n"
            "It is sent to / played for the user automatically."
        ))
