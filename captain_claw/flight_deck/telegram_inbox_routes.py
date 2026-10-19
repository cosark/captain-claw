"""HTTP side of the Telegram inbox (engine: telegram_inbox.py).

* ``/fd/telegram-inbox``        — the user's own inbox: switch it on (with
  consent) or off, retention, groups, a peek at what's kept (Connections →
  Telegram → Inbox).
* ``/fd/telegram-inbox/agent/*`` — what the user's agents read: owner only,
  identified like the Google / Jev routes (``X-Agent-Auth``); a shared agent's
  member never gets here (GrantGuardMiddleware refuses member markers).

Read-only throughout: no route sends anything to Telegram on the user's behalf.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captain_claw.flight_deck import fd_crypto, telegram_inbox
from captain_claw.flight_deck import telegram_shared as ts
from captain_claw.flight_deck.auth import get_current_user, get_db
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["telegram-inbox"])


def _auth_enabled() -> bool:
    return os.environ.get("FD_AUTH_ENABLED", "true").lower() in ("true", "1", "yes")


async def _unavailable_reason(db) -> str:
    """Why this deck can't keep a Telegram inbox ("" when it can)."""
    if not _auth_enabled():
        return "The Telegram inbox needs Flight Deck sign-in, which is off on this deck."
    cfg = await ts.load_config(db)
    if not (cfg["token"] and cfg["enabled"]):
        return "This deck has no team Telegram bot yet."
    if not cfg["inbox"]:
        return "An admin turned the Telegram inbox off on this deck."
    if not fd_crypto.available():
        return ("This Flight Deck can't encrypt the inbox yet — it needs the 'cryptography' "
                "package (pip install cryptography) and a restart.")
    return ""


def _when(ts_: Any) -> str | None:
    return telegram_inbox._iso(ts_)


async def _view(user: dict) -> dict[str, Any]:
    db = get_db()
    uid = user["id"]
    reason = await _unavailable_reason(db)
    auth = _auth_enabled()
    link = await db.get_telegram_link(uid) if auth else None
    linked = bool(link and link.get("status") == "approved" and link.get("tg_user_id"))
    settings = (await db.get_tg_inbox_settings(uid) if auth else None) or {}
    enabled = bool(settings.get("enabled"))
    readable = enabled and fd_crypto.available()
    chats = await telegram_inbox.chats(db, uid) if readable else []
    stats = await db.tg_inbox_stats(uid) if readable else {}
    live = [c for c in (await db.list_tg_inbox_connections(uid) if auth else []) if c.get("enabled")]
    try:
        rights = json.loads(live[0].get("rights") or "[]") if live else []
    except ValueError:
        rights = []
    try:
        await ts.refresh_flags()
    except Exception:
        pass
    cfg = await ts.load_config(db)
    return {
        "available": not reason,
        "reason": reason,
        "bot_username": cfg["bot_username"] if not reason else "",
        "bot": ts.bot_flags(),
        "linked": linked,
        "tg_username": (link or {}).get("tg_username") or "",
        "enabled": enabled,
        "collecting": enabled and linked and not reason,
        "consented_at": settings.get("consented_at"),
        "retention_days": int(settings.get("retention_days") or telegram_inbox.DEFAULT_RETENTION),
        "retention_choices": list(telegram_inbox.RETENTION_CHOICES),
        "connection": {
            "connected_at": live[0].get("connected_at"),
            "rights": rights if isinstance(rights, list) else [],
        } if live else None,
        "groups": [c for c in chats if c["kind"] == "group"],
        "private_chats": sum(1 for c in chats if c["kind"] == "private"),
        "stats": {
            "messages": int(stats.get("messages") or 0),
            "chats": int(stats.get("chats") or 0),
            "oldest": _when(stats.get("oldest")),
            "newest": _when(stats.get("newest")),
        },
    }


# ── the user's own inbox ────────────────────────────────────────────


