"""Topic auto-replies for Gmail — the engine.

Flight Deck's own loop, separate from the event spine and the arbiter: it
polls each opted-in owner's Gmail through the History API, claims every new
INBOX message in an exactly-once ledger (``gmail_autoreplies``), and walks each
one through a compare-and-set state machine::

    new → matching → [waiting_agent] → writing → checking → approved → sending
    → sent | unknown
    terminal: skipped · held · blocked · failed · dismissed
    owner path: held → approved (approved_by) → sending → sent

* **Detection** (:func:`_poll`): ``history.list`` from the cursor the enabling
  PUT took as its baseline (:func:`baseline_and_enable`), so mail that was
  already there is never answered. Every new id is claimed (``INSERT OR
  IGNORE``) before any fetch or model call, and the cursor moves only after
  the claims. More than 20 in one poll: the newest 20 are worked, the rest are
  ``skipped/burst``. An expired cursor backfills the last hour, then
  re-baselines.
* **Checks before any model**: the deterministic prefilters 1–9
  (:func:`gmail_inbound.prefilter`), then the ledger caps (check 10: per
  thread, per sender, sender flood, daily limit) and the daily classification
  budget.
* **Matching** (:func:`gmail_autoreply_judges.classify`), then the human
  takeover rule (check 11: a draft in the thread, or mail the owner sent
  there themselves — the engine stays out for good), then the writer
  (:mod:`gmail_autoreply_writer`; a stopped agent means ``waiting_agent`` for
  up to an hour), then the guard: L1 (:mod:`captain_claw.outbound_guard`) —
  any block → ``blocked`` with no text kept, any hold → ``held`` — and only a
  clean reply reaches the L2 judge.
* **Send** (:func:`_send`): through ``gmail_send_routes.send_autoreply``,
  bound to the verified sender. Its ``before_send`` runs under the owner's
  send lock and re-reads the config (the same enablement, topic, brief and
  account), the deck switches, the guard's L1 with the current never-
  disclose list and the caps, then takes the commit point — the CAS
  ``approved → sending``. Nothing after it is ever retried: a send Gmail may
  have made is ``unknown``, and a restart (:func:`recover_on_start`) turns an
  interrupted ``sending`` into ``unknown``.

Every precondition is re-read on every tick, and a row reads its Google
token again right before each Gmail call that follows model work (the
token's mailbox proven to be the bound account's, once per token). The deck
switches ``FD_GMAIL_AUTOREPLY=off`` and ``FD_GMAIL_SEND=off`` stop everything; a
changed Google account, a disconnect (:func:`on_google_disconnect`) or three
replies blocked for leaking (:func:`_check_pause`) switch the owner off.
Nothing here starts an agent, opens a chat, writes a Gmail draft, or touches
the autonomy ledger: a handled email is only marked done on the event spine
(``EventsStore.claim_handled``) so the arbiter never nudges about it.

For the routes: :func:`baseline_and_enable`, :func:`disable`,
:func:`held_send`, :func:`dismiss`, :func:`run_dry_run`, :func:`run_test`,
:func:`missing_scopes`, :func:`bound_elsewhere` and :func:`config_lock`.

Never logs email text, a reply, a token or a finding's value.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import time
import unicodedata
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import httpx
from fastapi import HTTPException

from captain_claw import gmail_compose, gmail_inbound, mail_signature, outbound_guard
from captain_claw.flight_deck import agent_sharing, auth, events
from captain_claw.flight_deck import gmail_autoreply_config as arcfg
from captain_claw.flight_deck import gmail_autoreply_judges as judges
from captain_claw.flight_deck import gmail_autoreply_writer as writer
from captain_claw.flight_deck import gmail_send_routes as gs
from captain_claw.flight_deck import google_oauth_routes as _google
from captain_claw.flight_deck.db import AUTOREPLY_SEND_STATES
from captain_claw.logging import get_logger

log = get_logger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────

POLL_DEFAULT_S = 30
POLL_MIN_S = 15
CLASSIFY_PER_DAY_DEFAULT = 200
CLASSIFY_PER_DAY_RANGE = (20, 1000)

HISTORY_PAGE_SIZE = 100
HISTORY_MAX_PAGES = 5
BURST_MAX = 20                  # new ids worked per poll; older ones → skipped/burst
BACKFILL_QUERY = "in:inbox newer_than:1h"
BACKFILL_MAX = 50
PROCESS_BUDGET = 5              # rows worked per owner per tick
WAITING_SCAN = 50               # waiting_agent rows looked at per tick (cheap)
POLL_CONCURRENCY = 4
LLM_CONCURRENCY = 3             # classifier, writer and judge calls, every owner together
TEST_LLM_CONCURRENCY = 3        # breadth tests and "Try it" (every owner), never the engine
GMAIL_TIMEOUT_S = 30

FETCH_ATTEMPTS = 3
CLASSIFY_ATTEMPTS = 3
CLASSIFY_RETRY_S = 60 * 60      # a failed classification is retried while the email is this young
AGENT_WAIT_S = 60 * 60          # waiting_agent → failed/agent_offline after this
SEND_MAX_AGE_S = 90 * 60        # the pre-send age re-check
HELD_ACTION_DAYS = 7            # the owner may send a held card this long after receipt

THREAD_CAP_DAY = 1
THREAD_CAP_TOTAL = 3
SENDER_CAP_DAY = 3
FLOOD_PER_HOUR = 10             # more inbound than this from one sender → suppressed 24 h
PAUSE_BLOCKS = 3                # leak blocks in 24 h → auto-pause
DRIFT_DAYS = 7
DRIFT_MIN_SAMPLES = 20
DRIFT_SHARE = 0.5
OWN_ADDRESSES_TTL = timedelta(hours=24)
BELL_CAP_PER_DAY = 20           # held + blocked bells, then one summary
PURGE_EVERY_S = 24 * 3600

DRY_RUN_QUERY = "in:inbox newer_than:30d"
DRY_RUN_MAX = 30
TEST_SENDER = "sender@example.com"  # "Try it" with no From: a clearly fake sample sender
TEST_BODY_CHARS = (1, gmail_inbound.MAX_BODY_CHARS)
TEST_SUBJECT_CHARS = 1000
HELD_TEXT_CHARS = 10_000

_G = "https://www.googleapis.com/auth/"
READ_SCOPES = frozenset({_G + "gmail.readonly", _G + "gmail.modify", "https://mail.google.com/"})
SEND_SCOPES = frozenset(gmail_compose.SEND_SCOPES)

# Bells (notifications.type), all with ref_type REF_TYPE.
BELL_SENT = "email_autoreply"
BELL_HELD = "email_autoreply_held"
BELL_BLOCKED = "email_autoreply_blocked"
BELL_ISSUE = "email_autoreply_issue"
REF_TYPE = "gmail_autoreply"
USAGE_EVENT = judges.USAGE_EVENT

# Every static prompt a reply must not echo (the guard's prompt_texts): the
# classifier and judge prompts and the writer's lookup and compose prompts.
STATIC_PROMPTS: tuple[str, ...] = (*judges.STATIC_PROMPTS, *writer.PROMPT_TEXTS)

# Ledger reasons the engine writes (the prefilter, classifier, writer and
# judge codes come from their modules).
GONE = "gone"
FETCH = "fetch"
BURST = "burst"
CAP_THREAD = "cap_thread"
CAP_SENDER = "cap_sender"
CAP_FLOOD = "cap_flood"          # the email that tipped a sender over the flood rule
FLOOD_SUPPRESSED = "flood_suppressed"   # later ones, within 24 h of that email
CAP_DAILY = "cap_daily"
CAP_CLASSIFY = "cap_classify"
TOPIC_OFF = "topic_off"
DRAFT_IN_THREAD = "draft_in_thread"
OWNER_IN_THREAD = "owner_in_thread"
THREAD_HAS_UNANSWERED = "thread_has_unanswered"  # an earlier email in the thread still waits for the owner
THREAD_UNREADABLE = "thread_unreadable"
AGENT_OFFLINE = "agent_offline"
GUARD_ERROR = "guard_error"
GUARD_PREFIX = "guard:"         # held / blocked by L1: guard:<kinds>
NO_SIGNATURE = "no_signature"
BRIEF_CHANGED = "brief_changed"  # held: the topic's brief changed while the reply was in flight
STALE = "stale"
BEFORE_ENABLE = "before_enable"
TURNED_OFF = "turned_off"
ALREADY_ANSWERED = "already_answered"
ALREADY_HANDLED = "already_handled"
BUILD_ERROR = "build_error"
SEND_ERROR = "send_error"
SEND_UNCONFIRMED = "send_unconfirmed"
INTERNAL_ERROR = "internal_error"
EXPIRED = "expired"

# Why an owner was switched off (config.disabled_reason), and issue-bell reasons.
GOOGLE_ACCOUNT_CHANGED = "google_account_changed"
GOOGLE_DISCONNECTED = "google_disconnected"
GOOGLE_NOT_CONNECTED = "google_not_connected"
AUTO_PAUSED = "auto_paused"
MISSING_SCOPES = "missing_scopes"
HISTORY_EXPIRED = "history_expired"
BURST_TRUNCATED = "burst_truncated"
NO_AGENT = "no_agent"
NO_TOPICS = "no_topics"
BELL_CAP = "bell_cap"
GMAIL_UNREACHABLE = "gmail_unreachable"

# States a row can leave on an unexpected error without anything having been sent.
_PRE_SEND = ("new", "matching", "waiting_agent", "writing", "checking", "approved")
# Rows nothing works on while the owner is off; switching off skips them.
_QUEUED = ("new", "waiting_agent")
# Skip codes of checks 1–6 (and a check that failed): such a row never proved
# its From address, so a forged From can't make it count for that sender.
# Rows the flood rule never counts: every prefilter skip (checks 1–9). A
# forged From claim, but also a sender's genuine signed mail replayed with the
# owner only on the envelope (not_addressed_to_me) must not suppress them.
_UNVERIFIED = (*gmail_inbound.PREFILTER_CODES, gmail_inbound.CHECK_ERROR)
# Block kinds that only the owner's own material can produce: the evidence a
# writer leaked. Anything a sender can make the writer synthesise (a card
# number, "localhost", its own instructions, a canary, a random key) can't
# switch the feature off.
_LEAK_KINDS = frozenset({"never_disclose", "private_copy"})
PAUSE_SCAN_ROWS = 5000   # above any day's classification budget
RUNNER_STALE_S = 180     # a runner whose heartbeat is this old is taken over (at least)
# The poller's own label filter (check 1 runs on the full message later).
_POLL_SKIP_LABELS = frozenset({"SENT", "DRAFT", "SPAM", "TRASH"})
# Gmail ids go into URL paths — never let one walk them.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_CONNECTIONS = "Connections → Google"
_ISSUES: dict[str, tuple[str, str]] = {
    GOOGLE_ACCOUNT_CHANGED: (
        "Topic auto-replies turned off: the Google account changed",
        "The Google account connected now isn't the one auto-replies were turned on for. "
        f"Turn them on again in {_CONNECTIONS}."),
    GOOGLE_DISCONNECTED: (
        "Topic auto-replies turned off: Google disconnected",
        f"Reconnect Google, then turn auto-replies on again in {_CONNECTIONS}."),
    GOOGLE_NOT_CONNECTED: (
        "Topic auto-replies can't read your Gmail",
        "Google isn't connected, or its token could not be refreshed. "
        f"Reconnect it in {_CONNECTIONS}."),
    MISSING_SCOPES: (
        "Topic auto-replies need more Gmail access",
        "The Google connection must allow reading (gmail.readonly) and sending (gmail.send) "
        "mail: the deck admin adds the scopes, then you reconnect Google."),
    HISTORY_EXPIRED: (
        "Topic auto-replies may have missed mail",
        "Gmail's change history ran out while Flight Deck wasn't polling. Emails from the "
        "last hour were picked up; older ones were not answered."),
    BURST_TRUNCATED: (
        "Topic auto-replies may have missed mail",
        "More mail arrived since the last check than Flight Deck reads in one go. Emails "
        "from the last hour were picked up; older ones were not answered."),
    CAP_DAILY: (
        "Daily auto-reply limit reached",
        f"Further topic emails wait for you in Gmail. The limit is in {_CONNECTIONS}."),
    CAP_CLASSIFY: (
        "Topic auto-replies paused until tomorrow",
        "Today's budget of email checks is spent; further mail waits for you in Gmail."),
    judges.CLASSIFIER_UNAVAILABLE: (
        "Topic auto-replies can't check incoming mail",
        "Neither Jev nor your Balanced / Reason model answered. Those emails wait for you "
        "in Gmail."),
    FETCH: (
        "Topic auto-replies can't read new mail",
        "Gmail didn't return an email after three tries. It waits for you in Gmail."),
    THREAD_UNREADABLE: (
        "Topic auto-replies can't read a conversation",
        "Gmail didn't return an email's thread, so it was left for you."),
    writer.WRITER_UNAVAILABLE: (
        "The auto-reply agent's model isn't answering",
        "A topic email was left for you: the agent couldn't write a reply."),
    writer.WRITER_FORMAT: (
        "The auto-reply agent answered in the wrong format",
        "A topic email was left for you. A weak model often does this."),
    GUARD_ERROR: (
        "A topic auto-reply couldn't be checked",
        "Your profile couldn't be read for the safety check, so nothing was sent."),
    BUILD_ERROR: (
        "A topic auto-reply could not be sent",
        "Gmail couldn't build the email. Nothing was sent."),
    SEND_ERROR: (
        "A topic auto-reply could not be sent",
        "Gmail refused it or didn't answer before it went out. Nothing was sent."),
    AUTO_PAUSED: (
        "Auto-replies paused: three replies contained data that must never be sent — "
        "review them.",
        f"Nothing was sent. Review the blocked replies, then turn auto-replies on again in "
        f"{_CONNECTIONS}."),
    BELL_CAP: (
        "More topic auto-replies need you",
        f"Further held or blocked replies today are in {_CONNECTIONS} → Activity."),
}
_AGENT_ISSUE = (
    "The auto-reply agent can't write replies",
    f"It is gone, isn't yours, or has no token or port. Choose another one in {_CONNECTIONS}.",
)


class AutoReplyError(Exception):
    """A refused owner action (a route maps it to ``HTTPException(status,
    detail)``). ``findings`` are the masked L1 findings of a 422; ``headers``
    carry ``X-FD-Gmail-Send-Outcome: unknown`` when Gmail may have sent."""

    def __init__(self, status: int, detail: str, *, findings: list[dict] | None = None,
                 headers: Mapping[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.findings = list(findings or [])
        self.headers = dict(headers or {})


class _TickStopError(Exception):
    """Ends an owner's tick: the token is gone (``google_not_connected``), the
    mailbox isn't the bound account any more (``google_account_changed``) or
    Gmail didn't answer the check (``gmail_<status>``). Whatever row was in
    flight is left retryable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── Switches and knobs ────────────────────────────────────────────────────


