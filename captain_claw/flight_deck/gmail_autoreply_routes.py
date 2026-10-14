"""Topic auto-replies for Gmail — the owner's routes (``/fd/google/gmail-autoreply``).

The signed-in user's own config, status, activity and held cards; the engine
(:mod:`captain_claw.flight_deck.gmail_autoreply`) does the work. Like all of
/fd/google, only on an auth-enabled deck (``_require_auth_deck``). Every route
acts as the session user (``get_current_user`` — an admin's ``X-FD-Act-As`` is
not honoured) and there is no agent path: no agent can read the config or
switch it on.

* ``GET`` / ``PUT /gmail-autoreply`` — the config plus its ``status``. A PUT is
  a partial update (``enabled``, ``agent_ref``, ``daily_limit``,
  ``never_disclose``, ``topics`` as a whole list), validated and linted by
  :mod:`gmail_autoreply_config`; here, too, a topic's folder must be a folder
  in the user's files, the agent one of their own eligible agents, and no
  topic may say a never-disclose term (a reply quoting it would be blocked).
  ``enabled: true`` needs every requirement (:func:`_requirements`) and then
  the Gmail baseline — any miss is a 409 listing what is missing, with nothing
  written. ``enabled: false`` drops the engine's state row at once. A PUT
  never sets a topic's breadth test; with the view's ``updated_at`` it is a
  409 when the config changed since.
* ``GET /gmail-autoreply/agents`` — the agents the user may pick.
* ``POST /gmail-autoreply/topics/{id}/dry-run`` — the breadth test on the
  user's recent mail, stored on the topic (once a minute); a topic that is on
  and now matched too much is switched off.
* ``POST /gmail-autoreply/test`` — "Try it": every stage, nothing sent, no
  ledger row, Gmail never read (20 an hour).
* ``GET /gmail-autoreply/activity`` — the ledger, quiet skips with ``all``;
  every open held card always included.
* ``POST /gmail-autoreply/activity/{id}/send`` and ``…/dismiss`` — held cards
  (compare-and-set: a second call is a 409).
* ``POST /gmail-autoreply/pause`` — off, ``paused_by_user``.

Config writes are read-modify-write under the engine's
:func:`gmail_autoreply.config_lock`. Never logs a term, an email or a reply.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from captain_claw import mail_signature, outbound_guard, vfs
from captain_claw.flight_deck import agent_sharing
from captain_claw.flight_deck import gmail_autoreply as engine
from captain_claw.flight_deck import gmail_autoreply_config as arcfg
from captain_claw.flight_deck import gmail_autoreply_judges as judges
from captain_claw.flight_deck import gmail_autoreply_writer as writer
from captain_claw.flight_deck import gmail_send_routes as gs
from captain_claw.flight_deck import google_oauth_routes as _google
from captain_claw.flight_deck.auth import get_current_user, get_db
from captain_claw.flight_deck.db import AUTOREPLY_SEND_STATES
from captain_claw.flight_deck.google_oauth_routes import _require_auth_deck
from captain_claw.logging import get_logger

log = get_logger(__name__)


router = APIRouter(
    prefix="/fd/google",
    tags=["google-gmail-autoreply"],
    dependencies=[Depends(_require_auth_deck)],
)


ACTIVITY_DEFAULT = 50
ACTIVITY_MAX = 200
DRY_RUN_PER_MINUTE = 1
TEST_PER_HOUR = 20
PAUSED_BY_USER = "paused_by_user"
# The breadth test's answer when the topic was reworded while it ran.
STALE_TEST_NOTICE = ("the topic changed while the breadth test ran — this result is for "
                     "its earlier wording; re-run it")

# The ledger rows whose reply text the activity list shows: what went out (or
# may have — Gmail never confirmed it) and what waits for the owner. No other
# row's text was ever cleared to send.
TEXT_STATES = frozenset({"sent", "unknown", "held"})

# What turning auto-replies on needs, in order; a 409 lists the missing codes.
DECK_DISABLED = "deck_disabled"
SEND_DECK_DISABLED = "send_deck_disabled"
GOOGLE_NOT_CONNECTED = "google_not_connected"
MISSING_SCOPES = "missing_scopes"
NO_SIGNATURE = "no_signature"
NO_AGENT = "no_agent"
AGENT_INELIGIBLE = "agent_ineligible"
AGENT_UNAVAILABLE = "agent_unavailable"
NO_TOPICS = "no_topics"
TOPIC_GATE = "topic_gate"
NO_CLASSIFIER = "no_classifier"
NO_JUDGE = "no_judge"
BOUND_ELSEWHERE = "bound_elsewhere"
BASELINE = "baseline"
REQUIREMENTS = (DECK_DISABLED, SEND_DECK_DISABLED, GOOGLE_NOT_CONNECTED, MISSING_SCOPES,
                NO_SIGNATURE, NO_AGENT, AGENT_INELIGIBLE, AGENT_UNAVAILABLE, NO_TOPICS,
                TOPIC_GATE, NO_CLASSIFIER, NO_JUDGE, BOUND_ELSEWHERE, BASELINE)
# status.last_error when auto-replies are on but the engine has no state row
# for them (it is inert then: fail closed).
STATE_MISSING = "state_missing"

_CONNECTIONS = "Connections → Google"
_AGENT_REASONS = {
    writer.AGENT_NOT_FOUND: "isn't one of your agents",
    writer.AGENT_MANAGED: "is a worker Flight Deck runs for itself",
    writer.AGENT_NO_TOKEN: "has no access token",
    writer.AGENT_NO_PORT: "has no port",
}


# ── per-user rate limits ────────────────────────────────────────────


def _clock() -> float:
    return time.monotonic()


class _Window:
    """A per-user sliding-window counter — the shape of
    ``rate_limiter._SlidingWindow``, kept here because these limits are this
    feature's own (a breadth test reads 30 emails and classifies them; a "Try
    it" runs every model stage), not the deck's plan limits."""

    def __init__(self, max_count: int, window_seconds: float) -> None:
        self.max_count = max_count
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    def check(self, user_id: str) -> bool:
        """True (and counted) when allowed, False when over the limit."""
        now = _clock()
        cutoff = now - self.window_seconds
        self._requests[user_id] = [t for t in self._requests[user_id] if t > cutoff]
        if len(self._requests[user_id]) >= self.max_count:
            return False
        self._requests[user_id].append(now)
        return True


