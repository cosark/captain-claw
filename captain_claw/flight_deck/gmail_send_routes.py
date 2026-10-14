"""Gmail sending through Flight Deck — the one gate, audit trail and notifier.

Under Flight Deck an agent's ``google_mail`` tool never sends Gmail itself: it
asks ``POST /fd/google/gmail/send`` and Flight Deck decides, by the agent
OWNER's per-user policy (user_settings ``google_oauth:gmail_send`` — the
``google_oauth:`` prefix is server-owned, so ``/fd/settings`` can't write it,
and a Google disconnect leaves it in place):

* ``enabled`` — OFF by default: nothing changes until the user opts in
  (Connections → Google → Email sending). On means automatic — there is no
  per-send confirmation;
* ``allowed_recipients`` — empty = anyone; else exact addresses and
  ``@domain`` entries (see :mod:`captain_claw.gmail_compose`);
* ``daily_limit`` — emails per rolling 24 hours (default 50, 1..500).

The same routes carry the user's agentic signature (``signature``; stored
apart, under :data:`mail_signature.GOOGLE_KEY`): plain text Flight Deck adds
to the end of every send it composes here, and the agent's google_mail adds
to every draft (it reads the signature from ``/agent_status``). Sending
doesn't need to be on for it — drafts get it too.

The deck kill switch ``FD_GMAIL_SEND=off`` (also ``0`` / ``false`` / ``no``)
refuses every send regardless. A composed send that repeats an email already in
the user's Drafts or Sent (recipient + subject; ``FD_GMAIL_REPEAT_CHECK_DAYS``,
default 14, 0 = off) is refused unless the agent passes ``allow_repeat``. A send
that passes goes out with the owner's token, lands in ``gmail_sends`` (the
user's history, the daily count and the duplicate check) and rings the owner's
bell. So does a send whose outcome is unknown (Gmail answered 5xx, or never
answered after the request went out) — as ``status = 'unknown'``, so a blind
retry is caught as a duplicate and the user is told to check their Sent folder.

A composed send may carry ``attachments`` (the agent read the files; see
:mod:`captain_claw.mail_attachments`): Flight Deck re-checks them
(``from_wire``: names, types, base64, count, total), builds the email itself,
refuses one over Gmail's 25 MB and uploads it through Gmail's upload endpoint.
The files' names go into the audit row and the bell; their digest into the
duplicate check's content hash.

The gws Workspace CLI tool is retired (``config.RETIRED_TOOLS``: never
registered, stripped from every tools list, refused by the shell tool), so
google_mail → this route is the only tool path to a Gmail send under Flight
Deck.

The user routes — ``GET``/``PUT /fd/google/gmail-send`` and ``GET
/fd/google/gmail-sends`` — are the signed-in user's own policy and history; no
admin involved. Like all of /fd/google, only on an auth-enabled deck
(``_require_auth_deck``).

The send itself is two steps any Flight Deck sender shares: compose and check
(:func:`_compose_checked` — reply threading, recipients against an allowlist,
the signature, the build, the repeat check) and send under the owner's lock
(:func:`_send_locked` — limit, duplicate check, the Gmail call, audit, bell).
The topic auto-reply engine sends through :func:`send_autoreply`: the same
two steps, its recipient bound to the one verified sender, its own ``origin``
on the audit row (so it neither counts toward nor shows in the agents' limit
and history) and no ``email_sent`` bell — the engine rings its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captain_claw import gmail_compose, mail_attachments, mail_signature
from captain_claw.flight_deck import google_oauth_routes as _google
from captain_claw.flight_deck import speaker_grants, tenant_profile
from captain_claw.flight_deck.auth import get_current_user, get_db
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.google_oauth_routes import _require_auth_deck
from captain_claw.logging import get_logger

log = get_logger(__name__)


router = APIRouter(
    prefix="/fd/google",
    tags=["google-gmail-send"],
    dependencies=[Depends(_require_auth_deck)],
)


_K_GMAIL_SEND = "google_oauth:gmail_send"  # per-user, server-owned

DEFAULT_DAILY_LIMIT = 50
MAX_DAILY_LIMIT = 500
MAX_ALLOWED_RECIPIENTS = 200

_DAY = timedelta(hours=24)
_DUPLICATE_WINDOW = timedelta(minutes=10)
# The repeat check (gmail_compose.find_repeats) on a composed send: the same
# email — recipient + subject — already in Drafts (any age) or in Sent this many
# days back. FD_GMAIL_REPEAT_CHECK_DAYS overrides it on a deck; 0 turns it off.
DEFAULT_REPEAT_CHECK_DAYS = 14

_ENABLE_WHERE = "Flight Deck → Connections → Google → Email sending"
_RECONNECT = "Flight Deck → Connections → Google"

# The message fields of a composed send (the other shape is {"draft_id"}).
_MESSAGE_FIELDS = ("to", "cc", "bcc", "subject", "body", "html_body", "reply_to_message_id")

# Gmail message / draft ids go into the URL path — never let one walk it.
_GMAIL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_OFF_DETAIL = (
    "Email sending is turned off for this user, so nothing was sent. Create a "
    "draft with google_mail action=create_draft instead, and tell the user they "
    f"can turn sending on in {_ENABLE_WHERE}."
)
_DECK_OFF_DETAIL = (
    "Email sending is disabled on this Flight Deck by its administrator "
    "(FD_GMAIL_SEND), so nothing was sent. Create a draft with google_mail "
    "action=create_draft instead."
)

# Every topic auto-reply carries these (RFC 3834 §5): other auto-responders
# (out-of-office, ticket systems, Exchange) don't answer it, so two automated
# mailboxes can't ping-pong.
AUTOREPLY_HEADERS: Mapping[str, str] = MappingProxyType({
    "Auto-Submitted": "auto-replied",
    "X-Auto-Response-Suppress": "All",
})
# The ``gmail_sends.origin`` of a send_autoreply: the engine's own, or one the
# owner approved from a held card. Never ``agent`` — an auto-reply must not
# count toward, or show in, the agents' limit and history.
AUTOREPLY_ORIGINS = ("autoreply", "autoreply_approved")
_AUTOREPLY_DECK_OFF_DETAIL = (
    "Email sending is disabled on this Flight Deck by its administrator "
    "(FD_GMAIL_SEND), so the auto-reply was not sent."
)

# Per user: policy read-modify-write, and the count → duplicate check → send →
# audit sequence (so two concurrent sends can't both slip under the limit or
# both pass the duplicate check).
_policy_locks: dict[str, asyncio.Lock] = {}
_send_locks: dict[str, asyncio.Lock] = {}


def _lock(locks: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock


# ── policy ──────────────────────────────────────────────────────────


def deck_send_disabled() -> bool:
    """The deck kill switch: ``FD_GMAIL_SEND`` = off / 0 / false / no."""
    return os.environ.get("FD_GMAIL_SEND", "").strip().lower() in ("off", "0", "false", "no")


def repeat_check_days() -> int:
    """Days of Sent mail a composed send is checked against —
    ``FD_GMAIL_REPEAT_CHECK_DAYS`` (a whole number; 0 = no repeat check), else
    :data:`DEFAULT_REPEAT_CHECK_DAYS`. An unreadable value keeps the default:
    a typo never turns the check off."""
    raw = os.environ.get("FD_GMAIL_REPEAT_CHECK_DAYS", "").strip()
    try:
        days = int(raw) if raw else DEFAULT_REPEAT_CHECK_DAYS
    except ValueError:
        return DEFAULT_REPEAT_CHECK_DAYS
    return days if days >= 0 else DEFAULT_REPEAT_CHECK_DAYS


def _default_policy() -> dict[str, Any]:
    return {"enabled": False, "allowed_recipients": [], "daily_limit": DEFAULT_DAILY_LIMIT}


def _valid_daily_limit(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= MAX_DAILY_LIMIT)


async def load_gmail_send_policy(db: FlightDeckDB, user_id: str) -> dict[str, Any]:
    """*user_id*'s sending policy — the defaults (sending OFF) when none is
    stored or the record is unreadable: a corrupt record never turns sending on
    or widens the allowlist."""
    raw = await db.get_setting(user_id, _K_GMAIL_SEND)
    if not raw:
        return _default_policy()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        enabled = data.get("enabled", False)
        daily_limit = data.get("daily_limit", DEFAULT_DAILY_LIMIT)
        allowed = gmail_compose.normalize_allowlist(data.get("allowed_recipients") or [])
        if (not isinstance(enabled, bool) or not _valid_daily_limit(daily_limit)
                or len(allowed) > MAX_ALLOWED_RECIPIENTS):
            raise ValueError("field out of range")
    except Exception as exc:
        log.warning("Unreadable Gmail send policy for a user; using the defaults (off): %s",
                    type(exc).__name__)
        return _default_policy()
    return {"enabled": enabled, "allowed_recipients": allowed, "daily_limit": daily_limit}


async def _sent_last_24h(db: FlightDeckDB, user_id: str) -> int:
    """The agents' sends in the last 24 hours — what their daily limit counts."""
    since = (datetime.now(timezone.utc) - _DAY).isoformat()
    return await db.count_gmail_sends_since(user_id, since, origin="agent")


