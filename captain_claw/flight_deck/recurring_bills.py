"""Recurring bills — Payments Phase 4d (docs/payments-phase4-plan.md).

Once a day per owner Flight Deck looks for bills that repeat — in Captain's own
paid bills and the outgoing payments on the bank statement — and suggests them
on the Payments page. Nothing acts until the owner confirms one ("Track it").

For an *active* recurring bill, each period (one expected payment) moves on:

- a payment that fits — a Captain bill paid in the period, or a statement row
  to the same IBAN (a direct debit or standing order) — covers it, and nothing
  more is sent;
- a bill with a fixed amount and reference is **prepared** five days before it's
  due (an ordinary bill: the checks, reminders and the user paying it in their
  bank all apply), optionally as a Revolut Business draft scheduled for the due
  date — still approved by the owner in the Revolut app;
- a bill that changes (utilities) waits for its invoice: one the agent prepares
  for the same IBAN is linked to the period; if none has come five days after
  the usual day, the next reminder digest says it's missing.

The agent can list recurring bills and propose one ("this is monthly"); only the
owner confirms, changes or removes them.
"""

from __future__ import annotations

import secrets
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from captain_claw.flight_deck.auth import get_current_user
from captain_claw.logging import get_logger
from captain_claw.payments import checks, iban as iban_mod, recurring as rc

log = get_logger(__name__)

router = APIRouter(tags=["payments-recurring"])
_detected: dict[str, date] = {}
_ID_PREFIX = "rec_"


def _pr():
    from captain_claw.flight_deck import payments_routes

    return payments_routes


def store():
    return _pr().store()


def _day(ts) -> date | None:
    return datetime.fromtimestamp(float(ts)).date() if ts else None


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def can_prepare(bill: dict) -> bool:
    """Captain can prepare it on its own only when nothing changes between periods."""
    return bill["amount_kind"] == "fixed" and bill["reference_kind"] == "fixed" and int(bill["amount_cents"]) > 0


def _template(row: dict) -> dict:
    f = row.get("fields") or {}
    return {k: f[k] for k in ("payee_street", "payee_city", "payee_oib", "purpose_code", "description", "currency")
            if f.get(k)}


async def _bell(owner: str, title: str, body: str, ref: str) -> None:
    try:
        from captain_claw.flight_deck.auth import get_db

        await get_db().add_notification(owner, "payment", title[:200], body[:2000], ref_type="payment", ref_id=ref)
    except Exception as exc:  # noqa: BLE001
        log.warning("Recurring bill notification failed", error=str(exc))


# ── detection ────────────────────────────────────────────────────────

async def detect_owner(owner: str, today: date) -> int:
    """Suggest the owner's repeating bills; returns how many new suggestions."""
    from captain_claw.flight_deck import bank_statements as bs
    from captain_claw.flight_deck.payments_store import PAID_STATES

    s = store()
    rows = await s.list(owner, statuses=PAID_STATES, since=time.time() - bs.RETENTION_DAYS * 86400, limit=2000)
    history = [{"iban": iban_mod.normalize(r["iban"]), "payee_name": (r.get("fields") or {}).get("payee_name", ""),
                "payee_key": r.get("payee_key", ""), "amount_cents": int(r["amount_cents"] or 0),
                "model": (r.get("fields") or {}).get("model", ""), "reference": r.get("reference", ""),
                "day": _day(r.get("paid_at"))} for r in rows if r.get("iban") and r.get("paid_at")]
    txs = await s.bank_transactions(owner, since=(today - timedelta(days=bs.RETENTION_DAYS)).isoformat(), limit=10000)
    statement = [{"iban": t["creditor_iban"], "creditor_name": t.get("creditor_name", ""),
                  "amount_cents": int(t["amount_cents"]), "day": _iso(t["booking_date"])}
                 for t in txs if t["status"] == "BOOK" and t.get("creditor_iban") and t.get("booking_date")
                 and t.get("currency", "EUR") == "EUR"]
    known = {b["iban"]: b for b in await s.list_recurring(owner)}
    latest = {r["iban"]: r for r in sorted(rows, key=lambda r: r.get("paid_at") or 0)}
    new = 0
    for p in rc.detect(history, statement, today=today):
        b = known.get(p.iban)
        stats = {"amount_min": p.amount_min, "amount_max": p.amount_max, "occurrences": p.occurrences}
        if b is None:
            await s.create_recurring({
                "id": f"{_ID_PREFIX}{secrets.token_hex(6)}", "owner_id": owner, "status": "suggested",
                "source": p.source, "payee_name": p.payee_name[:120], "payee_key": p.payee_key, "iban": p.iban,
                "model": p.model, "reference": p.reference, "reference_kind": p.reference_kind,
                "amount_kind": p.amount_kind, "amount_cents": p.amount_cents, "cadence": p.cadence,
                "anchor_day": p.anchor_day, "last_paid": p.last_paid, "auto_prepare": 1,
                "template": _template(latest[p.iban]) if p.iban in latest else {}, **stats})
            new += 1
        elif b["status"] == "suggested":
            await s.update_recurring(b["id"], cadence=p.cadence, anchor_day=p.anchor_day, amount_kind=p.amount_kind,
                                     amount_cents=p.amount_cents, last_paid=p.last_paid, **stats)
        elif b["status"] == "active" and b["amount_kind"] == "variable":
            await s.update_recurring(b["id"], **stats)
    return new


