"""Bank statements — Payments Phase 4a (docs/payments-phase4-plan.md).

The owner connects a bank account for reading (Enable Banking AIS, the deck's
Phase 2 application) or switches on their Revolut Business statement, and
Flight Deck keeps a copy of the account's *outgoing* payments so 4b can confirm
bills from the bank. Owner routes only: the agent never sees a statement.

Syncs run on two daily slots (``FD_PAYMENTS_SYNC_TIMES``, deck time) from the
payments loop, and on demand while the user is on the page. An Enable Banking
account is read at most four times a day without the user present (PSD2); a
read with the user present carries their PSU headers and doesn't count.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from captain_claw.flight_deck import bank_statements as bs
from captain_claw.flight_deck.auth import get_current_user
from captain_claw.flight_deck.bank_link import BankLinkError
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["payments-statements"])
public_router = APIRouter(tags=["payments-statements-public"])

EB, REV = bs.ENABLEBANKING, bs.REVOLUT
REVOLUT_SESSION = "revolut"
STATE_TTL = 900
MANUAL_EVERY = 60
RENEW_NOTICE = 7 * 86400
_DEFAULT_TIMES = "07:30,16:30"
_STATES: dict[str, dict] = {}                 # one-time state → {owner, aspsp, psu_type, valid_until, expires}
_LAST_MANUAL: dict[str, float] = {}
_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}
_TASKS: set[asyncio.Task] = set()


def _pr():
    from captain_claw.flight_deck import payments_routes

    return payments_routes


def store():
    return _pr().store()


def _require_statements() -> None:
    _pr()._require_on()
    if not bs.enabled():
        raise HTTPException(404, "Bank statements are off on this deck (FD_PAYMENTS_STATEMENTS)")


# ── sync slots and PSU presence ──────────────────────────────────────

def sync_times() -> list[tuple[int, int]]:
    """The unattended sync slots, at most four (the PSD2 daily limit)."""
    out = []
    for part in (os.environ.get("FD_PAYMENTS_SYNC_TIMES") or _DEFAULT_TIMES).split(","):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", part)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            out.append((int(m.group(1)), int(m.group(2))))
    return sorted(set(out))[:bs.UNATTENDED_PER_DAY] or [(7, 30), (16, 30)]


def latest_slot(now: datetime | None = None) -> float:
    """The most recent slot at or before *now* (deck local time), as epoch."""
    now = now or datetime.now()
    times = sync_times()
    for day in (now.date(), now.date() - timedelta(days=1)):
        for hh, mm in reversed(times):
            at = datetime(day.year, day.month, day.day, hh, mm)
            if at <= now:
                return at.timestamp()
    return (now - timedelta(days=1)).timestamp()


def next_slot(now: datetime | None = None) -> float:
    now = now or datetime.now()
    for day in (now.date(), now.date() + timedelta(days=1)):
        for hh, mm in sync_times():
            at = datetime(day.year, day.month, day.day, hh, mm)
            if at > now:
                return at.timestamp()
    return (now + timedelta(days=1)).timestamp()


def psu_headers(request: Request, required: list[str]) -> dict | None:
    """The user's presence headers, or None when the bank needs one we can't give
    (Enable Banking takes all required PSU headers or none)."""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    available = {
        "Psu-Ip-Address": forwarded or request.headers.get("x-real-ip") or (request.client.host if request.client else ""),
        "Psu-User-Agent": request.headers.get("user-agent", ""),
        "Psu-Accept": request.headers.get("accept", ""),
        "Psu-Accept-Language": request.headers.get("accept-language", ""),
        "Psu-Accept-Encoding": request.headers.get("accept-encoding", ""),
        "Psu-Accept-Charset": request.headers.get("accept-charset", ""),
        "Psu-Referer": request.headers.get("referer", ""),
    }
    available = {k: v[:500] for k, v in available.items() if v}
    by_lower = {k.lower(): k for k in available}
    wanted = {by_lower.get(str(r).lower()) for r in required}
    if None in wanted or not available.get("Psu-Ip-Address"):
        return None
    return {k: available[k] for k in wanted | {"Psu-Ip-Address", "Psu-User-Agent"} if k in available}


# ── sync ─────────────────────────────────────────────────────────────

async def sync_session(sess: dict, *, psu: dict | None = None, now: float | None = None) -> dict:
    """Read the watched accounts of one connection and store their outgoing
    payments. Without *psu* it's an unattended read and counts against the
    bank's daily limit."""
    now = time.time() if now is None else now
    owner, provider, sid = sess["owner_id"], sess["provider"], sess["session_id"]
    lock = _LOCKS.setdefault((owner, provider, sid), asyncio.Lock())
    async with lock:
        sess = await store().get_bank_session(owner, provider, sid) or sess
        summary = {"new": 0, "changed": 0, "error": ""}
        stamps = [t for t in sess.get("unattended_at") or [] if now - float(t) < 86400]
        unattended = provider == EB and psu is None
        if unattended and bs.unattended_left(stamps, now) <= 0:
            summary["error"] = "Captain already read this bank four times today; it tries again at the next sync."
            return summary
        accounts = [a for a in await store().bank_accounts(owner, provider, sid) if a["watched"]]
        changes: dict = {"last_attempt_at": now}      # a failed read still uses up its slot
        try:
            if unattended:
                stamps.append(now)
            if provider == EB:
                for a in accounts:
                    date_from, first = bs.sync_from(a["synced_through"])
                    raw = await bs.transactions(a["uid"], date_from=date_from, psu_headers=psu, longest=first)
                    await _store_rows(owner, a, [r for r in (bs.outgoing(t, a["account_key"]) for t in raw) if r],
                                      summary)
            else:
                conn = await _pr()._revolut_conn(owner)
                if not conn:
                    raise BankLinkError(409, "Revolut Business isn't connected any more")
                client, cache = _pr()._client(conn), {}
                for a in accounts:
                    date_from, _ = bs.sync_from(a["synced_through"])
                    raw = await bs.revolut_transactions(client, a["uid"], date_from=date_from)
                    rows = []
                    for tx in raw:
                        rows += await bs.revolut_outgoing(client, tx, a["uid"], cache)
                    await _store_rows(owner, a, rows, summary)
            changes.update(last_sync_at=now, last_error="", status="active")
        except BankLinkError as exc:
            if bs.session_ended(exc):
                changes.update(status="needs_renewal", last_error="Your bank's consent has ended — renew it to keep reading.")
            elif bs.rate_limited(exc):
                changes["last_error"] = "Your bank asked Captain to slow down; it tries again at the next sync."
            else:
                changes["last_error"] = exc.message[:300]
            summary["error"] = changes["last_error"]
        except Exception as exc:  # noqa: BLE001 — one bank's failure never breaks the loop
            from captain_claw.flight_deck import revolut_business as rb

            message = exc.message if isinstance(exc, rb.RevolutError) else f"Reading the statement failed ({type(exc).__name__})"
            changes["last_error"] = message[:300]
            summary["error"] = changes["last_error"]
            if not isinstance(exc, rb.RevolutError):
                log.warning("Statement sync failed", provider=provider, error=type(exc).__name__)
        if unattended:
            changes["unattended_at"] = stamps
        await store().save_bank_session(owner, provider, sid, **changes)
        log.info("Statement sync", provider=provider, new=summary["new"], changed=summary["changed"],
                 error=bool(summary["error"]))
        return summary


