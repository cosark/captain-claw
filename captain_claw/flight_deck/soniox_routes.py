"""Soniox (speech-to-text, text-to-speech) on Flight Deck — whose key, and
every way it is used.

Soniox transcribes WhatsApp voice notes, the StickS3 / StopWatch push-to-talk,
agents' ``stt`` tool, ``video_vision`` and the browser / hotkey mic, and speaks
replies (glasses, devices, WhatsApp). Its key used to be one ``SONIOX_API_KEY``
in the server's environment. It now lives in Flight Deck, like Jev's
(``jev_routes.py``):

* Per user in ``user_settings`` — ``soniox:api_key`` and ``soniox:meta`` (JSON
  ``{verified, verify_error, verified_at, updated_at, models}``). ``soniox:`` is
  server-owned: ``/fd/settings`` never returns it and refuses to write it. An
  auth-disabled deck has one synthetic user, so there the same keys go into
  ``system_settings`` — one key for the deck (``scope: "deck"``).
* The team key: an admin shares one key with every user (Admin → Integrations),
  in ``system_settings`` as ``soniox:shared_api_key`` / ``soniox:shared_meta``.
  A user with no key of their own uses it; their own key wins.
* ``SONIOX_API_KEY`` in the environment is the last fallback (``source:
  "env"``), so a deck that had it keeps working. An admin moves it into Flight
  Deck with one click (``POST /import-env``). Agents no longer inherit it
  (``server._FD_ONLY_ENV_VARS``).

Which key a use takes: a person's own (``own``), else the team key
(``shared``), else the environment (``env``). Surfaces with a person behind
them use that person's — a device its owner's, an agent its owner's, the
WhatsApp bridge its agent's owner's. Surfaces with none (the glasses pages,
behind their shared bridge token) use the deck's: the team key, else the env.

Agents never hold a long-lived key. ``/agent/transcribe`` transcribes a file
with the calling agent's OWNER's key (the owner resolved from this deck's own
records by ``X-Agent-Auth``, as for Google / Jev), and ``/agent/realtime-key``
mints a short-lived Soniox key for one realtime session (the mic stream). No
route ever returns a stored key — only :func:`soniox_api.key_hint`.

The frontend reads a 401 as "the Flight Deck session expired", so a user route
never answers 401 for a Soniox-side problem: a refused key is a 400. Every
transcription and realtime key is logged to ``usage_logs`` (``soniox_call``)
for its owner.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captain_claw import soniox_api
from captain_claw.flight_deck import google_oauth_routes as _g
from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.agent_identity import require_agent_caller
from captain_claw.flight_deck.auth import _LOCAL_USER, _fd_auth_enabled, get_current_user, get_db
from captain_claw.logging import get_logger
from captain_claw.soniox_api import SonioxError, Transcript

log = get_logger(__name__)

router = APIRouter(prefix="/fd/soniox", tags=["soniox"])

_K_KEY = "soniox:api_key"
_K_META = "soniox:meta"
# The admin's team key (system_settings). Separate names on purpose: an
# auth-disabled deck keeps ITS key under _K_KEY in system_settings, and that
# must never turn into a team key if sign-in is switched on later.
_K_SHARED_KEY = "soniox:shared_api_key"
_K_SHARED_META = "soniox:shared_meta"
_ENV_VAR = "SONIOX_API_KEY"

# An agent's upload (a video's whole audio track can be long).
MAX_AGENT_AUDIO_BYTES = 500 * 1024 * 1024

NOT_CONNECTED = (
    "Soniox isn't connected — add a Soniox API key in Connections → Soniox, "
    "or ask your admin to share the team key (Admin → Integrations)"
)
NOT_CONNECTED_DECK = (
    "Soniox isn't set up on this deck — an admin adds the team key in "
    "Admin → Integrations → Shared credentials"
)

# What Flight Deck learned about the environment key this run (never stored:
# the env can change with a restart).
_ENV_META: dict[str, Any] = {}


class SonioxNotConnected(LookupError):  # noqa: N818 — reads as the condition
    """No Soniox key for this owner (nor the team's, nor the environment's)."""

    def __init__(self, message: str = NOT_CONNECTED) -> None:
        super().__init__(message)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── storage ─────────────────────────────────────────────────────────


def _deck_scope() -> bool:
    """Auth off: one key for the whole deck, in ``system_settings``."""
    return not _fd_auth_enabled()


def _env_key() -> str:
    return (os.environ.get(_ENV_VAR) or "").strip()


async def _read(owner: str, key: str) -> str:
    db = get_db()
    if _deck_scope():
        return (await db.get_system_setting(key)) or ""
    if not owner:
        return ""
    return (await db.get_setting(owner, key)) or ""


async def _write(owner: str, values: dict[str, str]) -> None:
    db = get_db()
    if _deck_scope():
        for key, value in values.items():
            await db.set_system_setting(key, value)
        return
    await db.set_settings(owner, values)


async def _erase(owner: str) -> None:
    db = get_db()
    if _deck_scope():
        # system_settings has no delete; an empty value reads as "no key".
        for key in (_K_KEY, _K_META):
            await db.set_system_setting(key, "")
        return
    for key in (_K_KEY, _K_META):
        await db.delete_setting(owner, key)


async def _shared_key() -> str:
    """The admin's team key, or "" (always "" on an auth-disabled deck)."""
    if _deck_scope():
        return ""
    return ((await get_db().get_system_setting(_K_SHARED_KEY)) or "").strip()