def autoreply_deck_disabled() -> bool:
    """The deck switch: ``FD_GMAIL_AUTOREPLY`` = off / 0 / false / no."""
    return os.environ.get("FD_GMAIL_AUTOREPLY", "").strip().lower() in ("off", "0", "false", "no")


def _decks_off() -> bool:
    return gs.deck_send_disabled() or autoreply_deck_disabled()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def poll_seconds() -> int:
    """The tick: ``FD_GMAIL_AUTOREPLY_POLL_SECONDS`` (default 30, at least 15)."""
    return max(POLL_MIN_S, _env_int("FD_GMAIL_AUTOREPLY_POLL_SECONDS", POLL_DEFAULT_S))


def classify_per_day() -> int:
    """Classifications per owner per UTC day:
    ``FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY`` (default 200, 20..1000)."""
    lo, hi = CLASSIFY_PER_DAY_RANGE
    return min(hi, max(lo, _env_int("FD_GMAIL_AUTOREPLY_CLASSIFY_PER_DAY",
                                    CLASSIFY_PER_DAY_DEFAULT)))


# ── Time ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _now_ms() -> int:
    return int(_now().timestamp() * 1000)


def _since(**delta: float) -> str:
    return (_now() - timedelta(**delta)).isoformat()


def _ms_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms > 0 else ""


def _parse_iso(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or "").strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _row_age_s(row: Mapping[str, Any]) -> float:
    """Seconds since the row's email arrived (since its claim when unknown)."""
    at = _parse_iso(row.get("received_at")) or _parse_iso(row.get("created_at"))
    return (_now() - at).total_seconds() if at else float("inf")


# ── Runtime (per event loop) ──────────────────────────────────────────────


@dataclass
class _Runtime:
    poll: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(POLL_CONCURRENCY))
    llm: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(LLM_CONCURRENCY))
    # The owner-triggered breadth test and "Try it": their own slots, so no
    # test can queue ahead of (or starve) anyone's live mail.
    tests: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(TEST_LLM_CONCURRENCY))
    bind: asyncio.Lock = field(default_factory=asyncio.Lock)
    owners: dict[str, asyncio.Lock] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    # owner → (enablement, token digest) whose mailbox was checked to be the
    # bound account's; a new access token (refresh, reconnect) is checked again.
    verified: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Retry budgets per ledger row, one per stage, so a fetch blip doesn't use
    # up the classifier's retries (nor either a restart's: that is the row's
    # persisted ``attempts``). A restart starts them afresh; the 60-minute age
    # rule bounds them.
    fetch_failures: dict[str, int] = field(default_factory=dict)
    classify_failures: dict[str, int] = field(default_factory=dict)


_RUNTIME: tuple[Any, _Runtime] | None = None


def _rt() -> _Runtime:
    """The semaphores and locks of the running event loop (a new loop — a
    test's — gets its own)."""
    global _RUNTIME
    loop = asyncio.get_running_loop()
    if _RUNTIME is None or _RUNTIME[0] is not loop:
        _RUNTIME = (loop, _Runtime())
    return _RUNTIME[1]


def config_lock(owner: str) -> asyncio.Lock:
    """The owner's config read-modify-write lock, shared with the routes.
    Not re-entrant: :func:`disable` takes it itself."""
    return gs._lock(gs._policy_locks, f"ar:{owner}")


# ── Small helpers ─────────────────────────────────────────────────────────


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _labels(message: Mapping[str, Any]) -> set[str]:
    return {str(x) for x in _list(message.get("labelIds"))}