_dry_run_limit = _Window(DRY_RUN_PER_MINUTE, 60.0)
_test_limit = _Window(TEST_PER_HOUR, 3600.0)


# ── what the requirements and the status are read from ──────────────


@dataclass(frozen=True)
class _Facts:
    deck_disabled: bool
    send_deck_disabled: bool
    sub: str                        # the connected Google account
    email: str
    connected: bool                 # a refresh token (live: a token that works)
    missing_scopes: tuple[str, ...]
    signature_set: bool
    agent: dict[str, Any]           # see _agent
    classifier: judges.Resolved
    judge: judges.Resolved
    bound_elsewhere: bool


async def _shared(db: Any, ref: str, owner: str) -> bool:
    """Does the agent have members? Unreadable reads as shared (as the
    writer reads it: that only takes the insights and playbooks lookups away)."""
    try:
        return bool(await db.list_agent_members(ref, owner))
    except Exception:
        return True


async def _agent(db: Any, owner: str, ref: str) -> dict[str, Any]:
    """``{ref, name, running, shared, eligible, reason}`` of the agent *ref*
    for *owner*: ``reason`` "" when it may write their replies (running or
    not), else a writer reason code. Another owner's agent reads as not
    found, name and state unshown."""
    out: dict[str, Any] = {"ref": ref, "name": "", "running": False, "shared": False,
                           "eligible": False, "reason": writer.NO_AGENT}
    if not ref:
        return out
    try:
        rec = await asyncio.to_thread(agent_sharing.resolve_agent_record, ref, strict=True)
    except Exception:  # RecordUnavailable: can't tell right now
        out["reason"] = writer.AGENT_UNAVAILABLE
        return out
    reason = writer.check_writer(rec, owner)
    if rec is not None and rec.owner == owner:
        out.update(name=rec.name, running=bool(rec.running))
    out.update(eligible=not reason, reason=reason)
    if not reason:
        out["shared"] = await _shared(db, ref, owner)
    return out