# ── periods ──────────────────────────────────────────────────────────

async def _paid_in_window(owner: str, bill: dict, start: date, end: date) -> tuple[str, str, str, date | None]:
    """(kind, intent_id, note, day): 'paid' by a Captain bill, 'covered' by a statement row, or ''."""
    from captain_claw.flight_deck.payments_store import PAID_STATES

    s = store()
    for r in await s.list(owner, statuses=PAID_STATES, since=time.time() - 400 * 86400, limit=500):
        paid = _day(r.get("paid_at"))
        if iban_mod.normalize(r["iban"]) == bill["iban"] and paid and start <= paid <= end \
                and rc.amount_fits(bill, int(r["amount_cents"] or 0)):
            return "paid", r["id"], f"paid {paid.strftime('%d.%m.')}", paid
    for t in await s.bank_transactions(owner, since=start.isoformat(), limit=2000):
        day = _iso(t.get("booking_date"))
        if (t["status"] == "BOOK" and t.get("creditor_iban") == bill["iban"] and day and start <= day <= end
                and rc.amount_fits(bill, int(t["amount_cents"]))):
            return ("covered", t.get("intent_id", ""),
                    f"paid outside Captain on {day.strftime('%d.%m.')} (direct debit or standing order?)", day)
    return "", "", "", None


async def prepare_bill(bill: dict, expected: date) -> dict:
    """An ordinary bill for this period (checks, reminders, the user pays it)."""
    pr = _pr()
    owner = bill["owner_id"]
    t = bill.get("template") or {}
    fields = {**t, "payee_name": bill["payee_name"], "iban": bill["iban"], "amount_cents": int(bill["amount_cents"]),
              "currency": t.get("currency") or "EUR", "model": bill["model"], "reference": bill["reference"],
              "due_date": expected.isoformat(),
              "description": t.get("description") or f"{bill['payee_name']} {expected.strftime('%m/%Y')}"}
    provenance = {k: "recurring" for k in ("payee_name", "iban", "amount_cents", "model", "reference")}
    intent_id = f"pay_{secrets.token_hex(6)}"
    warnings = await pr._evaluate(owner, intent_id, fields, provenance, recurring=True)
    status = checks.status_for(fields, warnings)
    row = await store().create({
        "id": intent_id, "owner_id": owner, "agent": "recurring", "session_id": "", "status": status,
        "fields": fields, "provenance": provenance, "warnings": warnings, "acks": {},
        "payee_key": checks.payee_key(fields["payee_name"], fields.get("payee_oib", "")), "iban": fields["iban"],
        "amount_cents": fields["amount_cents"], "reference": fields["reference"], "due_date": fields["due_date"],
        "source_name": "", "source_sha256": "", "source_kind": "recurring", "barcodes": checks.barcodes_for(fields),
        "supersedes": "", "automated": 1, "recurring_id": bill["id"],
    })
    await store().add_event(intent_id, owner, "recurring", "prepared", {"recurring": bill["id"], "status": status})
    amount = checks.format_amount(fields["amount_cents"], fields["currency"])
    await _bell(owner, f"Prepared from your recurring bill: {bill['payee_name']} · {amount}",
                f"Due {expected.strftime('%d.%m.%Y.')} — pay it in your bank as usual." if status == "ready"
                else "Review it on the Payments page before paying.", intent_id)
    if bill.get("revolut_draft") and status == "ready" and await pr._revolut_conn(owner):
        try:
            await pr._handoff_revolut(row, owner, "recurring", schedule_for=expected.isoformat())
        except HTTPException as exc:
            await store().add_event(intent_id, owner, "recurring", "revolut_draft_failed", {"reason": str(exc.detail)})
    return await store().get(intent_id)


