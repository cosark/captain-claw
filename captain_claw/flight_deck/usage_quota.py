"""Daily plan allowances: human messages and LLM tokens per user per UTC day.

Each plan tier (``rate_limiter.PLAN_LIMITS``) carries ``messages_per_day`` and
``tokens_per_day`` (0 = unlimited; per-user overrides in the user's metadata,
like the other limits). What a user spent today lives in ``daily_usage``.

* **Messages** are counted where Flight Deck relays a human turn to an agent —
  the browser chat proxy, a shared agent's member socket, the device bridge,
  WhatsApp/Messenger and the deck's Telegram bot — each through
  :func:`admit_message`. Over the limit, the turn is refused with
  :func:`limit_text`, which the channel shows or sends back to the person.
* **Tokens** are reported by the agents themselves (``captain_claw/
  fd_usage_report.py`` → ``POST /fd/quota/agent/usage``): only the agent sees
  every token it spends. A shared agent's member turn is billed to the member,
  everything else to the agent's owner. Over the token limit, new messages are
  refused and scheduled tasks skip their run (:func:`automation_blocked`). A
  turn already running when a limit is reached finishes.

Admins, the standalone app and decks without sign-in are never held to these
limits (their usage is still counted, so an admin can see it).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from captain_claw.flight_deck.admin_routes import require_admin
from captain_claw.flight_deck.auth import get_current_user, get_db
from captain_claw.flight_deck.rate_limiter import (
    STANDALONE_MODE, get_user_limits, get_user_plan,
)
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/fd", tags=["quota"])

AGENT_USAGE_PATH = "/fd/quota/agent/usage"

# Share of an allowance after which the person is warned it is running out.
WARN_AT = 0.8

# Commands that never reach a model: not counted, and they still work when the
# day's allowance is spent. "/new" and "/clear" only without trailing text —
# "/new <question>" starts the new chat with that question.
_FREE_COMMANDS = frozenset({
    "/help", "/history", "/session", "/sessions", "/stop", "/exit", "/models",
    "/config", "/slash_commands", "/todo", "/contacts", "/skills", "/scripts",
    "/apis", "/monitor", "/approve",
})
_FREE_BARE_COMMANDS = frozenset({"/new", "/clear"})

# One report never moves the counter by more than this (a garbled or hostile
# report can't spend someone's whole month).
_MAX_TOKENS_PER_REPORT = 5_000_000


def today() -> str:
    """The current UTC day, ``YYYY-MM-DD`` — the key allowances are counted under."""
    return datetime.now(UTC).date().isoformat()


def resets_at(now: datetime | None = None) -> datetime:
    """When today's allowances start over: the next UTC midnight."""
    now = now or datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)


def _auth_enabled() -> bool:
    return os.environ.get("FD_AUTH_ENABLED", "true").lower() in ("true", "1", "yes")


def exempt(user: dict | None) -> bool:
    """Is *user* free of the daily allowances? Admins, the standalone app and a
    deck without sign-in are."""
    if STANDALONE_MODE or not _auth_enabled():
        return True
    return not user or str(user.get("role") or "user") == "admin"


def plan_label(plan: str) -> str:
    return str(plan or "free").replace("_", " ").title()


def is_free_command(content: Any) -> bool:
    """A chat frame's *content* is a command that never reaches a model."""
    text = str(content or "").strip()
    if not text.startswith("/"):
        return False
    head, _, rest = text.partition(" ")
    head = head.lower()
    if head in _FREE_BARE_COMMANDS:
        return not rest.strip()
    return head in _FREE_COMMANDS


def _allowance(used: int, limit: int) -> dict:
    return {
        "used": used,
        "limit": limit,  # 0 = unlimited
        "remaining": max(0, limit - used) if limit else None,
    }


async def _user(db, user_or_id: dict | str) -> dict | None:
    if isinstance(user_or_id, dict):
        return user_or_id
    try:
        return await db.get_user_by_id(str(user_or_id))
    except Exception:
        return None


