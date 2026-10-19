"""Due-date reminders — Payments Phase 4c (docs/payments-phase4-plan.md).

Once a day, after the owner's reminder time and outside quiet hours
(``FD_SCHEDULER_QUIET_HOURS``), Flight Deck sends each owner one digest of the
bills due soon, today or overdue — to the bell, and to the chat the owner last
prepared a bill from (WhatsApp or Telegram, through the same delivery path as
other proactive messages, which keeps the WhatsApp allowlist and mute).

The text is Flight Deck's own: no model writes it. Each payable bill gets a
fresh link to its phone page (the barcode and "I paid"); a fresh link replaces
the earlier one, because only one link per bill is live. When bank statements
are connected and older than six hours, they're read first, so a bill paid
meanwhile isn't in the digest. Reminders can be switched off per bill (card,
phone page, or by asking the agent) or altogether (Payments page).
"""

from __future__ import annotations

import secrets
import time
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from captain_claw.flight_deck.auth import get_current_user
from captain_claw.logging import get_logger
from captain_claw.payments import checks, reminders as rem

log = get_logger(__name__)

router = APIRouter(tags=["payments-reminders"])
public_router = APIRouter(tags=["payments-reminders-public"])

PREF = "reminders"
SENT_PREF = "reminders_sent_day"
STATEMENT_FRESH = 6 * 3600
REMINDABLE = ("ready", "handed_off", "needs_review")


def _pr():
    from captain_claw.flight_deck import payments_routes

    return payments_routes


def store():
    return _pr().store()


async def owner_settings(owner: str) -> dict:
    return rem.settings(await store().get_pref(owner, PREF))


def _due(row: dict) -> date | None:
    try:
        return date.fromisoformat(str(row.get("due_date") or (row.get("fields") or {}).get("due_date") or ""))
    except ValueError:
        return None


async def _link_base(owner: str) -> str:
    """The deck address links should use (the same rule as shared files); '' when unknown."""
    try:
        from captain_claw.flight_deck import public_file_shares as pfs
        from captain_claw.flight_deck.auth import get_db

        db = get_db()
        base = (pfs._public_base() or await db.get_public_link_origin(owner) or await db.get_public_link_origin()
                or pfs._allowed_hosts_base())
    except Exception:  # noqa: BLE001
        base = ""
    return (base or "").rstrip("/")


async def _fresh_link(row: dict, base: str) -> str:
    """A new phone-page link for the bill (it replaces the earlier one)."""
    pr = _pr()
    token = secrets.token_urlsafe(24)
    await store().update(row["id"], page_token_hash=pr._token_hash(token), page_expires_at=time.time() + pr.PAGE_DAYS * 86400)
    return f"{base}/p/{token}"


async def _statements_first(owner: str, now: float) -> None:
    """Read stale statements before reminding, so bills paid meanwhile drop out."""
    from captain_claw.flight_deck import bank_statements, statements_routes

    if not bank_statements.enabled():
        return
    for sess in await store().list_bank_sessions(owner):
        if sess["status"] == "active" and now - float(sess.get("last_sync_at") or 0) > STATEMENT_FRESH:
            try:
                await statements_routes.sync_session(sess, now=now)
            except Exception as exc:  # noqa: BLE001 — remind anyway
                log.warning("Statement read before reminders failed", error=type(exc).__name__)


def _quiet(now_dt: datetime) -> bool:
    from captain_claw.flight_deck import fd_scheduler

    return fd_scheduler._in_quiet_hours(now_dt)


