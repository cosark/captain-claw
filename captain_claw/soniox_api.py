"""Soniox REST client — the pieces Flight Deck and agents share.

Soniox does speech-to-text (async file transcription, realtime WebSocket) and
text-to-speech for Captain Claw. Its API key belongs to a person or a team and
is kept by Flight Deck (``flight_deck/soniox_routes.py``); this module only
talks to Soniox with a key it is handed:

* :func:`transcribe` — the async file dance (upload → job → poll → transcript,
  then delete both), with a user-facing reason on every failure.
* :func:`list_models` — the cheapest authenticated call, used to check a key.
* :func:`temporary_key` — a short-lived key for one realtime session
  (``transcribe_websocket`` or ``tts_rt``), so an agent can stream without ever
  holding the real key.
* :func:`synthesize` — text to speech (one request; long text in chunks).

``SONIOX_API_BASE`` / ``SONIOX_TTS_URL`` point everything at another
Soniox-compatible endpoint (a local stub in tests and live checks).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from captain_claw.logging import get_logger

log = get_logger(__name__)

DEFAULT_API_BASE = "https://api.soniox.com"
STT_MODEL = "stt-async-v4"
USAGE_TYPES = ("transcribe_websocket", "tts_rt")
MAX_TEMP_KEY_SECONDS = 3600

DEFAULT_TTS_URL = "https://tts-rt.soniox.com/tts"
TTS_MODEL = "tts-rt-v1"
DEFAULT_VOICE = "Adrian"
# Soniox's voices (all multilingual), by accent — the glasses settings page
# offers the same list.
VOICES: dict[str, tuple[str, ...]] = {
    "neutral": ("Maya", "Daniel", "Noah", "Nina", "Emma", "Jack", "Adrian", "Claire",
                "Grace", "Owen", "Mina", "Kenji"),
    "british": ("Oliver", "Arthur", "Isla", "Victoria"),
    "spanish": ("Rafael", "Mateo", "Lucia", "Sofia"),
    "australian": ("Cooper", "Mason", "Ruby", "Elise"),
    "indian": ("Arjun", "Rohan", "Priya", "Meera"),
}
TTS_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav", "aac": "audio/aac",
               "opus": "audio/ogg", "flac": "audio/flac"}
# One Soniox request at most (the glasses cap); longer text is spoken in chunks.
TTS_CHUNK_CHARS = 4000
MAX_TTS_CHARS = 40000


class SonioxError(Exception):
    """Soniox refused or failed. ``status`` is the HTTP status (0: no answer);
    the message is short and safe to show a user (never the key)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status or 0)

    @property
    def refused(self) -> bool:
        """The key itself was refused (wrong, revoked, or not allowed)."""
        return self.status in (401, 403)


@dataclass
class Transcript:
    text: str = ""
    # Soniox's tokens: ``{"text", "start_ms", "end_ms"}`` (only those three).
    tokens: list[dict[str, Any]] = field(default_factory=list)


def api_base() -> str:
    return ((os.environ.get("SONIOX_API_BASE") or "").strip() or DEFAULT_API_BASE).rstrip("/")


def tts_url() -> str:
    return (os.environ.get("SONIOX_TTS_URL") or "").strip() or DEFAULT_TTS_URL


def all_voices() -> list[str]:
    return [v for group in VOICES.values() for v in group]


def voice_for(name: str) -> str:
    """Soniox's spelling of a voice name ("emma" → "Emma"), or "" if unknown."""
    wanted = str(name or "").strip().lower()
    return next((v for v in all_voices() if v.lower() == wanted), "")


def key_hint(api_key: str) -> str:
    """``····ab12`` — enough to recognise a key, never enough to use it."""
    key = str(api_key or "").strip()
    return f"····{key[-4:]}" if len(key) >= 8 else ("····" if key else "")


def _reason(stage: str, status: int) -> str:
    if status in (401, 403):
        return f"Soniox refused the API key during {stage} (HTTP {status}) — check the Soniox key in Connections"
    if status == 429:
        return f"Soniox is rate-limiting {stage} (HTTP 429) — try again shortly"
    return f"Soniox {stage} failed (HTTP {status})"


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("detail") or data.get("error_message") or "")[:200]
    return ""