async def status(db, user_or_id: dict | str) -> dict:
    """The person's allowances today: plan, used / limit for messages and
    tokens, when they reset, and whether they are spent (``blocked``:
    ``"messages"`` / ``"tokens"`` / None) or running out (``warning``)."""
    user = await _user(db, user_or_id)
    uid = str(user["id"]) if user else str(user_or_id)
    plan = get_user_plan(user) if user else "free"
    limits = get_user_limits(user) if user else {}
    day = today()
    used = await db.get_daily_usage(uid, day)
    msg_limit = max(0, int(limits.get("messages_per_day") or 0))
    tok_limit = max(0, int(limits.get("tokens_per_day") or 0))
    free = exempt(user)
    blocked = None
    warning = None
    if not free:
        if msg_limit and used["messages"] >= msg_limit:
            blocked = "messages"
        elif tok_limit and used["tokens"] >= tok_limit:
            blocked = "tokens"
        elif msg_limit and used["messages"] >= WARN_AT * msg_limit:
            warning = "messages"
        elif tok_limit and used["tokens"] >= WARN_AT * tok_limit:
            warning = "tokens"
    return {
        "plan": plan,
        "plan_label": plan_label(plan),
        "day": day,
        "resets_at": resets_at().isoformat(),
        "exempt": free,
        "messages": _allowance(used["messages"], msg_limit),
        "tokens": _allowance(used["tokens"], tok_limit),
        "blocked": blocked,
        "warning": warning,
    }