async def _facts(db: Any, owner: str, cfg: arcfg.AutoReplyConfig, *, live: bool) -> _Facts:
    """The deck, Google, signature, agent and checker facts. *live* (turning
    on) asks for a working Google token, which may refresh it; otherwise the
    stored records only — a status read never calls Google."""
    user = await _google._load_user(db, owner)
    user = user if isinstance(user, dict) else {}
    sub = str(user.get("sub") or "")
    email = str(user.get("email") or "").strip().lower()
    if live:
        connected = bool(await _google.get_valid_google_access_token(owner))
    else:
        tokens = await _google._load_tokens(db, owner)
        connected = bool(tokens and tokens.refresh_token)
    return _Facts(
        deck_disabled=engine.autoreply_deck_disabled(),
        send_deck_disabled=gs.deck_send_disabled(),
        sub=sub, email=email, connected=connected,
        missing_scopes=tuple(await engine.missing_scopes(db, owner)),
        signature_set=bool(
            (await mail_signature.load(db, owner, mail_signature.GOOGLE_KEY)).strip()),
        agent=await _agent(db, owner, cfg.agent_ref),
        classifier=await judges.resolve_classifier(owner, db=db),
        judge=await judges.resolve_judge(owner, db=db),
        bound_elsewhere=await engine.bound_elsewhere(db, owner, sub),
    )


def _requirements(cfg: arcfg.AutoReplyConfig, f: _Facts, *,
                  now: datetime | None = None) -> list[tuple[str, str]]:
    """What turning *cfg* on still needs, as ``(code, what to do)`` in
    requirement order — [] when only the Gmail baseline is left."""
    out: list[tuple[str, str]] = []
    if f.deck_disabled:
        out.append((DECK_DISABLED, "the deck administrator has turned topic auto-replies "
                                   "off on this Flight Deck (FD_GMAIL_AUTOREPLY)"))
    if f.send_deck_disabled:
        out.append((SEND_DECK_DISABLED, "the deck administrator has turned email sending "
                                        "off on this Flight Deck (FD_GMAIL_SEND)"))
    if not (f.connected and f.sub and f.email):
        out.append((GOOGLE_NOT_CONNECTED, f"connect Google in {_CONNECTIONS}"))
    if f.missing_scopes:
        out.append((MISSING_SCOPES, "the Google connection must allow "
                                    + " and ".join(f.missing_scopes)
                                    + " (the deck admin adds the scope, then you reconnect "
                                      "Google)"))
    if not f.signature_set:
        out.append((NO_SIGNATURE, "set your agentic signature (Email sending) — every "
                                  "auto-reply ends with it"))
    reason = f.agent["reason"]
    if reason == writer.NO_AGENT:
        out.append((NO_AGENT, "choose the agent that writes the replies"))
    elif reason == writer.AGENT_UNAVAILABLE:
        out.append((AGENT_UNAVAILABLE, "Flight Deck can't read its agent records right now "
                                       "— try again"))
    elif reason:
        out.append((AGENT_INELIGIBLE, "the chosen agent "
                                      + _AGENT_REASONS.get(reason, "can't write replies")
                                      + " — choose another"))
    if not cfg.enabled_topics:
        out.append((NO_TOPICS, "turn on at least one topic"))
    for i, topic in enumerate(cfg.topics):
        if topic.enabled:
            gate = arcfg.dry_run_gate(topic, now=now)
            if not gate.ok:
                out.append((TOPIC_GATE, f"topic {i + 1} ({topic.name}): {gate.reason}"))
    if not f.classifier.available:
        out.append((NO_CLASSIFIER, "nothing can check incoming mail — connect Jev, or set a "
                                   "Balanced or Reason model in the Library"))
    if not f.judge.available:
        out.append((NO_JUDGE, "nothing can check replies before they go out — connect Jev, "
                              "or set a Reason or Balanced model in the Library"))
    if f.bound_elsewhere:
        out.append((BOUND_ELSEWHERE, "another user on this Flight Deck already has topic "
                                     "auto-replies on for this Google account"))
    return out


def _not_ready(missing: list[tuple[str, str]]) -> JSONResponse:
    """The 409 for turning on: ``detail`` says what is missing, ``missing``
    lists the codes (in requirement order, each once). Nothing was written."""
    return JSONResponse(status_code=409, content={
        "detail": ("Topic auto-replies can't be turned on yet: "
                   + "; ".join(text for _code, text in missing) + "."),
        "missing": list(dict.fromkeys(code for code, _text in missing)),
    })


def _refusal(exc: engine.AutoReplyError) -> JSONResponse:
    """An engine refusal as the answer: ``{"detail"}`` as an HTTPException
    has it, plus the masked ``findings`` of a 422, with the engine's headers
    (``X-FD-Gmail-Send-Outcome: unknown`` when Gmail may have sent)."""
    content: dict[str, Any] = {"detail": exc.detail}
    if exc.findings:
        content["findings"] = exc.findings
    return JSONResponse(status_code=exc.status, content=content, headers=exc.headers or None)


