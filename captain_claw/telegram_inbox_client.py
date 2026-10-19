"""An agent's way to its owner's Telegram inbox.

Flight Deck keeps the inbox (``flight_deck/telegram_inbox.py``): a read-only
copy of the owner's private Telegram chats (through a Chat Automation
connection to the deck's shared bot) and the groups they added that bot to.
The agent only reads it, through ``/fd/telegram-inbox/agent/*`` — owner only,
so a shared agent's member never reaches it.

:func:`inbox_usable` is the synchronous answer the tool registry needs (the
``telegram_inbox`` tool shows only while it is True); :func:`refresh_connected`
refreshes it and runs once per turn. There is no standalone mode: without
Flight Deck there is no inbox.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from captain_claw.logging import get_logger

log = get_logger(__name__)

MEMBER_REFUSED = (
    "The Telegram inbox isn't available during a shared-agent member's turn — it "
    "belongs to the agent's owner."
)
_OLD_DECK = "this Flight Deck has no Telegram inbox yet — it needs an update and a restart"

# Whether the owner keeps a Telegram inbox: (connected, checked_at).
_CONNECTED: tuple[bool, float] = (False, 0.0)
_LAST_CHECK_AT = 0.0
# While connected, re-check at most this often (a turn may refresh many times).
_REFRESH_MIN_GAP = 20.0
# How long a positive check keeps the tool listed without a fresh one.
_USABLE_MAX_AGE = 900.0


class InboxError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _fd_base() -> str:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._flight_deck_base()


def _member_call() -> bool:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    return GoogleOAuthManager._member_call()


def _fd_headers() -> dict[str, str]:
    from captain_claw.google_oauth_manager import GoogleOAuthManager

    # Owner-only: never the member grant — /fd/telegram-inbox/* is not grant-aware.
    return GoogleOAuthManager._flight_deck_headers(as_speaker=False)


def _mark(connected: bool) -> None:
    global _CONNECTED
    _CONNECTED = (connected, time.time())


def inbox_usable() -> bool:
    """Whether the telegram_inbox tool should be offered right now (sync)."""
    connected, at = _CONNECTED
    return connected and at > 0 and time.time() - at <= _USABLE_MAX_AGE


async def refresh_connected(force: bool = False) -> bool:
    """Re-check whether the owner keeps a Telegram inbox. A member's turn
    reports not connected and leaves the owner's flag alone; an unreachable
    Flight Deck keeps the previous flag."""
    global _LAST_CHECK_AT
    if not _fd_base() or _member_call():
        return False
    connected, _at = _CONNECTED
    if not force and connected and time.time() - _LAST_CHECK_AT < _REFRESH_MIN_GAP:
        _mark(True)
        return True
    _LAST_CHECK_AT = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_fd_base()}/fd/telegram-inbox/agent/status", headers=_fd_headers())
    except Exception as exc:
        log.debug("Flight Deck Telegram inbox status check failed: %s", exc)
        return inbox_usable()
    try:
        connected = resp.status_code == 200 and bool((resp.json() or {}).get("connected"))
    except Exception:
        connected = False
    _mark(connected)
    return connected


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    detail = data.get("detail") if isinstance(data, dict) else ""
    return detail if isinstance(detail, str) else ""


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    if _member_call():
        raise InboxError(0, MEMBER_REFUSED)
    if not _fd_base():
        raise InboxError(0, "The Telegram inbox lives in Flight Deck, and this agent isn't running under one")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{_fd_base()}{path}", json=body, headers=_fd_headers())
    except Exception as exc:
        raise InboxError(0, f"Could not reach Flight Deck for the Telegram inbox: {type(exc).__name__}") from None
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError:
            raise InboxError(200, "Flight Deck returned a non-JSON answer") from None
        return data if isinstance(data, dict) else {}
    detail = _detail(resp)
    if resp.status_code in (404, 405) and detail in ("", "Not Found", "Method Not Allowed"):
        detail = _OLD_DECK
    if resp.status_code in (401, 403, 409):
        _mark(False)
    raise InboxError(resp.status_code, detail or f"Flight Deck refused (HTTP {resp.status_code})")


# ── what's new, once per owner turn ─────────────────────────────────

_NEWS_CHATS = 5
_TITLE_MAX = 40


def _clean_title(title: Any) -> str:
    """A chat name for the context line: other people chose it, so one line,
    no brackets or quotes, short."""
    text = " ".join(str(title or "").split())
    text = "".join(ch for ch in text if ch.isprintable() and ch not in "[]{}<>\"\u201c\u201d")
    return (text[:_TITLE_MAX - 1] + "…") if len(text) > _TITLE_MAX else (text or "a chat")


def news_line(data: dict[str, Any] | None, *, detail: str = "normal") -> str:
    """One context line from Flight Deck's news ("" when nothing is new):
    counts and chat names only, never message text."""
    if not isinstance(data, dict) or not data.get("connected"):
        return ""
    chats = [c for c in (data.get("chats") or []) if isinstance(c, dict) and int(c.get("new") or 0) > 0]
    if not chats or detail == "nano":
        return ""
    parts = []
    for c in chats[:_NEWS_CHATS]:
        n, name = int(c["new"]), _clean_title(c.get("title"))
        if detail == "micro":
            parts.append(f"\"{name}\" {n}")
        elif c.get("kind") == "group":
            parts.append(f"{n} new in \"{name}\" (group)")
        else:
            parts.append(f"{n} new from \"{name}\"")
    more = len(chats) - _NEWS_CHATS
    if more > 0:
        parts.append(f"+{more} more chat{'' if more == 1 else 's'}")
    when = "in the last day" if data.get("first_look") else "since your last turn"
    if detail == "micro":
        return f"Telegram new ({when}): {', '.join(parts)} — telegram_inbox reads them."
    return (
        f"Telegram {when}: {', '.join(parts)}. Read them with telegram_inbox if they bear on "
        "what the user is doing or look urgent; otherwise don't bring them up."
    )


async def news() -> dict[str, Any] | None:
    """What's new on the owner's Telegram since this agent's last look —
    once per owner turn (the look moves the agent's cursor in Flight Deck).
    Also refreshes the tool's "connected" flag. None when it can't be asked."""
    if not _fd_base() or _member_call():
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{_fd_base()}/fd/telegram-inbox/agent/news",
                                     json={}, headers=_fd_headers())
    except Exception as exc:
        log.debug("Flight Deck Telegram news failed: %s", exc)
        return None
    if resp.status_code != 200:
        if resp.status_code in (401, 403, 404):
            _mark(False)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    _mark(bool(data.get("connected")))
    return data


async def chats() -> dict[str, Any]:
    return await _post("/fd/telegram-inbox/agent/chats", {})


async def messages(
    *, chat: str = "", query: str = "", since: int | None = None, until: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return await _post("/fd/telegram-inbox/agent/messages", {
        "chat": chat, "query": query, "since": since, "until": until, "limit": limit,
    })