async def _resolve(owner: str | None) -> tuple[str, str]:
    """``(key, source)``: *owner*'s own key (``"own"``; the deck's with auth
    off), else the team key (``"shared"``), else ``SONIOX_API_KEY``
    (``"env"``), else ``("", "")``. No *owner* — a surface with no person
    behind it — skips straight to the team key."""
    own = (await _read(owner or "", _K_KEY)).strip()
    if own:
        return own, "own"
    shared = await _shared_key()
    if shared:
        return shared, "shared"
    env = _env_key()
    if env:
        return env, "env"
    return "", ""


async def key_for(owner: str | None) -> str:
    """The Soniox key a use on *owner*'s behalf takes (None: the deck's), or
    "" — for Flight Deck's own use only (never to a browser or an agent)."""
    return (await _resolve(owner))[0]


def _parse_meta(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


async def _load_meta(owner: str | None, source: str) -> dict[str, Any]:
    if source == "shared":
        return _parse_meta(await get_db().get_system_setting(_K_SHARED_META))
    if source == "env":
        return dict(_ENV_META) if _ENV_META.get("hint") == soniox_api.key_hint(_env_key()) else {}
    return _parse_meta(await _read(owner or "", _K_META))


async def _update_meta(owner: str | None, **fields: Any) -> None:
    """Merge *fields* into the meta of the key *owner* uses (only while one is)."""
    key, source = await _resolve(owner)
    if not key:
        return
    meta = await _load_meta(owner, source)
    meta.update(fields)
    if source == "shared":
        await get_db().set_system_setting(_K_SHARED_META, json.dumps(meta, ensure_ascii=True))
    elif source == "env":
        _ENV_META.clear()
        _ENV_META.update(meta, hint=soniox_api.key_hint(key))
    else:
        await _write(owner or "", {_K_META: json.dumps(meta, ensure_ascii=True)})


async def _mark_ok(owner: str | None) -> None:
    key, source = await _resolve(owner)
    if key and (await _load_meta(owner, source)).get("verified") is not True:
        await _update_meta(owner, verified=True, verify_error=None, verified_at=_now())


async def _mark_refused(owner: str | None) -> None:
    await _update_meta(owner, verified=False, verify_error="refused")


def _key_view(key: str, meta: dict[str, Any]) -> dict[str, Any]:
    models = meta.get("models") if isinstance(meta.get("models"), list) else []
    verified = meta.get("verified")
    verify_error = meta.get("verify_error")
    return {
        "key_set": bool(key),
        "key_hint": soniox_api.key_hint(key),
        "verified": verified if isinstance(verified, bool) else None,
        # Why an unverified key is unverified: Soniox "refused" it, or couldn't
        # be reached to check it ("unreachable").
        "verify_error": verify_error if verify_error in ("refused", "unreachable") else None,
        "verified_at": meta.get("verified_at") or None,
        "updated_at": meta.get("updated_at") or None,
        "models": [str(m) for m in models],
    }


async def status_for_owner(owner: str) -> dict[str, Any]:
    """SonioxStatus — never the key itself. ``source``: ``"own"``,
    ``"shared"`` (the team key), ``"env"`` (the server's SONIOX_API_KEY) or None."""
    key, source = await _resolve(owner)
    meta = await _load_meta(owner, source) if key else {}
    return {
        **_key_view(key, meta),
        "source": source or None,
        "shared_available": bool(await _shared_key()),
        "auth_enabled": _fd_auth_enabled(),
        "scope": "deck" if _deck_scope() else "user",
    }


async def shared_status() -> dict[str, Any]:
    """The team key as the admin sees it (never the key), plus whether the
    server's environment still carries a SONIOX_API_KEY to move in."""
    key = await _shared_key()
    meta = _parse_meta(await get_db().get_system_setting(_K_SHARED_META)) if key else {}
    env = _env_key()
    return {
        **_key_view(key, meta),
        "available": not _deck_scope(),
        "env_key_set": bool(env),
        "env_key_hint": soniox_api.key_hint(env),
    }


# ── service (bridges, devices, agent routes) ────────────────────────


async def _log_call(owner: str | None, **detail: Any) -> None:
    """One ``usage_logs`` row per Soniox use. Never fails the call."""
    try:
        await get_db().log_usage(owner or _LOCAL_USER["id"], "soniox_call", json.dumps(detail))
    except Exception as exc:
        log.warning("Soniox usage log failed: %s", type(exc).__name__)


async def transcribe_for(
    owner: str | None, audio: bytes, *, mime: str = "audio/ogg", filename: str = "audio",
    language_hints: list[str] | None = None, language_identification: bool = True,
    poll_max: int = 60, source: str = "", agent: str = "",
) -> Transcript:
    """Transcribe *audio* with the key *owner* uses (None: the deck's).

    Raises :class:`SonioxNotConnected` (no key) or :class:`SonioxError`."""
    key = await key_for(owner)
    if not key:
        raise SonioxNotConnected(NOT_CONNECTED if owner else NOT_CONNECTED_DECK)
    t0 = time.monotonic()
    ok, status = False, 0
    try:
        out = await soniox_api.transcribe(
            key, audio, mime=mime, filename=filename, language_hints=language_hints,
            language_identification=language_identification, poll_max=poll_max,
        )
        ok, status = True, 200
    except SonioxError as exc:
        status = exc.status
        if exc.refused:
            await _mark_refused(owner)
        raise
    finally:
        await _log_call(
            owner, kind="stt", source=source, agent=agent, audio_bytes=len(audio or b""),
            ok=ok, status=status, latency_ms=int((time.monotonic() - t0) * 1000),
        )
    await _mark_ok(owner)
    return out


async def transcribe_text(
    owner: str | None, audio: bytes, mime: str = "audio/ogg", **kwargs: Any,
) -> tuple[str, str]:
    """``(text, reason)`` for a bridge: never raises; an empty text comes
    with a short reason the person can read."""
    try:
        out = await transcribe_for(owner, audio, mime=mime, **kwargs)
    except (SonioxNotConnected, SonioxError) as exc:
        log.warning("soniox STT for %s: %s", owner or "deck", exc)
        return "", str(exc)
    if not out.text:
        log.info("soniox STT: job completed but transcript was empty (silence / no speech?)")
        return "", "no speech detected in the audio"
    return out.text, ""


async def realtime_key_for(
    owner: str | None, usage_type: str, *, expires_in_seconds: int = 600,
    source: str = "", agent: str = "",
) -> dict[str, str]:
    """A short-lived Soniox key for one realtime session, made with the key
    *owner* uses. Raises :class:`SonioxNotConnected` / :class:`SonioxError`."""
    key = await key_for(owner)
    if not key:
        raise SonioxNotConnected(NOT_CONNECTED if owner else NOT_CONNECTED_DECK)
    ok, status = False, 0
    try:
        out = await soniox_api.temporary_key(
            key, usage_type, expires_in_seconds=expires_in_seconds,
            client_reference_id=f"captain-claw:{agent or source or 'fd'}",
        )
        ok, status = True, 201
    except SonioxError as exc:
        status = exc.status
        if exc.refused:
            await _mark_refused(owner)
        raise
    finally:
        await _log_call(owner, kind="realtime_key", usage_type=usage_type, source=source,
                        agent=agent, ok=ok, status=status)
    await _mark_ok(owner)
    return out


def http_error(exc: Exception) -> HTTPException:
    """The HTTP answer for a service error. Never a 401: the frontend reads
    401 as an expired Flight Deck session, so a refused key is a 400."""
    if isinstance(exc, SonioxNotConnected):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SonioxError):
        if exc.refused:
            return HTTPException(status_code=400, detail=str(exc))
        if exc.status == 429:
            return HTTPException(status_code=429, detail=str(exc))
        if exc.status == 400:
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail=f"Soniox request failed: {type(exc).__name__}")