async def _store_rows(owner: str, account: dict, rows: list[dict], summary: dict) -> None:
    new, changed = await store().upsert_bank_transactions(owner, rows)
    summary["new"] += new
    summary["changed"] += changed
    await store().update_bank_account(owner, account["uid"], synced_through=date.today().isoformat())


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def _first_read(sess: dict, psu: dict | None, wait: float = 20.0) -> str:
    """Start the first read; wait for it a little so the page can say how it went."""
    task = _spawn(sync_session(sess, psu=psu))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=wait)
    except asyncio.TimeoutError:
        return "Captain is still reading the last months of outgoing payments"
    if result.get("error"):
        return f"The first read didn't work ({html.escape(result['error'])}); Captain tries again at the next sync"
    return f"Captain read {result['new']} outgoing payments from the last months"


async def _has_work(owner: str) -> bool:
    """Bills a statement could confirm: live ones and ones the user said they paid."""
    from captain_claw.flight_deck.payments_store import LIVE_STATES

    return bool(await store().list(owner, statuses=(*LIVE_STATES, "user_paid"), limit=1))


async def _notice(owner: str, title: str, body: str, ref: str) -> None:
    try:
        from captain_claw.flight_deck.auth import get_db

        await get_db().add_notification(owner, "payment", title, body, ref_type="bank_statements", ref_id=ref)
    except Exception as exc:  # noqa: BLE001
        log.warning("Statement notification failed", error=str(exc))