# ── the view ────────────────────────────────────────────────────────


async def _status(db: Any, owner: str, cfg: arcfg.AutoReplyConfig) -> dict[str, Any]:
    """The view's ``status``: the facts, the engine's last poll and error, the
    counts, ``missing`` (the requirement codes unmet now) and ``state`` —
    ``off``, ``on``, or ``attention`` (on, but something stops or delays
    replies: an unmet requirement, a poll error, no state row, another Google
    account, the agent stopped)."""
    f = await _facts(db, owner, cfg, live=False)
    state = await db.get_autoreply_state(owner)
    missing = _requirements(cfg, f)
    bound_ok = not cfg.enabled or (bool(cfg.google_sub) and f.sub == cfg.google_sub)
    last_error = str((state or {}).get("last_error") or "")
    if cfg.enabled and state is None:
        last_error = STATE_MISSING
    if not cfg.enabled:
        phase = "off"
    elif (last_error or not bound_ok or not f.agent["running"]
          # A topic that is on keeps running past its breadth test's 30 days;
          # the gate only stands in the way of turning on. (Its age is all
          # it can fail: the loader reads a topic that is on without a test of
          # its wording, or with a too-broad one, as off.)
          or any(code != TOPIC_GATE for code, _text in missing)):
        phase = "attention"
    else:
        phase = "on"
    day = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    return {
        "deck_disabled": f.deck_disabled,
        "send_deck_disabled": f.send_deck_disabled,
        "google_connected": bool(f.connected and f.sub and f.email),
        "account_email": f.email,
        "bound_ok": bound_ok,
        "scopes_ok": not f.missing_scopes,
        "missing_scopes": list(f.missing_scopes),
        "signature_set": f.signature_set,
        "agent": f.agent,
        "classifier": f.classifier.to_json(),
        "judge": f.judge.to_json(),
        # What the daily limit counts: sent, sending, or maybe sent.
        "sent_last_24h": await db.count_autoreplies(owner, day, AUTOREPLY_SEND_STATES),
        "held_open": await db.count_autoreplies(owner, "", "held"),
        "last_poll_at": str((state or {}).get("last_poll_at") or ""),
        "last_error": last_error,
        "disabled_reason": cfg.disabled_reason,
        "state": phase,
        "missing": list(dict.fromkeys(code for code, _text in missing)),
    }


async def _view(db: Any, owner: str, cfg: arcfg.AutoReplyConfig) -> dict[str, Any]:
    """The config as the owner sees it (no bound Google ``sub``; each topic
    with its breadth ``gate``, its folder's ``hidden_passages`` and
    ``folder_error``) plus ``status``."""
    view = cfg.to_view()
    indexes = await asyncio.gather(*(
        writer.load_folder_index(owner, t.sources.folder, never_disclose=cfg.never_disclose)
        for t in cfg.topics))
    for shown, index in zip(view["topics"], indexes, strict=True):
        shown["hidden_passages"] = index.hidden_passages if index is not None else []
        shown["folder_error"] = index.error if index is not None else ""
    view["status"] = await _status(db, owner, cfg)
    return view


def _activity_row(row: dict[str, Any]) -> dict[str, Any]:
    """A ledger row for the activity list — the reply text only where it was
    cleared to send (:data:`TEXT_STATES`); findings are stored masked."""
    state = str(row.get("state") or "")
    return {
        "id": row.get("id", ""),
        "state": state,
        "reason": row.get("reason", ""),
        "detail": row.get("detail", ""),
        "from": row.get("from_addr", ""),
        "subject": row.get("subject", ""),
        "topic_id": row.get("topic_id", ""),
        "topic_name": row.get("topic_name", ""),
        "confidence": row.get("confidence"),
        "match_judge": row.get("match_judge", ""),
        "sources_used": row.get("sources_used") or [],
        "findings": row.get("findings") or [],
        "judge": row.get("judge") or {},
        "agent_name": row.get("agent_name", ""),
        "thread_id": row.get("thread_id", ""),
        "inbound_message_id": row.get("inbound_message_id", ""),
        "sent_message_id": row.get("sent_message_id", ""),
        "reply_text": row.get("reply_text", "") if state in TEXT_STATES else "",
        "received_at": row.get("received_at", ""),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }


