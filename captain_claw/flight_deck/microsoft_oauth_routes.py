"""Microsoft (Entra ID / Microsoft account) OAuth endpoints on Flight Deck.

The Microsoft twin of ``google_oauth_routes``, built the same way:

* The deployment's OAuth *client* — an app registration's client id + secret,
  its tenant, the scopes — lives in ``system_settings`` (admin only).
* Each Flight Deck user connects THEIR OWN Microsoft account; the tokens live
  in that user's ``user_settings`` (``microsoft_oauth:*`` — server-owned, never
  readable or writable through ``/fd/settings``).
* Agents this deck spawned pull a fresh access token for their OWNER from
  ``GET /fd/microsoft/access_token``, identified by the ``X-Agent-Auth`` token
  the deck minted — the same resolver as Google (``_agent_owner``), so an agent
  never gets another user's mailbox. Only one redirect URI is registered with
  Microsoft: this deck's ``/fd/microsoft/callback``.
* The sign-in popup is bound to the browser that started it exactly like
  Google's: an authenticated ``POST /connect-ticket`` mints a single-use ticket
  (also an HttpOnly SameSite=Strict cookie), ``/login`` takes it only from
  that browser, and ``/callback`` only completes a state whose cookie matches.

Not (yet) carried over from Google: shared-agent members' own connections (a
member request to these routes is refused by the speaker-grant middleware —
``/fd/microsoft/*`` is not grant-aware) and a send gate (the agent tools only
draft). Like Google, only on an auth-enabled deck.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from captain_claw.flight_deck import google_oauth_routes as _g
from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.auth import _fd_auth_enabled, get_current_user, get_db
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.logging import get_logger
from captain_claw.microsoft_oauth import (
    DEFAULT_SCOPES,
    DEFAULT_TENANT,
    MANAGE_ACCESS_HINT,
    REQUIRED_SCOPES,
    SCOPE_CATALOG,
    MicrosoftOAuthTokens,
    build_authorization_url,
    exchange_code_for_tokens,
    fetch_user_info,
    generate_pkce_pair,
    refresh_access_token,
    sanitize_scopes,
    scope_key,
    valid_tenant,
)

log = get_logger(__name__)

_MESSAGE_TYPE = "captain-claw-microsoft-oauth"

_AUTH_OFF_DETAIL = (
    "Microsoft via Flight Deck isn't available on this deck: Flight Deck auth "
    "is disabled (FD_AUTH_ENABLED=false). Enable auth to connect Microsoft."
)
AUTH_OFF_HEADER = "X-FD-Microsoft-Unavailable"
AUTH_OFF_VALUE = "auth-disabled"

_AGENT_ROUTE_SUFFIXES = ("/access_token", "/agent_status")


def _require_auth_deck(request: Request) -> None:
    """Router-wide gate (see ``google_oauth_routes._require_auth_deck``): an
    auth-disabled deck keeps no DB and can't tell its users apart."""
    if _fd_auth_enabled():
        return
    agent_route = request.url.path.rstrip("/").endswith(_AGENT_ROUTE_SUFFIXES)
    raise HTTPException(
        status_code=403 if agent_route else 503,
        detail=_AUTH_OFF_DETAIL,
        headers={AUTH_OFF_HEADER: AUTH_OFF_VALUE},
    )


router = APIRouter(
    prefix="/fd/microsoft",
    tags=["microsoft-oauth"],
    dependencies=[Depends(_require_auth_deck)],
)


# ── settings keys ───────────────────────────────────────────────────

_K_CLIENT_ID = "microsoft_oauth:client_id"
_K_CLIENT_SECRET = "microsoft_oauth:client_secret"
_K_TENANT = "microsoft_oauth:tenant"
_K_SCOPES = "microsoft_oauth:scopes"
_K_TOKENS = "microsoft_oauth:tokens"  # per user
_K_USER = "microsoft_oauth:user"      # per user