async def list_models(api_key: str, *, timeout: float = 15.0) -> list[dict[str, Any]]:
    """``GET /v1/models`` — Soniox's models for this key. Raises :class:`SonioxError`."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{api_base()}/v1/models", headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise SonioxError(0, f"Soniox couldn't be reached ({type(exc).__name__})") from None
    if resp.status_code != 200:
        detail = _error_text(resp)
        raise SonioxError(resp.status_code, f"Soniox answered HTTP {resp.status_code}{': ' + detail if detail else ''}")
    try:
        data = resp.json()
    except ValueError:
        raise SonioxError(200, "Soniox returned a non-JSON model list") from None
    models = data.get("models") if isinstance(data, dict) else data
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []


def model_names(models: list[dict[str, Any]]) -> list[str]:
    return [str(m.get("id") or m.get("name")) for m in models if m.get("id") or m.get("name")]


async def temporary_key(
    api_key: str, usage_type: str, *, expires_in_seconds: int = 600,
    max_session_seconds: int | None = None, client_reference_id: str = "",
    timeout: float = 15.0,
) -> dict[str, str]:
    """A short-lived key for one realtime session: ``{"api_key", "expires_at"}``.
    Raises :class:`SonioxError`."""
    if usage_type not in USAGE_TYPES:
        raise SonioxError(400, f"usage_type must be one of {', '.join(USAGE_TYPES)}")
    body: dict[str, Any] = {
        "usage_type": usage_type,
        "expires_in_seconds": max(1, min(int(expires_in_seconds), MAX_TEMP_KEY_SECONDS)),
    }
    if max_session_seconds:
        body["max_session_duration_seconds"] = max(1, min(int(max_session_seconds), 18000))
    if client_reference_id:
        body["client_reference_id"] = client_reference_id[:256]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{api_base()}/v1/auth/temporary-api-key",
                headers={"Authorization": f"Bearer {api_key}"}, json=body,
            )
    except httpx.HTTPError as exc:
        raise SonioxError(0, f"Soniox couldn't be reached ({type(exc).__name__})") from None
    if resp.status_code not in (200, 201):
        log.warning("soniox temporary key failed: HTTP %s — %s", resp.status_code, _error_text(resp))
        raise SonioxError(resp.status_code, _reason("realtime key creation", resp.status_code))
    try:
        data = resp.json() or {}
    except ValueError:
        data = {}
    temp = str(data.get("api_key") or "")
    if not temp:
        raise SonioxError(resp.status_code, "Soniox returned no temporary key")
    return {"api_key": temp, "expires_at": str(data.get("expires_at") or "")}


async def transcribe(
    api_key: str, audio: bytes, *, mime: str = "audio/ogg", filename: str = "audio",
    language_hints: list[str] | None = None, language_identification: bool = True,
    poll_max: int = 60, timeout: float = 120.0,
) -> Transcript:
    """Transcribe *audio* with Soniox's async API.

    Returns the :class:`Transcript` (``text`` may be "" — Soniox heard no
    speech). Raises :class:`SonioxError` with a short user-facing reason for
    every failure; each one is also logged with Soniox's real answer. The
    uploaded file and the job are deleted on every exit path.
    """
    if not api_key:
        raise SonioxError(0, "no Soniox API key")
    if not audio:
        raise SonioxError(0, "the audio is empty (0 bytes)")
    base = api_base()
    headers = {"Authorization": f"Bearer {api_key}"}
    file_id = transcription_id = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                up = await client.post(
                    f"{base}/v1/files", headers=headers,
                    files={"file": (filename or "audio", audio, mime or "application/octet-stream")},
                )
                if up.status_code >= 400:
                    log.warning("soniox STT upload failed: HTTP %s — %s", up.status_code, up.text[:300])
                    raise SonioxError(up.status_code, _reason("upload", up.status_code))
                file_id = str((up.json() or {}).get("id") or "")
                if not file_id:
                    log.warning("soniox STT upload: no file id — %s", up.text[:300])
                    raise SonioxError(up.status_code, "Soniox upload returned no file id")

                job: dict[str, Any] = {"model": STT_MODEL, "file_id": file_id}
                if language_hints:
                    job["language_hints"] = list(language_hints)
                if language_identification:
                    job["enable_language_identification"] = True
                cr = await client.post(
                    f"{base}/v1/transcriptions",
                    headers={**headers, "Content-Type": "application/json"}, json=job,
                )
                if cr.status_code >= 400:
                    log.warning("soniox STT job create failed: HTTP %s — %s", cr.status_code, cr.text[:300])
                    raise SonioxError(cr.status_code, _reason("job creation", cr.status_code))
                transcription_id = str((cr.json() or {}).get("id") or "")
                if not transcription_id:
                    log.warning("soniox STT job create: no id — %s", cr.text[:300])
                    raise SonioxError(cr.status_code, "Soniox job creation returned no id")

                status = ""
                for _ in range(max(1, poll_max)):
                    p = await client.get(f"{base}/v1/transcriptions/{transcription_id}", headers=headers)
                    if p.status_code >= 400:
                        log.warning("soniox STT status poll failed: HTTP %s — %s", p.status_code, p.text[:300])
                        raise SonioxError(p.status_code, _reason("status poll", p.status_code))
                    pj = p.json() or {}
                    status = str(pj.get("status") or "")
                    if status == "completed":
                        break
                    if status == "error":
                        err = str(pj.get("error_message") or pj.get("error") or "").strip()
                        log.warning("soniox STT job errored: %s", err or "(no error_message)")
                        raise SonioxError(200, f"Soniox couldn't process the audio{(': ' + err) if err else ''}")
                    await asyncio.sleep(1)
                if status != "completed":
                    log.warning("soniox STT timed out after %ss (last status=%r)", poll_max, status)
                    raise SonioxError(0, f"Soniox timed out after {poll_max}s")

                tr = await client.get(f"{base}/v1/transcriptions/{transcription_id}/transcript", headers=headers)
                if tr.status_code >= 400:
                    log.warning("soniox STT transcript fetch failed: HTTP %s — %s", tr.status_code, tr.text[:300])
                    raise SonioxError(tr.status_code, _reason("transcript fetch", tr.status_code))
                data = tr.json() or {}
                tokens = []
                for tok in data.get("tokens") or []:
                    if isinstance(tok, dict) and tok.get("text"):
                        tokens.append({
                            "text": str(tok.get("text")),
                            "start_ms": tok.get("start_ms"),
                            "end_ms": tok.get("end_ms"),
                        })
                return Transcript(text=str(data.get("text") or "").strip(), tokens=tokens)
            finally:
                for path in (
                    f"/v1/transcriptions/{transcription_id}" if transcription_id else "",
                    f"/v1/files/{file_id}" if file_id else "",
                ):
                    if path:
                        try:
                            await client.delete(f"{base}{path}", headers=headers)
                        except Exception:
                            pass
    except SonioxError:
        raise
    except Exception as exc:
        log.warning("soniox STT: unexpected error: %s", exc)
        raise SonioxError(0, f"transcription error ({type(exc).__name__})") from None


def _tts_chunks(text: str, limit: int = TTS_CHUNK_CHARS) -> list[str]:
    """Split *text* at paragraph / sentence / word boundaries into pieces of at
    most *limit* characters."""
    import re

    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?…])\s+|\n{2,}", text):
        sentence = sentence.strip()
        while len(sentence) > limit:  # one huge "sentence": cut at a space
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


async def synthesize(
    api_key: str, text: str, *, voice: str = "", language: str = "", audio_format: str = "mp3",
    timeout: float = 90.0,
) -> bytes:
    """Speak *text* with Soniox TTS; the audio bytes in *audio_format*.

    Text over :data:`TTS_CHUNK_CHARS` is spoken in pieces whose audio is
    joined (mp3 frames play back to back). Raises :class:`SonioxError`."""
    if not api_key:
        raise SonioxError(0, "no Soniox API key")
    text = str(text or "").strip()
    if not text:
        raise SonioxError(400, "nothing to speak (empty text)")
    if len(text) > MAX_TTS_CHARS:
        raise SonioxError(400, f"text too long to speak ({len(text)} characters, max {MAX_TTS_CHARS})")
    audio_format = (audio_format or "mp3").strip().lower()
    if audio_format not in TTS_FORMATS:
        raise SonioxError(400, f"audio_format must be one of {', '.join(TTS_FORMATS)}")
    chunks = _tts_chunks(text)
    if len(chunks) > 1 and audio_format != "mp3":
        raise SonioxError(400, f"text over {TTS_CHUNK_CHARS} characters can only be spoken as mp3")
    payload = {
        "model": os.environ.get("SONIOX_TTS_MODEL", "").strip() or TTS_MODEL,
        "language": (language or os.environ.get("SONIOX_TTS_LANGUAGE", "") or "en").strip(),
        "voice": (voice or os.environ.get("SONIOX_TTS_VOICE", "") or DEFAULT_VOICE).strip(),
        "audio_format": audio_format,
    }
    out = bytearray()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for chunk in chunks:
                resp = await client.post(
                    tts_url(), json={**payload, "text": chunk},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code != 200:
                    detail = _error_text(resp)
                    log.warning("soniox TTS failed: HTTP %s — %s", resp.status_code, detail)
                    if resp.status_code in (401, 403, 429):
                        raise SonioxError(resp.status_code, _reason("speech", resp.status_code))
                    raise SonioxError(resp.status_code, f"Soniox speech failed (HTTP {resp.status_code}){': ' + detail if detail else ''}")
                out.extend(resp.content)
    except httpx.HTTPError as exc:
        raise SonioxError(0, f"Soniox couldn't be reached ({type(exc).__name__})") from None
    if not out:
        raise SonioxError(200, "Soniox returned no audio")
    return bytes(out)