# ── PUT checks beyond the config's own ──────────────────────────────


def _is_folder(owner: str, folder: str) -> bool:
    """Is *folder* (``vfs:<project>/<path>``) a directory in *owner*'s files?"""
    try:
        root = vfs.resolve_under(owner, "", folder)
        return root is not None and root.is_dir()
    except Exception:
        return False


def _says_never_disclose(text: str, terms: tuple[str, ...]) -> bool:
    """Does *text* contain one of *terms*, matched as the reply guard does?"""
    if not terms or not text:
        return False
    return "never_disclose" in outbound_guard.scan_material(text, never_disclose=terms).kinds


async def _check_update(db: Any, owner: str, current: arcfg.AutoReplyConfig,
                        cfg: arcfg.AutoReplyConfig, patch: dict[str, Any]) -> None:
    """A PUT's checks beyond :func:`arcfg.apply_patch`: the agent is one of the
    owner's eligible agents, a new or moved topic folder is a folder in their
    files, and no topic says a never-disclose term (every reply quoting the
    brief would be blocked). Raises :class:`HTTPException`; error details
    never echo a term."""
    if cfg.agent_ref != current.agent_ref:
        if not cfg.agent_ref:
            if cfg.enabled:
                raise HTTPException(status_code=400, detail=(
                    "Turn topic auto-replies off before removing the agent that writes them."))
        else:
            reason = (await _agent(db, owner, cfg.agent_ref))["reason"]
            if reason == writer.AGENT_UNAVAILABLE:
                raise HTTPException(status_code=503, detail=(
                    "Flight Deck can't read its agent records right now — try again."))
            if reason:
                raise HTTPException(status_code=400, detail=(
                    f"That agent {_AGENT_REASONS.get(reason, 'can not write replies')} — "
                    "choose another."))
    if patch.get("topics") is not None:
        for i, topic in enumerate(cfg.topics):
            old = current.topic(topic.id)
            folder = topic.sources.folder
            if folder and (old is None or old.sources.folder != folder):
                if not await asyncio.to_thread(_is_folder, owner, folder):
                    raise HTTPException(status_code=400, detail=(
                        f"Topic {i + 1}: {folder} isn't a folder in your files."))
    if patch.get("topics") is not None or patch.get("never_disclose") is not None:
        for i, topic in enumerate(cfg.topics):
            for label, text in (("name", topic.name), ("description", topic.about),
                                ("what the agent may say", topic.brief)):
                if _says_never_disclose(text, cfg.never_disclose):
                    raise HTTPException(status_code=400, detail=(
                        f"Topic {i + 1}: the {label} contains one of your never-disclose "
                        "terms, so every reply quoting it would be blocked. Remove the term "
                        "from one of them."))


# ── routes ──────────────────────────────────────────────────────────


class AutoReplyUpdate(BaseModel):
    # Every field optional; absent / null leaves it unchanged. Typed loosely and
    # checked by gmail_autoreply_config so any bad value is a 400 with a
    # readable detail. Server-managed fields are not fields here: ignored.
    enabled: Any = None
    agent_ref: Any = None
    daily_limit: Any = None
    never_disclose: Any = None
    topics: Any = None
    # Not a change: the view's ``updated_at`` the edit was made on. When given
    # and the stored config changed since (Flight Deck switched a topic off, an
    # other tab saved), the PUT is a 409 and writes nothing — a page left open
    # can't put back what changed under it.
    updated_at: Any = None


class HeldSend(BaseModel):
    text: Any = None  # absent / null = send the held reply as it is