# ── key input ───────────────────────────────────────────────────────


async def _key_from_body(request: Request) -> str:
    """``api_key`` from a ``{"api_key": "..."}`` JSON body. Read by hand, not
    through a pydantic model: a malformed body would otherwise reach FD's
    validation handler, which logs (and echoes) the raw input — the key."""
    try:
        data = await request.json()
    except Exception:
        data = None
    raw = data.get("api_key") if isinstance(data, dict) else None
    key = raw.strip() if isinstance(raw, str) else ""
    if not key:
        raise HTTPException(status_code=422, detail="Paste a Soniox API key")
    _check_key_chars(key)
    return key


def _check_key_chars(key: str) -> None:
    # Hidden characters from copying can't be in a key — nor in an HTTP header.
    if not (key.isascii() and key.isprintable()) or any(c.isspace() for c in key):
        raise HTTPException(
            status_code=422,
            detail=(
                "That doesn't look like a Soniox API key — it has characters a key can't "
                "have (hidden spaces or curly quotes from copying). Copy it again from "
                "console.soniox.com. Nothing was saved."
            ),
        )


async def _checked_meta(key: str) -> tuple[dict[str, Any], str]:
    """Ask Soniox about *key* before it is saved: ``(meta, warning)``. A key
    Soniox refuses raises a 400 (nothing is saved); one Soniox couldn't be
    asked about comes back unverified, with a warning."""
    now = _now()
    meta: dict[str, Any] = {
        "verified": None, "verify_error": None, "verified_at": None, "updated_at": now, "models": [],
    }
    try:
        models = await soniox_api.list_models(key)
    except SonioxError as exc:
        if exc.refused:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Soniox refused this key (HTTP {exc.status}) — check that it is "
                    "correct and active. Nothing was saved."
                ),
            ) from None
        meta.update(verified=False, verify_error="unreachable")
        return meta, f"Saved, but couldn't verify it with Soniox: {exc}"
    meta.update(verified=True, verified_at=now, models=soniox_api.model_names(models))
    return meta, ""