async def background_sync(now: float | None = None) -> int:
    """The loop's statement step: renewal notices, then one unattended read per
    connection per slot. Returns how many connections were read."""
    now = time.time() if now is None else now
    slot = latest_slot(datetime.fromtimestamp(now))
    done = 0
    for sess in await store().list_bank_sessions():
        owner, provider, sid = sess["owner_id"], sess["provider"], sess["session_id"]
        valid_until = float(sess.get("valid_until") or 0)
        if provider == EB and valid_until:
            ends = datetime.fromtimestamp(valid_until).strftime("%d.%m.%Y.")
            if valid_until < now and sess["status"] == "active":
                await store().save_bank_session(owner, provider, sid, status="needs_renewal", notified="expired")
                await _notice(owner, f"Bank statement access for {sess['aspsp_name']} has ended",
                              "Renew it on the Payments page so Captain can keep confirming your bills.", sid)
                continue
            if valid_until - now < RENEW_NOTICE and not sess.get("notified"):
                await store().save_bank_session(owner, provider, sid, notified="expiring")
                await _notice(owner, f"Bank statement access for {sess['aspsp_name']} ends on {ends}",
                              "Renew it on the Payments page to keep Captain confirming your bills.", sid)
        tried = max(float(sess.get("last_sync_at") or 0), float(sess.get("last_attempt_at") or 0))
        if sess["status"] != "active" or tried >= slot:
            continue
        # Nothing to confirm: keep the history fresh, but only once a day.
        if not await _has_work(owner) and now - tried < 20 * 3600:
            continue
        await sync_session(sess, now=now)
        done += 1
    return done


async def purge(today: date | None = None) -> int:
    before = ((today or date.today()) - timedelta(days=bs.RETENTION_DAYS)).isoformat()
    return await store().purge_bank_transactions(before)


# ── views ────────────────────────────────────────────────────────────

async def _session_view(sess: dict) -> dict:
    owner = sess["owner_id"]
    accounts = await store().bank_accounts(owner, sess["provider"], sess["session_id"])
    return {
        "provider": sess["provider"], "session_id": sess["session_id"],
        "bank": sess["aspsp_name"] or ("Revolut Business" if sess["provider"] == REV else ""),
        "country": sess["aspsp_country"], "status": sess["status"], "valid_until": sess.get("valid_until"),
        "last_sync_at": sess.get("last_sync_at"), "last_error": sess.get("last_error", ""),
        "next_sync_at": next_slot() if sess["status"] == "active" else None,
        "unattended_left": (bs.unattended_left(sess.get("unattended_at") or []) if sess["provider"] == EB else None),
        "accounts": [{"uid": a["uid"], "label": a["label"], "currency": a["currency"], "watched": bool(a["watched"]),
                      "synced_through": a["synced_through"],
                      "outgoing": await store().count_bank_transactions(owner, a["account_key"])} for a in accounts],
    }


async def _redirect_url(request: Request, owner: str) -> str:
    override = os.environ.get("FD_ENABLEBANKING_STATEMENTS_REDIRECT_URL", "").strip()
    if override:
        return override
    base, _ = await _pr()._link_base(request, owner)
    return f"{base}/fd/payments/statements/callback"


# ── owner routes ─────────────────────────────────────────────────────

@router.get("/fd/payments/statements")
async def statements_status(request: Request, user: dict = Depends(get_current_user)):
    from captain_claw.flight_deck import bank_link

    _pr()._require_on()
    out = {"enabled": bs.enabled(), "configured": bank_link.configured(),
           "revolut_connected": bool(await _pr()._revolut_conn(user["id"])),
           "sync_times": [f"{h:02d}:{m:02d}" for h, m in sync_times()], "redirect_url": "", "sessions": []}
    if not out["enabled"]:
        return out
    if out["configured"]:
        out["redirect_url"] = await _redirect_url(request, user["id"])
    out["sessions"] = [await _session_view(s) for s in await store().list_bank_sessions(user["id"])]
    return out