@router.get("/gmail-autoreply")
async def autoreply_get(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """This user's topic auto-reply config and status."""
    db = get_db()
    uid = _google._effective_owner(_user)
    return await _view(db, uid, await arcfg.load_autoreply_config(db, uid))


@router.put("/gmail-autoreply", response_model=None)
async def autoreply_put(
    body: AutoReplyUpdate,
    _user: dict = Depends(get_current_user),
) -> Any:
    """Change this user's config (any subset of the fields). ``enabled:
    true`` checks every requirement, then takes the Gmail baseline — a 409
    ``{detail, missing}`` otherwise, with nothing written. With ``updated_at``
    (the view's), a config changed since is a 409 too. A topic's breadth test
    is never taken from the body: one switched on needs a passing test of its
    current wording stored by the breadth-test route. The answer is the view
    plus ``notice`` (:data:`arcfg.RERUN_NOTICE` when an edit switched a topic
    off)."""
    db = get_db()
    uid = _google._effective_owner(_user)
    patch = body.model_dump(exclude={"updated_at"})
    async with engine.config_lock(uid):
        current = await arcfg.load_autoreply_config(db, uid)
        if body.updated_at is not None and body.updated_at != current.updated_at:
            raise HTTPException(status_code=409, detail=(
                "Your topic auto-reply settings changed since this page loaded — reload "
                "them, then make your change again."))
        try:
            result = arcfg.apply_patch(current, patch)
        except arcfg.ConfigError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        cfg = result.config
        await _check_update(db, uid, current, cfg, patch)
        if result.wants_enable:
            missing = _requirements(cfg, await _facts(db, uid, cfg, live=True))
            if missing:
                return _not_ready(missing)
            try:
                stored = await engine.baseline_and_enable(db, uid, cfg)
            except engine.AutoReplyError as exc:
                return _not_ready([(BASELINE, exc.detail.rstrip("."))])
            log.info("gmail autoreply turned on", owner=uid, topics=len(stored.enabled_topics))
        else:
            stored = await arcfg.save_autoreply_config(db, uid, cfg)
            if patch.get("enabled") is False:
                # Off: the engine stops at once (it polls only owners with a
                # row), and mail it queued but never answered is closed out.
                await db.delete_autoreply_state(uid)
                await engine.skip_queued(db, uid)
                if current.enabled:
                    log.info("gmail autoreply turned off", owner=uid)
    view = await _view(db, uid, stored)
    view["notice"] = result.notice
    return view


@router.get("/gmail-autoreply/agents")
async def autoreply_agents(_user: dict = Depends(get_current_user)) -> list[dict[str, Any]]:
    """The agents this user may choose to write the replies: their own
    process agents and this deck's containers labelled theirs, FD-managed
    workers left out — ``[{ref, name, runtime, running, shared, eligible,
    reason}]``."""
    db = get_db()
    uid = _google._effective_owner(_user)
    return await writer.list_eligible_agents(db, uid)


@router.post("/gmail-autoreply/topics/{topic_id}/dry-run", response_model=None)
async def autoreply_dry_run(
    topic_id: str,
    _user: dict = Depends(get_current_user),
) -> Any:
    """The breadth test: the topic alone against this user's recent inbox,
    stored on the topic — its ``gate`` says whether it may be switched on.
    A topic that is on and now matched too much is switched off at once
    (``switched_off``, with ``notice``): it doesn't keep answering mail it was
    just measured too broad for. Once a minute. ``{topic_id, eligible,
    matched, rate, rows, gate, switched_off, notice}``."""
    db = get_db()
    uid = _google._effective_owner(_user)
    topic = (await arcfg.load_autoreply_config(db, uid)).topic(topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="No such topic")
    if not _dry_run_limit.check(uid):
        raise HTTPException(status_code=429, detail=(
            "The breadth test runs at most once a minute — try again shortly."))
    try:
        out = await engine.run_dry_run(db, uid, topic)
    except engine.AutoReplyError as exc:
        return _refusal(exc)
    async with engine.config_lock(uid):
        cfg = await arcfg.load_autoreply_config(db, uid)
        before = cfg.topic(topic_id)
        try:
            # The topic's wording when the test STARTED: an edit made meanwhile
            # leaves a test that simply doesn't open the gate (and never
            # replaces a test of the new wording). One that matched too much
            # switches a topic that is on off.
            cfg = arcfg.record_dry_run(cfg, topic_id, match_hash=out["match_hash"],
                                       eligible=out["eligible"], matched=out["matched"])
        except arcfg.ConfigError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        stored = await arcfg.save_autoreply_config(db, uid, cfg)
    saved = stored.topic(topic_id)
    switched_off = bool(before is not None and before.enabled
                        and saved is not None and not saved.enabled)
    notice = ""
    if switched_off:
        notice = arcfg.TOO_BROAD_NOTICE
        log.info("gmail autoreply topic switched off by its breadth test", owner=uid,
                 topic=topic_id)
    elif saved is not None and out["match_hash"] != saved.match_hash:
        notice = STALE_TEST_NOTICE
    return {
        "topic_id": topic_id,
        "eligible": out["eligible"],
        "matched": out["matched"],
        "rate": out["rate"],
        "rows": out["rows"],
        "gate": arcfg.dry_run_gate(saved).to_json() if saved is not None else None,
        "switched_off": switched_off,
        "notice": notice,
    }


@router.post("/gmail-autoreply/test", response_model=None)
async def autoreply_test(
    request: Request,
    _user: dict = Depends(get_current_user),
) -> Any:
    """"Try it": ``{topic_id?, from?, subject, body}`` through the classifier,
    the lookups and writer, the local scan and the judge — every stage
    returned (findings masked). Never sends, never writes the ledger, never
    reads Gmail. 20 an hour."""
    db = get_db()
    uid = _google._effective_owner(_user)
    if not _test_limit.check(uid):
        raise HTTPException(status_code=429, detail=(
            f"Try it runs at most {TEST_PER_HOUR} times an hour — try again later."))
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="The body must be a JSON object")
    try:
        return await engine.run_test(db, uid, payload)
    except engine.AutoReplyError as exc:
        return _refusal(exc)