async def advance(bill: dict, today: date) -> str:
    """Move one active recurring bill's current period on. Returns what happened."""
    from captain_claw.flight_deck.payments_store import PAID_STATES

    s = store()
    periods = await s.recurring_periods(bill["id"])
    cur = periods[-1] if periods else None
    if cur and today > rc.window(date.fromisoformat(cur["expected"]), bill["cadence"])[1]:
        cur = None                                         # that period is over, whatever it ended as
    if cur is None:
        base = _iso(periods[-1]["expected"]) if periods else _iso(bill["last_paid"]) or today
        expected = rc.next_due(base, bill["cadence"], bill["anchor_day"])
        while rc.window(expected, bill["cadence"])[1] < today:   # back from a pause: skip what's gone
            expected = rc.next_due(expected, bill["cadence"], bill["anchor_day"])
        if bill.get("next_due") != expected.isoformat():
            await s.update_recurring(bill["id"], next_due=expected.isoformat())
        if today < expected - timedelta(days=rc.LEAD_DAYS):
            return ""
        # Opened after its day (just confirmed, or the deck was off): Captain didn't watch it,
        # so it's never called missing.
        cur = await s.save_period(bill["id"], expected.isoformat(), status="expected",
                                  late=1 if today > expected else 0)
    expected = date.fromisoformat(cur["expected"])
    if cur["status"] in ("paid", "covered", "skipped"):
        return ""
    start, end = rc.window(expected, bill["cadence"])
    if cur["intent_id"]:
        linked = await s.get(cur["intent_id"])
        if linked and linked["status"] in PAID_STATES:
            await s.save_period(bill["id"], cur["expected"], status="paid",
                                note=f"paid {(_day(linked.get('paid_at')) or today).strftime('%d.%m.')}")
            await s.update_recurring(bill["id"], last_paid=(_day(linked.get("paid_at")) or today).isoformat())
            return "paid"
        if linked and linked["status"] in ("skipped", "cancelled"):
            await s.save_period(bill["id"], cur["expected"], status="skipped", note=f"bill {linked['status']}")
            return "skipped"
    kind, intent_id, note, paid_day = await _paid_in_window(bill["owner_id"], bill, start, min(end, today))
    if kind:
        await s.save_period(bill["id"], cur["expected"], status=kind, note=note,
                            intent_id=cur["intent_id"] or intent_id)
        await s.update_recurring(bill["id"], last_paid=max(bill["last_paid"] or "", paid_day.isoformat()))
        return kind
    if cur["intent_id"]:
        return ""                                          # the bill is out; its own reminders take it from here
    if (can_prepare(bill) and bill["auto_prepare"]
            and expected - timedelta(days=rc.PREPARE_DAYS) <= today <= expected + timedelta(days=rc.PREPARE_LATE_DAYS)):
        row = await prepare_bill(bill, expected)
        await s.save_period(bill["id"], cur["expected"], status="prepared", intent_id=row["id"])
        return "prepared"
    if cur["status"] == "expected" and not cur["late"] and today > expected + timedelta(days=rc.MISSING_AFTER_DAYS):
        await s.save_period(bill["id"], cur["expected"], status="missing")
        return "missing"
    return ""


async def link_new_bill(owner: str, row: dict) -> dict:
    """A bill the agent just prepared for a recurring payee belongs to its open period."""
    s = store()
    bill = next((b for b in await s.list_recurring(owner, ("active",))
                 if b["iban"] == iban_mod.normalize(row.get("iban") or "")), None)
    if not bill:
        return row
    periods = await s.recurring_periods(bill["id"])
    open_ = next((p for p in reversed(periods) if p["status"] in ("expected", "missing") and not p["intent_id"]), None)
    expected = open_["expected"] if open_ else (row.get("due_date") or bill.get("next_due") or date.today().isoformat())
    await s.save_period(bill["id"], expected, status="prepared", intent_id=row["id"])
    return await s.update(row["id"], recurring_id=bill["id"])


async def run_owner(owner: str, today: date | None = None) -> dict:
    today = today or date.today()
    out = {"suggested": 0}
    if _detected.get(owner) != today:
        out["suggested"] = await detect_owner(owner, today)
        _detected[owner] = today
    for bill in await store().list_recurring(owner, ("active",)):
        happened = await advance(bill, today)
        if happened:
            out[happened] = out.get(happened, 0) + 1
    return out


async def run_all(today: date | None = None) -> int:
    n = 0
    for owner in await store().payment_owners():
        try:
            await run_owner(owner, today)
            n += 1
        except Exception as exc:  # noqa: BLE001 — one owner's failure never stops the rest
            log.warning("Recurring bills failed", error=f"{type(exc).__name__}: {exc}")
    return n