# ── user routes (Connections → Soniox) ──────────────────────────────


@router.get("/status")
async def soniox_status(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """This user's Soniox connection (the deck's, with auth off)."""
    return await status_for_owner(_g._effective_owner(_user))


@router.put("/key")
async def soniox_put_key(request: Request, _user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Save a key after checking it with Soniox. A key Soniox refuses is not
    saved; one it couldn't be asked about is saved unverified (``warning``)."""
    key = await _key_from_body(request)
    owner = _g._effective_owner(_user)
    meta, warning = await _checked_meta(key)
    await _write(owner, {_K_KEY: key, _K_META: json.dumps(meta, ensure_ascii=True)})
    out = await status_for_owner(owner)
    if warning:
        out["warning"] = warning
    return out


@router.delete("/key")
async def soniox_delete_key(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Forget this user's key. They fall back to the team key, then the env."""
    owner = _g._effective_owner(_user)
    await _erase(owner)
    return await status_for_owner(owner)


@router.post("/test")
async def soniox_test(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """A live check of the key this user's Soniox calls take: its model list."""
    owner = _g._effective_owner(_user)
    key = await key_for(owner)
    if not key:
        raise http_error(SonioxNotConnected())
    t0 = time.monotonic()
    try:
        models = await soniox_api.list_models(key)
    except SonioxError as exc:
        if exc.refused:
            await _mark_refused(owner)
        raise http_error(exc) from None
    await _update_meta(owner, verified=True, verify_error=None, verified_at=_now(),
                       models=soniox_api.model_names(models))
    return {"ok": True, "models": soniox_api.model_names(models),
            "latency_ms": int((time.monotonic() - t0) * 1000)}


# ── admin routes (the team key) ─────────────────────────────────────


def _require_team_deck() -> None:
    if _deck_scope():
        raise HTTPException(
            status_code=409,
            detail="Sign-in is off on this deck, so its one Soniox key is already everyone's",
        )


async def _save_shared(key: str) -> dict[str, Any]:
    meta, warning = await _checked_meta(key)
    db = get_db()
    await db.set_system_setting(_K_SHARED_KEY, key)
    await db.set_system_setting(_K_SHARED_META, json.dumps(meta, ensure_ascii=True))
    out = await shared_status()
    if warning:
        out["warning"] = warning
    return out


@router.get("/shared")
async def soniox_shared_status(_admin: dict = Depends(require_admin)) -> dict[str, Any]:
    return await shared_status()


@router.put("/shared-key")
async def soniox_put_shared_key(request: Request, _admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Share one Soniox key with every user who has none of their own."""
    _require_team_deck()
    return await _save_shared(await _key_from_body(request))


@router.delete("/shared-key")
async def soniox_delete_shared_key(_admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Stop sharing the team key (users' own keys are untouched)."""
    db = get_db()
    await db.set_system_setting(_K_SHARED_KEY, "")
    await db.set_system_setting(_K_SHARED_META, "")
    return await shared_status()


@router.post("/import-env")
async def soniox_import_env(_admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Move the server's ``SONIOX_API_KEY`` into Flight Deck: the team key
    (sign-in on) or the deck's key (sign-in off). Checked like a pasted key.
    The env var can be removed from the server afterwards."""
    env = _env_key()
    if not env:
        raise HTTPException(status_code=404, detail="The server's environment has no SONIOX_API_KEY to move")
    _check_key_chars(env)
    if not _deck_scope():
        return await _save_shared(env)
    meta, warning = await _checked_meta(env)
    await _write(_LOCAL_USER["id"], {_K_KEY: env, _K_META: json.dumps(meta, ensure_ascii=True)})
    out = await status_for_owner(_LOCAL_USER["id"])
    if warning:
        out["warning"] = warning
    return out


# ── agent routes ────────────────────────────────────────────────────


def _agent_slug(request: Request) -> str:
    """Best-effort FD slug of the calling agent, for the usage log."""
    try:
        from captain_claw.flight_deck.server import _find_agent_by_auth

        matched, _owner, slug = _find_agent_by_auth(request.headers.get("X-Agent-Auth", ""))
    except Exception:
        return ""
    return str(slug) if matched and slug else ""


async def _agent_caller(request: Request) -> tuple[str, str]:
    """``(owner, slug)`` of the calling agent, from this deck's records only
    (the Google / Jev gate). Auth off: the agent gate, and the deck's key."""
    if _fd_auth_enabled():
        _g._authorize_agent_call(request)
        owner = await _g._agent_owner(request, service="Soniox")
        return owner, _agent_slug(request)
    caller = require_agent_caller(request, what="Soniox")
    return _LOCAL_USER["id"], caller.slug


@router.get("/agent/status")
async def soniox_agent_status(request: Request) -> dict[str, Any]:
    """Whether the calling agent's owner has a Soniox key (no network)."""
    owner, _slug = await _agent_caller(request)
    return {"connected": bool(await key_for(owner))}


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@router.post("/agent/transcribe")
async def soniox_agent_transcribe(request: Request) -> dict[str, Any]:
    """Transcribe the request body (raw audio bytes) for the calling agent,
    with its OWNER's key: ``{"text", "tokens": [{text, start_ms, end_ms}]}``.

    Query: ``filename``, ``mime`` (else the Content-Type), ``language_hints``
    (comma-separated), ``language_identification`` (default on), ``poll_max``
    (seconds to wait for the job, 1–600)."""
    owner, slug = await _agent_caller(request)
    q = request.query_params
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=422, detail="Send the audio as the request body")
    if len(audio) > MAX_AGENT_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="That audio is too large to transcribe")
    hints = [h.strip() for h in (q.get("language_hints") or "").split(",") if h.strip()]
    try:
        poll_max = max(1, min(int(q.get("poll_max") or 120), 600))
    except ValueError:
        poll_max = 120
    try:
        out = await transcribe_for(
            owner, audio,
            mime=(q.get("mime") or request.headers.get("content-type") or "application/octet-stream"),
            filename=(q.get("filename") or "audio")[:200],
            language_hints=hints or None,
            language_identification=_flag(q.get("language_identification"), True),
            poll_max=poll_max, source="agent", agent=slug,
        )
    except (SonioxNotConnected, SonioxError) as exc:
        raise http_error(exc) from None
    return {"text": out.text, "tokens": out.tokens}


class RealtimeKeyBody(BaseModel):
    usage_type: str = "transcribe_websocket"
    expires_in_seconds: int = 600


@router.post("/agent/realtime-key")
async def soniox_agent_realtime_key(request: Request, body: RealtimeKeyBody) -> dict[str, Any]:
    """A short-lived Soniox key for one realtime session of the calling agent
    (``transcribe_websocket`` — the mic stream; ``tts_rt``), minted with its
    OWNER's key. The owner's key itself never leaves Flight Deck."""
    owner, slug = await _agent_caller(request)
    try:
        return await realtime_key_for(
            owner, body.usage_type, expires_in_seconds=body.expires_in_seconds,
            source="agent", agent=slug,
        )
    except (SonioxNotConnected, SonioxError) as exc:
        raise http_error(exc) from None


# ── helpers for other Flight Deck modules ───────────────────────────


def owner_of_agent_slug(slug: str) -> str:
    """The recorded owner of a process agent on this deck, or "" (unknown,
    no owner, or an auth-disabled deck — where the deck's key is everyone's)."""
    slug = (slug or "").strip()
    if not slug or not _fd_auth_enabled():
        return ""
    try:
        from captain_claw.flight_deck.server import _load_process_registry

        entry = _load_process_registry().get(slug) or {}
    except Exception:
        return ""
    return str(entry.get("owner") or "")