def _int(value: Any) -> int:
    """A Gmail number field (``internalDate`` is a string) as an int; 0 when
    unreadable."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _own(state: Mapping[str, Any], cfg: arcfg.AutoReplyConfig) -> list[str]:
    """The mailbox's own addresses: the baseline's (and the 24-hourly
    refresh's) plus the bound account address."""
    addrs = [*(state.get("own_addresses") or []), cfg.account_email]
    return list(dict.fromkeys(a.strip().lower() for a in addrs
                              if isinstance(a, str) and a.strip()))


def _enabled_ms(cfg: arcfg.AutoReplyConfig, state: Mapping[str, Any]) -> int:
    """When auto-replies were turned on — the later of the config's and the
    state row's stamps; 0 (nothing passes check 2) when either is unknown."""
    a = gmail_inbound.iso_to_ms(state.get("enabled_at"))
    b = gmail_inbound.iso_to_ms(cfg.enabled_at)
    return max(a, b) if a and b else 0


def _auto_submitted(inb: gmail_inbound.Inbound) -> bool:
    """An ``Auto-Submitted`` header with a value other than ``no``."""
    for value in gmail_inbound.header_values(inb, "Auto-Submitted"):
        token = re.split(r"[;\s(]", value.strip().lower(), maxsplit=1)[0]
        if token and token != "no":
            return True
    return False


def _reason_label(reason: str) -> str:
    """A held reason as the bell says it."""
    if reason == NO_SIGNATURE:
        return "your agentic signature is empty"
    if reason == BRIEF_CHANGED:
        return "the topic's brief changed while the reply was being written"
    if reason == judges.JUDGE_UNAVAILABLE:
        return "the reply checker was unavailable"
    if reason.startswith(judges.JUDGE_REASON_PREFIX):
        names = reason[len(judges.JUDGE_REASON_PREFIX):].split(",")
        return "the checker flagged: " + ", ".join(n.replace("_", " ") for n in names if n)
    if reason.startswith(GUARD_PREFIX):
        return "it mentions " + outbound_guard.describe_kinds(
            reason[len(GUARD_PREFIX):].split(","))
    return reason.replace("_", " ")


def _from_material(reply: str, background: tuple[str, ...], never_disclose: tuple[str, ...],
                   private: list[str]) -> frozenset[str]:
    """The leak kinds of *reply* that the writer's BACKGROUND (not the
    sender, not a SHAREABLE brief that restates the profile) supplied —
    decided per value: a never-disclose term the reply AND the background
    both hold; profile text the reply copies from background text that is
    itself profile text. Never raises."""
    out: set[str] = set()
    try:
        bg = "\n\n".join(background)
        if not bg:
            return frozenset()
        for term in never_disclose:
            in_reply = outbound_guard.scan_blocks(reply, never_disclose=[term])
            if "never_disclose" in in_reply.kinds and "never_disclose" in outbound_guard.scan_blocks(
                    bg, never_disclose=[term]).kinds:
                out.add("never_disclose")
                break
        if private:
            reply_private = "private_copy" in outbound_guard.scan_blocks(
                reply, never_disclose=[], private_corpus=private).kinds
            bg_private = "private_copy" in outbound_guard.scan_blocks(
                bg, never_disclose=[], private_corpus=private).kinds
            copies_bg = "private_copy" in outbound_guard.scan_blocks(
                reply, never_disclose=[], private_corpus=[bg]).kinds
            if reply_private and bg_private and copies_bg:
                out.add("private_copy")
    except Exception:
        return frozenset()
    return frozenset(out)


def _leak_evidence(findings: Any) -> bool:
    """Does a blocked row count toward auto-pause? Only when one of its block
    findings is owner-held knowledge (:data:`_LEAK_KINDS`: a never-disclose
    term, the private profile) that did NOT come from the sender's own text
    AND was in the material the writer was given (``from_material``, set by
    :func:`_block`) — proof the writer brought it from the owner's
    knowledge. A sender who gets the writer to repeat their own token, or to
    synthesise a term or a bio sentence ("read 'noirO' backwards"), is
    blocked, but can't pause the feature."""
    return any(isinstance(f, dict) and f.get("level") == outbound_guard.BLOCK
               and f.get("kind") in _LEAK_KINDS and not f.get("echo")
               and f.get("from_material")
               for f in _list(findings))


# ── Gmail ─────────────────────────────────────────────────────────────────


async def _gmail_get(client: httpx.AsyncClient, token: str, path: str,
                     params: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """``GET users/me{path}`` → ``(200, json)``, or ``(status, {})`` — 0 when
    Gmail never answered, 502 for an answer that isn't a JSON object. Never
    raises (cancellation aside)."""
    try:
        resp = await client.get(f"{gmail_compose.GMAIL_API}/users/me{path}",
                                params=dict(params or {}),
                                headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return 0, {}
    if resp.status_code != 200:
        return resp.status_code, {}
    try:
        data = resp.json()
    except ValueError:
        return 502, {}
    return (200, data) if isinstance(data, dict) else (502, {})


class _Mailbox(NamedTuple):
    """The Gmail profile: its address and current ``historyId``, the own
    addresses (the address plus the verified send-as aliases), and whether
    the aliases could be read at all (else ``own`` is the address alone)."""

    address: str
    history_id: str
    own: list[str]
    aliases_ok: bool


async def _mailbox(client: httpx.AsyncClient, token: str) -> _Mailbox | None:
    """The Gmail profile plus its verified send-as aliases; None when the
    profile can't be read. A failed alias lookup is reported
    (``aliases_ok``), never passed off as "no aliases"."""
    st, profile = await _gmail_get(client, token, "/profile")
    address = str(profile.get("emailAddress") or "").strip().lower()
    history_id = str(profile.get("historyId") or "").strip()
    if st != 200 or not address or not history_id:
        return None
    own = [address]
    st, data = await _gmail_get(client, token, "/settings/sendAs")
    if st == 200:
        for alias in _list(data.get("sendAs")):
            if not isinstance(alias, dict):
                continue
            addr = str(alias.get("sendAsEmail") or "").strip().lower()
            verified = alias.get("isPrimary") is True or alias.get("verificationStatus") == "accepted"
            if verified and gmail_compose.parse_addresses(addr) == [addr]:
                own.append(addr)
    return _Mailbox(address, history_id, list(dict.fromkeys(own)), st == 200)


def _same_address(a: Any, b: Any) -> bool:
    """Two addresses, both set, equal ignoring case."""
    a, b = str(a or "").strip().casefold(), str(b or "").strip().casefold()
    return bool(a) and a == b


def _bound(mailbox: _Mailbox, state: Mapping[str, Any]) -> None:
    """The mailbox the token reads must be the account auto-replies were
    turned on for — else :class:`_TickStopError` (``google_account_changed``)."""
    if not _same_address(mailbox.address, state.get("account_email")):
        raise _TickStopError(GOOGLE_ACCOUNT_CHANGED)


async def _thread(client: httpx.AsyncClient, token: str, thread_id: str) -> dict[str, Any] | None:
    """The thread ``format=full``, or None when it can't be read."""
    if not _ID_RE.match(thread_id or ""):
        return None
    st, data = await _gmail_get(client, token, f"/threads/{thread_id}", {"format": "full"})
    if st != 200 or not isinstance(data.get("messages"), list):
        return None
    return data


async def _takeover(db: Any, owner: str, thread_id: str, thread: Mapping[str, Any],
                    message_id: str = "") -> str:
    """Check 11, the human-takeover rule: ``draft_in_thread`` when the thread
    holds a draft; ``owner_in_thread`` when it holds a SENT message that is
    neither one of our ledger's auto-replies nor marked ``Auto-Submitted`` —
    once the owner writes there themselves the engine stays out for good;
    ``thread_has_unanswered`` when an inbox email older than *message_id*
    (and newer than our last reply in the thread) is still unanswered."""
    messages = [m for m in _list(thread.get("messages")) if isinstance(m, dict)]
    if any("DRAFT" in _labels(m) for m in messages):
        return DRAFT_IN_THREAD
    ours = await db.autoreply_sent_ids(owner, thread_id)
    last_sent = 0
    for m in messages:
        if "SENT" not in _labels(m):
            continue
        last_sent = max(last_sent, _int(m.get("internalDate")))
        if str(m.get("id") or "") in ours:
            continue
        if not await asyncio.to_thread(lambda m=m: _auto_submitted(gmail_inbound.parse_message(m))):
            return OWNER_IN_THREAD
    # An earlier email in the thread that nothing answered yet (newer than
    # our last reply there): answering only the latest would leave it behind
    # and hide the whole thread from Autonomous Work — the owner takes it.
    if message_id:
        mine = next((m for m in messages if str(m.get("id") or "") == message_id), None)
        mine_ms = _int(mine.get("internalDate")) if mine else 0
        for m in messages:
            labels = _labels(m)
            if (str(m.get("id") or "") == message_id or "SENT" in labels or "DRAFT" in labels
                    or "INBOX" not in labels):
                continue
            ms = _int(m.get("internalDate"))
            if last_sent < ms < mine_ms:
                return THREAD_HAS_UNANSWERED
    return ""


async def missing_scopes(db: Any, owner: str) -> list[str]:
    """What *owner*'s Google grant lacks for auto-replies: ``gmail.readonly``
    (any read scope) and / or ``gmail.send`` (any send scope); [] when both
    are there. An unreadable grant lacks both."""
    try:
        tokens = await _google._load_tokens(db, owner)
        granted = set(str(tokens.scope or "").split()) if tokens else set()
    except Exception:
        granted = set()
    out = []
    if not granted & READ_SCOPES:
        out.append("gmail.readonly")
    if not granted & SEND_SCOPES:
        out.append("gmail.send")
    return out


# ── Bells, usage, spine ───────────────────────────────────────────────────


async def _bell(db: Any, owner: str, kind: str, title: str, body: str, ref_id: str) -> None:
    try:
        await db.add_notification(owner, kind, _clip(title, 120), _clip(body, 300),
                                  REF_TYPE, ref_id)
    except Exception as exc:
        log.warning("gmail autoreply bell failed", owner=owner, error=type(exc).__name__)


async def _issue(db: Any, owner: str, reason: str, title: str = "", body: str = "", *,
                 ref_id: str = "") -> None:
    """An ``email_autoreply_issue`` bell, at most one per (owner, ``ref_id`` —
    the reason by default) per 24 hours."""
    ref = ref_id or reason
    default_title, default_body = _ISSUES.get(reason, ("", ""))
    try:
        if await db.count_notifications_since(owner, _since(hours=24), BELL_ISSUE,
                                              ref_type=REF_TYPE, ref_id=ref):
            return
    except Exception as exc:
        log.warning("gmail autoreply bell count failed", owner=owner, error=type(exc).__name__)
        return
    await _bell(db, owner, BELL_ISSUE, title or default_title or reason.replace("_", " "),
                body or default_body, ref)


async def _card_bell(db: Any, owner: str, kind: str, title: str, body: str, row_id: str) -> None:
    """A held / blocked bell: at most :data:`BELL_CAP_PER_DAY` a day, then one
    summary."""
    try:
        n = await db.count_notifications_since(owner, _since(hours=24), (BELL_HELD, BELL_BLOCKED))
    except Exception:
        n = 0
    if n < BELL_CAP_PER_DAY:
        await _bell(db, owner, kind, title, body, row_id)
    else:
        await _issue(db, owner, BELL_CAP)


async def _claim_spine(owner: str, message_id: str) -> None:
    """Mark the email handled on the event spine, so a later Gmail event poll
    dedups it and the arbiter never proposes a reply of its own. Taken
    provisionally once a matched email passes the thread check (the engine
    works on it from then on), and settled by :func:`_settle_spine` when the
    row ends. Never fails the row."""
    try:
        await asyncio.to_thread(
            lambda: events.get_store().claim_handled(owner, "gmail", f"gmail:{message_id}"))
    except Exception as exc:
        log.warning("gmail autoreply spine claim failed", owner=owner, error=type(exc).__name__)


async def _release_spine(owner: str, message_id: str) -> None:
    """Give an email the engine did NOT handle back to the event spine, so
    Autonomous Work can surface it ("<sender> is waiting for a reply").
    Never fails the row."""
    try:
        await asyncio.to_thread(
            lambda: events.get_store().release_claim(owner, "gmail", f"gmail:{message_id}"))
    except Exception as exc:
        log.warning("gmail autoreply spine release failed", owner=owner,
                    error=type(exc).__name__)


# Outcomes the owner sees (a sent reply, or a card waiting for them) keep the
# spine claim; a row that ENDED unhandled gives the email back to Autonomous
# Work. A row the engine still means to answer (queued again, waiting for its
# agent) keeps its provisional claim: released, the arbiter would propose a
# reply next to the auto-reply that follows (round-1 #14).
_SPINE_HANDLED = frozenset({"sent", "unknown", "held", "blocked", "dismissed"})
_SPINE_RELEASED = frozenset({"skipped", "failed"})


async def _settle_spine(db: Any, owner: str, row_id: str) -> None:
    """After a row's work: claim the email on the spine when the row ended
    handled, release it when it ended skipped / failed / back in the queue.
    A row still mid-flight is left alone. Never raises."""
    try:
        row = await db.get_autoreply(owner, row_id)
    except Exception:
        return
    if not row:
        return
    mid = str(row.get("inbound_message_id") or "")
    st = str(row.get("state") or "")
    if st in _SPINE_HANDLED:
        await _claim_spine(owner, mid)
    elif st in _SPINE_RELEASED:
        await _release_spine(owner, mid)


async def _log_writer_usage(db: Any, owner: str, result: writer.WriterResult) -> None:
    """The writer's model calls in ``usage_logs`` — stage, model, tokens; no content."""
    for u in result.usage:
        try:
            await db.log_usage(owner, USAGE_EVENT, json.dumps({
                "stage": str(u.get("stage") or ""), "source": "agent",
                "model": str(u.get("model") or ""), "tokens": int(u.get("tokens") or 0),
            }))
        except Exception as exc:
            log.warning("gmail autoreply usage log failed", owner=owner,
                        error=type(exc).__name__)


async def _agent_name(owner: str, ref: str) -> str:
    """The agent's name for a bell, best effort."""
    try:
        rec = await asyncio.to_thread(agent_sharing.resolve_agent_record, ref)
    except Exception:
        rec = None
    if rec is not None and rec.owner == owner and rec.name:
        return rec.name
    return "Your auto-reply agent"


# ── Switching an owner off ────────────────────────────────────────────────


async def skip_queued(db: Any, owner: str) -> int:
    """*owner*'s rows nothing will work on now that auto-replies are off —
    ``new`` and ``waiting_agent`` — become ``skipped/turned_off`` (rows in a
    work state are left to their own CAS and ``before_send``, which refuse).
    Called wherever the state row is dropped, and before a fresh baseline.
    Returns how many moved."""
    moved = 0
    for _batch in range(100):
        rows = await db.next_autoreplies(owner, _QUEUED, limit=500)
        progressed = False
        for row in rows:
            if await db.cas_autoreply(row["id"], owner, _QUEUED, "skipped", reason=TURNED_OFF):
                moved += 1
                progressed = True
                # A waiting row held its provisional claim: give the email back.
                await _release_spine(owner, str(row.get("inbound_message_id") or ""))
        if not progressed:
            break
    return moved


async def _switch_off(db: Any, owner: str, reason: str, *,
                      enabled_at: str | None = None) -> bool:
    """Turn *owner*'s auto-replies off with *reason* and drop their state row
    (so the engine stops at once) and their queue (:func:`skip_queued`).
    True when they were on. With *enabled_at* (what the engine saw), only
    that enablement: one the owner made since — a fresh baseline — is left
    alone."""
    async with config_lock(owner):
        cfg = await arcfg.load_autoreply_config(db, owner)
        if enabled_at is not None and cfg.enabled and cfg.enabled_at != enabled_at:
            return False
        await db.delete_autoreply_state(owner)
        await skip_queued(db, owner)
        if not cfg.enabled:
            return False
        await arcfg.save_autoreply_config(db, owner, arcfg.disable(cfg, reason))
        return True


async def disable(owner: str, reason: str, *, db: Any = None) -> arcfg.AutoReplyConfig:
    """Turn *owner*'s auto-replies off (``paused_by_user``, ``auto_paused``,
    ``google_account_changed`` …) and delete their state row; returns the
    stored config. Takes :func:`config_lock` itself — don't hold it."""
    db = db if db is not None else auth.get_db()
    await _switch_off(db, owner, reason)
    return await arcfg.load_autoreply_config(db, owner)


async def on_google_disconnect(db: Any, uid: str) -> None:
    """Google was disconnected for *uid*: their auto-replies go off
    (``google_disconnected``) and ring a bell; reconnecting — even to the same
    account — needs a deliberate re-enable. A no-op for a user who never
    turned them on. Never raises."""
    try:
        if await _switch_off(db, uid, GOOGLE_DISCONNECTED):
            await _issue(db, uid, GOOGLE_DISCONNECTED)
    except Exception as exc:
        log.warning("gmail autoreply disconnect hook failed", error=type(exc).__name__)


async def bound_elsewhere(db: Any, owner: str, sub: str) -> bool:
    """Does another user on this deck have auto-replies on for the Google
    account *sub*? (Exclusive binding: two FD users can hold one account.)"""
    if not sub:
        return False
    for st in await db.list_autoreply_states():
        if st.get("owner_id") != owner and st.get("google_sub") == sub:
            return True
    return False


async def baseline_and_enable(db: Any, owner: str, cfg: arcfg.AutoReplyConfig, *,
                              now: datetime | None = None) -> arcfg.AutoReplyConfig:
    """Turn *owner*'s auto-replies on: the Gmail baseline, the state row, the
    stored config. The caller holds :func:`config_lock` and has checked the
    other enable preconditions; *cfg* is the config to switch on.

    The profile's ``historyId`` is the cursor — no backlog is ever answered —
    and its address must be the connected Google account's. The verified
    send-as aliases join the own addresses (best effort). Refused, with
    nothing written, as :class:`AutoReplyError` (409): Google not connected,
    another user bound to the same account, the profile unreadable or of
    another account. Returns the stored config."""
    user = await _google._load_user(db, owner)
    sub = str((user or {}).get("sub") or "")
    email = str((user or {}).get("email") or "").strip().lower()
    if not sub or not email:
        raise AutoReplyError(409, f"Connect Google first ({_CONNECTIONS}).")
    token = await _google.get_valid_google_access_token(owner)
    if not token:
        raise AutoReplyError(409, f"Google isn't connected, or its token could not be "
                                  f"refreshed — reconnect it in {_CONNECTIONS}.")
    async with _rt().bind:
        if await bound_elsewhere(db, owner, sub):
            raise AutoReplyError(409, "Another user on this Flight Deck already has topic "
                                      "auto-replies on for this Google account.")
        async with httpx.AsyncClient(timeout=GMAIL_TIMEOUT_S) as client:
            mailbox = await _mailbox(client, token)
        if mailbox is None:
            raise AutoReplyError(409, "Couldn't read your Gmail profile to start from — "
                                      "check the Gmail scopes and try again.")
        address, history_id, own = mailbox.address, mailbox.history_id, mailbox.own
        if not _same_address(address, email):
            raise AutoReplyError(409, "The Gmail mailbox isn't the connected Google account — "
                                      f"reconnect Google in {_CONNECTIONS}.")
        on = arcfg.enable(cfg, google_sub=sub, account_email=address, now=now)
        stamp = _now().isoformat()
        # Whatever an earlier enablement left queued is from before this baseline.
        await skip_queued(db, owner)
        await db.set_autoreply_state(
            owner, history_id=history_id, google_sub=sub, account_email=address,
            enabled_at=on.enabled_at, own_addresses=own,
            # A failed alias lookup leaves the stamp empty: the next tick re-reads them.
            own_addresses_at=stamp if mailbox.aliases_ok else "",
            last_poll_at="", last_ok_at=stamp, last_error="")
        try:
            return await arcfg.save_autoreply_config(db, owner, on, now=now)
        except Exception:
            await db.delete_autoreply_state(owner)
            raise


# ── The loop ──────────────────────────────────────────────────────────────


async def autoreply_loop(stop: asyncio.Event, db: Any = None) -> None:
    """The engine: boot recovery, then every :func:`poll_seconds` a tick for
    every enabled owner (one at a time per owner), and the daily purge and
    drift check. Started by the Flight Deck lifespan unless the deck switch
    is off."""
    db = db if db is not None else auth.get_db()
    log.info("gmail autoreply loop started", poll_seconds=poll_seconds())
    recovered_once = False
    next_purge = 0.0
    while not stop.is_set():
        # Two decks can share one database: only the runner ticks, and only
        # the runner recovers — another deck's recovery would rewrite this
        # one's sends in flight.
        if await _hold_runner(db):
            if not recovered_once:
                recovered_once = True
                try:
                    recovered = await recover_on_start(db)
                    if recovered:
                        log.info("gmail autoreply: interrupted sends marked unknown",
                                 count=recovered)
                except Exception as exc:
                    log.warning("gmail autoreply recovery failed", error=type(exc).__name__)
            if time.monotonic() >= next_purge:
                next_purge = time.monotonic() + PURGE_EVERY_S
                await purge(db)
            try:
                await _spawn(db)
            except Exception as exc:
                log.warning("gmail autoreply loop iteration failed", error=type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds())
        except TimeoutError:
            pass
    await _drain()
    try:
        await db.release_autoreply_runner(os.getpid(), socket.gethostname())
    except Exception:
        pass
    log.info("gmail autoreply loop stopped")


async def _hold_runner(db: Any) -> bool:
    """Claim (or keep) the engine's single-runner row; a claim whose
    heartbeat is :data:`RUNNER_STALE_S` old is taken over. Never raises: an
    unreadable claim reads as not ours."""
    try:
        now = _now()
        # The heartbeat moves once per loop iteration: stale only after
        # several polls went by without one, whatever the poll interval.
        stale_s = max(RUNNER_STALE_S, 3 * poll_seconds() + 60)
        return await db.claim_autoreply_runner(
            os.getpid(), socket.gethostname(), now.isoformat(),
            (now - timedelta(seconds=stale_s)).isoformat())
    except Exception as exc:
        log.warning("gmail autoreply runner claim failed", error=type(exc).__name__)
        return False


async def run_once(db: Any) -> None:
    """One tick for every enabled owner, awaited (the loop doesn't wait)."""
    tasks = await _spawn(db)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _spawn(db: Any) -> list[asyncio.Task]:
    """Start a tick for each owner with a state row whose previous tick is
    done. Nothing at all while a deck switch is off: no poll, no claim (the
    age filter covers the gap)."""
    if _decks_off():
        return []
    rt = _rt()
    started: list[asyncio.Task] = []
    for st in await db.list_autoreply_states():
        owner = str(st.get("owner_id") or "")
        if not owner:
            continue
        lock = rt.owners.setdefault(owner, asyncio.Lock())
        if lock.locked():
            continue
        task = asyncio.create_task(_tick_locked(db, owner, lock))
        rt.tasks.add(task)
        task.add_done_callback(rt.tasks.discard)
        started.append(task)
    return started


async def _drain(timeout: float = 3.0) -> None:
    """At shutdown: let running ticks finish briefly, then cancel them (an
    interrupted send is recovered as ``unknown`` on the next boot)."""
    tasks = [t for t in _rt().tasks if not t.done()]
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()


async def _tick_locked(db: Any, owner: str, lock: asyncio.Lock) -> None:
    if lock.locked():
        return
    async with lock:
        try:
            await _tick_owner(db, owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("gmail autoreply tick failed", owner=owner, error=type(exc).__name__)


async def _state_update(db: Any, owner: str, state: Mapping[str, Any], **fields: Any) -> bool:
    """Write the owner's state row — only while it is still this enablement
    (a tick that outlives a turn-off never recreates it)."""
    return await db.update_autoreply_state(owner, if_enabled_at=str(state.get("enabled_at") or ""),
                                           **fields)


async def _still_on(db: Any, owner: str, state: Mapping[str, Any]) -> bool:
    current = await db.get_autoreply_state(owner)
    return current is not None and current.get("enabled_at") == state.get("enabled_at")


async def _tick_owner(db: Any, owner: str) -> None:
    """One owner's tick. Every precondition is re-read; any failure pauses
    this owner (and says why in ``last_error``)."""
    state = await db.get_autoreply_state(owner)
    if state is None or _decks_off():
        return
    cfg = await arcfg.load_autoreply_config(db, owner)
    if not cfg.enabled:
        # Under the config lock: an enabling PUT creates the state row a
        # moment before it stores the config, and must not lose it here.
        async with config_lock(owner):
            if not (await arcfg.load_autoreply_config(db, owner)).enabled:
                await db.delete_autoreply_state(owner)
                await skip_queued(db, owner)
        return
    try:
        _check_sub(cfg, await _google._load_user(db, owner), state)
        token = await _google.get_valid_google_access_token(owner)
        if not token:
            raise _TickStopError(GOOGLE_NOT_CONNECTED)
        missing = await missing_scopes(db, owner)
        if missing:
            await _state_update(db, owner, state,
                                last_error=f"{MISSING_SCOPES}:{','.join(missing)}")
            await _issue(db, owner, MISSING_SCOPES)
            return
        if not cfg.agent_ref or not cfg.enabled_topics:
            await _state_update(db, owner, state,
                                last_error=NO_AGENT if not cfg.agent_ref else NO_TOPICS)
            return
        await _verify_mailbox(owner, cfg, token)
        rt = _rt()
        async with httpx.AsyncClient(timeout=GMAIL_TIMEOUT_S) as client:
            async with rt.poll:
                state = await _refresh_own_addresses(db, owner, client, token, state)
                await _poll(db, owner, client, token, state)
            await _process(db, owner, client, cfg, state)
    except _TickStopError as stop:
        await _stopped(db, owner, cfg, state, stop.reason)


def _check_sub(cfg: arcfg.AutoReplyConfig, user: Mapping[str, Any] | None,
               state: Mapping[str, Any]) -> None:
    """The connected Google account (its stored ``sub``) must be the bound
    one — else :class:`_TickStopError` (``google_account_changed``)."""
    sub = str((user or {}).get("sub") or "")
    if not cfg.google_sub or sub != cfg.google_sub or state.get("google_sub") != cfg.google_sub:
        raise _TickStopError(GOOGLE_ACCOUNT_CHANGED)


async def _verify_mailbox(owner: str, cfg: arcfg.AutoReplyConfig, token: str) -> None:
    """Prove that *token* reads the bound mailbox (``cfg.account_email``) —
    the stored userinfo can be stale after a reconnect whose userinfo call
    failed. Once per access token (a refresh or a reconnect brings a new
    one), so about one profile read an hour. :class:`_TickStopError` when Gmail
    doesn't answer or the mailbox is another account."""
    rt = _rt()
    key = (cfg.enabled_at, hashlib.sha256(token.encode()).hexdigest())
    if rt.verified.get(owner) == key:
        return
    async with httpx.AsyncClient(timeout=GMAIL_TIMEOUT_S) as client:
        st, profile = await _gmail_get(client, token, "/profile")
    if st != 200:
        raise _TickStopError(f"gmail_{st}" if st else GMAIL_UNREACHABLE)
    if not _same_address(profile.get("emailAddress"), cfg.account_email):
        raise _TickStopError(GOOGLE_ACCOUNT_CHANGED)
    rt.verified[owner] = key


async def _row_token(db: Any, owner: str, cfg: arcfg.AutoReplyConfig,
                     state: Mapping[str, Any]) -> str:
    """A token for the next Gmail call of a row — read again right before
    it, never the tick's: a row's model work takes minutes, and the tick's
    token may have expired since (it is only refreshed in its last 5).
    Still the bound account, else :class:`_TickStopError`."""
    _check_sub(cfg, await _google._load_user(db, owner), state)
    token = await _google.get_valid_google_access_token(owner)
    if not token:
        raise _TickStopError(GOOGLE_NOT_CONNECTED)
    await _verify_mailbox(owner, cfg, token)
    return token


async def _stopped(db: Any, owner: str, cfg: arcfg.AutoReplyConfig,
                   state: Mapping[str, Any], reason: str) -> None:
    """What a stopped tick leaves: another account → switched off (this
    enablement) with a bell; no token → ``last_error`` and a bell (once a
    day); Gmail not answering → ``last_error``."""
    if reason == GOOGLE_ACCOUNT_CHANGED:
        if await _switch_off(db, owner, GOOGLE_ACCOUNT_CHANGED, enabled_at=cfg.enabled_at):
            await _issue(db, owner, GOOGLE_ACCOUNT_CHANGED)
        return
    await _state_update(db, owner, state, last_error=reason)
    if reason == GOOGLE_NOT_CONNECTED:
        await _issue(db, owner, GOOGLE_NOT_CONNECTED)


async def _refresh_own_addresses(db: Any, owner: str, client: httpx.AsyncClient, token: str,
                                 state: dict[str, Any]) -> dict[str, Any]:
    """Re-read the mailbox's own addresses every 24 hours. Only a complete
    read replaces them (a failed alias lookup keeps the known list, and the
    stale stamp retries next tick); a mailbox of another account is
    :class:`_TickStopError`."""
    at = _parse_iso(state.get("own_addresses_at"))
    if at is not None and _now() - at < OWN_ADDRESSES_TTL:
        return state
    mailbox = await _mailbox(client, token)
    if mailbox is None:
        return state
    _bound(mailbox, state)
    if not mailbox.aliases_ok:
        return state
    stamp = _now().isoformat()
    if await _state_update(db, owner, state, own_addresses=mailbox.own, own_addresses_at=stamp):
        return {**state, "own_addresses": mailbox.own, "own_addresses_at": stamp}
    return state


# ── Poll ──────────────────────────────────────────────────────────────────


async def _claim_all(db: Any, owner: str, found: list[tuple[str, str]]) -> None:
    """Claim *found* (oldest first) before any fetch or model call: the
    newest :data:`BURST_MAX` as ``new``, any older as ``skipped/burst``."""
    overflow = max(0, len(found) - BURST_MAX)
    for i, (mid, tid) in enumerate(found):
        if i < overflow:
            await db.claim_autoreply(owner, mid, tid, state="skipped", reason=BURST)
        else:
            await db.claim_autoreply(owner, mid, tid)
    if found and not await db.get_autoreply_state(owner):
        # Switched off while this poll ran: nothing will work on these.
        await skip_queued(db, owner)


def _added(data: Mapping[str, Any], seen: set[str], found: list[tuple[str, str]]) -> None:
    """The INBOX messages a ``history.list`` page added (not sent, drafts,
    spam or trash), deduplicated across the poll, in order."""
    for record in _list(data.get("history")):
        if not isinstance(record, dict):
            continue
        for added in _list(record.get("messagesAdded")):
            msg = (added or {}).get("message") if isinstance(added, dict) else None
            if not isinstance(msg, dict):
                continue
            mid = str(msg.get("id") or "")
            tid = str(msg.get("threadId") or "")
            labels = _labels(msg)
            if (not _ID_RE.match(mid) or mid in seen or "INBOX" not in labels
                    or labels & _POLL_SKIP_LABELS):
                continue
            seen.add(mid)
            found.append((mid, tid if _ID_RE.match(tid) else ""))


async def _poll(db: Any, owner: str, client: httpx.AsyncClient, token: str,
                state: Mapping[str, Any]) -> None:
    """``history.list`` from the cursor, up to :data:`HISTORY_MAX_PAGES`
    pages; claim every new INBOX id; then — only after the claims — move the
    cursor. 404 (an expired cursor) → :func:`_expired`. More pages than that
    (history is oldest first, so what was read is the OLDEST mail) → nothing
    read is claimed: :func:`_expired` claims the newest hour instead and
    re-baselines (``burst_truncated``, with a bell). Any other error keeps
    the cursor for the next tick."""
    now = _now().isoformat()
    start = str(state.get("history_id") or "")
    if not start:
        await _expired(db, owner, client, token, state, now)
        return
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    latest = ""
    page = ""
    for _page_no in range(HISTORY_MAX_PAGES):
        params: dict[str, Any] = {"startHistoryId": start, "historyTypes": "messageAdded",
                                  "labelId": "INBOX", "maxResults": HISTORY_PAGE_SIZE}
        if page:
            params["pageToken"] = page
        st, data = await _gmail_get(client, token, "/history", params)
        if st == 404:
            await _expired(db, owner, client, token, state, now)
            return
        if st != 200:
            await _state_update(db, owner, state, last_poll_at=now,
                                last_error=f"gmail_{st}" if st else GMAIL_UNREACHABLE)
            return
        latest = str(data.get("historyId") or latest)
        _added(data, seen, found)
        page = str(data.get("nextPageToken") or "")
        if not page:
            break
    if page:
        await _expired(db, owner, client, token, state, now, reason=BURST_TRUNCATED)
        return
    await _claim_all(db, owner, found)
    fields: dict[str, Any] = {"last_poll_at": now, "last_ok_at": now, "last_error": ""}
    if latest:
        fields["history_id"] = latest
    await _state_update(db, owner, state, **fields)


async def _expired(db: Any, owner: str, client: httpx.AsyncClient, token: str,
                   state: Mapping[str, Any], now: str, *, reason: str = HISTORY_EXPIRED) -> None:
    """The cursor's history can't be walked (expired, or more than a poll
    reads — *reason*): re-baseline. The profile's ``historyId`` is read
    FIRST, then the last hour of INBOX mail is claimed (bounded; the
    prefilter still enforces "not before enabling"), then the cursor moves
    to that ``historyId`` — so mail arriving in between is replayed by the
    next ``history.list`` (and claimed once). Should either read fail, the
    cursor stays and the next tick tries again. A mailbox of another
    account is :class:`_TickStopError`. One bell a day."""
    mailbox = await _mailbox(client, token)
    st, data = 0, {}
    if mailbox is not None:
        _bound(mailbox, state)
        st, data = await _gmail_get(client, token, "/messages",
                                    {"q": BACKFILL_QUERY, "maxResults": BACKFILL_MAX})
    if mailbox is not None and st == 200:
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for msg in reversed(_list(data.get("messages"))[:BACKFILL_MAX]):  # oldest first
            mid = str((msg or {}).get("id") or "") if isinstance(msg, dict) else ""
            tid = str(msg.get("threadId") or "") if isinstance(msg, dict) else ""
            if _ID_RE.match(mid) and mid not in seen:
                seen.add(mid)
                found.append((mid, tid if _ID_RE.match(tid) else ""))
        await _claim_all(db, owner, found)
        await _state_update(db, owner, state, history_id=mailbox.history_id, last_poll_at=now,
                            last_ok_at=now, last_error=reason)
    else:
        await _state_update(db, owner, state, last_poll_at=now, last_error=reason)
    await _issue(db, owner, reason)


# ── Process ───────────────────────────────────────────────────────────────


async def _process(db: Any, owner: str, client: httpx.AsyncClient,
                   cfg: arcfg.AutoReplyConfig, state: Mapping[str, Any],
                   budget: int = PROCESS_BUDGET) -> None:
    """Work the owner's queue, oldest first: rows waiting for the agent, then
    new ones — at most *budget* rows that do real work. Stops as soon as the
    owner is switched off (auto-pause, a route). Every row reads its own
    token right before its Gmail calls (:func:`_row_token`)."""
    budget = await _process_waiting(db, owner, client, cfg, state, budget)
    if budget <= 0:
        return
    for row in await db.next_autoreplies(owner, "new", limit=budget):
        if not await _still_on(db, owner, state):
            return
        await _guarded(db, owner, row, _handle_new(db, owner, client, cfg, state, row))


async def _guarded(db: Any, owner: str, row: Mapping[str, Any], work: Awaitable[None]) -> None:
    """Run one row's work; an unexpected error fails the row (it is never
    retried — nothing was sent from any state it can be in here). A
    :class:`_TickStopError` ends the tick: the row's work left it retryable."""
    try:
        await work
    except asyncio.CancelledError:
        raise
    except _TickStopError:
        await _settle_spine(db, owner, row["id"])
        raise
    except Exception as exc:
        log.warning("gmail autoreply row failed", owner=owner, row=row.get("id"),
                    error=type(exc).__name__)
        try:
            await db.cas_autoreply(row["id"], owner, _PRE_SEND, "failed",
                                   reason=INTERNAL_ERROR, detail=type(exc).__name__,
                                   reply_text="")
        except Exception:
            pass
    await _settle_spine(db, owner, row["id"])


def _retry_count(counter: dict[str, int], rid: str) -> int:
    """This failure's number for row *rid* in a per-stage retry *counter*."""
    if len(counter) > 10_000:       # rows that left without a word; never grows unbounded
        counter.clear()
    counter[rid] = counter.get(rid, 0) + 1
    return counter[rid]


def _row_ms(row: Mapping[str, Any]) -> int:
    """When the row's email arrived (its claim when unknown), epoch ms; 0
    when neither is readable."""
    return (gmail_inbound.iso_to_ms(row.get("received_at"))
            or gmail_inbound.iso_to_ms(row.get("created_at")))


async def _process_waiting(db: Any, owner: str, client: httpx.AsyncClient,
                           cfg: arcfg.AutoReplyConfig, state: Mapping[str, Any],
                           budget: int) -> int:
    """``waiting_agent`` rows. An hour after the email arrived the row ends,
    and only a stopped agent rings the stopped-agent bell: an email from
    before this enablement → ``skipped/before_enable``; the agent still
    stopped → ``failed/agent_offline`` (one bell a day); the agent gone or
    unusable → ``failed/<why>`` with its bell; the agent running (the row
    waited on something else — Google, Gmail) → ``skipped/stale``, quietly.
    Younger: wait while the agent is stopped, resume once it runs (within
    *budget*). Returns the budget left."""
    rows = await db.next_autoreplies(owner, "waiting_agent", limit=WAITING_SCAN)
    if not rows:
        return budget
    available = await writer.resolve_writer(db, owner, cfg.agent_ref)
    enabled_ms = _enabled_ms(cfg, state)
    for row in rows:
        if not await _still_on(db, owner, state):
            return 0
        rid = row["id"]
        if _row_age_s(row) > AGENT_WAIT_S:
            await _expire_waiting(db, owner, cfg, row, available, enabled_ms)
            continue
        if isinstance(available, str):
            if available not in writer.WAIT_REASONS:
                if await db.cas_autoreply(rid, owner, "waiting_agent", "failed",
                                          reason=available):
                    await _issue(db, owner, available, *_AGENT_ISSUE)
                await _settle_spine(db, owner, rid)
            continue
        if budget <= 0:
            continue
        budget -= 1
        await _guarded(db, owner, row, _resume(db, owner, client, cfg, state, row))
    return budget


async def _expire_waiting(db: Any, owner: str, cfg: arcfg.AutoReplyConfig,
                          row: Mapping[str, Any], available: Any, enabled_ms: int) -> None:
    """A ``waiting_agent`` row an hour old ends (see :func:`_process_waiting`)
    and gives its email back to Autonomous Work."""
    rid = row["id"]
    if not enabled_ms or _row_ms(row) < enabled_ms:
        await db.cas_autoreply(rid, owner, "waiting_agent", "skipped", reason=BEFORE_ENABLE)
    elif isinstance(available, str) and available in writer.WAIT_REASONS:
        if await db.cas_autoreply(rid, owner, "waiting_agent", "failed", reason=AGENT_OFFLINE):
            name = await _agent_name(owner, cfg.agent_ref)
            await _issue(db, owner, AGENT_OFFLINE,
                         f"{name} is stopped — topic emails are waiting for you",
                         "Emails it could have answered within an hour are left for "
                         "you in Gmail. Flight Deck never starts the agent itself.")
    elif isinstance(available, str):
        if await db.cas_autoreply(rid, owner, "waiting_agent", "failed", reason=available):
            await _issue(db, owner, available, *_AGENT_ISSUE)
    else:
        await db.cas_autoreply(rid, owner, "waiting_agent", "skipped", reason=STALE)
    await _settle_spine(db, owner, rid)


async def _fetch_inbound(db: Any, owner: str, client: httpx.AsyncClient, token: str,
                         row: Mapping[str, Any], from_state: str) -> gmail_inbound.Inbound | None:
    """The row's email, ``format=full``, parsed — and its from / subject /
    thread written to the row. A 404 → ``skipped/gone``. Any other failure
    is never treated as a message: a ``new`` row retries (``failed/fetch``
    after :data:`FETCH_ATTEMPTS` fetch failures — its own budget, not the
    classifier's), a waiting one keeps waiting."""
    rid, mid = row["id"], row["inbound_message_id"]
    rt = _rt()
    st, msg = (await _gmail_get(client, token, f"/messages/{mid}", {"format": "full"})
               if _ID_RE.match(mid) else (404, {}))
    if st in (400, 404):
        rt.fetch_failures.pop(rid, None)
        await db.cas_autoreply(rid, owner, from_state, "skipped", reason=GONE)
        return None
    if st != 200 or str(msg.get("id") or "") != mid:
        if from_state == "new":
            if _retry_count(rt.fetch_failures, rid) >= FETCH_ATTEMPTS:
                rt.fetch_failures.pop(rid, None)
                if await db.cas_autoreply(rid, owner, "new", "failed", reason=FETCH,
                                          detail=f"gmail {st}"):
                    await _issue(db, owner, FETCH)
            else:
                await db.cas_autoreply(rid, owner, "new", "new", detail=f"gmail {st}")
        return None
    rt.fetch_failures.pop(rid, None)
    inb = await asyncio.to_thread(gmail_inbound.parse_message, msg)
    sender = gmail_inbound.sender_address(inb) or (inb.from_addrs[0] if inb.from_addrs else "")
    await db.update_autoreply(
        rid, owner, thread_id=inb.thread_id or row.get("thread_id") or "",
        received_at=_ms_iso(inb.internal_ms), from_addr=sender,
        from_domain=sender.rpartition("@")[2], subject=inb.subject)
    return inb


def _prefilter(inb: gmail_inbound.Inbound, cfg: arcfg.AutoReplyConfig,
               state: Mapping[str, Any]) -> tuple[str, str]:
    return gmail_inbound.prefilter(inb, own_addresses=_own(state, cfg),
                                   enabled_at_ms=_enabled_ms(cfg, state), now_ms=_now_ms())


async def _cap(db: Any, owner: str, cfg: arcfg.AutoReplyConfig, thread_id: str, sender: str, *,
               flood: bool) -> str:
    """Check 10, from the ledger (``sending`` / ``sent`` / ``unknown`` rows):
    the thread (1 a day, 3 ever), the sender (3 a day), the daily limit — and
    with *flood*, the sender flood: more than 10 emails in an hour from a
    sender that PROVED its address (rows skipped at checks 1–6 are forged
    From claims as far as anyone knows, and never count) → ``cap_flood``,
    and for 24 hours from that email — not from the latest one, so a
    sender who keeps writing isn't suppressed for good — ``flood_suppressed``.
    "" when within every cap."""
    day = _since(hours=24)
    if flood and sender:
        if await db.count_autoreplies(owner, day, "skipped", from_addr=sender,
                                      reasons=(CAP_FLOOD,), arrived_only=True):
            return FLOOD_SUPPRESSED
        hour = _since(hours=1)
        # Arrivals only: a held card dismissed (or expired) today is not mail
        # that arrived today.
        inbound = await db.count_autoreplies(owner, hour, None, from_addr=sender,
                                             arrived_only=True)
        unverified = await db.count_autoreplies(owner, hour, "skipped", from_addr=sender,
                                                reasons=_UNVERIFIED, arrived_only=True)
        if inbound - unverified > FLOOD_PER_HOUR:
            return CAP_FLOOD
    if not thread_id:
        return CAP_THREAD  # a reply that can't be counted per thread isn't sent
    if await db.count_autoreplies(owner, day, AUTOREPLY_SEND_STATES,
                                  thread_id=thread_id) >= THREAD_CAP_DAY:
        return CAP_THREAD
    if await db.count_autoreplies(owner, "", AUTOREPLY_SEND_STATES,
                                  thread_id=thread_id) >= THREAD_CAP_TOTAL:
        return CAP_THREAD
    if not sender or await db.count_autoreplies(owner, day, AUTOREPLY_SEND_STATES,
                                                from_addr=sender) >= SENDER_CAP_DAY:
        return CAP_SENDER
    if await db.count_autoreplies(owner, day, AUTOREPLY_SEND_STATES) >= cfg.daily_limit:
        return CAP_DAILY
    return ""


async def _handle_new(db: Any, owner: str, client: httpx.AsyncClient,
                      cfg: arcfg.AutoReplyConfig, state: Mapping[str, Any],
                      row: Mapping[str, Any]) -> None:
    """A claimed email: fetch, checks 1–10 (no model has run yet), the
    classification budget, the classifier — then, the topic accepted, the
    spine claim and :func:`_after_match`."""
    rid = row["id"]
    token = await _row_token(db, owner, cfg, state)        # _TickStopError: the row stays new
    inb = await _fetch_inbound(db, owner, client, token, row, "new")
    if inb is None:
        return
    code, detail = _prefilter(inb, cfg, state)
    if code:
        await db.cas_autoreply(rid, owner, "new", "skipped", reason=code, detail=detail)
        return
    sender = gmail_inbound.sender_address(inb)
    cap = await _cap(db, owner, cfg, inb.thread_id, sender, flood=True)
    if cap:
        if await db.cas_autoreply(rid, owner, "new", "skipped", reason=cap) and cap == CAP_DAILY:
            await _issue(db, owner, CAP_DAILY)
        return
    if not await db.cas_autoreply(rid, owner, "new", "matching", detail=detail):
        return
    if not await db.incr_classify_budget(owner, _now().date().isoformat(), classify_per_day()):
        if not await _still_on(db, owner, state):
            # Turned off since (no state row to count in): that, not a spent budget.
            await db.cas_autoreply(rid, owner, "matching", "skipped", reason=TURNED_OFF)
        elif await db.cas_autoreply(rid, owner, "matching", "skipped", reason=CAP_CLASSIFY):
            await _issue(db, owner, CAP_CLASSIFY)
        return
    async with _rt().llm:
        match = await judges.classify(owner, inb, cfg.enabled_topics, db=db, stage="match")
    failures = _rt().classify_failures
    if match.failed:
        retry = (_retry_count(failures, rid) < CLASSIFY_ATTEMPTS
                 and _now_ms() - inb.internal_ms <= CLASSIFY_RETRY_S * 1000)
        if retry:
            await db.cas_autoreply(rid, owner, "matching", "new", detail=match.detail)
            return
        failures.pop(rid, None)
        if await db.cas_autoreply(rid, owner, "matching", "failed",
                                  reason=judges.CLASSIFIER_UNAVAILABLE, detail=match.detail):
            await _issue(db, owner, judges.CLASSIFIER_UNAVAILABLE)
        return
    failures.pop(rid, None)
    # The drift breaker reads match_judge (a classification ran) and
    # topic_id (set only for an accepted topic).
    leaned = cfg.topic(match.topic_id or match.candidate)
    fields = {"match_judge": match.source, "confidence": match.confidence,
              "detail": "; ".join(d for d in (detail, match.detail) if d),
              "topic_id": match.topic_id, "topic_name": leaned.name if leaned else ""}
    if not match.accepted:
        await db.cas_autoreply(rid, owner, "matching", "skipped",
                               reason=match.reason or judges.NO_MATCH, **fields)
        return
    topic = cfg.topic(match.topic_id)
    if topic is None or not topic.enabled:
        await db.cas_autoreply(rid, owner, "matching", "skipped", reason=TOPIC_OFF,
                               **{**fields, "topic_id": ""})
        return
    await db.update_autoreply(rid, owner, **fields)
    await _after_match(db, owner, client, cfg, state, row, inb, topic, "matching")


async def _resume(db: Any, owner: str, client: httpx.AsyncClient,
                  cfg: arcfg.AutoReplyConfig, state: Mapping[str, Any],
                  row: Mapping[str, Any]) -> None:
    """A ``waiting_agent`` row whose agent runs again: the email re-fetched
    and re-checked (its labels may have changed), its topic still on, the
    caps — then :func:`_after_match`."""
    rid = row["id"]
    token = await _row_token(db, owner, cfg, state)        # _TickStopError: the row keeps waiting
    inb = await _fetch_inbound(db, owner, client, token, row, "waiting_agent")
    if inb is None:
        return
    code, detail = _prefilter(inb, cfg, state)
    if code:
        await db.cas_autoreply(rid, owner, "waiting_agent", "skipped", reason=code, detail=detail)
        return
    topic = cfg.topic(str(row.get("topic_id") or ""))
    if topic is None or not topic.enabled:
        await db.cas_autoreply(rid, owner, "waiting_agent", "skipped", reason=TOPIC_OFF)
        return
    cap = await _cap(db, owner, cfg, inb.thread_id, gmail_inbound.sender_address(inb),
                     flood=False)
    if cap:
        await db.cas_autoreply(rid, owner, "waiting_agent", "skipped", reason=cap)
        return
    await _after_match(db, owner, client, cfg, state, row, inb, topic, "waiting_agent")


async def _after_match(db: Any, owner: str, client: httpx.AsyncClient,
                       cfg: arcfg.AutoReplyConfig, state: Mapping[str, Any],
                       row: Mapping[str, Any], inb: gmail_inbound.Inbound,
                       topic: arcfg.Topic, from_state: str) -> None:
    """A matched email: check 11 on the thread, the writer agent (stopped →
    ``waiting_agent``), the reply, then the guard."""
    rid = row["id"]
    try:
        token = await _row_token(db, owner, cfg, state)    # after the classifier's minutes
    except _TickStopError:
        if from_state == "matching":
            # Nothing was written yet: back in the queue (a waiting row keeps waiting).
            await db.cas_autoreply(rid, owner, "matching", "new")
        raise
    thread = await _thread(client, token, inb.thread_id)
    if thread is None:
        if await db.cas_autoreply(rid, owner, from_state, "failed", reason=THREAD_UNREADABLE):
            await _issue(db, owner, THREAD_UNREADABLE)
        return
    takeover = await _takeover(db, owner, inb.thread_id, thread, row["inbound_message_id"])
    if takeover:
        await db.cas_autoreply(rid, owner, from_state, "skipped", reason=takeover)
        return
    # The engine works on this email from now on: claim it on the event
    # spine at once — not after the send minutes later — so the arbiter
    # never turns it into a reply proposal of its own (PR #56) meanwhile.
    # Provisional: _settle_spine releases it if the row ends unhandled.
    await _claim_spine(owner, row["inbound_message_id"])
    resolved = await writer.resolve_writer(db, owner, cfg.agent_ref)
    if isinstance(resolved, str):
        if resolved in writer.WAIT_REASONS:
            if from_state != "waiting_agent":
                await db.cas_autoreply(rid, owner, from_state, "waiting_agent", reason=resolved)
            return
        if await db.cas_autoreply(rid, owner, from_state, "failed", reason=resolved):
            await _issue(db, owner, resolved, *_AGENT_ISSUE)
        return
    rec, shared = resolved
    if not await db.cas_autoreply(rid, owner, from_state, "writing", reason="",
                                  agent_ref=rec.ref, agent_name=rec.name):
        return
    async with _rt().llm:
        result = await writer.compose_reply(
            db, owner, cfg, topic, inb, _list(thread.get("messages")), rec, shared,
            own_addresses=_own(state, cfg), now=_now())
    await _log_writer_usage(db, owner, result)
    sources = list(result.sources_used)
    if result.state == writer.REPLY:
        await _check(db, owner, cfg, state, row, inb, topic, rec, result)
    elif result.state == writer.SKIP:
        await db.cas_autoreply(rid, owner, "writing", "skipped", reason=writer.WRITER_SKIP,
                               detail=result.detail, sources_used=sources)
    elif result.state == writer.BLOCKED:
        if await db.cas_autoreply(rid, owner, "writing", "blocked", reason=result.reason,
                                  findings=list(result.findings), sources_used=sources,
                                  reply_text=""):
            await _card_bell(db, owner, BELL_BLOCKED, f"Auto-reply blocked: {topic.name}",
                             "It would have contained hidden formatting characters. "
                             "Nothing was sent.", rid)
            await _claim_spine(owner, row["inbound_message_id"])
            await _check_pause(db, owner, cfg.enabled_at)
    else:
        reason = result.reason or writer.WRITER_UNAVAILABLE
        if await db.cas_autoreply(rid, owner, "writing", "failed", reason=reason,
                                  sources_used=sources) and reason in _ISSUES:
            await _issue(db, owner, reason)


async def _block(db: Any, owner: str, row: Mapping[str, Any], topic: arcfg.Topic,
                 verdict: outbound_guard.Verdict, from_state: str, enabled_at: str, *,
                 material_kinds: frozenset[str] = frozenset()) -> None:
    """→ ``blocked/guard:<kinds>`` with no text kept, the masked findings, a
    bell, the spine claim — then the auto-pause count. *material_kinds* are
    the block kinds the writer's own material (brief, excerpts, background)
    holds: a finding of one of those is marked ``from_material``."""
    findings = [dict(f, from_material=f.get("kind") in material_kinds)
                for f in verdict.as_dicts()]
    if not await db.cas_autoreply(row["id"], owner, from_state, "blocked",
                                  reason=GUARD_PREFIX + ",".join(verdict.kinds),
                                  detail=outbound_guard.describe_kinds(verdict.kinds),
                                  findings=findings, reply_text=""):
        return
    await _card_bell(db, owner, BELL_BLOCKED, f"Auto-reply blocked: {topic.name}",
                     f"It would have contained "
                     f"{outbound_guard.describe_kinds(verdict.kinds)}. Nothing was sent.",
                     row["id"])
    await _claim_spine(owner, row["inbound_message_id"])
    await _check_pause(db, owner, enabled_at)


async def _check(db: Any, owner: str, cfg: arcfg.AutoReplyConfig,
                 state: Mapping[str, Any], row: Mapping[str, Any], inb: gmail_inbound.Inbound,
                 topic: arcfg.Topic, rec: agent_sharing.AgentRecord,
                 result: writer.WriterResult) -> None:
    """The guard: L1 first — a block → ``blocked`` (no text kept), a hold →
    ``held``, and either way no judge sees it; a clean reply goes to the L2
    judge (a flag or no judge → ``held``); a pass → ``approved`` → send."""
    rid = row["id"]
    if not await db.cas_autoreply(rid, owner, "writing", "checking",
                                  sources_used=list(result.sources_used)):
        return
    signature = await mail_signature.load(db, owner, mail_signature.GOOGLE_KEY)
    try:
        private = await writer.load_private_corpus(db, owner)
    except Exception:
        if await db.cas_autoreply(rid, owner, "checking", "failed", reason=GUARD_ERROR):
            await _issue(db, owner, GUARD_ERROR)
        return
    verdict = result.scan(never_disclose=cfg.never_disclose, own_addresses=_own(state, cfg),
                          signature=signature, private_corpus=private,
                          extra_prompt_texts=judges.STATIC_PROMPTS)
    if verdict.level == outbound_guard.BLOCK:
        leaked = await asyncio.to_thread(
            _from_material, result.body, result.background, cfg.never_disclose, private)
        await _block(db, owner, row, topic, verdict, "checking", cfg.enabled_at,
                     material_kinds=leaked)
        return
    if verdict.level == outbound_guard.HOLD:
        await _hold(db, owner, row, topic, GUARD_PREFIX + ",".join(verdict.kinds),
                    reply_text=result.body, findings=verdict.as_dicts())
        return
    shareable = tuple(t for t in (*result.excerpts, signature) if t)
    async with _rt().llm:
        judged = await judges.judge(owner, topic, shareable, result.background, inb,
                                    result.body, thread=result.thread, db=db)
    if not judged.passed:
        await _hold(db, owner, row, topic, judged.reason, reply_text=result.body,
                    judge=judged.to_json())
        return
    if not await db.cas_autoreply(rid, owner, "checking", "approved", reply_text=result.body,
                                  judge=judged.to_json()):
        return
    await _send(db, owner, cfg, row, inb, topic, rec, result, state)


async def _hold(db: Any, owner: str, row: Mapping[str, Any], topic: arcfg.Topic, reason: str,
                *, from_states: tuple[str, ...] = ("checking",), **fields: Any) -> None:
    """→ ``held/<reason>`` with the text kept for the owner's card; bell; spine."""
    if not await db.cas_autoreply(row["id"], owner, from_states, "held", reason=reason, **fields):
        return
    current = await db.get_autoreply(owner, row["id"]) or row
    await _card_bell(db, owner, BELL_HELD, f"Auto-reply waiting for you: {topic.name}",
                     f"From {current.get('from_addr') or '?'} — "
                     f"{current.get('subject') or '(no subject)'} — {_reason_label(reason)}",
                     row["id"])
    await _claim_spine(owner, row["inbound_message_id"])


async def _check_pause(db: Any, owner: str, enabled_at: str) -> None:
    """Auto-pause: :data:`PAUSE_BLOCKS` replies blocked in 24 hours for
    owner-held knowledge the writer brought (:func:`_leak_evidence` — not the
    sender's own text, nothing a sender can make it synthesise) switch the
    owner off (the enablement *enabled_at* — not one made since). Blocks from
    before the owner last turned auto-replies on don't count: re-enabling
    starts afresh."""
    since = max(_since(hours=24), (_parse_iso(enabled_at) or datetime.min.replace(tzinfo=UTC))
                .astimezone(UTC).isoformat())
    # Every blocked row of the window (bounded by the daily classification
    # budget), not the newest few: blocks that aren't evidence must not push
    # the ones that are out of the count.
    rows = await db.list_autoreplies(owner, limit=PAUSE_SCAN_ROWS, include_quiet=True,
                                     states="blocked", since_iso=since)
    if sum(1 for r in rows if _leak_evidence(r.get("findings"))) < PAUSE_BLOCKS:
        return
    if await _switch_off(db, owner, AUTO_PAUSED, enabled_at=enabled_at):
        await _issue(db, owner, AUTO_PAUSED)


# ── Send ──────────────────────────────────────────────────────────────────

# How the send core words its own daily-limit 429 (Gmail's rate limit is a 429 too).
_LIMIT_PREFIX = gs._limit_refusal(0, 0, "autoreply").split(":", 1)[0]


async def _send_gate(db: Any, owner: str, cfg: arcfg.AutoReplyConfig, state: Mapping[str, Any],
                     topic: arcfg.Topic, inb: gmail_inbound.Inbound,
                     result: writer.WriterResult, found: list[outbound_guard.Verdict]) -> str:
    """``before_send``'s re-check, under the owner's send lock. "" when the
    send may go, else why not:

    * the deck switches;
    * the SAME enablement the reply was made under (``turned_off``
      otherwise): the stored config and state row still on with the tick's
      ``enabled_at`` — a pause and a quick re-enable is a fresh baseline,
      and nothing from before it is sent — the topic still on, the same
      agent, the same Google account;
    * the topic's brief unchanged (``brief_changed`` — the reply was written
      and judged against the old one);
    * the guard's L1 again, with the current never-disclose list and
      profile (``guard:<kinds>``, the verdict appended to *found*;
      ``guard_error`` when the profile can't be read);
    * the thread / sender / daily caps."""
    if _decks_off():
        return TURNED_OFF
    current = await arcfg.load_autoreply_config(db, owner)
    live = current.topic(topic.id)
    stored = await db.get_autoreply_state(owner)
    if (not current.enabled or not current.enabled_at or current.enabled_at != cfg.enabled_at
            or stored is None or stored.get("enabled_at") != state.get("enabled_at")
            or live is None or not live.enabled
            or current.agent_ref != cfg.agent_ref or not current.google_sub
            or current.google_sub != cfg.google_sub
            or stored.get("google_sub") != current.google_sub):
        return TURNED_OFF
    if live.brief != topic.brief:
        return BRIEF_CHANGED
    try:
        private = await writer.load_private_corpus(db, owner)
    except Exception:
        return GUARD_ERROR
    signature = await mail_signature.load(db, owner, mail_signature.GOOGLE_KEY)
    verdict = result.scan(never_disclose=current.never_disclose,
                          own_addresses=_own(state, cfg), signature=signature,
                          private_corpus=private, extra_prompt_texts=judges.STATIC_PROMPTS)
    if verdict.level != outbound_guard.PASS:
        found.append(verdict)
        return GUARD_PREFIX + ",".join(verdict.kinds)
    return await _cap(db, owner, current, inb.thread_id, gmail_inbound.sender_address(inb),
                      flood=False)


async def _send(db: Any, owner: str, cfg: arcfg.AutoReplyConfig,
                row: Mapping[str, Any], inb: gmail_inbound.Inbound, topic: arcfg.Topic,
                rec: agent_sharing.AgentRecord, result: writer.WriterResult,
                state: Mapping[str, Any]) -> None:
    """Send an ``approved`` reply to the verified sender — never retried once
    ``before_send`` took the commit point (``approved → sending``). The
    token is read right here, not taken from the tick: the reply's model
    work took minutes. No token (or another account) → the row goes back
    to ``new``; nothing went out."""
    rid, mid = row["id"], row["inbound_message_id"]
    reply = result.body
    if _decks_off():
        await db.cas_autoreply(rid, owner, "approved", "skipped", reason=TURNED_OFF)
        return
    if not await mail_signature.load(db, owner, mail_signature.GOOGLE_KEY):
        await _hold(db, owner, row, topic, NO_SIGNATURE, from_states=("approved",))
        return
    age = gmail_inbound.age_blocker(inb, enabled_at_ms=_enabled_ms(cfg, state),
                                    now_ms=_now_ms(), max_age_s=SEND_MAX_AGE_S)
    if age:
        await db.cas_autoreply(rid, owner, "approved", "skipped", reason=age)
        return
    try:
        token = await _row_token(db, owner, cfg, state)
    except _TickStopError:
        await db.cas_autoreply(rid, owner, "approved", "new", reply_text="", judge={})
        raise
    # Check 1 again: the owner may have trashed the email, or reported it as
    # spam, in the minutes the reply took. Unreadable → nothing is sent.
    async with httpx.AsyncClient(timeout=20) as client:
        st, current = await _gmail_get(client, token, f"/messages/{mid}", {"format": "minimal"})
    if st in (400, 404):
        await db.cas_autoreply(rid, owner, "approved", "skipped", reason=GONE)
        return
    labels = _labels(current)
    if st != 200:
        if await db.cas_autoreply(rid, owner, "approved", "failed", reason=FETCH,
                                  detail=f"gmail {st}"):
            await _issue(db, owner, FETCH)
        return
    if "INBOX" not in labels or labels & {"SPAM", "TRASH"}:
        await db.cas_autoreply(rid, owner, "approved", "skipped", reason="labels",
                               detail="moved out of the inbox while the reply was written")
        return
    stop: list[str] = []
    found: list[outbound_guard.Verdict] = []
    committed = False

    async def before_send() -> None:
        nonlocal committed
        reason = await _send_gate(db, owner, cfg, state, topic, inb, result, found)
        if not reason and not await db.cas_autoreply(rid, owner, "approved", "sending"):
            reason = ALREADY_HANDLED
        if reason:
            stop.append(reason)
            raise HTTPException(status_code=403 if reason == TURNED_OFF else 409,
                                detail=f"Auto-reply stopped before sending: {reason}.")
        committed = True

    agent = rec.name or rec.slug or "Your agent"
    try:
        out = await gs.send_autoreply(
            db, owner, token=token, inbound_message_id=mid,
            to_addr=gmail_inbound.sender_address(inb),
            subject_fallback="" if inb.subject.strip() else "Re: (no subject)",
            body=reply, agent_label=agent, topic_id=topic.id, daily_limit=cfg.daily_limit,
            origin="autoreply", before_send=before_send)
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        if stop and not committed:
            await _stopped_send(db, owner, row, topic, stop[0], found, cfg.enabled_at)
        else:
            await _send_refused(db, owner, row, agent, exc)
        return
    except Exception as exc:
        log.warning("gmail autoreply send failed", owner=owner, error=type(exc).__name__)
        if committed:
            await _unknown(db, owner, row, agent)
        elif await db.cas_autoreply(rid, owner, "approved", "failed", reason=SEND_ERROR,
                                    detail=type(exc).__name__):
            await _issue(db, owner, SEND_ERROR)
        return
    await db.cas_autoreply(rid, owner, "sending", "sent", reason="",
                           sent_message_id=str(out.get("message_id") or ""),
                           reply_text=str(out.get("body") or reply))
    await _bell(db, owner, BELL_SENT, f"{agent} auto-replied: {topic.name}",
                f"To {out.get('to') or ''} — {out.get('subject') or ''}", rid)
    await _claim_spine(owner, mid)


async def _unknown(db: Any, owner: str, row: Mapping[str, Any], agent: str) -> None:
    """Gmail may have sent it: ``unknown``, a bell, the spine claim — and
    never a retry."""
    if await db.cas_autoreply(row["id"], owner, ("sending", "approved"), "unknown",
                              reason=SEND_UNCONFIRMED):
        await _issue(db, owner, SEND_UNCONFIRMED, f"{agent} may have auto-replied — check Sent",
                     "Gmail didn't confirm the reply. It is never sent again.",
                     ref_id=row["id"])
        await _claim_spine(owner, row["inbound_message_id"])


async def _stopped_send(db: Any, owner: str, row: Mapping[str, Any], topic: arcfg.Topic,
                        reason: str, found: list[outbound_guard.Verdict],
                        enabled_at: str) -> None:
    """Our own ``before_send`` stopped the send (nothing went out): a changed
    brief or a re-check hold → ``held`` for the owner; a re-check block →
    ``blocked``; an unreadable profile → ``failed/guard_error``; else
    ``skipped/<why>`` (with the daily-limit bell)."""
    rid = row["id"]
    if reason == BRIEF_CHANGED:
        await _hold(db, owner, row, topic, BRIEF_CHANGED, from_states=("approved",))
    elif reason == GUARD_ERROR:
        if await db.cas_autoreply(rid, owner, "approved", "failed", reason=GUARD_ERROR,
                                  reply_text=""):
            await _issue(db, owner, GUARD_ERROR)
    elif reason.startswith(GUARD_PREFIX) and found:
        verdict = found[0]
        if verdict.level == outbound_guard.BLOCK:
            await _block(db, owner, row, topic, verdict, "approved", enabled_at)
        else:
            await _hold(db, owner, row, topic, reason, from_states=("approved",),
                        findings=verdict.as_dicts())
    elif await db.cas_autoreply(rid, owner, "approved", "skipped",
                                reason=reason) and reason == CAP_DAILY:
        await _issue(db, owner, CAP_DAILY)


async def _send_refused(db: Any, owner: str, row: Mapping[str, Any], agent: str,
                        exc: HTTPException) -> None:
    """Map a send the core or Gmail refused to the ledger:
    ``X-FD-Gmail-Send-Outcome: unknown`` → ``unknown``; a repeat or duplicate
    (409) → ``skipped/already_answered``; the core's OWN daily limit (its
    429) → ``cap_daily``; the deck's send switch (its 403) → ``turned_off``;
    404 → ``gone``; 400 → ``failed/build_error``; anything else — Gmail's
    rate limit or refusal too, which no limit or switch of the owner's
    explains — → ``failed/send_error`` with its bell. Nothing went out in
    any case but the first."""
    headers = exc.headers or {}
    if headers.get(gmail_compose.SEND_OUTCOME_HEADER) == "unknown":
        await _unknown(db, owner, row, agent)
        return
    status = int(exc.status_code)
    if status == 429:
        own_limit = str(exc.detail or "").startswith(_LIMIT_PREFIX)
        to_state, reason = ("skipped", CAP_DAILY) if own_limit else ("failed", SEND_ERROR)
    elif status == 403:
        deck = bool(headers.get(gmail_compose.SEND_REFUSED_HEADER))
        to_state, reason = ("skipped", TURNED_OFF) if deck else ("failed", SEND_ERROR)
    else:
        to_state, reason = {
            409: ("skipped", ALREADY_ANSWERED),
            404: ("skipped", GONE),
            400: ("failed", BUILD_ERROR),
        }.get(status, ("failed", SEND_ERROR))
    moved = await db.cas_autoreply(row["id"], owner, ("approved", "sending"), to_state,
                                   reason=reason, detail=f"gmail send {status}")
    if moved and reason in (CAP_DAILY, BUILD_ERROR, SEND_ERROR):
        await _issue(db, owner, reason)


# ── Boot recovery, retention, drift ───────────────────────────────────────


async def recover_on_start(db: Any) -> int:
    """Once before the first tick: an interrupted ``sending`` → ``unknown``
    with a bell, never resent; interrupted work requeued or failed; an owner-
    approved reply back to held (``db.recover_autoreplies``). Returns how many
    sends became ``unknown``."""
    interrupted = await db.interrupted_autoreplies()
    rows = await db.recover_autoreplies()
    for row in interrupted:     # failed 'interrupted' now: give the email back
        await _release_spine(str(row.get("owner_id") or ""),
                             str(row.get("inbound_message_id") or ""))
    # Requeued rows of owners who are off now would sit 'new' for good (no
    # tick runs for them): close their queue.
    on = {str(st.get("owner_id") or "") for st in await db.list_autoreply_states()}
    for owner in await db.autoreply_queue_owners():
        if owner not in on:
            await skip_queued(db, owner)
    for row in rows:
        owner = str(row.get("owner_id") or "")
        agent = str(row.get("agent_name") or "") or "Your agent"
        await _issue(db, owner, SEND_UNCONFIRMED, f"{agent} may have auto-replied — check Sent",
                     f"To {row.get('from_addr') or '?'} — {row.get('subject') or ''}. Flight "
                     "Deck stopped while it was sending; it is never sent again.",
                     ref_id=str(row.get("id") or ""))
        await _claim_spine(owner, str(row.get("inbound_message_id") or ""))
    return len(rows)


async def purge(db: Any) -> dict[str, int]:
    """The daily job: ledger retention (``db.purge_autoreplies``), then the
    drift breaker for every enabled owner. Never raises."""
    counts: dict[str, int] = {}
    try:
        counts = await db.purge_autoreplies(_now())
    except Exception as exc:
        log.warning("gmail autoreply purge failed", error=type(exc).__name__)
    try:
        for st in await db.list_autoreply_states():
            await drift_check(db, str(st.get("owner_id") or ""))
    except Exception as exc:
        log.warning("gmail autoreply drift check failed", error=type(exc).__name__)
    return counts


async def drift_check(db: Any, owner: str) -> list[str]:
    """Layer 4 of narrowness: an enabled topic that matched more than half of
    the owner's classified mail (at least 20 samples) is switched off with a
    bell. The sample is the last 7 days, but never mail classified before the
    topic's breadth test: rows matched under an earlier wording (topic ids
    survive an edit) or an earlier, broader test say nothing about the
    topic now — narrowed, re-tested and switched on, it starts afresh.

    The switch-off also drops the topic's breadth test (it isn't narrow,
    whatever that test said): switching it on again takes a new one, so no
    form saved from before — the stored test is server-managed, a PUT can't
    restore it — puts the broad topic back. Returns the topics switched off."""
    if not owner:
        return []
    week = _now() - timedelta(days=DRIFT_DAYS)
    off: list[str] = []
    for topic in (await arcfg.load_autoreply_config(db, owner)).enabled_topics:
        # From when this wording was first tested — a re-test of the same
        # wording doesn't restart the sample.
        tested = _parse_iso(topic.dry_run.since or topic.dry_run.at)
        since = max(week, tested.astimezone(UTC)) if tested is not None else week
        stats = await db.autoreply_match_stats(owner, since.isoformat())
        total = int(stats.get("classified") or 0)
        n = int((stats.get("matched") or {}).get(topic.id) or 0)
        if total < DRIFT_MIN_SAMPLES or n / total <= DRIFT_SHARE:
            continue
        async with config_lock(owner):
            cfg = await arcfg.load_autoreply_config(db, owner)
            live = cfg.topic(topic.id)
            if (live is None or not live.enabled
                    or live.dry_run.match_hash != topic.dry_run.match_hash
                    or (live.dry_run.since or live.dry_run.at)
                    != (topic.dry_run.since or topic.dry_run.at)):
                continue        # off, or reworded and re-tested since this sample was taken
            # Off, and its breadth test dropped with it (disable_topic).
            await arcfg.save_autoreply_config(db, owner, arcfg.disable_topic(cfg, topic.id))
        off.append(topic.id)
        await _bell(db, owner, BELL_ISSUE,
                    f'Topic "{topic.name}" matches most of your mail — it isn\'t narrow.',
                    "It was switched off. Narrow it, re-run the breadth test, then switch it "
                    "on again.", f"drift:{topic.id}")
    return off


# ── Owner actions on held cards ───────────────────────────────────────────

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍﻿"))


def _edited_text(text: Any) -> str:
    """The owner's edited reply: plain text, line breaks normalised, no
    invisible control characters."""
    if not isinstance(text, str):
        raise AutoReplyError(400, "text must be a string")
    body = text.replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE).strip()
    if not body:
        raise AutoReplyError(400, "The reply is empty.")
    if len(body) > HELD_TEXT_CHARS:
        raise AutoReplyError(400, f"The reply can be at most {HELD_TEXT_CHARS} characters.")
    if any(unicodedata.category(ch) in ("Cc", "Cf") and ch not in "\n\t" for ch in body):
        raise AutoReplyError(400, "The reply contains invisible control characters.")
    return body


async def _folder_texts(owner: str, topic: arcfg.Topic | None,
                        never_disclose: tuple[str, ...]) -> list[str]:
    """The topic's knowledge folder as the writer may quote it — every
    passage the pre-scan clears with the CURRENT never-disclose list — so
    the owner's re-check counts the same material shareable as the
    writer-time scan did (its excerpts come from these). [] with no folder
    or an unreadable one (stricter, never looser)."""
    if topic is None or not topic.sources.folder:
        return []
    try:
        index = await writer.load_folder_index(owner, topic.sources.folder,
                                               never_disclose=never_disclose)
    except Exception as exc:
        log.warning("gmail autoreply folder read failed", owner=owner,
                    error=type(exc).__name__)
        return []
    if index is None:
        return []
    return [seg for f in index.readable for seg in f.segments if seg]


async def held_send(db: Any, owner: str, row_id: str, uid: str,
                    text: str | None = None) -> dict[str, Any]:
    """The owner sends a held reply — as it is (*text* None) or edited.

    Up to :data:`HELD_ACTION_DAYS` after the email arrived (older → dismissed
    as expired). The text must pass the L1 block classes with the current
    never-disclose list, the brief, signature and topic folder shareable as
    when it was written (422 with the masked findings otherwise; the hold
    classes are the owner's call). The deck switches, the bound Google
    account (its mailbox too), the token and the human-takeover rule are
    re-checked, and the
    sender re-verified from the thread. Then CAS ``held → approved`` (*uid*
    as ``approved_by``) and ``send_autoreply`` with origin
    ``autoreply_approved`` and no daily limit — L2 is not re-run. A second
    call → 409. A send Gmail refused puts the card back to held. Raises
    :class:`AutoReplyError`; returns ``ok``, ``message_id``, ``thread_id``,
    ``to``, ``subject`` and the updated ``row``."""
    row = await db.get_autoreply(owner, row_id)
    if row is None:
        raise AutoReplyError(404, "No such auto-reply.")
    if row["state"] != "held":
        raise AutoReplyError(409, "This auto-reply isn't waiting for you any more.")
    if _row_age_s(row) > HELD_ACTION_DAYS * 86400:
        await db.cas_autoreply(row_id, owner, "held", "dismissed", reason=EXPIRED)
        raise AutoReplyError(409, f"This auto-reply is more than {HELD_ACTION_DAYS} days old "
                                  "and has expired.")
    body = _edited_text(text) if text is not None else str(row.get("reply_text") or "")
    if not body.strip():
        raise AutoReplyError(409, "This auto-reply has no text to send.")
    cfg = await arcfg.load_autoreply_config(db, owner)
    topic = cfg.topic(str(row.get("topic_id") or ""))
    signature = await mail_signature.load(db, owner, mail_signature.GOOGLE_KEY)
    state = await db.get_autoreply_state(owner) or {}
    verdict = outbound_guard.scan_blocks(
        body, never_disclose=cfg.never_disclose,
        shareable=[*(t for t in (topic.brief if topic else "", signature) if t),
                   *await _folder_texts(owner, topic, cfg.never_disclose)],
        own_addresses=_own(state, cfg), prompt_texts=STATIC_PROMPTS,
        private_corpus=await writer.load_private_corpus(db, owner))
    if verdict.level == outbound_guard.BLOCK:
        raise AutoReplyError(422, f"Not sent: the reply contains "
                                  f"{outbound_guard.describe_kinds(verdict.kinds)}.",
                             findings=verdict.as_dicts())
    if _decks_off():
        raise AutoReplyError(403, "Sending auto-replies is turned off on this Flight Deck.")
    user = await _google._load_user(db, owner)
    if not cfg.google_sub or str((user or {}).get("sub") or "") != cfg.google_sub:
        raise AutoReplyError(409, "The connected Google account isn't the one this reply "
                                  "was written for.")
    token = await _google.get_valid_google_access_token(owner)
    if not token:
        raise AutoReplyError(409, f"Google isn't connected — reconnect it in {_CONNECTIONS}.")
    try:
        await _verify_mailbox(owner, cfg, token)
    except _TickStopError as stop:
        if stop.reason == GOOGLE_ACCOUNT_CHANGED:
            raise AutoReplyError(409, "The connected Google mailbox isn't the one this reply "
                                      "was written for.") from None
        raise AutoReplyError(502, "Couldn't reach Gmail — try again.") from None
    async with httpx.AsyncClient(timeout=GMAIL_TIMEOUT_S) as client:
        thread = await _thread(client, token, str(row.get("thread_id") or ""))
    if thread is None:
        raise AutoReplyError(502, "Couldn't read the email's thread in Gmail — try again.")
    if await _takeover(db, owner, str(row["thread_id"]), thread):
        raise AutoReplyError(409, "You already replied in this thread (or have a draft "
                                  "there) — not sent.")
    raw = next((m for m in _list(thread.get("messages"))
                if isinstance(m, dict) and m.get("id") == row["inbound_message_id"]), None)
    if raw is None:
        raise AutoReplyError(404, "The email is gone from Gmail.")
    inb = await asyncio.to_thread(gmail_inbound.parse_message, raw)
    sender = gmail_inbound.sender_address(inb)
    if (not gmail_inbound.sender_authenticated(inb)[0] or not sender
            or sender != str(row.get("from_addr") or "")):
        raise AutoReplyError(409, "The sender can't be verified any more — not sent.")
    if not await db.cas_autoreply(row_id, owner, "held", "approved", approved_by=uid,
                                  reply_text=body):
        raise AutoReplyError(409, "This auto-reply isn't waiting for you any more.")
    committed = False

    async def before_send() -> None:
        nonlocal committed
        if not await db.cas_autoreply(row_id, owner, "approved", "sending"):
            raise HTTPException(status_code=409, detail="This auto-reply was already handled.")
        committed = True

    agent = str(row.get("agent_name") or "") or "Auto-reply"
    try:
        out = await gs.send_autoreply(
            db, owner, token=token, inbound_message_id=row["inbound_message_id"], to_addr=sender,
            subject_fallback="" if inb.subject.strip() else "Re: (no subject)", body=body,
            agent_label=agent, topic_id=str(row.get("topic_id") or ""), daily_limit=None,
            origin="autoreply_approved", before_send=before_send)
    except asyncio.CancelledError:
        raise
    except HTTPException as exc:
        headers = dict(exc.headers or {})
        if headers.get(gmail_compose.SEND_OUTCOME_HEADER) == "unknown":
            await db.cas_autoreply(row_id, owner, ("sending", "approved"), "unknown",
                                   reason=SEND_UNCONFIRMED)
            raise AutoReplyError(int(exc.status_code), str(exc.detail), headers=headers) from exc
        await db.cas_autoreply(row_id, owner, ("approved", "sending"), "held", approved_by="")
        raise AutoReplyError(int(exc.status_code), str(exc.detail), headers=headers) from exc
    except Exception as exc:
        if committed:
            await db.cas_autoreply(row_id, owner, "sending", "unknown", reason=SEND_UNCONFIRMED)
            raise AutoReplyError(502, "Gmail didn't confirm the reply — check your Sent folder "
                                      "before trying again.",
                                 headers={gmail_compose.SEND_OUTCOME_HEADER: "unknown"}) from exc
        await db.cas_autoreply(row_id, owner, "approved", "held", approved_by="")
        raise AutoReplyError(502, "The reply could not be sent.") from exc
    await db.cas_autoreply(row_id, owner, "sending", "sent",
                           sent_message_id=str(out.get("message_id") or ""),
                           reply_text=str(out.get("body") or body))
    return {"ok": True, "message_id": out.get("message_id", ""),
            "thread_id": out.get("thread_id", ""), "to": out.get("to", ""),
            "subject": out.get("subject", ""), "row": await db.get_autoreply(owner, row_id)}


async def dismiss(db: Any, owner: str, row_id: str) -> dict[str, Any]:
    """The owner dismisses a held reply (CAS ``held → dismissed``; a second
    call → 409). Returns the row."""
    if await db.get_autoreply(owner, row_id) is None:
        raise AutoReplyError(404, "No such auto-reply.")
    if not await db.cas_autoreply(row_id, owner, "held", "dismissed"):
        raise AutoReplyError(409, "This auto-reply isn't waiting for you any more.")
    return await db.get_autoreply(owner, row_id) or {}


# ── Breadth test and "Try it" ─────────────────────────────────────────────


def _dry_run_blocker(inb: gmail_inbound.Inbound, own: list[str]) -> str:
    """Why a recent email doesn't count for the breadth test (prefilters 1, 3,
    4, 5 and 8), "" when it is eligible."""
    blockers = gmail_inbound.auto_reply_blockers(inb, own_addresses=own)
    if blockers:
        return blockers[0]
    return gmail_inbound.addressed_to_owner(inb, own)


async def run_dry_run(db: Any, owner: str, topic: arcfg.Topic) -> dict[str, Any]:
    """The breadth test: the owner's last 30 days of inbox mail (30 emails),
    the eligible ones (prefilters 1, 3, 4, 5, 8) classified against *topic*
    ALONE. Returns ``{topic_id, match_hash, eligible, matched, rate, rows}``
    — ``match_hash`` is the topic's when the test started, for
    ``record_dry_run``. Uses no classification budget (usage is logged), nor
    the engine's model slots or Gmail poll slots: a test never delays live
    mail. Raises :class:`AutoReplyError`: Google not connected or no read
    scope (409); Gmail unreadable — the profile and its addresses (without
    them every email reads "not addressed to me"), the list, or any one
    email — or a classification that failed (502). Nothing is then
    recorded: a failed read or a flaky checker never makes a topic look
    narrow, nor turns into "too little mail to judge"."""
    match_hash = topic.match_hash
    token = await _google.get_valid_google_access_token(owner)
    if not token:
        raise AutoReplyError(409, f"Connect Google first ({_CONNECTIONS}).")
    if "gmail.readonly" in await missing_scopes(db, owner):
        raise AutoReplyError(409, "The Google connection can't read Gmail (gmail.readonly).")
    cfg = await arcfg.load_autoreply_config(db, owner)
    state = await db.get_autoreply_state(owner) or {}
    rt = _rt()
    gets = asyncio.Semaphore(POLL_CONCURRENCY)
    async with httpx.AsyncClient(timeout=GMAIL_TIMEOUT_S) as client:
        if state.get("own_addresses"):
            own = _own(state, cfg)
        else:
            mailbox = await _mailbox(client, token)
            if mailbox is None or not mailbox.aliases_ok:
                raise AutoReplyError(502, "Couldn't read your Gmail address and aliases, so "
                                          "nothing was recorded — try again.")
            own = mailbox.own
        st, data = await _gmail_get(client, token, "/messages",
                                    {"q": DRY_RUN_QUERY, "maxResults": DRY_RUN_MAX})
        if st != 200:
            raise AutoReplyError(502, "Couldn't list your recent Gmail — try again.")
        ids = [str(m.get("id") or "") for m in _list(data.get("messages"))[:DRY_RUN_MAX]
               if isinstance(m, dict)]

        async def fetch(mid: str) -> tuple[str, dict[str, Any]]:
            async with gets:
                code, msg = await _gmail_get(client, token, f"/messages/{mid}", {"format": "full"})
            if code == 200 and str(msg.get("id") or "") == mid:
                return "ok", msg
            # Deleted since the list: not mail to judge. Anything else is a failed read.
            return ("gone" if code == 404 else "failed"), {}

        fetched = await asyncio.gather(*(fetch(mid) for mid in ids if _ID_RE.match(mid)))
    unread = sum(1 for outcome, _msg in fetched if outcome == "failed")
    if unread:
        raise AutoReplyError(502, f"Gmail didn't return {unread} of {len(fetched)} recent "
                                  "emails, so nothing was recorded — try again.")
    inbounds = await asyncio.to_thread(
        lambda: [gmail_inbound.parse_message(m) for outcome, m in fetched if outcome == "ok"])

    async def classify(inb: gmail_inbound.Inbound) -> judges.Match:
        async with rt.tests:
            return await judges.classify_single(owner, inb, topic, db=db, stage="dry_run")

    rows: list[dict[str, Any]] = []
    eligible: list[gmail_inbound.Inbound] = []
    for inb in inbounds:
        code = _dry_run_blocker(inb, own)
        if code:
            rows.append({"from": gmail_inbound.sender_address(inb), "subject": inb.subject,
                         "verdict": f"not eligible: {code}", "fit": "", "confidence": None})
        else:
            eligible.append(inb)
    matches = await asyncio.gather(*(classify(inb) for inb in eligible))
    failed = sum(1 for m in matches if m.failed)
    if failed:
        raise AutoReplyError(502, f"The classifier couldn't check {failed} of {len(eligible)} "
                                  "emails, so nothing was recorded — try again.")
    matched = sum(1 for m in matches if m.accepted)
    for inb, m in zip(eligible, matches, strict=True):
        rows.append({"from": gmail_inbound.sender_address(inb), "subject": inb.subject,
                     "verdict": "match" if m.accepted else m.reason, "fit": m.fit,
                     "confidence": m.confidence})
    return {"topic_id": topic.id, "match_hash": match_hash, "eligible": len(eligible),
            "matched": matched, "rate": round(matched / len(eligible), 4) if eligible else 0.0,
            "rows": rows}


def _test_field(payload: Mapping[str, Any], key: str, limit: int) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AutoReplyError(400, f"{key} must be a string")
    if len(value) > limit:
        raise AutoReplyError(400, f"{key} can be at most {limit} characters")
    return value


async def run_test(db: Any, owner: str, payload: Any) -> dict[str, Any]:
    """"Try it": ``{topic_id?, from?, subject, body}`` through the classifier,
    the writer (with its lookups), L1 and the judge, every stage returned
    (findings masked; the reply text left out when L1 blocks it). Never sends,
    never writes the ledger, never reads Gmail; uses its own model slots,
    never the engine's. With no ``from`` the sample comes from
    :data:`TEST_SENDER` (the writer needs someone to answer). ``outcome``:
    ``skipped``, ``waiting``, ``failed``, ``blocked``, ``held`` or
    ``would_send``. Raises :class:`AutoReplyError` (400 bad input, 404
    unknown topic)."""
    if not isinstance(payload, Mapping):
        raise AutoReplyError(400, "The body must be a JSON object")
    subject = _test_field(payload, "subject", TEST_SUBJECT_CHARS)
    body = _test_field(payload, "body", TEST_BODY_CHARS[1])
    sender = _test_field(payload, "from", 320).strip().lower()
    topic_id = _test_field(payload, "topic_id", 64)
    if not body.strip():
        raise AutoReplyError(400, "body is required")
    if sender and gmail_compose.parse_addresses(sender) != [sender]:
        raise AutoReplyError(400, "from must be one plain address")
    cfg = await arcfg.load_autoreply_config(db, owner)
    inb = writer.sample_inbound(sender or TEST_SENDER, subject, body)
    out: dict[str, Any] = {"match": None, "topic": None, "forced": False, "writer": None,
                           "guard": None, "judge": None, "outcome": "skipped"}
    rt = _rt()
    topic = cfg.topic(topic_id) if topic_id else None
    if topic_id and topic is None:
        raise AutoReplyError(404, "No such topic")
    async with rt.tests:
        if topic is not None:
            match = await judges.classify_single(owner, inb, topic, db=db, stage="test")
        else:
            match = await judges.classify(owner, inb, cfg.enabled_topics, db=db, stage="test")
            topic = cfg.topic(match.topic_id) if match.accepted else None
    out["match"] = match.to_json()
    if topic is None:
        return out
    out["topic"] = {"id": topic.id, "name": topic.name}
    out["forced"] = not match.accepted
    resolved = await writer.resolve_writer(db, owner, cfg.agent_ref)
    if isinstance(resolved, str):
        out["writer"] = {"state": writer.FAILED, "reason": resolved}
        out["outcome"] = "waiting" if resolved in writer.WAIT_REASONS else "failed"
        return out
    rec, shared = resolved
    state = await db.get_autoreply_state(owner) or {}
    async with rt.tests:
        result = await writer.compose_reply(db, owner, cfg, topic, inb, (), rec, shared,
                                            own_addresses=_own(state, cfg), now=_now())
    await _log_writer_usage(db, owner, result)
    out["writer"] = {"state": result.state, "reason": result.reason, "detail": result.detail,
                     "plan": result.plan.to_json(), "sources_used": list(result.sources_used),
                     "reply": ""}
    if result.state != writer.REPLY:
        out["outcome"] = {writer.SKIP: "skipped", writer.BLOCKED: "blocked"}.get(
            result.state, "failed")
        if result.state == writer.BLOCKED:
            out["guard"] = {"level": outbound_guard.BLOCK, "findings": list(result.findings)}
        return out
    signature = await mail_signature.load(db, owner, mail_signature.GOOGLE_KEY)
    verdict = result.scan(never_disclose=cfg.never_disclose, own_addresses=_own(state, cfg),
                          signature=signature,
                          private_corpus=await writer.load_private_corpus(db, owner),
                          extra_prompt_texts=judges.STATIC_PROMPTS)
    out["guard"] = {"level": verdict.level, "findings": verdict.as_dicts()}
    if verdict.level != outbound_guard.BLOCK:
        out["writer"]["reply"] = result.body
    if verdict.findings:
        out["outcome"] = "blocked" if verdict.level == outbound_guard.BLOCK else "held"
        return out
    async with rt.tests:
        judged = await judges.judge(owner, topic,
                                    tuple(t for t in (*result.excerpts, signature) if t),
                                    result.background, inb, result.body, thread=result.thread,
                                    db=db, stage="test")
    out["judge"] = judged.to_json()
    out["outcome"] = "would_send" if judged.passed else "held"
    return out