async def missing_lines(owner: str) -> list[tuple[str, str, str]]:
    """(recurring_id, expected, line) for missing invoices the digest hasn't mentioned yet."""
    out = []
    for bill in await store().list_recurring(owner, ("active",)):
        for p in await store().recurring_periods(bill["id"]):
            if p["status"] == "missing" and not p["nudged"]:
                around = date.fromisoformat(p["expected"]).strftime("%d.%m.")
                out.append((bill["id"], p["expected"], f"{bill['payee_name']} — the bill usually comes around {around}, "
                                                       "not seen yet. Check your e-mail or their website?"))
    return out


async def mark_nudged(items: list[tuple[str, str, str]]) -> None:
    for rid, expected, _ in items:
        await store().save_period(rid, expected, nudged=1)


async def expected_soon(owner: str, today: date, days: int = 7) -> list[dict]:
    """Open periods without a bill yet, for the agent's list status=due."""
    out = []
    for bill in await store().list_recurring(owner, ("active",)):
        periods = await store().recurring_periods(bill["id"])
        cur = periods[-1] if periods else None
        if cur and cur["status"] in ("expected", "missing") and not cur["intent_id"]:
            exp = cur["expected"]
        elif bill.get("next_due") and (not cur or cur["status"] in ("paid", "covered", "skipped")):
            exp = bill["next_due"]
        else:
            continue
        if _iso(exp) and _iso(exp) <= today + timedelta(days=days):
            out.append({"payee_name": bill["payee_name"], "expected_on": exp, "amount_text": amount_text(bill),
                        "status": cur["status"] if cur and cur["expected"] == exp else "expected"})
    return out


# ── views and routes ─────────────────────────────────────────────────

def amount_text(bill: dict) -> str:
    if bill["amount_kind"] == "fixed" or bill["amount_min"] == bill["amount_max"]:
        return checks.format_amount(int(bill["amount_cents"]))
    return f"{checks.format_amount(int(bill['amount_min']))} – {checks.format_amount(int(bill['amount_max']))}"


async def view(bill: dict) -> dict:
    periods = (await store().recurring_periods(bill["id"]))[-6:]
    return {
        "id": bill["id"], "status": bill["status"], "source": bill["source"], "payee_name": bill["payee_name"],
        "iban_grouped": iban_mod.grouped(bill["iban"]), "cadence": bill["cadence"], "anchor_day": bill["anchor_day"],
        "amount_kind": bill["amount_kind"], "amount_text": amount_text(bill), "reference_kind": bill["reference_kind"],
        "reference": " ".join(x for x in (bill["model"], bill["reference"]) if x), "can_prepare": can_prepare(bill),
        "auto_prepare": bill["auto_prepare"], "revolut_draft": bill["revolut_draft"], "next_due": bill["next_due"],
        "last_paid": bill["last_paid"], "occurrences": bill["occurrences"],
        "periods": [{k: p[k] for k in ("expected", "status", "note", "intent_id")} for p in periods],
    }


async def _owned(recurring_id: str, owner: str) -> dict:
    bill = await store().get_recurring(recurring_id) if recurring_id.startswith(_ID_PREFIX) else None
    if not bill or bill["owner_id"] != owner:
        raise HTTPException(404, "No such recurring bill")
    return bill


@router.get("/fd/payments/recurring")
async def recurring_list(user: dict = Depends(get_current_user)):
    pr = _pr()
    pr._require_on()
    bills = [b for b in await store().list_recurring(user["id"]) if b["status"] != "dismissed"]
    conn = await pr._revolut_conn(user["id"])
    return {"bills": [await view(b) for b in bills], "revolut": bool(conn and conn.get("account_id"))}


class RecurringUpdate(BaseModel):
    status: str | None = None
    auto_prepare: bool | None = None
    revolut_draft: bool | None = None
    cadence: str | None = None
    anchor_day: int | None = None
    reference_kind: str | None = None


