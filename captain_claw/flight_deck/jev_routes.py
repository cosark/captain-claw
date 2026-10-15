"""Jev (TypeSafe AI) on Flight Deck — each user's key, and every way to use it.

Jev answers typed questions about a piece of text (see :mod:`captain_claw.jev_client`).
Its API key is the user's own and lives ONLY here:

* Per user in ``user_settings`` — ``jev:api_key`` and ``jev:meta`` (JSON
  ``{verified, verify_error, verified_at, updated_at, models}``). ``jev:`` is
  server-owned:
  ``/fd/settings`` never returns it and refuses to write it. An auth-disabled
  deck has one synthetic user (``local``) that isn't a row in ``users`` (the
  user_settings FK would refuse it), so there the same keys go into
  ``system_settings`` — one key for the deck (``scope: "deck"``).
* The team key: an admin may share one key with every user (``PUT``/``DELETE
  /shared-key``, Admin → Integrations), kept in ``system_settings`` as
  ``jev:shared_api_key`` / ``jev:shared_meta``. A user with no key of their own
  uses it (``source: "shared"``); their own key, once saved, wins. Not on an
  auth-disabled deck — its one key is already the deck's.
* The user routes (Connections → Jev): ``GET /status``, ``PUT``/``DELETE
  /key``, ``POST /test`` and the playground's ``POST /ask``. None of them ever
  returns the key — only :func:`jev_client.key_hint`.
* Agents never hold the key. ``/agent/status``, ``/agent/ask`` and
  ``/agent/models`` ask Jev with the calling agent's OWNER's key, the owner
  resolved from this deck's own records by the agent's ``X-Agent-Auth`` (the
  Google / Microsoft resolver, ``google_oauth_routes._agent_owner``), never from
  the agent's word. ``/fd/jev/*`` is not grant-aware, so a shared-agent
  member's request is refused by the speaker-grant middleware (owner-only).
* Flows (the FD-internal ``tool: jev`` step) and the inbound MCP server
  (``jev_ask``) go through the same service functions — :func:`ask_for_owner`
  and friends — as the routes.

The frontend reads a 401 as "the Flight Deck session expired", so a user route
never answers 401 for a Jev-side problem: a refused key is a 400 (see
:func:`http_error`). Every call to Jev's ``/v1/systemone`` is logged to
``usage_logs`` (``jev_call``) for its owner.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captain_claw import jev_client
from captain_claw.flight_deck import google_oauth_routes as _g
from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.agent_identity import require_agent_caller
from captain_claw.flight_deck.auth import _LOCAL_USER, _fd_auth_enabled, get_current_user, get_db
from captain_claw.jev_client import JevAPI, JevError, JevInputError
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/fd/jev", tags=["jev"])

_K_KEY = "jev:api_key"
_K_META = "jev:meta"
# The admin's team key (system_settings). Separate names on purpose: an
# auth-disabled deck keeps ITS key under _K_KEY in system_settings, and that
# must never turn into a team key if sign-in is switched on later.
_K_SHARED_KEY = "jev:shared_api_key"
_K_SHARED_META = "jev:shared_meta"


class JevNotConnected(LookupError):  # noqa: N818 — reads as the condition
    """The owner has no Jev key on this deck."""

    def __init__(self, message: str = jev_client.NOT_CONNECTED) -> None:
        super().__init__(message)


def _api(api_key: str) -> JevAPI:
    """The Jev client for one key (tests replace this factory).

    ``FD_JEV_BASE_URL`` points the whole deck at another Jev-compatible
    endpoint (a proxy in front of api.typesafe.ai, or a local stub)."""
    base = (os.environ.get("FD_JEV_BASE_URL") or "").strip() or jev_client.DEFAULT_BASE_URL
    return JevAPI(api_key, base_url=base)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── storage ─────────────────────────────────────────────────────────


def _deck_scope() -> bool:
    """Auth off: one key for the whole deck, in ``system_settings``."""
    return not _fd_auth_enabled()


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


async def _resolve(owner: str) -> tuple[str, str]:
    """``(key, source)`` for *owner*: their own key (``"own"``), else the team
    key an admin shared (``"shared"``), else ``("", "")``."""
    own = (await _read(owner, _K_KEY)).strip()
    if own:
        return own, "own"
    if owner:
        shared = await _shared_key()
        if shared:
            return shared, "shared"
    return "", ""


async def get_key(owner: str) -> str:
    """The Jev key *owner* uses (their own, else the team's), or "" — for
    Flight Deck's own use only (never returned to a browser or an agent)."""
    return (await _resolve(owner))[0]


def _parse_meta(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


async def _load_meta(owner: str, source: str | None = None) -> dict[str, Any]:
    """What Flight Deck knows about the key *owner* uses — the team key's meta
    when that is the one."""
    if source is None:
        source = (await _resolve(owner))[1]
    if source == "shared":
        return _parse_meta(await get_db().get_system_setting(_K_SHARED_META))
    return _parse_meta(await _read(owner, _K_META))


async def _update_meta(owner: str, **fields: Any) -> None:
    """Merge *fields* into the stored meta of the key *owner* uses (only while
    one is set)."""
    key, source = await _resolve(owner)
    if not key:
        return
    meta = await _load_meta(owner, source)
    meta.update(fields)
    if source == "shared":
        await get_db().set_system_setting(_K_SHARED_META, json.dumps(meta, ensure_ascii=True))
        return
    await _write(owner, {_K_META: json.dumps(meta, ensure_ascii=True)})


async def _mark_verified(owner: str, models: list[dict[str, Any]] | None = None) -> None:
    fields: dict[str, Any] = {"verified": True, "verify_error": None, "verified_at": _now()}
    if models is not None:
        fields["models"] = _model_names(models)
    await _update_meta(owner, **fields)


def _model_names(models: list[dict[str, Any]]) -> list[str]:
    return [str(m.get("name")) for m in models if isinstance(m, dict) and m.get("name")]


def _key_view(key: str, meta: dict[str, Any]) -> dict[str, Any]:
    models = meta.get("models") if isinstance(meta.get("models"), list) else []
    verified = meta.get("verified")
    verify_error = meta.get("verify_error")
    return {
        "key_set": bool(key),
        "key_hint": jev_client.key_hint(key),
        "verified": verified if isinstance(verified, bool) else None,
        # Why an unverified key is unverified: Jev "refused" it (replace it) or
        # couldn't be reached to check it ("unreachable").
        "verify_error": verify_error if verify_error in ("refused", "unreachable") else None,
        "verified_at": meta.get("verified_at") or None,
        "updated_at": meta.get("updated_at") or None,
        "models": [str(m) for m in models],
    }


async def status_for_owner(owner: str) -> dict[str, Any]:
    """JevStatus — never the key itself. ``source`` says whose key it is:
    ``"own"``, ``"shared"`` (the team key an admin shared) or None."""
    key, source = await _resolve(owner)
    meta = await _load_meta(owner, source) if key else {}
    return {
        **_key_view(key, meta),
        "source": source or None,
        "shared_available": bool(await _shared_key()),
        "default_model": jev_client.DEFAULT_MODEL,
        "auth_enabled": _fd_auth_enabled(),
        "scope": "deck" if _deck_scope() else "user",
    }


async def shared_status() -> dict[str, Any]:
    """The team key, as the admin sees it (never the key itself)."""
    key = await _shared_key()
    meta = _parse_meta(await get_db().get_system_setting(_K_SHARED_META)) if key else {}
    return {**_key_view(key, meta), "available": not _deck_scope()}


# ── service (routes, flows, inbound MCP) ────────────────────────────


async def _log_call(
    owner: str, *, source: str, agent: str, model: str, questions: int,
    usage: dict[str, Any], latency_ms: int, ok: bool, status: int,
) -> None:
    """One ``usage_logs`` row per Jev call. Never fails the call."""
    try:
        await get_db().log_usage(owner or _LOCAL_USER["id"], "jev_call", json.dumps({
            "source": source, "agent": agent or "", "model": model,
            "questions": questions,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "latency_ms": latency_ms, "ok": ok, "status": status,
        }))
    except Exception as exc:
        log.warning("Jev usage log failed: %s", type(exc).__name__)


async def ask_for_owner(
    owner: str, state: Any, questions: Any, model: str | None = None,
    *, source: str, agent: str = "",
) -> dict[str, Any]:
    """Ask Jev with *owner*'s key.

    Returns ``{model, answers, usage, values, summary, latency_ms}``. Raises
    :class:`JevInputError` (malformed request — checked before anything else),
    :class:`JevNotConnected` (no key) or :class:`JevError` (Jev refused or
    failed).
    """
    body = jev_client.build_request(state, questions, model)
    key = await get_key(owner)
    if not key:
        raise JevNotConnected()
    t0 = time.monotonic()
    resp: dict[str, Any] = {}
    status, ok = 0, False
    try:
        resp = await _api(key).ask(body["state"], body["questions"], body["model"])
        status, ok = 200, True
    except JevError as exc:
        # A malformed 2xx keeps its real status but is still a failed call.
        status = exc.status
        if exc.status == 401:
            await _update_meta(owner, verified=False, verify_error="refused")
        raise
    finally:
        latency_ms = int((time.monotonic() - t0) * 1000)
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        await _log_call(
            owner, source=source, agent=agent, model=body["model"],
            questions=len(body["questions"]), usage=usage, latency_ms=latency_ms,
            ok=ok, status=status,
        )
    if (await _load_meta(owner)).get("verified") is not True:
        await _mark_verified(owner)
    return {
        "model": resp.get("model") or body["model"],
        "answers": resp.get("answers") or {},
        "usage": usage,
        "values": jev_client.answer_values(resp),
        "summary": jev_client.format_answers(resp),
        "latency_ms": latency_ms,
    }


async def models_for_owner(owner: str) -> list[dict[str, Any]]:
    """Jev's model list with *owner*'s key (also proves the key works, so the
    stored meta is updated either way)."""
    key = await get_key(owner)
    if not key:
        raise JevNotConnected()
    try:
        models = await _api(key).models()
    except JevError as exc:
        if exc.status in (401, 403):
            await _update_meta(owner, verified=False, verify_error="refused")
        raise
    await _mark_verified(owner, models)
    return models


def http_error(exc: Exception) -> HTTPException:
    """The HTTP answer for a service error. Never a 401: the frontend reads
    401 as an expired Flight Deck session, so a bad / revoked key is a 400."""
    if isinstance(exc, JevNotConnected):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, JevInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, JevError):
        if exc.status in (401, 403, 404):
            return HTTPException(status_code=400, detail=str(exc))
        if exc.status == 422:
            return HTTPException(status_code=422, detail=str(exc))
        if exc.status == 429:
            headers = None
            if exc.retry_after is not None:
                headers = {"Retry-After": str(int(math.ceil(exc.retry_after)))}
            return HTTPException(status_code=429, detail=str(exc), headers=headers)
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail=f"Jev request failed: {type(exc).__name__}")