@router.get("/fd/payments/statements/aspsps")
async def statements_aspsps(country: str = "HR", psu_type: str = "personal", user: dict = Depends(get_current_user)):
    _require_statements()
    if psu_type not in ("personal", "business") or not re.fullmatch(r"[A-Za-z]{2}", country):
        raise HTTPException(422, "country is a two-letter code; psu_type is personal or business")
    try:
        banks = await bs.aspsps(country, psu_type)
    except BankLinkError as exc:
        raise HTTPException(exc.status if exc.status in (409, 503) else 502, exc.message)
    return {"banks": [{"name": b.get("name"), "country": b.get("country"), "logo": b.get("logo", ""),
                       "beta": bool(b.get("beta")), "consent_days": bs.consent_seconds(b) // 86400} for b in banks]}


class ConnectRequest(BaseModel):
    name: str
    country: str = "HR"
    psu_type: str = "personal"


@router.post("/fd/payments/statements/connect")
async def statements_connect(req: ConnectRequest, request: Request, user: dict = Depends(get_current_user)):
    _require_statements()
    country = req.country.upper()
    if req.psu_type not in ("personal", "business") or not re.fullmatch(r"[A-Z]{2}", country):
        raise HTTPException(422, "country is a two-letter code; psu_type is personal or business")
    try:
        aspsp = next((b for b in await bs.aspsps(country, req.psu_type) if b.get("name") == req.name), None)
        if not aspsp:
            raise HTTPException(422, "That bank doesn't offer account information through Enable Banking")
        now = time.time()
        for stale in [s for s, e in _STATES.items() if e["expires"] < now]:
            _STATES.pop(stale, None)
        state = secrets.token_urlsafe(18)
        started = await bs.start_auth(aspsp=aspsp, psu_type=req.psu_type, state=state,
                                      redirect_url=await _redirect_url(request, user["id"]))
    except BankLinkError as exc:
        raise HTTPException(exc.status if exc.status in (409, 503) else 502, exc.message)
    _STATES[state] = {"owner": user["id"], "aspsp": aspsp, "psu_type": req.psu_type,
                      "valid_until": started["valid_until"], "expires": time.time() + STATE_TTL}
    return {"url": started["url"]}


async def save_session(owner: str, data: dict, aspsp: dict, psu_type: str, valid_until: float) -> dict:
    """Store a new Enable Banking session and its accounts (EUR current accounts
    watched). Reconnecting the same bank is a renewal: its accounts keep their
    watch choice and history, and the old session goes."""
    access = data.get("access") or {}
    try:
        until = datetime.fromisoformat(str(access.get("valid_until")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        until = valid_until
    sess = await store().save_bank_session(
        owner, EB, data["session_id"], aspsp_name=str((data.get("aspsp") or aspsp).get("name") or ""),
        aspsp_country=str((data.get("aspsp") or aspsp).get("country") or ""), psu_type=psu_type,
        required_psu_headers=list(aspsp.get("required_psu_headers") or []), valid_until=until,
        status="active", last_error="", notified="")
    known = {a["account_key"]: a for a in await store().bank_accounts(owner) if a["provider"] == EB}
    accounts = []
    for a in data.get("accounts") or []:
        if not a.get("uid"):
            continue
        before = known.get(bs.account_key(a))
        accounts.append({"uid": a["uid"], "account_key": bs.account_key(a),
                         "label": bs.account_label(a, sess["aspsp_name"]),
                         "currency": str(a.get("currency") or "EUR").upper(),
                         "watched": bool(before["watched"]) if before else bs.watch_by_default(a),
                         "synced_through": before["synced_through"] if before else ""})
    renewed = {(b["session_id"]) for b in known.values() if b["account_key"] in {a["account_key"] for a in accounts}}
    for old in renewed - {data["session_id"]}:
        await store().delete_bank_session(owner, EB, old, keep_transactions=True)
        try:
            await bs.delete_session(old)
        except BankLinkError as exc:
            log.info("Old Enable Banking session delete failed", error=exc.message)
    await store().save_bank_accounts(owner, EB, data["session_id"], accounts)
    return sess


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f'<title>{html.escape(title)}</title><body style="font:16px system-ui;max-width:520px;'
        f'margin:40px auto;padding:0 16px"><h2>{html.escape(title)}</h2>{body}</body>',
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@public_router.get("/fd/payments/statements/callback")
async def statements_callback(request: Request, code: str = "", state: str = "", error: str = "",
                              error_description: str = ""):
    """The bank (via Enable Banking) sends the user back here. The one-time state names the owner."""
    if not (_pr().enabled() and bs.enabled()):
        return _page("Statements are off", "<p>Bank statements aren't turned on for this Flight Deck.</p>", 404)
    entry = _STATES.pop(state, None) if state else None
    if not entry or entry["expires"] < time.time():
        return _page("Link expired", "<p>This connection link isn't known here any more. Start again from the "
                     "Payments page.</p>", 404)
    if error or not code:
        return _page("Not connected", "<p>Your bank didn't grant read access"
                     + (f" ({html.escape((error_description or error)[:200])})" if error else "")
                     + ". Nothing was shared. You can try again from the Payments page.</p>", 400)
    try:
        data = await bs.create_session(code)
        sess = await save_session(entry["owner"], data, entry["aspsp"], entry["psu_type"], entry["valid_until"])
    except BankLinkError as exc:
        return _page("Not connected", f"<p>{html.escape(exc.message)}</p>", 502)
    if not data.get("accounts"):
        return _page("Connected, but no accounts", "<p>Your bank shared no accounts. If this deck's Enable "
                     "Banking application is in <b>restricted</b> mode, it can only read accounts linked in its "
                     "Enable Banking control panel — link yours there, then connect again.</p>")
    # The user is here now: read the history with their presence (the page waits a little).
    read = await _first_read(sess, psu_headers(request, sess.get("required_psu_headers") or []))
    return _page("Bank statements connected", f"<p>{read} from {html.escape(sess['aspsp_name'])}, to confirm "
                 "the bills you pay. Captain never keeps incoming payments or balances, and it can't move money. "
                 "You can close this tab.</p>")


class WatchRequest(BaseModel):
    watched: bool


@router.put("/fd/payments/statements/accounts/{uid}")
async def statements_watch(uid: str, req: WatchRequest, user: dict = Depends(get_current_user)):
    _require_statements()
    if not any(a["uid"] == uid for a in await store().bank_accounts(user["id"])):
        raise HTTPException(404, "No such account")
    await store().update_bank_account(user["id"], uid, watched=1 if req.watched else 0)
    return {"ok": True}


@router.post("/fd/payments/statements/sync")
async def statements_sync(request: Request, user: dict = Depends(get_current_user)):
    """Read now, with the user present."""
    _require_statements()
    now = time.time()
    if now - _LAST_MANUAL.get(user["id"], 0) < MANUAL_EVERY:
        raise HTTPException(429, "Captain just read your statements — try again in a minute.")
    _LAST_MANUAL[user["id"]] = now
    results = []
    for sess in await store().list_bank_sessions(user["id"]):
        if sess["status"] != "active":
            continue
        psu = psu_headers(request, sess.get("required_psu_headers") or []) if sess["provider"] == EB else None
        results.append({"provider": sess["provider"], "session_id": sess["session_id"],
                        **await sync_session(sess, psu=psu)})
    return {"results": results, "sessions": [await _session_view(s)
                                             for s in await store().list_bank_sessions(user["id"])]}


@router.delete("/fd/payments/statements/{provider}/{session_id}")
async def statements_disconnect(provider: str, session_id: str, user: dict = Depends(get_current_user)):
    _require_statements()
    sess = await store().get_bank_session(user["id"], provider, session_id)
    if not sess:
        raise HTTPException(404, "No such connection")
    if provider == EB:
        try:
            await bs.delete_session(session_id)
        except BankLinkError as exc:   # forget it here anyway; the bank's consent lapses on its own
            log.info("Enable Banking session delete failed", error=exc.message)
    await store().delete_bank_session(user["id"], provider, session_id)
    return {"ok": True}


class RevolutStatementRequest(BaseModel):
    enabled: bool


@router.put("/fd/payments/statements/revolut")
async def statements_revolut(req: RevolutStatementRequest, user: dict = Depends(get_current_user)):
    """Use (or stop using) the Revolut Business connection's statement."""
    from captain_claw.flight_deck import revolut_business as rb

    _require_statements()
    owner = user["id"]
    if not req.enabled:
        await store().delete_bank_session(owner, REV, REVOLUT_SESSION)
        return {"enabled": False}
    conn = await _pr()._revolut_conn(owner)
    if not conn:
        raise HTTPException(409, "Connect Revolut Business first (Payments page → Revolut Business).")
    try:
        accounts = await _pr()._client(conn).accounts()
    except rb.RevolutError as exc:
        raise HTTPException(502, f"Revolut: {exc.message}")
    sess = await store().save_bank_session(owner, REV, REVOLUT_SESSION, aspsp_name="Revolut Business",
                                           status="active", last_error="")
    await store().save_bank_accounts(owner, REV, REVOLUT_SESSION, [
        {"uid": a["id"], "label": f"Revolut {a.get('name') or ''}".strip()[:60] + f" ({a.get('currency', '')})",
         "currency": str(a.get("currency") or "").upper(), "watched": str(a.get("currency")).upper() == "EUR"}
        for a in accounts if a.get("id") and str(a.get("state") or "active") == "active"])
    read = await _first_read(sess, None)
    return {"enabled": True, "message": read}