@router.put("/fd/payments/recurring/{recurring_id}")
async def recurring_update(recurring_id: str, req: RecurringUpdate, user: dict = Depends(get_current_user)):
    _pr()._require_on()
    bill = await _owned(recurring_id, user["id"])
    changes: dict = {}
    if req.status is not None:
        if req.status not in ("active", "paused", "dismissed"):
            raise HTTPException(422, "status is active, paused or dismissed")
        changes["status"] = req.status
    if req.cadence is not None:
        if req.cadence not in rc.CADENCES:
            raise HTTPException(422, f"cadence is one of {', '.join(rc.CADENCES)}")
        changes["cadence"] = req.cadence
    if req.anchor_day is not None:
        changes["anchor_day"] = max(1, min(31, int(req.anchor_day)))
    if req.reference_kind is not None:
        if req.reference_kind not in ("fixed", "per_bill"):
            raise HTTPException(422, "reference_kind is fixed or per_bill")
        changes["reference_kind"] = req.reference_kind
    if req.auto_prepare is not None:
        changes["auto_prepare"] = 1 if req.auto_prepare else 0
    if req.revolut_draft is not None:
        changes["revolut_draft"] = 1 if req.revolut_draft else 0
    bill = await store().update_recurring(bill["id"], **changes)
    if bill["status"] == "active":
        await advance(bill, date.today())
    return {"bill": await view(await store().get_recurring(bill["id"]))}


@router.delete("/fd/payments/recurring/{recurring_id}")
async def recurring_delete(recurring_id: str, user: dict = Depends(get_current_user)):
    _pr()._require_on()
    await store().delete_recurring((await _owned(recurring_id, user["id"]))["id"])
    return {"ok": True}


class RepeatsRequest(BaseModel):
    cadence: str = "monthly"


async def from_bill(row: dict, cadence: str, status: str, source: str) -> dict:
    """A recurring bill made from one of the owner's bills ("this repeats")."""
    if cadence not in rc.CADENCES:
        raise HTTPException(422, f"cadence is one of {', '.join(rc.CADENCES)}")
    f = row.get("fields") or {}
    iban = iban_mod.normalize(row.get("iban") or f.get("iban") or "")
    if not iban or not int(row.get("amount_cents") or 0):
        raise HTTPException(409, "This bill has no IBAN or amount to repeat")
    anchor = (_iso(row.get("due_date")) or _day(row.get("paid_at")) or date.today()).day
    existing = next((b for b in await store().list_recurring(row["owner_id"]) if b["iban"] == iban), None)
    if existing:
        bill = await store().update_recurring(existing["id"], status=status if existing["status"] != "active" else "active",
                                              cadence=cadence)
    else:
        bill = await store().create_recurring({
            "id": f"{_ID_PREFIX}{secrets.token_hex(6)}", "owner_id": row["owner_id"], "status": status,
            "source": source, "payee_name": str(f.get("payee_name") or "")[:120], "payee_key": row.get("payee_key", ""),
            "iban": iban, "model": f.get("model", ""), "reference": f.get("reference", ""),
            "reference_kind": "per_bill", "amount_kind": "fixed", "amount_cents": int(row["amount_cents"]),
            "amount_min": int(row["amount_cents"]), "amount_max": int(row["amount_cents"]), "cadence": cadence,
            "anchor_day": anchor, "last_paid": (_day(row.get("paid_at")) or _iso(row.get("due_date")) or date.today()).isoformat(),
            "occurrences": 1, "auto_prepare": 1, "template": _template(row)})
    await store().update(row["id"], recurring_id=bill["id"])
    return bill


@router.post("/fd/payments/{intent_id}/recurring")
async def owner_repeats(intent_id: str, req: RepeatsRequest, user: dict = Depends(get_current_user)):
    """The owner says this bill repeats: tracked at once."""
    pr = _pr()
    pr._require_on()
    bill = await from_bill(await pr._owned(intent_id, user["id"]), req.cadence, "active", "user")
    await advance(bill, date.today())                     # its next date shows at once
    return {"bill": await view(await store().get_recurring(bill["id"]))}


@router.post("/fd/payments/agent/{intent_id}/recurring")
async def agent_repeats(intent_id: str, req: RepeatsRequest, request: Request):
    """The user told the agent this bill repeats: a suggestion the owner confirms."""
    pr = _pr()
    pr._require_on()
    owner, _ = pr._agent_owner(request)
    bill = await from_bill(await pr._owned(intent_id, owner), req.cadence, "suggested", "user")
    return {"bill": await view(bill)}


@router.get("/fd/payments/agent/recurring")
async def agent_recurring(request: Request):
    pr = _pr()
    pr._require_on()
    owner, _ = pr._agent_owner(request)
    bills = [b for b in await store().list_recurring(owner) if b["status"] in ("active", "suggested", "paused")]
    return {"bills": [{"payee_name": b["payee_name"], "status": b["status"], "cadence": b["cadence"],
                       "amount_text": amount_text(b), "next_due": b["next_due"], "anchor_day": b["anchor_day"],
                       "prepares_itself": can_prepare(b) and b["auto_prepare"]} for b in bills]}
