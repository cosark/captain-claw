"""Telegram inbox — a read-only copy of a user's Telegram chats that their
agents read, the way they read mail.

Both ways in go through the deck's shared bot, which only Flight Deck polls
(telegram_shared.route_update hands every update here first):

* **Private chats** — the user connects the bot to their own account in
  Telegram → Settings → Chat Automation (Telegram 12.7+; with Premium also
  Settings → Telegram Business → Chatbots) — a Bot API "business connection",
  no Premium needed since Bot API 10.0 — and picks which chats it covers. The
  bot needs Secretary Mode on in BotFather (getMe ``can_connect_to_business``). Telegram
  then hands the bot both sides of those chats, their edits and deletions.
  It never gets older history: the inbox starts when the connection does.
* **Groups** — the user adds the bot to a group. It sees every message once
  its privacy mode is off in BotFather (or once it's a group admin); with
  privacy mode on it sees only commands and replies to it.

Nothing is ever sent on the user's behalf: no replies in these chats, nothing
marked read, whatever rights the user grants the connection. The bot's only
words are Flight Deck's own notices — to the user in their private chat with
the bot, and one line in a group when it joins or leaves, so the members know
it reads along.

Who: a Flight Deck user whose Telegram link to the shared bot is approved and
bound (telegram_shared), who turned the inbox on with explicit consent
(Connections → Telegram → Inbox), on a deck whose admin allows it. Turning it
off — or disconnecting Telegram — deletes everything kept.

What's kept: per user, in Flight Deck's database, text and names sealed with
fd_crypto, for ``retention_days`` (default 30); deleted messages lose their
text. Agents read it through ``/fd/telegram-inbox/agent/*`` (owner only), and
on each of the owner's own turns get one line of what's new (:func:`news`).
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

from captain_claw.flight_deck import fd_crypto
from captain_claw.flight_deck import telegram_shared as ts
from captain_claw.logging import get_logger

log = get_logger(__name__)

RETENTION_CHOICES = (7, 30, 90, 365)
DEFAULT_RETENTION = 30
# A search reads (and opens) at most this many of the newest messages.
_SEARCH_SCAN = 20000
_PURGE_EVERY = 3600.0
_UNKNOWN_RETRY = 600.0
_GROUP_KINDS = ("group", "supergroup")
_PRESENT = ("member", "administrator", "creator", "restricted")
_GONE = ("left", "kicked")

_last_purge = 0.0
# Business connections Telegram wouldn't describe (or whose owner isn't ours):
# connection id → when we last asked.
_unknown: dict[str, float] = {}


# ── notices (Flight Deck speaking, never the agent) ─────────────────

_NOT_LINKED = (
    "This bot only reads chats for Flight Deck users who connected Telegram "
    "(Flight Deck → Connections → Telegram). Nothing is kept — you can remove it "
    "in Settings → Chat Automation (or Settings → Telegram Business → Chatbots)."
)
_NOT_AVAILABLE = (
    "The Telegram inbox isn't available on this Flight Deck, so nothing is kept. "
    "You can remove the bot in Settings → Chat Automation (or Settings → Telegram "
    "Business → Chatbots)."
)
_INBOX_OFF = (
    "Connected, but your Telegram inbox is off, so nothing is kept yet. Turn it on in "
    "Flight Deck → Connections → Telegram → Inbox."
)
_CONNECTED = (
    "📥 Connected. Messages in the private chats you picked now go to your Telegram "
    "inbox in Flight Deck, where your agents can read them. The bot never replies and "
    "never marks anything read."
)
_RIGHTS_TIP = (
    "\n\nIt needs no permissions — you can switch them all off where you connected it "
    "(Settings → Chat Automation, or Telegram Business → Chatbots)."
)
_DISCONNECTED = "📭 Disconnected — nothing new from your private chats is kept."
_JOINED = (
    "📥 {name} added this bot so their assistant can read this group. It keeps a "
    "private copy of the messages here for them and won't post. Remove the bot to stop it."
)
_REFUSED = (
    "This bot only joins groups for people who turned on the Telegram inbox in Flight "
    "Deck. Leaving."
)
_LEAVING = "👋 Nobody keeps this group in their inbox any more — leaving."


async def _say(chat_id: Any, text: str) -> None:
    try:
        await ts.send_text(chat_id, text)
    except Exception:
        pass


async def _leave(chat_id: str) -> None:
    token = ts.token()
    if token:
        try:
            await ts.call(token, "leaveChat", {"chat_id": int(chat_id)})
        except Exception:
            pass


# ── who ─────────────────────────────────────────────────────────────


def allowed() -> bool:
    """The deck offers the inbox: a shared bot is running and the admin allows it."""
    return bool(ts.token()) and ts.inbox_allowed()


def _linked_user(tg_user_id: Any) -> str:
    """The Flight Deck user an approved link binds this Telegram account to."""
    link = ts._state.links_by_tg.get(str(tg_user_id or ""))
    if link and link.get("status") == "approved":
        return str(link["user_id"])
    return ""


def _tg_for_user(user_id: str) -> str:
    link = ts._state.links_by_user.get(user_id)
    if link and link.get("status") == "approved":
        return str(link.get("tg_user_id") or "")
    return ""


async def collecting(db, user_id: str) -> bool:
    """Whether new messages for ``user_id`` are kept right now."""
    if not user_id or not allowed() or not fd_crypto.available():
        return False
    settings = await db.get_tg_inbox_settings(user_id)
    return bool(settings and settings.get("enabled")) and bool(_tg_for_user(user_id))


# ── message → stored row ────────────────────────────────────────────


def _person(obj: Any) -> str:
    """A user's or chat's display name: "First Last (@username)" / a title."""
    if not isinstance(obj, dict):
        return ""
    name = " ".join(str(p) for p in (obj.get("first_name"), obj.get("last_name")) if p).strip()
    if not name and obj.get("title"):
        name = str(obj["title"])
    if obj.get("username"):
        name = f"{name} (@{obj['username']})" if name else f"@{obj['username']}"
    return name or str(obj.get("id") or "")


def _media(msg: dict) -> str:
    """What a message carries besides text, in a few words ("" if nothing)."""
    if msg.get("photo"):
        return "photo"
    if isinstance(msg.get("voice"), dict):
        return f"voice message ({int(msg['voice'].get('duration') or 0)}s)"
    if msg.get("video_note"):
        return "video message"
    if msg.get("animation"):
        return "GIF"
    if msg.get("video"):
        return "video"
    if isinstance(msg.get("audio"), dict):
        a = msg["audio"]
        what = " – ".join(str(p) for p in (a.get("performer"), a.get("title")) if p)
        return f"audio: {what}" if what else "audio"
    if isinstance(msg.get("document"), dict):
        return f"file: {msg['document'].get('file_name') or 'document'}"
    if isinstance(msg.get("sticker"), dict):
        return f"sticker {msg['sticker'].get('emoji') or ''}".strip()
    if isinstance(msg.get("contact"), dict):
        c = msg["contact"]
        return f"contact: {_person(c)} {c.get('phone_number') or ''}".strip()
    if isinstance(msg.get("venue"), dict):
        v = msg["venue"]
        return f"place: {v.get('title') or ''}, {v.get('address') or ''}".strip(" ,")
    if isinstance(msg.get("location"), dict):
        loc = msg["location"]
        try:
            return f"location {float(loc['latitude']):.5f},{float(loc['longitude']):.5f}"
        except (KeyError, TypeError, ValueError):
            return "location"
    if isinstance(msg.get("poll"), dict):
        return f"poll: {msg['poll'].get('question') or ''}".strip()
    if isinstance(msg.get("checklist"), dict):
        return f"checklist: {msg['checklist'].get('title') or ''}".strip()
    if isinstance(msg.get("dice"), dict):
        return f"dice {msg['dice'].get('emoji') or ''} {msg['dice'].get('value') or ''}".strip()
    return ""


def _forwarded(msg: dict) -> str:
    origin = msg.get("forward_origin")
    if not isinstance(origin, dict):
        return ""
    if isinstance(origin.get("sender_user"), dict):
        return _person(origin["sender_user"])
    if origin.get("sender_user_name"):
        return str(origin["sender_user_name"])
    for key in ("sender_chat", "chat"):
        if isinstance(origin.get(key), dict):
            return _person(origin[key])
    return "someone"


async def _store(
    db, user_id: str, chat_id: str, kind: str, title: str, msg: dict, *,
    outgoing: bool, edited: bool,
) -> bool:
    text = str(msg.get("text") or msg.get("caption") or "")
    media = _media(msg)
    if not text and not media:
        return False  # service messages: joins, pins, title changes …
    try:
        message_id = int(msg.get("message_id") or 0)
    except (TypeError, ValueError):
        return False
    if not message_id:
        return False
    sender = msg.get("from") if isinstance(msg.get("from"), dict) else msg.get("sender_chat")
    body = fd_crypto.seal({
        "t": text, "s": "" if outgoing else _person(sender), "m": media, "f": _forwarded(msg),
    })
    sent_at = int(msg.get("date") or time.time())
    if edited and await db.edit_tg_inbox_message(
            user_id, chat_id, message_id, body=body,
            edited_at=int(msg.get("edit_date") or time.time())):
        return True
    # (An edit of a message we never saw is kept as a new one.)
    reply = msg.get("reply_to_message")
    reply_to = None
    if isinstance(reply, dict):
        try:
            reply_to = int(reply.get("message_id") or 0) or None
        except (TypeError, ValueError):
            reply_to = None
    sender_id = str(sender.get("id") or "") if isinstance(sender, dict) else ""
    await db.add_tg_inbox_message(
        user_id, chat_id, message_id, sender_id=sender_id, outgoing=outgoing,
        sent_at=sent_at, body=body, reply_to=reply_to,
    )
    await db.upsert_tg_inbox_chat(user_id, chat_id, kind, fd_crypto.seal(title), last_at=sent_at)
    return True


# ── updates ─────────────────────────────────────────────────────────


async def handle(db, update: dict[str, Any]) -> str | None:
    """Take the inbox's part of a shared-bot update. Returns what happened, or
    ``None`` when the update isn't the inbox's (route_update carries on)."""
    if "business_connection" in update:
        return await _on_connection(db, update["business_connection"])
    if "business_message" in update:
        return await _on_business_message(db, update["business_message"], edited=False)
    if "edited_business_message" in update:
        return await _on_business_message(db, update["edited_business_message"], edited=True)
    if "deleted_business_messages" in update:
        return await _on_business_deleted(db, update["deleted_business_messages"])
    if isinstance(update.get("my_chat_member"), dict):
        return await _on_bot_membership(db, update["my_chat_member"])
    for key, edited in (("message", False), ("edited_message", True)):
        msg = update.get(key)
        if isinstance(msg, dict):
            chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
            if chat.get("type") in _GROUP_KINDS:
                return await _on_group_message(db, msg, edited=edited)
            return None
    return None


def _rights(conn: dict) -> list[str]:
    """The rights the user granted the connection (we use none of them)."""
    rights = conn.get("rights")
    out = [k for k, v in rights.items() if v] if isinstance(rights, dict) else []
    if conn.get("can_reply") and "can_reply" not in out:
        out.append("can_reply")
    return sorted(out)


async def _on_connection(db, conn: Any, *, quiet: bool = False) -> str:
    """The user connected / changed / disconnected the bot in Chat Automation."""
    if not isinstance(conn, dict):
        return "ignored"
    cid = str(conn.get("id") or "")
    owner = conn.get("user") if isinstance(conn.get("user"), dict) else {}
    tg = str(owner.get("id") or "")
    if not cid or not tg:
        return "ignored"
    chat_id = conn.get("user_chat_id") or tg
    enabled = bool(conn.get("is_enabled", True))
    prev = await db.get_tg_inbox_connection(cid)
    user_id = _linked_user(tg)
    if not user_id or not allowed():
        if prev:
            await db.upsert_tg_inbox_connection(
                cid, prev["user_id"], tg, enabled=False, rights=prev.get("rights") or "[]")
        _unknown[cid] = time.monotonic()
        if enabled and not quiet:
            await _say(chat_id, _NOT_AVAILABLE if user_id else _NOT_LINKED)
        return "inbox:connection-refused"
    rights = _rights(conn)
    await db.upsert_tg_inbox_connection(cid, user_id, tg, enabled=enabled, rights=json.dumps(rights))
    _unknown.pop(cid, None)
    if quiet:
        return "inbox:connection-known"
    if not enabled:
        if prev and prev.get("enabled"):
            await _say(chat_id, _DISCONNECTED)
        return "inbox:connection-off"
    if not await collecting(db, user_id):
        await _say(chat_id, _INBOX_OFF)
        return "inbox:connection-inbox-off"
    if not prev or not prev.get("enabled"):
        await _say(chat_id, _CONNECTED + (_RIGHTS_TIP if rights else ""))
    return "inbox:connected"


async def _connection(db, cid: str) -> dict | None:
    """Our record of a business connection — asking Telegram for one we never
    saw connect (made while Flight Deck was down, or before this feature)."""
    row = await db.get_tg_inbox_connection(cid)
    if row:
        return row
    asked = _unknown.get(cid)
    token = ts.token()
    if not token or (asked is not None and time.monotonic() - asked < _UNKNOWN_RETRY):
        return None
    _unknown[cid] = time.monotonic()
    data = await ts.call(token, "getBusinessConnection", {"business_connection_id": cid})
    if data.get("ok") and isinstance(data.get("result"), dict):
        await _on_connection(db, data["result"], quiet=True)
        return await db.get_tg_inbox_connection(cid)
    return None


async def _owner_collecting(db, cid: str) -> tuple[dict, str] | None:
    """``(connection, user_id)`` when messages on ``cid`` are kept now."""
    conn = await _connection(db, cid) if cid else None
    if not conn or not conn.get("enabled"):
        return None
    user_id = str(conn["user_id"])
    if _linked_user(conn["tg_user_id"]) != user_id or not await collecting(db, user_id):
        return None
    return conn, user_id


async def _on_business_message(db, msg: Any, *, edited: bool) -> str:
    if not isinstance(msg, dict) or not allowed():
        return "inbox:off"
    found = await _owner_collecting(db, str(msg.get("business_connection_id") or ""))
    if found is None:
        return "inbox:off"
    conn, user_id = found
    chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "")
    if not chat_id or chat.get("type") not in (None, "private"):
        return "ignored"
    sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    outgoing = str(sender.get("id") or "") == str(conn["tg_user_id"])
    stored = await _store(db, user_id, chat_id, "private", _person(chat), msg,
                          outgoing=outgoing, edited=edited)
    return "inbox:stored" if stored else "inbox:skipped"


async def _on_business_deleted(db, upd: Any) -> str:
    if not isinstance(upd, dict) or not allowed():
        return "inbox:off"
    found = await _owner_collecting(db, str(upd.get("business_connection_id") or ""))
    chat = upd.get("chat") if isinstance(upd.get("chat"), dict) else {}
    ids = upd.get("message_ids") if isinstance(upd.get("message_ids"), list) else []
    if found is None or not chat.get("id"):
        return "inbox:off"
    _conn, user_id = found
    chat_id = str(chat["id"])
    for mid in ids:
        try:
            row = await db.get_tg_inbox_message(user_id, chat_id, int(mid))
        except (TypeError, ValueError):
            continue
        if row is None:
            continue
        # The text and attachment go; who sent it stays ("Ana deleted a message").
        old = fd_crypto.unseal(row.get("body") or "")
        sender = old.get("s", "") if isinstance(old, dict) else ""
        await db.mark_tg_inbox_deleted(user_id, chat_id, int(mid), body=fd_crypto.seal({"s": sender}))
    return "inbox:deleted"


async def _subscribe(db, user_id: str, chat: dict) -> None:
    await db.upsert_tg_inbox_chat(
        user_id, str(chat.get("id")), "group", fd_crypto.seal(_person(chat)), active=True)


async def _leave_if_unused(db, chat_id: str) -> bool:
    """Leave a group nobody collects any more. True if the bot left."""
    if await db.tg_inbox_group_subscribers(chat_id):
        return False
    await _say(chat_id, _LEAVING)
    await _leave(chat_id)
    await db.set_tg_inbox_group_active(chat_id, False)
    return True


async def _on_bot_membership(db, upd: dict) -> str | None:
    """The bot was added to or removed from a chat."""
    chat = upd.get("chat") if isinstance(upd.get("chat"), dict) else {}
    if chat.get("type") not in _GROUP_KINDS:
        return None
    if not allowed():
        return "ignored:group"
    chat_id = str(chat.get("id") or "")
    new = str((upd.get("new_chat_member") or {}).get("status") or "")
    old = str((upd.get("old_chat_member") or {}).get("status") or "")
    if new in _GONE:
        await db.set_tg_inbox_group_active(chat_id, False)
        return "inbox:group-left"
    if new in _PRESENT and (old in _GONE or not old):
        actor = upd.get("from") if isinstance(upd.get("from"), dict) else {}
        user_id = _linked_user(actor.get("id"))
        if user_id and await collecting(db, user_id):
            await _subscribe(db, user_id, chat)
            await _say(chat_id, _JOINED.format(name=actor.get("first_name") or "Someone"))
            return "inbox:group-joined"
        await _say(chat_id, _REFUSED)
        await _leave(chat_id)
        return "inbox:group-refused"
    return "ignored:group"


_COMMAND = re.compile(r"^/inbox(?:@(\w+))?(?:\s+(\w+))?\s*$", re.I)


def _inbox_command(text: str) -> str | None:
    """``/inbox`` → "on", ``/inbox off`` → "off" (None: not ours)."""
    m = _COMMAND.match(text or "")
    if not m:
        return None
    bot = ts.bot_username()
    if m.group(1) and bot and m.group(1).lower() != bot.lower():
        return None
    arg = (m.group(2) or "").lower()
    return "off" if arg in ("off", "stop") else "on"


async def _on_group_message(db, msg: dict, *, edited: bool) -> str:
    if not allowed():
        return "ignored:group"
    chat = msg["chat"]
    chat_id = str(chat.get("id") or "")
    if msg.get("migrate_to_chat_id"):
        await db.move_tg_inbox_chat(chat_id, str(msg["migrate_to_chat_id"]))
        return "inbox:group-moved"
    sender = msg.get("from") if isinstance(msg.get("from"), dict) else {}
    cmd = None if edited else _inbox_command(str(msg.get("text") or "").strip())
    if cmd is not None:
        return await _group_command(db, chat, sender, cmd)
    stored = 0
    for user_id in await db.tg_inbox_group_subscribers(chat_id):
        if not await collecting(db, user_id):
            continue
        outgoing = str(sender.get("id") or "") == _tg_for_user(user_id)
        if await _store(db, user_id, chat_id, "group", _person(chat), msg,
                        outgoing=outgoing, edited=edited):
            stored += 1
    return "inbox:group" if stored else "ignored:group"


async def _group_command(db, chat: dict, sender: dict, cmd: str) -> str:
    """``/inbox`` (on|off) in a group, from a linked user: start or stop keeping
    it. Answered in their private chat with the bot, never in the group."""
    tg = str(sender.get("id") or "")
    user_id = _linked_user(tg)
    if not user_id:
        return "ignored:group"
    chat_id = str(chat.get("id") or "")
    title = _person(chat) or "This group"
    if cmd == "off":
        await db.set_tg_inbox_group_active(chat_id, False, user_id=user_id)
        await _say(tg, f"📭 {title} no longer goes to your Telegram inbox.")
        await _leave_if_unused(db, chat_id)
        return "inbox:group-off"
    if not await collecting(db, user_id):
        await _say(tg, "Your Telegram inbox is off — turn it on in Flight Deck → Connections → "
                       "Telegram → Inbox, then send /inbox in the group again.")
        return "ignored:group"
    await _subscribe(db, user_id, chat)
    await _say(tg, f"📥 {title} now goes to your Telegram inbox.")
    return "inbox:group-on"


# ── the user's switches ─────────────────────────────────────────────


async def set_enabled(db, user_id: str, *, enabled: bool, retention_days: int | None = None) -> None:
    """Turn the inbox on (with consent recorded now) or off (deleting it all)."""
    current = await db.get_tg_inbox_settings(user_id) or {}
    days = int(retention_days or current.get("retention_days") or DEFAULT_RETENTION)
    if enabled:
        consented = current.get("consented_at") if current.get("enabled") else None
        await db.set_tg_inbox_settings(
            user_id, enabled=True, consented_at=consented or datetime.now(UTC).isoformat(),
            retention_days=days)
        await purge(db)
        return
    await forget(db, user_id, keep_settings_days=days)


async def forget(db, user_id: str, *, keep_settings_days: int | None = None) -> None:
    """Inbox off and everything kept for ``user_id`` deleted; groups nobody
    else collects are left."""
    groups = [c["chat_id"] for c in await db.list_tg_inbox_chats(user_id)
              if c.get("kind") == "group" and c.get("active")]
    await db.set_tg_inbox_settings(
        user_id, enabled=False, consented_at=None,
        retention_days=int(keep_settings_days or DEFAULT_RETENTION))
    await db.delete_tg_inbox_data(user_id, connections=True)
    for chat_id in groups:
        await _leave_if_unused(db, chat_id)


async def remove_group(db, user_id: str, chat_id: str) -> bool:
    chat = await db.get_tg_inbox_chat(user_id, chat_id)
    if not chat or chat.get("kind") != "group":
        return False
    await db.delete_tg_inbox_data(user_id, chat_id=chat_id)
    await _leave_if_unused(db, chat_id)
    return True


async def purge(db) -> int:
    global _last_purge
    _last_purge = time.monotonic()
    return await db.purge_tg_inbox_expired(int(time.time()))


async def maybe_purge(db) -> None:
    """Retention, at most hourly (the poller calls this)."""
    if time.monotonic() - _last_purge >= _PURGE_EVERY:
        try:
            await purge(db)
        except Exception as exc:
            log.warning("Telegram inbox purge failed", error=type(exc).__name__)


# ── reading ─────────────────────────────────────────────────────────


def _iso(ts_: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts_), UTC).isoformat() if ts_ else None
    except (TypeError, ValueError, OSError):
        return None


def _title(sealed: str) -> str:
    value = fd_crypto.unseal(sealed) if sealed else None
    return str(value) if isinstance(value, str) and value else "(unreadable)"


async def chats(db, user_id: str) -> list[dict[str, Any]]:
    out = []
    for row in await db.list_tg_inbox_chats(user_id):
        out.append({
            "chat_id": row["chat_id"],
            "kind": "group" if row["kind"] == "group" else "private",
            "title": _title(row.get("title") or ""),
            "active": bool(row.get("active")),
            "messages": int(row.get("messages") or 0),
            "last_at": _iso(row.get("last_at")),
        })
    return out


class ChatNotFound(LookupError):
    def __init__(self, message: str, candidates: list[dict] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


def find_chat(all_chats: list[dict], ref: str) -> dict:
    """A chat by id, exact title, or a unique part of its title."""
    ref = str(ref or "").strip()
    if not ref:
        raise ChatNotFound("name a chat")
    for c in all_chats:
        if c["chat_id"] == ref:
            return c
    low = ref.casefold().lstrip("@")
    exact = [c for c in all_chats if c["title"].casefold() == low]
    if len(exact) == 1:
        return exact[0]
    part = exact or [c for c in all_chats if low in c["title"].casefold()]
    if len(part) == 1:
        return part[0]
    if part:
        raise ChatNotFound(f"several chats match {ref!r} — pick one by its id", part[:10])
    raise ChatNotFound(f"no chat matches {ref!r}")


def _view(row: dict, chat: dict | None) -> dict[str, Any]:
    body = fd_crypto.unseal(row.get("body") or "") if row.get("body") else None
    body = body if isinstance(body, dict) else {}
    deleted = bool(row.get("deleted"))
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "chat": (chat or {}).get("title") or row["chat_id"],
        "kind": (chat or {}).get("kind") or "",
        "message_id": row["message_id"],
        "at": _iso(row.get("sent_at")),
        "ts": int(row.get("sent_at") or 0),
        "outgoing": bool(row.get("outgoing")),
        "sender": "you" if row.get("outgoing") else str(body.get("s") or ""),
        "text": "" if deleted else str(body.get("t") or ""),
        "media": "" if deleted else str(body.get("m") or ""),
        "forwarded_from": str(body.get("f") or ""),
        "reply_to": row.get("reply_to"),
        "edited": bool(row.get("edited_at")),
        "deleted": deleted,
        "unreadable": bool(row.get("body")) and not body,
    }


async def messages(
    db, user_id: str, *, chat_id: str | None = None, since: int | None = None,
    until: int | None = None, query: str = "", limit: int = 50,
) -> list[dict[str, Any]]:
    """Messages, newest first; ``query`` keeps those whose text, sender, chat
    or attachment contains every word."""
    limit = max(1, min(int(limit or 50), 500))
    by_id = {c["chat_id"]: c for c in await chats(db, user_id)}
    terms = [t for t in str(query or "").casefold().split() if t]
    rows = await db.list_tg_inbox_messages(
        user_id, chat_id=chat_id, since=since, until=until,
        limit=_SEARCH_SCAN if terms else limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _view(row, by_id.get(row["chat_id"]))
        if terms:
            hay = " ".join((item["text"], item["sender"], item["chat"], item["media"],
                            item["forwarded_from"])).casefold()
            if not all(t in hay for t in terms):
                continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


# ── what's new, per agent ───────────────────────────────────────────

# An agent's first look covers this much (not the whole kept history).
FIRST_LOOK_SECONDS = 86400


async def news(db, user_id: str, agent_slug: str) -> dict[str, Any]:
    """What others wrote since this agent last looked, per chat (counts and
    chat names, never text), newest activity first — and the look itself: the
    agent's cursor moves to the newest message kept. The first look covers the
    last day."""
    cursors = await db.tg_inbox_cursors(user_id, agent_slug)
    first = "" not in cursors
    top = await db.tg_inbox_max_id(user_id)
    rows = await db.tg_inbox_unseen(
        user_id, after_id=cursors.get("", 0),
        since=int(time.time()) - FIRST_LOOK_SECONDS if first else None)
    counts: dict[str, int] = {}
    for row in rows:
        chat_id = str(row["chat_id"])
        if int(row["id"]) > cursors.get(chat_id, 0):  # not read further already
            counts[chat_id] = counts.get(chat_id, 0) + 1
    await db.set_tg_inbox_seen_all(user_id, agent_slug, top)
    out = []
    if counts:
        for c in await chats(db, user_id):
            if c["chat_id"] in counts:
                out.append({"chat_id": c["chat_id"], "title": c["title"], "kind": c["kind"],
                            "new": counts[c["chat_id"]]})
    return {"chats": out, "total": sum(counts.values()), "first_look": first}


async def mark_read(db, user_id: str, agent_slug: str, items: list[dict]) -> None:
    """The agent read these: each chat's cursor moves to the newest of them."""
    newest: dict[str, int] = {}
    for item in items:
        chat_id, row_id = str(item.get("chat_id") or ""), int(item.get("id") or 0)
        if chat_id and row_id > newest.get(chat_id, 0):
            newest[chat_id] = row_id
    for chat_id, row_id in newest.items():
        await db.advance_tg_inbox_cursor(user_id, agent_slug, chat_id, row_id)