@router.get("/fd/telegram-inbox")
async def my_inbox(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    return await _view(user)


class InboxBody(BaseModel):
    enabled: bool | None = None
    # Turning it on needs this, every time: the user read what's kept and why.
    consent: bool = False
    retention_days: int | None = None


@router.put("/fd/telegram-inbox")
async def set_inbox(body: InboxBody, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    db = get_db()
    uid = user["id"]
    if body.retention_days is not None and body.retention_days not in telegram_inbox.RETENTION_CHOICES:
        raise HTTPException(status_code=422, detail="Keep messages for 7, 30, 90 or 365 days")
    settings = await db.get_tg_inbox_settings(uid) or {}
    on = bool(settings.get("enabled"))
    if body.enabled is True and not on:
        reason = await _unavailable_reason(db)
        if reason:
            raise HTTPException(status_code=409, detail=reason)
        link = await db.get_telegram_link(uid)
        if not (link and link.get("status") == "approved" and link.get("tg_user_id")):
            raise HTTPException(status_code=409, detail=(
                "Connect your Telegram account to the team bot first (above) — the inbox "
                "uses that link to know your messages are yours."))
        if not body.consent:
            raise HTTPException(status_code=422, detail="Turning the inbox on needs your consent")
        await telegram_inbox.set_enabled(db, uid, enabled=True, retention_days=body.retention_days)
    elif body.enabled is False and settings:
        await telegram_inbox.set_enabled(db, uid, enabled=False)
    elif body.retention_days is not None and on:
        await telegram_inbox.set_enabled(db, uid, enabled=True, retention_days=body.retention_days)
    return await _view(user)


@router.delete("/fd/telegram-inbox/messages")
async def clear_inbox(user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Delete every kept message (the inbox stays on, groups stay joined)."""
    await get_db().delete_tg_inbox_data(user["id"], keep_groups=True)
    return await _view(user)


@router.delete("/fd/telegram-inbox/groups/{chat_id}")
async def remove_group(chat_id: str, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Stop keeping a group and delete what was kept from it (the bot leaves
    when nobody else keeps it)."""
    if not await telegram_inbox.remove_group(get_db(), user["id"], chat_id):
        raise HTTPException(status_code=404, detail="No such group in your inbox")
    return await _view(user)


@router.get("/fd/telegram-inbox/recent")
async def recent(limit: int = 20, user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """The newest kept messages — what your agents see."""
    db = get_db()
    settings = await db.get_tg_inbox_settings(user["id"]) or {}
    if not settings.get("enabled") or not fd_crypto.available():
        return {"messages": []}
    items = await telegram_inbox.messages(db, user["id"], limit=max(1, min(limit, 100)))
    return {"messages": items}


# ── agents (owner only) ─────────────────────────────────────────────


async def _agent_owner(request: Request) -> str:
    from captain_claw.flight_deck import google_oauth_routes as _g

    if not _auth_enabled():
        raise HTTPException(
            status_code=409,
            detail="The Telegram inbox needs Flight Deck sign-in, which is off on this deck")
    _g._authorize_agent_call(request)
    return await _g._agent_owner(request, service="Telegram inbox")


async def _require_on(db, owner: str) -> None:
    settings = await db.get_tg_inbox_settings(owner) or {}
    if not settings.get("enabled"):
        raise HTTPException(status_code=409, detail=(
            "Your user's Telegram inbox is off — they can turn it on in Flight Deck → "
            "Connections → Telegram → Inbox"))
    if not fd_crypto.available():
        raise HTTPException(status_code=409, detail=await _unavailable_reason(db))


@router.get("/fd/telegram-inbox/agent/status")
async def agent_status(request: Request) -> dict[str, Any]:
    """Whether the calling agent's owner keeps a Telegram inbox (no network)."""
    if not _auth_enabled():
        return {"connected": False}
    owner = await _agent_owner(request)
    db = get_db()
    settings = await db.get_tg_inbox_settings(owner) or {}
    on = bool(settings.get("enabled")) and not await _unavailable_reason(db)
    return {"connected": on}


@router.post("/fd/telegram-inbox/agent/chats")
async def agent_chats(request: Request) -> dict[str, Any]:
    owner = await _agent_owner(request)
    db = get_db()
    await _require_on(db, owner)
    stats = await db.tg_inbox_stats(owner)
    return {
        "chats": await telegram_inbox.chats(db, owner),
        "collecting": await telegram_inbox.collecting(db, owner),
        "oldest": _when(stats.get("oldest")),
    }


class MessagesBody(BaseModel):
    chat: str = ""
    query: str = ""
    since: int | None = None
    until: int | None = None
    limit: int = 50


@router.post("/fd/telegram-inbox/agent/messages")
async def agent_messages(body: MessagesBody, request: Request) -> dict[str, Any]:
    """Messages, newest first — of one chat (by id or name) or all of them;
    ``query`` keeps those containing every word."""
    owner = await _agent_owner(request)
    db = get_db()
    await _require_on(db, owner)
    chat = None
    if body.chat.strip():
        try:
            chat = telegram_inbox.find_chat(await telegram_inbox.chats(db, owner), body.chat)
        except telegram_inbox.ChatNotFound as exc:
            options = "; ".join(f"{c['title']} (id {c['chat_id']})" for c in exc.candidates)
            raise HTTPException(status_code=404, detail=f"{exc}{': ' + options if options else ''}") from None
    items = await telegram_inbox.messages(
        db, owner, chat_id=chat["chat_id"] if chat else None, since=body.since,
        until=body.until, query=body.query, limit=max(1, min(body.limit, 200)))
    return {"chat": chat, "messages": items}