async def _policy_view(db: FlightDeckDB, user_id: str, policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(policy["enabled"]),
        "allowed_recipients": list(policy["allowed_recipients"]),
        "daily_limit": int(policy["daily_limit"]),
        "signature": await mail_signature.load(db, user_id, mail_signature.GOOGLE_KEY),
        "deck_disabled": deck_send_disabled(),
        "sent_last_24h": await _sent_last_24h(db, user_id),
    }


class GmailSendPolicyUpdate(BaseModel):
    # Every field optional; absent / null leaves it unchanged. Typed loosely and
    # checked in the handler so any bad value is a 400 with a readable detail.
    enabled: Any = None
    allowed_recipients: Any = None
    daily_limit: Any = None
    signature: Any = None  # "" clears it


# ── user routes: the signed-in user's own policy + history ──────────


@router.get("/gmail-send")
async def gmail_send_policy_get(
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """This user's Email sending policy (+ the deck switch and today's count)."""
    db = get_db()
    uid = _google._effective_owner(_user)
    return await _policy_view(db, uid, await load_gmail_send_policy(db, uid))


@router.put("/gmail-send")
async def gmail_send_policy_put(
    body: GmailSendPolicyUpdate,
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Change this user's Email sending policy (any subset of the fields)."""
    db = get_db()
    uid = _google._effective_owner(_user)
    updates: dict[str, Any] = {}
    if body.enabled is not None:
        if not isinstance(body.enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        updates["enabled"] = body.enabled
    if body.daily_limit is not None:
        if not _valid_daily_limit(body.daily_limit):
            raise HTTPException(
                status_code=400,
                detail=f"daily_limit must be a whole number from 1 to {MAX_DAILY_LIMIT}",
            )
        updates["daily_limit"] = body.daily_limit
    if body.allowed_recipients is not None:
        try:
            allowed = gmail_compose.normalize_allowlist(body.allowed_recipients)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if len(allowed) > MAX_ALLOWED_RECIPIENTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"allowed_recipients can hold at most {MAX_ALLOWED_RECIPIENTS} "
                    f"entries (got {len(allowed)})"
                ),
            )
        updates["allowed_recipients"] = allowed
    stored: dict[str, str] = {}
    if body.signature is not None:
        try:
            stored[mail_signature.GOOGLE_KEY] = mail_signature.normalize(body.signature)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with _lock(_policy_locks, uid):
        policy = await load_gmail_send_policy(db, uid)
        if updates:
            policy.update(updates)
            stored[_K_GMAIL_SEND] = json.dumps(policy, ensure_ascii=True)
        if stored:
            await db.set_settings(uid, stored)
    return await _policy_view(db, uid, policy)


@router.get("/gmail-sends")
async def gmail_sends_list(
    limit: int = 20,
    _user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """This user's emails sent by their agents, newest first."""
    db = get_db()
    uid = _google._effective_owner(_user)
    rows = await db.list_gmail_sends(uid, max(1, min(int(limit), 100)), origin="agent")
    return {"sends": [
        {
            "id": r["id"],
            # "sent", or "unknown": Gmail never confirmed it — it may have gone out.
            "status": r.get("status") or "sent",
            "agent": r["agent"],
            "to": r["to_addrs"],
            "cc": r["cc_addrs"],
            "bcc": r["bcc_addrs"],
            "subject": r["subject"],
            "gmail_message_id": r["gmail_message_id"],
            "thread_id": r["thread_id"],
            "draft_id": r["draft_id"],
            "attachments": _names(r.get("attachments")),
            "created_at": r["created_at"],
        }
        for r in rows
    ]}


def _names(raw: Any) -> list[str]:
    """A ``gmail_sends.attachments`` value as a list of names ([] if unreadable)."""
    try:
        names = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(n) for n in names] if isinstance(names, list) else []


# ── agent route ─────────────────────────────────────────────────────


def _refused(detail: str, reason: str) -> HTTPException:
    """403 for "sending is off" — the header tells the agent's tool to point
    the model at create_draft (other 403s don't carry it)."""
    return HTTPException(
        status_code=403, detail=detail,
        headers={gmail_compose.SEND_REFUSED_HEADER: reason},
    )


async def _read_payload(request: Request) -> dict[str, Any]:
    """The send body: every message field and ``draft_id`` as a string (absent
    = ""), plus ``allow_repeat`` — a boolean (absent = False): the agent's
    user explicitly asked for another copy, so skip the repeat check — and
    ``attachments``: the checked files (absent = none)."""
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="The body must be a JSON object")
    out: dict[str, Any] = {}
    for key in _MESSAGE_FIELDS + ("draft_id",):
        value = data.get(key)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{key} must be a string")
        out[key] = value
    allow_repeat = data.get("allow_repeat")
    if allow_repeat is None:
        allow_repeat = False
    if not isinstance(allow_repeat, bool):
        raise HTTPException(status_code=400, detail="allow_repeat must be true or false")
    out["allow_repeat"] = allow_repeat
    try:
        out["attachments"] = mail_attachments.from_wire(
            data.get("attachments"), max_total_bytes=mail_attachments.GMAIL_MAX_MESSAGE_BYTES,
        )
    except mail_attachments.AttachmentError as exc:
        text = str(exc).rstrip()
        raise HTTPException(
            status_code=400,
            detail=f"{text}{'' if text.endswith('.') else '.'} Nothing was sent.",
        ) from exc
    return out