@router.get("/gmail-autoreply/activity")
async def autoreply_activity(
    limit: int = ACTIVITY_DEFAULT,
    show_all: bool = Query(False, alias="all"),
    _user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """This user's ledger rows, newest first. Quiet skips (labels, lists,
    automated senders, no match, unverified senders, bursts) only with
    ``all=true``. Every open held card is always in the list, however much
    newer activity there is — it is the only place the owner can Send or
    Dismiss it before it expires: the newest other rows fill the rest of
    ``limit`` (held cards beyond ``limit`` are all still listed)."""
    db = get_db()
    uid = _google._effective_owner(_user)
    n = max(1, min(int(limit), ACTIVITY_MAX))
    held = await db.list_autoreplies(uid, limit=ACTIVITY_MAX, include_quiet=True,
                                     states=("held",))
    rows = await db.list_autoreplies(uid, limit=n, include_quiet=show_all)
    # A card acted on between the two reads shows as the later read has it.
    fresh = {r.get("id"): r for r in rows}
    pinned = [fresh.get(r.get("id"), r) for r in held]
    pinned_ids = {r.get("id") for r in held}
    others = [r for r in rows if r.get("id") not in pinned_ids][:max(0, n - len(pinned))]
    merged = heapq.merge(pinned, others, key=lambda r: str(r.get("created_at") or ""),
                         reverse=True)
    return [_activity_row(r) for r in merged]


@router.post("/gmail-autoreply/activity/{row_id}/send", response_model=None)
async def autoreply_held_send(
    row_id: str,
    body: HeldSend | None = None,
    _user: dict = Depends(get_current_user),
) -> Any:
    """Send a held reply — as it is, or edited (``{text}``). The text must
    pass the local scan's block classes (422 ``{detail, findings}``); the
    deck switches, the bound Google account and the thread are re-checked;
    not capped, the judge not re-run. A second call is a 409."""
    db = get_db()
    uid = _google._effective_owner(_user)
    try:
        out = await engine.held_send(db, uid, row_id, uid, body.text if body else None)
    except engine.AutoReplyError as exc:
        return _refusal(exc)
    row = out.get("row")
    return {
        "ok": True,
        "message_id": out.get("message_id", ""),
        "thread_id": out.get("thread_id", ""),
        "to": out.get("to", ""),
        "subject": out.get("subject", ""),
        "row": _activity_row(row) if row else None,
    }


@router.post("/gmail-autoreply/activity/{row_id}/dismiss", response_model=None)
async def autoreply_held_dismiss(
    row_id: str,
    _user: dict = Depends(get_current_user),
) -> Any:
    """Dismiss a held reply (nothing is sent). A second call is a 409."""
    db = get_db()
    uid = _google._effective_owner(_user)
    try:
        row = await engine.dismiss(db, uid, row_id)
    except engine.AutoReplyError as exc:
        return _refusal(exc)
    return {"ok": True, "row": _activity_row(row)}


@router.post("/gmail-autoreply/pause")
async def autoreply_pause(_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Turn this user's auto-replies off (``paused_by_user``); turning them on
    again takes a fresh baseline."""
    db = get_db()
    uid = _google._effective_owner(_user)
    # disable() takes the config lock itself.
    stored = await engine.disable(uid, PAUSED_BY_USER, db=db)
    log.info("gmail autoreply paused", owner=uid)
    return await _view(db, uid, stored)