async def flow_tool(args: dict[str, Any], *, owner_id: str = "") -> str:
    """The FD-internal ``tool: jev`` flow step: a JSON string, never raises.

    ``{"ok": true, "model", "answers", "values", "usage", "summary"}`` or
    ``{"ok": false, "error"}``. Runs as the run's owner (auth off: the deck's
    key). ``state`` is text (JSON object / array text is parsed);
    ``questions`` JSON text — a list of questions with a ``name`` or a map.
    """
    owner = owner_id if _fd_auth_enabled() else _LOCAL_USER["id"]
    if not owner:
        return json.dumps({
            "ok": False,
            "error": "This flow run has no owner, so it can't use anyone's Jev key.",
        })
    args = args if isinstance(args, dict) else {}
    try:
        out = await ask_for_owner(
            owner, args.get("state"), args.get("questions"),
            str(args.get("model") or "").strip() or None, source="flow",
        )
    except (JevNotConnected, JevInputError, JevError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — a flow step reports, never raises
        log.warning("Jev flow step failed: %s", type(exc).__name__)
        return json.dumps({"ok": False, "error": f"jev failed: {type(exc).__name__}"})
    return json.dumps({
        "ok": True,
        "model": out["model"],
        "answers": out["answers"],
        "values": out["values"],
        "usage": out["usage"],
        "summary": out["summary"],
    }, ensure_ascii=False)


# ── bodies ──────────────────────────────────────────────────────────


class JevAskBody(BaseModel):
    state: Any = None
    questions: Any = None
    model: str | None = None


# ── user routes (Connections → Jev) ─────────────────────────────────


@router.get("/status")
async def jev_status(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """This user's Jev connection (the deck's, with auth off)."""
    return await status_for_owner(_g._effective_owner(_user))


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
        raise HTTPException(status_code=422, detail="Paste a Jev API key")
    # Hidden characters from copying (zero-width spaces, curly quotes, inner
    # spaces) can't be in a key — and can't go in an HTTP header either.
    if not (key.isascii() and key.isprintable()) or any(c.isspace() for c in key):
        raise HTTPException(
            status_code=422,
            detail=(
                "That doesn't look like a Jev API key — it has characters a key can't "
                "have (hidden spaces or curly quotes from copying). Copy it again from "
                "console.typesafe.ai/keys. Nothing was saved."
            ),
        )
    return key


async def _checked_meta(key: str) -> tuple[dict[str, Any], str]:
    """Ask Jev about *key* before it is saved: ``(meta, warning)``. A key Jev
    refuses raises a 400 (nothing is saved); one Jev couldn't be asked about
    (network, rate limit, outage) comes back unverified, with a warning."""
    now = _now()
    meta: dict[str, Any] = {
        "verified": None, "verify_error": None, "verified_at": None, "updated_at": now, "models": [],
    }
    try:
        models = await _api(key).models()
    except JevError as exc:
        if exc.status in (401, 403):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Jev refused this key (HTTP {exc.status}) — check that it is "
                    "correct and active. Nothing was saved."
                ),
            ) from None
        meta.update(verified=False, verify_error="unreachable")
        return meta, f"Saved, but couldn't verify it with Jev: {exc}"
    meta.update(verified=True, verified_at=now, models=_model_names(models))
    return meta, ""