def _gmail_message(exc: httpx.HTTPStatusError) -> str:
    try:
        message = (exc.response.json().get("error") or {}).get("message") or ""
    except Exception:
        message = ""
    message = str(message or exc.response.text[:300] or f"HTTP {exc.response.status_code}")
    return message.strip().rstrip(".")


_SEND_SCOPES_HINT = "gmail.compose or gmail.send"
_DRAFT_SCOPES_HINT = "gmail.compose (gmail.send alone can't send drafts)"
_REPLY_SCOPES_HINT = "gmail.readonly (to read the email being replied to)"


_CHECK_SENT = (
    "Check the Sent folder (google_mail action=search query='in:sent ...') "
    "before trying again."
)


def _send_outcome_unknown(exc: httpx.HTTPError) -> bool:
    """For a failed send call (messages.send / drafts.send): may Gmail still
    have sent it? Yes on a 5xx, or when the connection broke after the request
    went out — anything but a failure to connect."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def _outcome_unknown(detail: str) -> HTTPException:
    return HTTPException(
        status_code=502, detail=detail,
        headers={gmail_compose.SEND_OUTCOME_HEADER: "unknown"},
    )


def _gmail_error(
    exc: httpx.HTTPStatusError, *, scopes_hint: str = _SEND_SCOPES_HINT,
    attempted_send: bool = False,
) -> HTTPException:
    """Map a Gmail API error to an answer the agent can act on. *scopes_hint*
    names the scope a 403 "insufficient scopes" means is missing.
    *attempted_send* is True only for the send call itself — an error on a
    lookup before it (drafts.get, the reply's messages.get) means not sent."""
    status = exc.response.status_code
    message = _gmail_message(exc)
    if attempted_send and _send_outcome_unknown(exc):
        return _outcome_unknown(
            f"Gmail API error ({status}): {message} — the email may or may not "
            f"have been sent. {_CHECK_SENT}"
        )
    not_sent = " The email was not sent."
    if status == 401:
        return HTTPException(
            status_code=502,
            detail=f"Google authentication expired — reconnect Google in {_RECONNECT}.{not_sent}",
        )
    if status == 403:
        if "insufficient" in message.lower() or "scope" in message.lower():
            return HTTPException(
                status_code=403,
                detail=(
                    "This deck's Google connection is missing a Gmail scope: the deck "
                    f"admin must include {scopes_hint} in {_RECONNECT} → Scopes, then "
                    f"the user reconnects Google.{not_sent}"
                ),
            )
        return HTTPException(status_code=403, detail=f"Gmail refused: {message}.{not_sent}")
    if status == 429:
        return HTTPException(
            status_code=429,
            detail=f"Gmail rate limit exceeded — try again later.{not_sent}",
        )
    return HTTPException(status_code=502, detail=f"Gmail API error ({status}): {message}.{not_sent}")


def _transport_error(exc: httpx.HTTPError, *, attempted_send: bool = False) -> HTTPException:
    """Map a network failure talking to Gmail. Only the send call itself
    (*attempted_send*) can leave the outcome unknown."""
    if attempted_send and _send_outcome_unknown(exc):
        return _outcome_unknown(
            f"Gmail did not answer ({type(exc).__name__}) — the email may or may not "
            f"have been sent. {_CHECK_SENT}"
        )
    return HTTPException(
        status_code=502,
        detail=f"Could not reach Gmail ({type(exc).__name__}). The email was not sent.",
    )


def _check_gmail_id(value: str, name: str) -> None:
    if not _GMAIL_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{name} is not a valid Gmail id: {value[:80]!r}")


async def _draft_headers(client: httpx.AsyncClient, token: str, draft_id: str) -> dict[str, Any]:
    """To / Cc / Bcc / Subject (+ thread, + ``files``: the names and sizes of
    the files it carries) of a draft, before sending it."""
    try:
        resp = await client.get(
            f"{gmail_compose.GMAIL_API}/users/me/drafts/{draft_id}",
            params={"format": "full"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 404):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Draft {draft_id} not found — it may have been sent or deleted; "
                    "google_mail action=list_drafts shows the current Draft IDs. "
                    "Nothing was sent."
                ),
            ) from exc
        raise _gmail_error(exc, scopes_hint=_DRAFT_SCOPES_HINT) from exc
    except httpx.HTTPError as exc:
        raise _transport_error(exc) from exc
    message = resp.json().get("message") or {}
    headers: dict[str, str] = {}
    for h in (message.get("payload") or {}).get("headers", []):
        name = (h.get("name") or "").lower()
        if name in ("to", "cc", "bcc", "subject"):
            headers[name] = h.get("value", "") or ""
    out: dict[str, Any] = dict(headers)
    out["thread_id"] = message.get("threadId", "") or ""
    out["files"] = gmail_compose.payload_files(message.get("payload") or {})
    return out