def _until(iso: str) -> str:
    try:
        left = datetime.fromisoformat(iso) - datetime.now(UTC)
    except (TypeError, ValueError):
        return ""
    minutes = max(1, int(left.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _messages(n: int) -> str:
    return f"{n} message{'' if n == 1 else 's'}"


def limit_text(st: dict) -> str:
    """What the person is told when a turn is refused: which allowance is
    spent, on which plan, and when it comes back."""
    plan = st.get("plan_label") or "your"
    if st.get("blocked") == "tokens":
        what = f"today's {_fmt_tokens(st['tokens']['limit'])} AI tokens"
    else:
        what = f"today's {_messages(st['messages']['limit'])}"
    until = _until(st.get("resets_at") or "")
    when = f" It resets at midnight UTC, in {until}." if until else " It resets at midnight UTC."
    return (f"You've used {what} on the {plan} plan.{when}"
            " Ask your admin if you need a bigger plan.")


def short_limit_text(st: dict) -> str:
    """:func:`limit_text` for a small screen."""
    if st.get("blocked") == "tokens":
        what = f"{_fmt_tokens(st['tokens']['limit'])} tokens"
    else:
        what = _messages(st['messages']['limit'])
    until = _until(st.get("resets_at") or "")
    return (f"Daily limit reached ({what}, {st.get('plan_label')} plan)."
            + (f" Resets in {until}." if until else ""))


async def admit_message(user_or_id: dict | str, content: Any = "") -> tuple[bool, dict | None]:
    """May this human message go to an agent? Counts it when it may.

    ``(True, status)`` after counting (status None for an uncounted free
    command); ``(False, status)`` when today's allowance is spent — the caller
    refuses the turn with :func:`limit_text`. Never raises: if the counters
    can't be read the message goes through (an outage of this bookkeeping must
    not take chat down)."""
    if is_free_command(content):
        return True, None
    try:
        db = get_db()
        user = await _user(db, user_or_id)
        if user is None:
            return True, None
        st = await status(db, user)
        if st["blocked"]:
            return False, st
        await db.add_daily_usage(str(user["id"]), st["day"], messages=1)
        return True, await status(db, user)
    except Exception as exc:
        log.warning("Daily allowance check failed; letting the message through",
                    error=type(exc).__name__)
        return True, None


async def spent(user_or_id: dict | str) -> dict | None:
    """Today's status when the person's allowance is spent, else None — a
    check before costly work (transcribing a voice note) that counts nothing.
    Never raises."""
    try:
        st = await status(get_db(), user_or_id)
    except Exception:
        return None
    return st if st["blocked"] else None


def owner_of_port(port: int) -> str:
    """The user who owns the agent on *port* ("" when unknown) — whose
    allowance a bridge's turn counts against."""
    try:
        from captain_claw.flight_deck.server import _resolve_agent_owner

        return _resolve_agent_owner(int(port)) or ""
    except Exception:
        return ""


def refusal_frame(st: dict) -> dict:
    """The chat-socket frame that refuses a turn over the allowance."""
    return {"type": "error", "code": "quota_exceeded", "message": limit_text(st), "quota": st}


async def screen_chat_frame(user_id: str, text: str) -> tuple[bool, dict | None]:
    """A raw frame a person's chat socket sends their agent: ``(forward it?,
    a frame to send back to them or None)``. Only a human ``chat`` frame is
    counted; a refused one gets :func:`refusal_frame`, a counted one an
    ``fd_quota`` frame with the new totals (the chat's allowance meter)."""
    if not user_id:
        return True, None
    try:
        frame = json.loads(text)
    except (TypeError, ValueError):
        return True, None
    if not isinstance(frame, dict) or frame.get("type") != "chat" or frame.get("automation"):
        return True, None
    allowed, st = await admit_message(user_id, frame.get("content"))
    if not allowed:
        return False, refusal_frame(st)
    return True, ({"type": "fd_quota", "quota": st} if st is not None else None)


async def automation_blocked(owner_id: str) -> dict | None:
    """The status when *owner_id*'s automated runs (scheduled tasks) should
    skip today — their token allowance is spent — else None. Never raises."""
    try:
        st = await status(get_db(), owner_id)
    except Exception:
        return None
    return st if st["blocked"] == "tokens" else None


async def record_tokens(db, user_id: str, tokens: int) -> None:
    tokens = max(0, min(int(tokens or 0), _MAX_TOKENS_PER_REPORT))
    if not user_id or not tokens:
        return
    await db.add_daily_usage(str(user_id), today(), tokens=tokens)


# ── Routes ───────────────────────────────────────────────────────────


@router.get("/quota")
async def my_quota(user: dict = Depends(get_current_user)) -> dict:
    """The caller's allowances today."""
    db = get_db()
    row = (await db.get_user_by_id(str(user["id"]))) or user
    return await status(db, row)


class AgentUsage(BaseModel):
    tokens: int = Field(ge=0)


async def _reporting_user(request: Request) -> str:
    """Who an agent's usage report bills: the member whose shared-agent turn
    it is (by the turn's grant), else the calling agent's owner."""
    from captain_claw.flight_deck import speaker_grants
    from captain_claw.flight_deck.context_packs import agent_transport_gate
    from captain_claw.flight_deck.deep_memory_routes import _agent_owner

    acting = await speaker_grants.acting_member(request)
    if acting is not None:
        return acting.user_id
    agent_transport_gate(request)  # agents only, never a browser
    return _agent_owner(request)


@router.post("/quota/agent/usage")
async def agent_usage(body: AgentUsage, request: Request) -> dict:
    """An agent reports LLM tokens it just spent."""
    uid = await _reporting_user(request)
    await record_tokens(get_db(), uid, body.tokens)
    return {"ok": True}


@router.get("/admin/usage/today")
async def usage_today(_admin: dict = Depends(require_admin)) -> dict:
    """Every user's messages and tokens today (admin only)."""
    day = today()
    return {"day": day, "resets_at": resets_at().isoformat(),
            "usage": await get_db().list_daily_usage(day)}


@router.delete("/admin/users/{user_id}/usage/today")
async def reset_usage_today(user_id: str, _admin: dict = Depends(require_admin)) -> dict:
    """Give a user today's allowances back (admin only)."""
    db = get_db()
    if not await db.get_user_by_id(user_id):
        raise HTTPException(404, "User not found")
    await db.reset_daily_usage(user_id, today())
    return {"ok": True}
