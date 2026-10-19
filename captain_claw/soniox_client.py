"""An agent's way to Soniox (speech-to-text).

Under Flight Deck the Soniox key belongs to the agent's owner (or their team)
and stays in Flight Deck (``flight_deck/soniox_routes.py``): a file is
transcribed by Flight Deck, and a realtime mic stream gets a short-lived key
minted for that one session. Standalone — or when the agent was deliberately
given its own ``SONIOX_API_KEY`` in its env — that key is used directly.

Flight Deck no longer passes its own ``SONIOX_API_KEY`` to the agents it
starts, so a key in the agent's env is always one somebody gave it on purpose.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from captain_claw import soniox_api
from captain_claw.logging import get_logger
from captain_claw.soniox_api import SonioxError, Transcript

log = get_logger(__name__)

_OLD_DECK = "this Flight Deck has no Soniox support yet — it needs an update and a restart"
NOT_CONFIGURED = "Soniox isn't configured — set SONIOX_API_KEY"

# Whether the owner has a Soniox key in Flight Deck: (connected, checked_at).
# "Connected" is trusted for a while; "not connected" is re-checked each time
# (a key the user just saved must work on the next try, and the check is a
# local lookup in Flight Deck).
_CONNECTED: tuple[bool, float] = (False, 0.0)
_CACHE_SECONDS = 60.0


def own_key() -> str:
    """A key this agent was given in its own environment, or ""."""
    return (os.environ.get("SONIOX_API_KEY") or "").strip()


def _fd_base() -> str:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._flight_deck_base()


def _fd_headers() -> dict[str, str]:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    # The agent's own identity (owner's key); /fd/soniox/* is not grant-aware.
    return GoogleOAuthManager._flight_deck_headers(as_speaker=False)


def under_flight_deck() -> bool:
    return bool(_fd_base())


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    detail = data.get("detail") if isinstance(data, dict) else ""
    return detail if isinstance(detail, str) else ""


def _fd_error(resp: httpx.Response) -> SonioxError:
    detail = _detail(resp)
    if resp.status_code in (404, 405) and detail in ("", "Not Found", "Method Not Allowed"):
        return SonioxError(404, _OLD_DECK)
    return SonioxError(resp.status_code, detail or f"Flight Deck refused the Soniox request (HTTP {resp.status_code})")


async def available() -> bool:
    """Whether a transcription can be tried at all (no Soniox call)."""
    global _CONNECTED
    if own_key():
        return True
    if not under_flight_deck():
        return False
    connected, at = _CONNECTED
    if connected and time.time() - at < _CACHE_SECONDS:
        return True
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_fd_base()}/fd/soniox/agent/status", headers=_fd_headers())
        connected = resp.status_code == 200 and bool((resp.json() or {}).get("connected"))
    except Exception as exc:
        log.debug("Flight Deck Soniox status check failed: %s", exc)
        return connected
    _CONNECTED = (connected, time.time())
    return connected


async def transcribe(
    audio: bytes, *, filename: str = "audio", mime: str = "application/octet-stream",
    language_hints: list[str] | None = None, language_identification: bool = True,
    poll_max: int = 120,
) -> Transcript:
    """Transcribe *audio* (text + timed tokens). Raises :class:`SonioxError`
    with a short reason the user can read."""
    key = own_key()
    if key:
        return await soniox_api.transcribe(
            key, audio, mime=mime, filename=filename, language_hints=language_hints,
            language_identification=language_identification, poll_max=poll_max,
        )
    if not under_flight_deck():
        raise SonioxError(0, NOT_CONFIGURED)
    params: dict[str, Any] = {
        "filename": filename, "mime": mime, "poll_max": str(poll_max),
        "language_identification": "1" if language_identification else "0",
    }
    if language_hints:
        params["language_hints"] = ",".join(language_hints)
    try:
        # Upload + Soniox's own job + transfer back: give it the poll window plus slack.
        async with httpx.AsyncClient(timeout=float(poll_max) + 180.0) as client:
            resp = await client.post(
                f"{_fd_base()}/fd/soniox/agent/transcribe", params=params, content=audio,
                headers={**_fd_headers(), "Content-Type": mime or "application/octet-stream"},
            )
    except httpx.TimeoutException:
        raise SonioxError(0, "Flight Deck didn't finish the transcription in time") from None
    except httpx.HTTPError as exc:
        raise SonioxError(0, f"Could not reach Flight Deck for Soniox ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise _fd_error(resp)
    data = resp.json() or {}
    tokens = [t for t in data.get("tokens") or [] if isinstance(t, dict)]
    return Transcript(text=str(data.get("text") or "").strip(), tokens=tokens)


async def realtime_key(usage_type: str = "transcribe_websocket", *, expires_in_seconds: int = 600) -> str:
    """A key for ONE realtime session: the agent's own, else a short-lived one
    Flight Deck mints with the owner's key. Raises :class:`SonioxError`."""
    key = own_key()
    if key:
        return key
    if not under_flight_deck():
        raise SonioxError(0, NOT_CONFIGURED)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_fd_base()}/fd/soniox/agent/realtime-key", headers=_fd_headers(),
                json={"usage_type": usage_type, "expires_in_seconds": expires_in_seconds},
            )
    except httpx.HTTPError as exc:
        raise SonioxError(0, f"Could not reach Flight Deck for Soniox ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise _fd_error(resp)
    temp = str((resp.json() or {}).get("api_key") or "")
    if not temp:
        raise SonioxError(resp.status_code, "Flight Deck returned no realtime key")
    return temp