def _check_recipients(to: str, cc: str, bcc: str, allowlist: list[str]) -> None:
    """400 for unusable / too many recipients, 403 for any off the allowlist."""
    bad = gmail_compose.invalid_recipients(to, cc, bcc)
    if bad:
        raise HTTPException(status_code=400, detail=gmail_compose.invalid_recipients_message(bad))
    addresses = gmail_compose.parse_addresses(to, cc, bcc)
    if not addresses:
        raise HTTPException(
            status_code=400,
            detail="No recipient — set to (or cc / bcc). Nothing was sent.",
        )
    if len(addresses) > gmail_compose.MAX_RECIPIENTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many recipients ({len(addresses)}) — at most "
                f"{gmail_compose.MAX_RECIPIENTS} per email across to/cc/bcc. Nothing was sent."
            ),
        )
    disallowed = gmail_compose.recipients_allowed(addresses, allowlist)
    if disallowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "Not on the user's allowed-recipients list: "
                f"{', '.join(disallowed)}. Nothing was sent. Create a draft with "
                "google_mail action=create_draft instead if the user wants this email; "
                f"they manage the list in {_ENABLE_WHERE}."
            ),
        )


def _agent_label(request: Request) -> str:
    """Best-effort name of the calling agent for the audit row and the bell —
    its Flight Deck slug, else "An agent". (The token is never logged.)"""
    try:
        from captain_claw.flight_deck.server import _find_agent_by_auth

        matched, _owner, slug = _find_agent_by_auth(request.headers.get("X-Agent-Auth", ""))
    except Exception:
        return "An agent"
    return str(slug) if matched and slug else "An agent"


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _audit_send(
    db: FlightDeckDB, owner: str, *, status: str, agent: str, to: str, cc: str,
    bcc: str, subject: str, message_id: str, thread_id: str, draft_id: str,
    content_hash: str, attachments: list[str] | None = None, origin: str = "agent",
    topic_id: str = "", in_reply_to_message_id: str = "",
) -> None:
    """The ``gmail_sends`` row — *status* ``sent``, or ``unknown`` when Gmail
    never confirmed; *attachments* the files' names; *origin* who decided the
    send (an auto-reply also names its topic and the email it answered). A
    failed insert is logged, never raised: the send (or its error) is what the
    agent must hear about."""
    try:
        await db.add_gmail_send(
            owner, agent=agent, to_addrs=to, cc_addrs=cc, bcc_addrs=bcc,
            subject=subject, gmail_message_id=message_id, thread_id=thread_id,
            draft_id=draft_id, content_hash=content_hash, status=status,
            attachments=json.dumps(attachments, ensure_ascii=False) if attachments else "",
            origin=origin, topic_id=topic_id, in_reply_to_message_id=in_reply_to_message_id,
        )
    except Exception as exc:
        log.error("Gmail send audit insert failed (%s, message %s): %s",
                  status, message_id or "-", type(exc).__name__)