async def digest_owner(owner: str, now: float | None = None) -> dict:
    """Send the owner's digest if one is due now. Returns {sent, bills, chat}."""
    now = time.time() if now is None else now
    now_dt = datetime.fromtimestamp(now)
    today = now_dt.date()
    cfg = await owner_settings(owner)
    out = {"sent": False, "bills": 0, "chat": ""}
    if not cfg["enabled"] or now_dt.strftime("%H:%M") < cfg["time"] or _quiet(now_dt):
        return out
    if await store().get_pref(owner, SENT_PREF) == today.isoformat():
        return out
    await _statements_first(owner, now)
    picked = []
    for row in await store().list(owner, statuses=REMINDABLE, limit=500):
        due, sent = _due(row), row.get("reminders") or {}
        if not due or sent.get("off"):
            continue
        st = rem.stage(due, today, sent, days_before=cfg["days_before"], overdue=cfg["overdue"])
        if st:
            picked.append((due, st, row))
    if not picked:
        return out
    picked.sort(key=lambda x: (x[0], x[2]["id"]))
    base = await _link_base(owner)
    lines, bell = [], []
    for due, st, row in picked:
        f = row.get("fields") or {}
        head = (f"{f.get('payee_name') or 'A bill'} · "
                f"{checks.format_amount(int(f.get('amount_cents') or 0), f.get('currency', 'EUR'))} — {rem.when(due, today, st)}")
        if row["status"] == "needs_review":
            note = "needs your review in Flight Deck first"
        elif row.get("handoff_rail") == "revolut" and row["status"] == "handed_off":
            note = "it's waiting in Revolut Business as a draft"
        else:
            note = await _fresh_link(row, base) if base else ""
        lines.append(f"• {head}" + (f"\n  {note}" if note else ""))
        bell.append(head + (f" — {note}" if note and not note.startswith("http") else ""))
    count = len(picked)
    title = bell[0] if count == 1 else f"{count} bills to pay"
    text = ("Bills to pay:\n" + "\n".join(lines)
            + '\nPaid already? Tap "I paid" on its page, or tell Captain.')
    try:
        from captain_claw.flight_deck.auth import get_db

        await get_db().add_notification(owner, "payment", title[:200], "\n".join(bell)[:2000],
                                        ref_type="payment", ref_id=picked[0][2]["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Reminder bell failed", error=str(exc))
    chat = ""
    origin = _pr().reminder_origin(await store().latest_origin(owner))
    if origin:
        from captain_claw.flight_deck.delivery_routes import deliver_to_origin

        try:
            delivered, note = await deliver_to_origin(origin, text)
        except Exception as exc:  # noqa: BLE001
            delivered, note = False, type(exc).__name__
        chat = origin["kind"] if delivered else f"{origin['kind']} ({note})"
    day = today.isoformat()
    for _, st, row in picked:
        sent = dict(row.get("reminders") or {})
        sent[st] = day
        await store().update(row["id"], reminders=sent)
        await store().add_event(row["id"], owner, "reminder", f"reminded_{st}", {"chat": chat or "bell"})
    await store().set_pref(owner, SENT_PREF, day)
    log.info("Payment reminder sent", bills=count, chat=chat or "bell")
    return {"sent": True, "bills": count, "chat": chat}


async def run_all(now: float | None = None) -> int:
    """The loop's reminder step: every owner with a bill that has a due date."""
    sent = 0
    for owner in await store().owners_with_due_bills():
        try:
            sent += 1 if (await digest_owner(owner, now))["sent"] else 0
        except Exception as exc:  # noqa: BLE001 — one owner's failure never stops the rest
            log.warning("Payment reminders failed", error=f"{type(exc).__name__}: {exc}")
    return sent


async def set_bill_reminders(row: dict, on: bool, actor: str) -> dict:
    sent = dict(row.get("reminders") or {})
    if on:
        sent.pop("off", None)
    else:
        sent["off"] = True
    row = await store().update(row["id"], reminders=sent)
    await store().add_event(row["id"], row["owner_id"], actor, "reminders_on" if on else "reminders_off", {})
    return row


# ── routes ───────────────────────────────────────────────────────────

class SettingsRequest(BaseModel):
    enabled: bool = True
    days_before: int = 3
    time: str = "09:00"
    overdue: bool = True


async def _settings_view(owner: str) -> dict:
    import os

    origin = _pr().reminder_origin(await store().latest_origin(owner))
    address = origin.get("address", "")
    chat = ({"kind": origin["kind"], "label": f"{origin['kind'].capitalize()} ••{address[-4:]}"} if origin else None)
    return {**await owner_settings(owner), "chat": chat,
            "quiet_hours": os.environ.get("FD_SCHEDULER_QUIET_HOURS", "").strip()}


@router.get("/fd/payments/reminders")
async def reminders_get(user: dict = Depends(get_current_user)):
    _pr()._require_on()
    return await _settings_view(user["id"])


@router.put("/fd/payments/reminders")
async def reminders_put(req: SettingsRequest, user: dict = Depends(get_current_user)):
    _pr()._require_on()
    cfg = rem.settings(req.model_dump())
    if cfg["time"] != req.time:
        raise HTTPException(422, "time is HH:MM")
    await store().set_pref(user["id"], PREF, cfg)
    return await _settings_view(user["id"])


class BillRemindersRequest(BaseModel):
    on: bool


@router.put("/fd/payments/{intent_id}/reminders")
async def bill_reminders_owner(intent_id: str, req: BillRemindersRequest, user: dict = Depends(get_current_user)):
    _pr()._require_on()
    row = await _pr()._owned(intent_id, user["id"])
    return {"intent": _pr().view(await set_bill_reminders(row, req.on, f"user:{user['id']}"))}


@router.post("/fd/payments/agent/{intent_id}/reminders")
async def bill_reminders_agent(intent_id: str, req: BillRemindersRequest, request: Request):
    """Reminders aren't money: the agent may switch them when the user asks."""
    pr = _pr()
    pr._require_on()
    owner, slug = pr._agent_owner(request)
    row = await pr._owned(intent_id, owner)
    return {"intent": pr.view(await set_bill_reminders(row, req.on, f"agent:{slug}"))}


@public_router.post("/p/{token}/no-reminders")
async def page_no_reminders(token: str):
    """ "Don't remind me" on the phone page."""
    pr = _pr()
    row = await pr._page_row(token)
    if not row:
        return pr._gone()
    await set_bill_reminders(row, False, "page")
    return RedirectResponse(f"/p/{token}", status_code=303, headers={"Cache-Control": "no-store"})