@router.put("/key")
async def jev_put_key(request: Request, _user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Save a key after checking it with Jev. A key Jev refuses is not saved;
    one Jev couldn't be asked about (network, rate limit, outage) is saved
    unverified, with a ``warning``."""
    key = await _key_from_body(request)
    owner = _g._effective_owner(_user)
    meta, warning = await _checked_meta(key)
    await _write(owner, {_K_KEY: key, _K_META: json.dumps(meta, ensure_ascii=True)})
    out = await status_for_owner(owner)
    if warning:
        out["warning"] = warning
    return out


@router.delete("/key")
async def jev_delete_key(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Forget this user's key (and what Flight Deck knew about it). With a team
    key shared, the user falls back to it."""
    owner = _g._effective_owner(_user)
    await _erase(owner)
    return await status_for_owner(owner)


# ── admin routes (the team key) ─────────────────────────────────────


def _require_team_deck() -> None:
    if _deck_scope():
        raise HTTPException(
            status_code=409,
            detail="Sign-in is off on this deck, so its one Jev key is already everyone's",
        )


@router.get("/shared")
async def jev_shared_status(_admin: dict = Depends(require_admin)) -> dict[str, Any]:
    return await shared_status()


@router.put("/shared-key")
async def jev_put_shared_key(request: Request, _admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Share one Jev key with every user who has none of their own."""
    _require_team_deck()
    key = await _key_from_body(request)
    meta, warning = await _checked_meta(key)
    db = get_db()
    await db.set_system_setting(_K_SHARED_KEY, key)
    await db.set_system_setting(_K_SHARED_META, json.dumps(meta, ensure_ascii=True))
    out = await shared_status()
    if warning:
        out["warning"] = warning
    return out


@router.delete("/shared-key")
async def jev_delete_shared_key(_admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Stop sharing the team key (users' own keys are untouched)."""
    db = get_db()
    # system_settings has no delete; an empty value reads as "no key".
    await db.set_system_setting(_K_SHARED_KEY, "")
    await db.set_system_setting(_K_SHARED_META, "")
    return await shared_status()


@router.post("/test")
async def jev_test(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """A live check with the saved key: Jev's model list."""
    t0 = time.monotonic()
    try:
        models = await models_for_owner(_g._effective_owner(_user))
    except (JevNotConnected, JevError) as exc:
        raise http_error(exc) from None
    return {"ok": True, "models": models, "latency_ms": int((time.monotonic() - t0) * 1000)}


@router.post("/ask")
async def jev_ask(body: JevAskBody, _user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """The playground: ask Jev with this user's key."""
    try:
        return await ask_for_owner(
            _g._effective_owner(_user), body.state, body.questions, body.model, source="ui",
        )
    except (JevNotConnected, JevInputError, JevError) as exc:
        raise http_error(exc) from None


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
    """``(owner, slug)`` of the calling agent, from this deck's records only.

    Auth on: the Google / Microsoft gate (transport, then ``X-Agent-Auth`` →
    owner; browsers, unknown agents and ex-users refused). Auth off: the agent
    gate alone, and the deck's key (owner ``local``)."""
    if _fd_auth_enabled():
        _g._authorize_agent_call(request)
        owner = await _g._agent_owner(request, service="Jev")
        return owner, _agent_slug(request)
    caller = require_agent_caller(request, what="Jev")
    return _LOCAL_USER["id"], caller.slug


@router.get("/agent/status")
async def jev_agent_status(request: Request) -> dict[str, Any]:
    """Whether the calling agent's owner has a Jev key (no network)."""
    owner, _slug = await _agent_caller(request)
    return {"connected": bool(await get_key(owner))}


@router.post("/agent/ask")
async def jev_agent_ask(request: Request, body: JevAskBody) -> dict[str, Any]:
    """Ask Jev for the calling agent, with its OWNER's key."""
    owner, slug = await _agent_caller(request)
    try:
        return await ask_for_owner(
            owner, body.state, body.questions, body.model, source="agent", agent=slug,
        )
    except (JevNotConnected, JevInputError, JevError) as exc:
        raise http_error(exc) from None


@router.get("/agent/models")
async def jev_agent_models(request: Request) -> dict[str, Any]:
    """Jev's models, listed with the calling agent's owner's key."""
    owner, _slug = await _agent_caller(request)
    try:
        return {"models": await models_for_owner(owner)}
    except (JevNotConnected, JevError) as exc:
        raise http_error(exc) from None