async def _notify_send(
    db: FlightDeckDB, owner: str, *, status: str, agent: str, to: str,
    subject: str, message_id: str, attachments: list[str] | None = None,
) -> None:
    """The owner's bell for a send (*status* ``sent`` / ``unknown``)."""
    files = f" - Attached: {', '.join(attachments)}" if attachments else ""
    if status == "unknown":
        title = f"{agent} may have sent an email"
        body = (f"Gmail did not confirm it — check your Sent folder. "
                f"To: {to} - Subject: {subject or '(no subject)'}{files}")
    else:
        title = f"{agent} sent an email"
        body = f"To: {to} - Subject: {subject or '(no subject)'}{files}"
    try:
        await db.add_notification(
            owner, "email_sent", _clip(title, 120), _clip(body, 300),
            "gmail_message" if message_id else "", message_id,
        )
    except Exception as exc:
        log.warning("Gmail send notification failed: %s", type(exc).__name__)


# ── the send core: compose + check, then send under the owner's lock ──


@dataclass(frozen=True)
class _Composed:
    """One email ready to send, every check short of the send done: the
    recipients as they go on the wire, the subject, the signed bodies, the
    built message (``raw`` for the JSON send, or ``message_bytes`` for the
    upload when it carries files; both empty for a draft send — Gmail sends the
    draft as it is), its thread, the Gmail id it answers (``reply_to``) and its
    files' digest (both part of the duplicate check), and the files' names /
    ``{"filename", "size"}``."""

    to: str
    cc: str
    bcc: str
    subject: str
    body: str
    html_body: str
    raw: str
    message_bytes: bytes
    thread_id: str
    reply_to: str
    atts_digest: str
    names: list[str]
    files: list[dict[str, Any]]


async def _compose_checked(
    db: FlightDeckDB, owner: str, client: httpx.AsyncClient, token: str, *,
    to: str, cc: str, bcc: str, subject: str, body: str, html_body: str,
    reply_to: str, atts: list[mail_attachments.Attachment], allowlist: list[str],
    allow_repeat: bool, extra_headers: Mapping[str, str] | None = None,
) -> _Composed:
    """Compose *owner*'s email and check it — everything short of the send.

    A reply (*reply_to*: the Gmail id of the email it answers) threads under
    it and takes its to / subject defaults when those are empty, as
    create_draft does. Then a subject and a body are required; the recipients
    must be usable and on *allowlist* (empty = anyone); the owner's agentic
    signature goes at the end; the email is built (*extra_headers*: see
    :func:`gmail_compose.build_message_bytes`) and must fit Gmail's 25 MB; and
    unless *allow_repeat* it must not repeat an email already drafted or sent.
    Any refusal is an :class:`HTTPException` — nothing sent. Takes no lock."""
    thread_id = in_reply_to = references = original_date = ""
    if reply_to:
        _check_gmail_id(reply_to, "reply_to_message_id")
        try:
            ctx = await gmail_compose.fetch_reply_context(client, token, reply_to)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404):
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"reply_to_message_id {reply_to} was not found in this "
                        "Gmail account. Nothing was sent."
                    ),
                ) from exc
            raise _gmail_error(exc, scopes_hint=_REPLY_SCOPES_HINT) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc
        thread_id = ctx["thread_id"]
        in_reply_to, references = ctx["in_reply_to"], ctx["references"]
        original_date = ctx["internal_date"]
        if not to and ctx["reply_to_default"]:
            to = ctx["reply_to_default"]
        if not subject and ctx["subject_default"]:
            subject = ctx["subject_default"]
    if not subject.strip() or not (body.strip() or html_body.strip()):
        raise HTTPException(
            status_code=400,
            detail="A send needs a subject and a body (body or html_body). Nothing was sent.",
        )
    _check_recipients(to, cc, bcc, allowlist)
    # The sender's agentic signature goes at the end (once — a body
    # that already ends with it is left alone).
    body, html_body = mail_signature.apply(
        body, html_body,
        await mail_signature.load(db, owner, mail_signature.GOOGLE_KEY),
    )
    message_bytes = b""
    try:
        # Put exactly the checked addresses on the wire.
        to, cc, bcc = (gmail_compose.format_recipients(f) for f in (to, cc, bcc))
        if atts:
            raw = ""
            message_bytes = gmail_compose.build_message_bytes(
                to=to, cc=cc, bcc=bcc, subject=subject,
                body=body, html_body=html_body,
                in_reply_to=in_reply_to, references=references, attachments=atts,
                extra_headers=extra_headers,
            )
        else:
            raw = gmail_compose.build_raw_message(
                to=to, cc=cc, bcc=bcc, subject=subject,
                body=body, html_body=html_body,
                in_reply_to=in_reply_to, references=references,
                extra_headers=extra_headers,
            )
    except ValueError as exc:  # e.g. a line break in the subject
        raise HTTPException(
            status_code=400,
            detail=f"Could not build the email: {exc}. Nothing was sent.",
        ) from exc
    if len(message_bytes) > mail_attachments.GMAIL_MAX_MESSAGE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This email would be {mail_attachments.human_size(len(message_bytes))} "
                "once encoded — Gmail takes at most 25 MB. Nothing was sent. Attach "
                "fewer or smaller files, or share big ones as a Drive link."
            ),
        )
    if not allow_repeat:
        # The same email already drafted or sent — from any session, a
        # cron run or the user's own Gmail, not only FD's audit rows.
        # Before the lock: these lookups never hold up the user's other
        # sends. Fails open (a compose-only grant still checks Drafts).
        repeats = await gmail_compose.find_repeats(
            client, token, to=to, cc=cc, bcc=bcc, subject=subject,
            thread_id=thread_id, after_ms=original_date, days=repeat_check_days(),
        )
        if repeats:
            raise HTTPException(
                status_code=409,
                detail=gmail_compose.repeat_refusal(repeats, lead="Duplicate: not sent"),
            )
    return _Composed(
        to=to, cc=cc, bcc=bcc, subject=subject, body=body, html_body=html_body,
        raw=raw, message_bytes=message_bytes, thread_id=thread_id, reply_to=reply_to,
        atts_digest=mail_attachments.digest(atts) if atts else "",
        names=[a.filename for a in atts],
        files=[{"filename": a.filename, "size": len(a.data)} for a in atts],
    )