_SCOPE_LABELS = {scope_key(s["scope"]): s["label"] for s in SCOPE_CATALOG}


def _label_scopes(scope_string: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for raw in str(scope_string or "").split():
        key = scope_key(raw)
        if key:
            out.append({"scope": raw, "label": _SCOPE_LABELS.get(key, raw)})
    return out


# ── pending sign-ins (see google_oauth_routes for the reasoning) ────

_pending_oauth: dict[str, dict[str, Any]] = {}
_PENDING_TTL = 600
_STATE_COOKIE = "fd_microsoft_oauth"
_COOKIE_PATH = "/fd/microsoft"

_connect_tickets: dict[str, dict[str, Any]] = {}
_TICKET_TTL = 120
_TICKET_COOKIE = "fd_microsoft_ticket"


def _purge_stale() -> None:
    now = time.time()
    for store, ttl in ((_pending_oauth, _PENDING_TTL), (_connect_tickets, _TICKET_TTL)):
        for k in [k for k, v in store.items() if v.get("ts", 0) < now - ttl]:
            store.pop(k, None)


# ── storage ─────────────────────────────────────────────────────────


async def _load_client(db: FlightDeckDB) -> dict[str, Any]:
    """The stored app registration (secret included — never return it)."""
    raw_scopes = (await db.get_system_setting(_K_SCOPES)) or ""
    scopes = list(DEFAULT_SCOPES)
    if raw_scopes:
        try:
            loaded = json.loads(raw_scopes)
            if isinstance(loaded, list):
                scopes = sanitize_scopes([str(s) for s in loaded])
        except Exception:
            pass
    tenant = ((await db.get_system_setting(_K_TENANT)) or "").strip()
    return {
        "client_id": (await db.get_system_setting(_K_CLIENT_ID)) or "",
        "client_secret": (await db.get_system_setting(_K_CLIENT_SECRET)) or "",
        "tenant": tenant if valid_tenant(tenant) else DEFAULT_TENANT,
        "scopes": scopes,
    }


async def _token_client(db: FlightDeckDB) -> dict[str, Any] | None:
    """The configured client, or None until an admin saved id + secret."""
    client = await _load_client(db)
    if not (client["client_id"] and client["client_secret"]):
        return None
    return client


async def _load_tokens(db: FlightDeckDB, user_id: str) -> MicrosoftOAuthTokens | None:
    raw = await db.get_setting(user_id, _K_TOKENS)
    if not raw:
        return None
    try:
        return MicrosoftOAuthTokens.from_dict(json.loads(raw))
    except Exception as exc:
        log.warning("Failed to deserialize Microsoft OAuth tokens: %s", exc)
        return None


async def _store_tokens(db: FlightDeckDB, user_id: str, tokens: MicrosoftOAuthTokens) -> None:
    await db.set_settings(user_id, {_K_TOKENS: json.dumps(tokens.to_dict(), ensure_ascii=True)})


async def _load_user(db: FlightDeckDB, user_id: str) -> dict[str, Any] | None:
    raw = await db.get_setting(user_id, _K_USER)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _clear_user(db: FlightDeckDB, user_id: str) -> None:
    await db.delete_setting(user_id, _K_TOKENS)
    await db.delete_setting(user_id, _K_USER)


async def _clear_all_users(db: FlightDeckDB) -> None:
    """Disconnect everyone: a new client, tenant or scope set invalidates every
    stored grant (a refresh token is bound to its client and consented scopes)."""
    try:
        users = await db.list_users(limit=10000)
    except Exception:
        users = []
    for u in users:
        uid = str(u.get("id") or "")
        if uid:
            await _clear_user(db, uid)


# Refresh tokens rotate on every use: one refresh per user at a time, so two
# concurrent agent calls never both redeem (and race to store) the same one.
_refresh_locks: dict[str, asyncio.Lock] = {}

_REFRESH_FAILED = (
    "Could not refresh the Microsoft access token — if this persists, reconnect "
    "Microsoft in Flight Deck → Connections → Microsoft"
)


async def _refresh_if_needed(
    db: FlightDeckDB, user_id: str, client: dict[str, Any], tokens: MicrosoftOAuthTokens,
) -> MicrosoftOAuthTokens | None:
    if not tokens.is_expired():
        return tokens
    if not tokens.refresh_token:
        return None
    lock = _refresh_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        # Another call may have refreshed while this one waited.
        current = await _load_tokens(db, user_id)
        if current and not current.is_expired():
            return current
        if current and current.refresh_token:
            tokens = current
        try:
            fresh = await refresh_access_token(
                refresh_token=tokens.refresh_token,
                client_id=client["client_id"],
                client_secret=client["client_secret"],
                scopes=client["scopes"],
                tenant=client["tenant"],
            )
        except Exception as exc:
            log.warning("Microsoft token refresh failed: %s", exc)
            return None
        await _store_tokens(db, user_id, fresh)
        return fresh


# ── FD-side helpers (pollers, exports — none use them yet) ──────────


async def get_valid_microsoft_access_token(user_id: str) -> str | None:
    """A valid Microsoft Graph access token for *user_id*, or None. No default
    owner: an unattributed call gets nothing."""
    if not user_id or not _fd_auth_enabled():
        return None
    try:
        db = get_db()
        client = await _token_client(db)
        if not client:
            return None
        tokens = await _load_tokens(db, user_id)
        if not tokens or not tokens.refresh_token:
            return None
        tokens = await _refresh_if_needed(db, user_id, client, tokens)
        return tokens.access_token if tokens else None
    except Exception:
        return None


async def is_microsoft_connected(user_id: str) -> bool:
    """No network: has *user_id* connected Microsoft on this deck?"""
    if not user_id or not _fd_auth_enabled():
        return False
    try:
        db = get_db()
        if not await _token_client(db):
            return False
        tokens = await _load_tokens(db, user_id)
        return bool(tokens and tokens.refresh_token)
    except Exception:
        return False


# ── redirect URI ────────────────────────────────────────────────────


def _redirect_uri(request: Request) -> str:
    """Must match a Web redirect URI on the app registration exactly.
    FD_PUBLIC_URL wins (reverse proxies); else the request's own host.
    Microsoft accepts plain http only for localhost."""
    public = _g._public_url()
    if public:
        return f"{public}/fd/microsoft/callback"
    host = request.headers.get("host") or request.url.netloc
    return f"{request.url.scheme}://{host}/fd/microsoft/callback"


# ── models ──────────────────────────────────────────────────────────


class MicrosoftConfigUpdate(BaseModel):
    # ``clear=True`` wipes the client and every user's connection. Otherwise a
    # field left None is unchanged; a non-empty string overwrites. ``scopes`` is
    # the full desired list.
    clear: bool | None = None
    client_id: str | None = None
    client_secret: str | None = None
    tenant: str | None = None
    scopes: list[str] | None = None


# ── status / config ─────────────────────────────────────────────────


@router.get("/status")
async def microsoft_status(request: Request, _user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """This user's Microsoft connection, for the Connections page."""
    db = get_db()
    uid = _g._effective_owner(_user)
    client = await _token_client(db)
    tokens = await _load_tokens(db, uid)
    connected = bool(client and tokens and tokens.refresh_token)
    return {
        "configured": client is not None,
        "connected": connected,
        "user": await _load_user(db, uid) if connected else None,
        "granted_scopes": _label_scopes(tokens.scope) if connected and tokens else [],
        "tenant": client["tenant"] if client else "",
        "redirect_uri": _redirect_uri(request),
    }


@router.get("/probe")
async def microsoft_probe(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Live health check: refresh if needed, then a read-only ``/me`` call."""
    db = get_db()
    uid = _g._effective_owner(_user)
    client = await _token_client(db)
    if not client:
        return {"configured": False, "connected": False, "ok": False,
                "error": "Microsoft OAuth not configured"}
    tokens = await _load_tokens(db, uid)
    if not tokens or not tokens.refresh_token:
        return {"configured": True, "connected": False, "ok": False,
                "error": "Not connected — no stored tokens"}
    tokens = await _refresh_if_needed(db, uid, client, tokens)
    if not tokens:
        return {"configured": True, "connected": True, "ok": False,
                "error": "Could not refresh access token"}
    try:
        info = await fetch_user_info(tokens.access_token)
    except Exception as exc:
        return {"configured": True, "connected": True, "ok": False,
                "error": f"Read-only API call failed: {exc}"}
    return {"configured": True, "connected": True, "ok": True, "email": info.get("email", "")}


@router.get("/config")
async def microsoft_config_get(request: Request, _user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """The app registration as the UI shows it — never the secret."""
    client = await _load_client(get_db())
    return {
        "client_id": client["client_id"],
        "client_id_set": bool(client["client_id"]),
        "client_secret_set": bool(client["client_secret"]),
        "tenant": client["tenant"],
        "scopes": client["scopes"],
        "default_scopes": list(DEFAULT_SCOPES),
        "required_scopes": list(REQUIRED_SCOPES),
        "scope_catalog": SCOPE_CATALOG,
        "redirect_uri": _redirect_uri(request),
    }


@router.post("/config")
async def microsoft_config_post(
    body: MicrosoftConfigUpdate, _admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Save the app registration (admin only). A new client id, tenant or
    scope set disconnects every user — their grants belong to the old one."""
    db = get_db()
    if body.clear:
        for key in (_K_CLIENT_ID, _K_CLIENT_SECRET, _K_TENANT, _K_SCOPES):
            await db.set_system_setting(key, "")
        await _clear_all_users(db)
        return {"ok": True, "cleared": True}

    def _clean(v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        return s or None

    client_id = _clean(body.client_id)
    client_secret = _clean(body.client_secret)
    tenant = _clean(body.tenant)
    if tenant is not None and not valid_tenant(tenant):
        raise HTTPException(
            status_code=400,
            detail=(
                "Tenant must be 'common', 'organizations', 'consumers', a "
                "directory (tenant) id, or a domain such as contoso.onmicrosoft.com"
            ),
        )

    before = await _load_client(db)
    changed = False
    if client_id is not None:
        changed |= client_id != before["client_id"]
        await db.set_system_setting(_K_CLIENT_ID, client_id)
    if client_secret is not None:
        await db.set_system_setting(_K_CLIENT_SECRET, client_secret)
    if tenant is not None:
        changed |= tenant.lower() != before["tenant"].lower()
        await db.set_system_setting(_K_TENANT, tenant)
    scopes_changed = False
    if body.scopes is not None:
        new_scopes = sanitize_scopes(body.scopes)
        scopes_changed = set(new_scopes) != set(before["scopes"])
        await db.set_system_setting(_K_SCOPES, json.dumps(new_scopes))
    if changed or scopes_changed:
        await _clear_all_users(db)
    return {"ok": True, "scopes_changed": scopes_changed, "disconnected_all": changed or scopes_changed}


# ── connect / callback / disconnect ─────────────────────────────────


@router.post("/connect-ticket")
async def microsoft_connect_ticket(
    request: Request, _user: dict = Depends(get_current_user),
) -> JSONResponse:
    """A single-use ticket that lets THIS browser start /login for the caller."""
    owner = str(_user.get("id") or "")
    if not owner:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _purge_stale()
    for k in [k for k, v in _connect_tickets.items() if v.get("owner") == owner]:
        _connect_tickets.pop(k, None)
    ticket = secrets.token_urlsafe(32)
    _connect_tickets[ticket] = {"owner": owner, "ts": time.time()}
    resp = JSONResponse({"ticket": ticket}, headers={"Cache-Control": "no-store"})
    resp.set_cookie(
        key=_TICKET_COOKIE, value=ticket, max_age=_TICKET_TTL, httponly=True,
        samesite="strict", path=_COOKIE_PATH, secure=_g._state_cookie_secure(request),
    )
    return resp


def _take_connect_ticket(request: Request) -> str:
    """Spend ``?ticket=`` and return its user — "" unless it is live and this
    browser's cookie holds it."""
    ticket = request.query_params.get("ticket", "")
    entry = _connect_tickets.pop(ticket, None) if ticket else None
    if not entry or time.time() - float(entry.get("ts") or 0) > _TICKET_TTL:
        return ""
    if not _g._same_secret(ticket, request.cookies.get(_TICKET_COOKIE, "")):
        log.warning("Microsoft connect refused: ticket not held by this browser")
        return ""
    return str(entry.get("owner") or "")


def _page(*, ok: bool, title: str, detail: str = "", email: str = "", status_code: int = 200) -> HTMLResponse:
    return _g._callback_html(
        ok=ok, title=title, detail=detail, email=email, status_code=status_code,
        message_type=_MESSAGE_TYPE,
    )


@router.get("/login")
async def microsoft_login(request: Request) -> Response:
    """Redirect the popup to Microsoft's sign-in, for the ticket's user."""
    db = get_db()
    client = await _token_client(db)
    if not client:
        return HTMLResponse(
            "<h3>Microsoft OAuth not configured</h3>"
            "<p>An admin must enter the app registration's Client ID and Client "
            "Secret on the Connections page first.</p>",
            status_code=400,
        )
    owner = _take_connect_ticket(request)
    fd_user = await db.get_user_by_id(owner) if owner else None
    if not fd_user:
        return _page(
            ok=False,
            title="Microsoft sign-in link expired",
            detail=(
                "This link is expired, already used, or was opened outside the "
                "Flight Deck tab that created it. Reload Flight Deck and click "
                "Connect again."
            ),
            status_code=401,
        )
    _purge_stale()
    state = secrets.token_urlsafe(32)
    browser = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    _pending_oauth[state] = {
        "verifier": verifier,
        "ts": time.time(),
        "owner": owner,
        "fd_user": str(fd_user.get("email") or fd_user.get("display_name") or owner),
        "browser": browser,
    }
    resp = RedirectResponse(
        build_authorization_url(
            client_id=client["client_id"],
            redirect_uri=_redirect_uri(request),
            scopes=client["scopes"],
            state=state,
            code_challenge=challenge,
            tenant=client["tenant"],
        ),
        status_code=302,
    )
    secure = _g._state_cookie_secure(request)
    resp.set_cookie(
        key=_STATE_COOKIE, value=browser, max_age=_PENDING_TTL, httponly=True,
        samesite="lax", path=_COOKIE_PATH, secure=secure,
    )
    resp.delete_cookie(_TICKET_COOKIE, path=_COOKIE_PATH, secure=secure, httponly=True, samesite="strict")
    return resp


@router.get("/callback")
async def microsoft_callback(request: Request) -> HTMLResponse:
    """Microsoft's redirect back: exchange the code, store the tokens."""
    resp = await _complete_callback(request)
    resp.delete_cookie(
        _STATE_COOKIE, path=_COOKIE_PATH, secure=_g._state_cookie_secure(request),
        httponly=True, samesite="lax",
    )
    return resp


async def _complete_callback(request: Request) -> HTMLResponse:
    state = request.query_params.get("state", "")
    pending = _pending_oauth.pop(state, None) if state else None
    if pending and time.time() - float(pending.get("ts") or 0) > _PENDING_TTL:
        pending = None

    error = request.query_params.get("error")
    if error:
        desc = request.query_params.get("error_description", error)
        return _page(ok=False, title="Microsoft sign-in error", detail=desc.splitlines()[0][:400])

    code = request.query_params.get("code", "")
    if not code or not state:
        return _page(ok=False, title="Missing code or state")
    if not pending:
        return _page(ok=False, title="Invalid or expired state, please try again.")
    if not _g._same_secret(str(pending.get("browser") or ""), request.cookies.get(_STATE_COOKIE, "")):
        log.warning("Microsoft OAuth callback refused: state not started in this browser")
        return _page(
            ok=False,
            title="Sign-in not started in this browser",
            detail=(
                "This Microsoft sign-in was started from a different browser (or "
                "its sign-in cookie was blocked), so nothing was saved. Open "
                f"Flight Deck at {_redirect_uri(request).rsplit('/fd/', 1)[0]} "
                "in this browser and click Connect Microsoft again."
            ),
            status_code=400,
        )
    owner = str(pending.get("owner") or "")
    if not owner:
        return _page(ok=False, title="Invalid or expired state, please try again.")

    db = get_db()
    client = await _token_client(db)
    if not client:
        return _page(ok=False, title="Microsoft OAuth credentials were removed mid-flow.")
    try:
        tokens = await exchange_code_for_tokens(
            code=code,
            client_id=client["client_id"],
            client_secret=client["client_secret"],
            redirect_uri=_redirect_uri(request),
            code_verifier=pending["verifier"],
            scopes=client["scopes"],
            tenant=client["tenant"],
        )
    except Exception as exc:
        log.error("Microsoft OAuth token exchange failed: %s", exc)
        return _page(ok=False, title="Token exchange failed", detail=str(exc))
    if not tokens.refresh_token:
        return _page(
            ok=False,
            title="No refresh token",
            detail="Microsoft returned no refresh token (offline_access missing?) — nothing was saved.",
        )
    try:
        user = await fetch_user_info(tokens.access_token)
    except Exception as exc:
        log.warning("Failed to fetch Microsoft user info: %s", exc)
        user = {}

    await _store_tokens(db, owner, tokens)
    if user:
        await db.set_settings(owner, {_K_USER: json.dumps(user, ensure_ascii=True)})
    else:
        await db.delete_setting(owner, _K_USER)
    return _page(
        ok=True,
        title="Connected",
        detail=(
            f"Linked {user.get('email') or 'your Microsoft account'} to Flight Deck "
            f"user {pending.get('fd_user') or owner}"
        ),
        email=str(user.get("email") or ""),
    )


@router.post("/logout")
async def microsoft_logout(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Forget this user's Microsoft connection. Microsoft has no endpoint that
    revokes one app's grant, so the answer says where the user can."""
    await _clear_user(get_db(), _g._effective_owner(_user))
    return {"disconnected": True, "note": MANAGE_ACCESS_HINT}


# ── agent-facing ────────────────────────────────────────────────────


@router.get("/access_token")
async def microsoft_access_token(request: Request) -> dict[str, Any]:
    """A currently-valid Graph access token for the calling agent's OWNER."""
    _g._authorize_agent_call(request)
    owner = await _g._agent_owner(request, service="Microsoft")
    db = get_db()
    client = await _token_client(db)
    if not client:
        raise HTTPException(status_code=404, detail="Microsoft OAuth not configured")
    tokens = await _load_tokens(db, owner)
    if not tokens or not tokens.refresh_token:
        raise HTTPException(status_code=404, detail="No stored Microsoft OAuth tokens")
    tokens = await _refresh_if_needed(db, owner, client, tokens)
    if not tokens:
        raise HTTPException(status_code=401, detail=_REFRESH_FAILED)
    return {
        "access_token": tokens.access_token,
        "token_type": tokens.token_type,
        "expires_at": tokens.expires_at,
        "scope": tokens.scope,
    }


@router.get("/agent_status")
async def microsoft_agent_status(request: Request) -> dict[str, Any]:
    """Whether the calling agent's owner has Microsoft connected (no network)."""
    _g._authorize_agent_call(request)
    owner = await _g._agent_owner(request, service="Microsoft")
    return {"connected": bool(await is_microsoft_connected(owner)), "enabled": True}