def _limit_refusal(used: int, daily_limit: int, count_origin: str) -> str:
    """The 429 detail — the agents' (who can draft instead), or an auto-reply's."""
    if count_origin == "agent":
        return (
            f"Daily send limit reached: {used} of {daily_limit} emails in the "
            "last 24 hours. Nothing was sent. Create a draft with "
            "google_mail action=create_draft instead, or ask the user to raise "
            f"the limit in {_ENABLE_WHERE}."
        )
    return (
        f"Daily auto-reply limit reached: {used} of {daily_limit} auto-replies in the "
        "last 24 hours. Nothing was sent."
    )


async def _send_locked(
    db: FlightDeckDB, owner: str, client: httpx.AsyncClient, token: str, c: _Composed, *,
    draft_id: str, daily_limit: int | None, count_origin: str, origin: str, agent: str,
    topic_id: str = "", in_reply_to_message_id: str = "", notify: bool = True,
    before_send: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str, str, int]:
    """Send *c* — or the draft *draft_id* — as *owner*. Returns ``(message_id,
    thread_id, used)``: *used* is how many *count_origin* sends the last 24
    hours held before this one.

    Under the owner's send lock, so two sends can't both slip under the limit
    or both pass the duplicate check: *daily_limit* over the last 24 hours'
    *count_origin* rows (None = no limit — a send the owner approved); the same
    email attempted in the last 10 minutes is a 409; then *before_send* (the
    caller's last word — raise to stop it; nothing is sent); then Gmail.

    A send Gmail may have made (a 5xx, no answer) is audited as ``unknown`` —
    and rings the bell when *notify* — before its error goes up with the
    ``X-FD-Gmail-Send-Outcome: unknown`` header. A confirmed one is audited as
    ``sent`` and rings the bell (when *notify*) once the lock is released. The
    audit row carries *agent*, *origin*, *topic_id* and
    *in_reply_to_message_id*."""
    async with _lock(_send_locks, owner):
        now = datetime.now(timezone.utc)
        used = await db.count_gmail_sends_since(
            owner, (now - _DAY).isoformat(), origin=count_origin,
        )
        if daily_limit is not None and used >= daily_limit:
            raise HTTPException(
                status_code=429, detail=_limit_refusal(used, daily_limit, count_origin),
            )
        chash = ""
        if not draft_id:  # a draft is one server-side object; Gmail consumes it
            chash = gmail_compose.content_hash(
                c.to, c.cc, c.bcc, c.subject, c.body, c.html_body, reply_to=c.reply_to,
                attachments_digest=c.atts_digest,
            )
            dup = await db.find_gmail_send_by_hash(
                owner, chash, (now - _DUPLICATE_WINDOW).isoformat(),
            )
            if dup and dup.get("status") == "unknown":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Duplicate: this exact email (same recipients, subject, body "
                        f"and files) was already attempted at {dup['created_at']} and Gmail "
                        "never confirmed it — it may have gone out. Not sent again. "
                        f"{_CHECK_SENT}"
                    ),
                )
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Duplicate: this exact email (same recipients, subject, body "
                        f"and files) was already sent at {dup['created_at']} (Gmail message "
                        f"{dup['gmail_message_id'] or '?'}). Not sent again."
                    ),
                )
        if before_send is not None:
            await before_send()

        if draft_id:
            url, send_body = (f"{gmail_compose.GMAIL_API}/users/me/drafts/send",
                              {"id": draft_id})
        else:
            url = f"{gmail_compose.GMAIL_API}/users/me/messages/send"
            send_body = {"raw": c.raw}
            if c.thread_id:
                send_body["threadId"] = c.thread_id
        session = ""
        if c.message_bytes:
            # Opening the upload sends nothing: a failure here is "not sent".
            try:
                session = await gmail_compose.start_upload(
                    client, token, "POST", "/users/me/messages/send",
                    {"threadId": c.thread_id} if c.thread_id else {}, len(c.message_bytes),
                )
            except httpx.HTTPStatusError as exc:
                raise _gmail_error(exc) from exc
            except httpx.HTTPError as exc:
                raise _transport_error(exc) from exc
        try:
            if session:
                sent = await gmail_compose.finish_upload(client, token, session, c.message_bytes)
            else:
                resp = await client.post(
                    url, json=send_body, headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                sent = resp.json()
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                err = _gmail_error(
                    exc, scopes_hint=_DRAFT_SCOPES_HINT if draft_id else _SEND_SCOPES_HINT,
                    attempted_send=True,
                )
            else:
                err = _transport_error(exc, attempted_send=True)
            if _send_outcome_unknown(exc):
                # Gmail may have sent it: record it like a send (daily count,
                # duplicate check, history, bell) so a retry can't double it.
                await _audit_send(
                    db, owner, status="unknown", agent=agent, to=c.to, cc=c.cc, bcc=c.bcc,
                    subject=c.subject, message_id="", thread_id=c.thread_id,
                    draft_id=draft_id, content_hash=chash, attachments=c.names,
                    origin=origin, topic_id=topic_id,
                    in_reply_to_message_id=in_reply_to_message_id,
                )
                if notify:
                    await _notify_send(db, owner, status="unknown", agent=agent, to=c.to,
                                       subject=c.subject, message_id="", attachments=c.names)
            raise err from exc
        message_id = str(sent.get("id") or "")
        sent_thread = str(sent.get("threadId") or c.thread_id or "")

        await _audit_send(
            db, owner, status="sent", agent=agent, to=c.to, cc=c.cc, bcc=c.bcc,
            subject=c.subject, message_id=message_id, thread_id=sent_thread,
            draft_id=draft_id, content_hash=chash, attachments=c.names,
            origin=origin, topic_id=topic_id, in_reply_to_message_id=in_reply_to_message_id,
        )

    if notify:
        await _notify_send(db, owner, status="sent", agent=agent, to=c.to,
                           subject=c.subject, message_id=message_id, attachments=c.names)
    return message_id, sent_thread, used


# ── POST /gmail/send ───────────────────────────────────────────────


@router.post("/gmail/send")
async def gmail_send(request: Request) -> dict[str, Any]:
    """Send an email as the calling agent's OWNER, if their policy allows.

    Body: the message fields ``to``, ``cc``, ``bcc``, ``subject``, ``body``,
    ``html_body``, ``reply_to_message_id`` (threading and the to / subject
    defaults as create_draft does them) — or ``{"draft_id"}`` to send an
    existing draft as it is now. Same agent gate as ``/access_token``.

    A composed send that repeats an email already drafted or sent (see
    :func:`gmail_compose.find_repeats`) is a 409, unless the body carries
    ``"allow_repeat": true`` (the user explicitly asked for another copy). A
    draft send is never a repeat — sending the draft is the way out of one.
    """
    _google._authorize_agent_call(request)
    # On a shared-agent member's turn the send is the MEMBER's — their opt-in
    # for this agent, policy, token, limits, audit row and bell — never the owner's.
    acting = await speaker_grants.acting_member(request, google=True)
    owner = acting.user_id if acting else await _google._agent_owner(request)

    # The enforcement point. It stops model mistakes and prompt injection (an
    # email, page or file telling the agent to "send this to …"). It is NOT a
    # tenant boundary against a shell-capable agent: that agent can already
    # fetch its owner's Gmail token (/access_token — gmail.compose sends) and
    # call Gmail directly. Shell agents are not a tenant boundary anywhere.
    if deck_send_disabled():
        raise _refused(_DECK_OFF_DETAIL, "deck-disabled")
    db = get_db()
    policy = await load_gmail_send_policy(db, owner)
    if not policy["enabled"]:
        raise _refused(_OFF_DETAIL, "off")
    token = await _google.get_valid_google_access_token(owner)
    if not token:
        raise HTTPException(
            status_code=404,
            detail=(
                "Google not connected for this user (or its token could not be "
                f"refreshed) — connect Google in {_RECONNECT}. Nothing was sent."
            ),
        )

    payload = await _read_payload(request)
    draft_id = payload["draft_id"].strip()
    atts: list[mail_attachments.Attachment] = payload["attachments"]
    # Files make Gmail's upload slow.
    async with httpx.AsyncClient(timeout=180 if atts else 60) as client:
        if draft_id:
            if any(payload[k].strip() for k in _MESSAGE_FIELDS) or atts:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Send either draft_id alone or the message fields "
                        "(to/cc/bcc/subject/body/html_body/reply_to_message_id/"
                        "attachments) — not both. A draft is sent with its own files."
                    ),
                )
            _check_gmail_id(draft_id, "draft_id")
            draft = await _draft_headers(client, token, draft_id)
            to, cc, bcc = draft.get("to", ""), draft.get("cc", ""), draft.get("bcc", "")
            _check_recipients(to, cc, bcc, policy["allowed_recipients"])
            # Sent as it is now, with its own files — nothing to build or sign.
            composed = _Composed(
                to=to, cc=cc, bcc=bcc, subject=draft.get("subject", ""), body="",
                html_body="", raw="", message_bytes=b"", thread_id=draft["thread_id"],
                reply_to="", atts_digest="", names=[f["filename"] for f in draft["files"]],
                files=draft["files"],
            )
        else:
            composed = await _compose_checked(
                db, owner, client, token,
                to=payload["to"], cc=payload["cc"], bcc=payload["bcc"],
                subject=payload["subject"], body=payload["body"],
                html_body=payload["html_body"],
                reply_to=payload["reply_to_message_id"].strip(), atts=atts,
                allowlist=policy["allowed_recipients"], allow_repeat=payload["allow_repeat"],
            )

        daily_limit = int(policy["daily_limit"])
        agent = (
            f"{acting.name} (shared by "
            f"{await tenant_profile.owner_name(db, acting.owner)})"
            if acting else _agent_label(request)
        )
        message_id, sent_thread, used = await _send_locked(
            db, owner, client, token, composed, draft_id=draft_id,
            daily_limit=daily_limit, count_origin="agent", origin="agent", agent=agent,
        )

    return {
        "ok": True,
        "message_id": message_id,
        "thread_id": sent_thread,
        "to": composed.to,
        "cc": composed.cc,
        "bcc": composed.bcc,
        "subject": composed.subject,
        "attachments": composed.files,
        "sent_last_24h": used + 1,
        "daily_limit": daily_limit,
    }


# ── topic auto-replies (no route: the engine calls this) ────────────


async def send_autoreply(
    db: FlightDeckDB, owner: str, *, token: str, inbound_message_id: str, to_addr: str,
    subject_fallback: str, body: str, agent_label: str, topic_id: str,
    daily_limit: int | None, origin: str,
    before_send: Callable[[], Awaitable[None]] | None,
) -> dict[str, Any]:
    """Send a topic auto-reply as *owner*: the plain-text *body*, answering
    the Gmail message *inbound_message_id* (threaded under it, subject ``Re:
    <its subject>`` — *subject_fallback* instead when that is set, for an
    email that had none) to *to_addr* and no one else.

    *to_addr* is the sender the engine verified, as one bare address. It is
    the recipient AND the whole allowlist the core checks against, so the
    reply is bound to it — a Reply-To never redirects it, and anything but one
    plain address is a 400. No cc / bcc, files or HTML of its own; never a
    repeat (``allow_repeat`` off); the owner's agentic signature at the end;
    :data:`AUTOREPLY_HEADERS` on the wire. The user's Email sending policy
    (on / off, allowed recipients) is not consulted — auto-replies are their
    own opt-in — but the deck's ``FD_GMAIL_SEND`` kill switch is (403).

    *daily_limit* caps *origin*'s sends over the last 24 hours (None = no cap:
    a send the owner approved); *origin* is one of :data:`AUTOREPLY_ORIGINS`
    (anything else is a ValueError — a caller bug). *before_send* runs under
    the owner's send lock, after the limit and duplicate checks, right before
    Gmail is called: raise an HTTPException there and nothing is sent. The
    audit row carries *agent_label*, *origin*, *topic_id* and the inbound id;
    no ``email_sent`` bell rings — the engine rings its own.

    Raises :class:`HTTPException` as ``POST /gmail/send`` does: 400 (nothing
    sendable), 403 (deck switch, Gmail refused), 404 (no token; the inbound
    email is gone), 409 (a repeat or duplicate), 429 (the limit), 502 — that
    one with the ``X-FD-Gmail-Send-Outcome: unknown`` header when Gmail may
    have sent it (audited as ``unknown``; never retry it). Returns ``ok``,
    ``message_id``, ``thread_id``, ``to``, ``subject``, ``body`` (as sent,
    signature included), ``sent_last_24h`` and ``daily_limit``.
    """
    if origin not in AUTOREPLY_ORIGINS:
        raise ValueError(f"not an auto-reply origin: {origin!r}")
    if deck_send_disabled():
        raise _refused(_AUTOREPLY_DECK_OFF_DETAIL, "deck-disabled")
    if not token:
        raise HTTPException(
            status_code=404,
            detail="Google not connected for this user. The auto-reply was not sent.",
        )
    _check_gmail_id(inbound_message_id, "inbound_message_id")
    addr = (to_addr or "").strip()
    if (gmail_compose.invalid_recipients(addr)
            or gmail_compose.parse_addresses(addr) != [addr.lower()]):
        raise HTTPException(
            status_code=400,
            detail=(f"An auto-reply goes to one plain address, not {addr[:120]!r}. "
                    "Nothing was sent."),
        )
    async with httpx.AsyncClient(timeout=60) as client:
        c = await _compose_checked(
            db, owner, client, token,
            to=addr, cc="", bcc="", subject=subject_fallback, body=body, html_body="",
            reply_to=inbound_message_id, atts=[], allowlist=[addr.lower()],
            allow_repeat=False, extra_headers=AUTOREPLY_HEADERS,
        )
        message_id, sent_thread, used = await _send_locked(
            db, owner, client, token, c, draft_id="", daily_limit=daily_limit,
            count_origin=origin, origin=origin, agent=agent_label, topic_id=topic_id,
            in_reply_to_message_id=inbound_message_id, notify=False, before_send=before_send,
        )
    return {
        "ok": True,
        "message_id": message_id,
        "thread_id": sent_thread,
        "to": c.to,
        "subject": c.subject,
        "body": c.body,
        "sent_last_24h": used + 1,
        "daily_limit": daily_limit,
    }
