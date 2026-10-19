"""Bank statement → bills — Payments Phase 4b (docs/payments-phase4-plan.md).

Runs the pure matcher (``payments/matching.py``) over one owner's bills and
stored outgoing transactions, and carries the decisions out:

- **settle**: an exact match books the bill as ``settled`` (the bank confirmed
  it); a bill the user already marked paid keeps its paid time.
- **pending**: the bank shows it pending; nothing changes yet.
- **suggest**: a near match waits for the owner to confirm or reject it on the
  card. A payment from before Captain prepared the bill is a blocking warning
  on it, so it isn't paid again before the owner looks.
- **evidence**: a bill already settled another way (a bank link, a Revolut
  draft) gets the statement row as evidence.
- **duplicate** / **reversed**: "paid twice" and "your bank reversed it" warnings
  plus a bell. Nothing is ever unsettled automatically.

Bills the user said they paid that the statement still doesn't show after four
business days get an info warning (``not_on_statement``), unless the owner
turned it off. The agent sees none of the transactions — only the summary on
the bill (booked on, which account).
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import date, datetime, timedelta

from captain_claw.logging import get_logger
from captain_claw.payments import checks, matching
from captain_claw.payments import iban as iban_mod

log = get_logger(__name__)

WARNINGS_PREF = "statement_warnings"
_LOCKS: dict[str, asyncio.Lock] = {}
_STATEMENT_CODES = ("not_on_statement", "paid_before_prepared")


def _pr():
    from captain_claw.flight_deck import payments_routes

    return payments_routes


def store():
    return _pr().store()


def _day(ts) -> date | None:
    return datetime.fromtimestamp(float(ts)).date() if ts else None


def _bill(row: dict) -> dict:
    f = row.get("fields") or {}
    return {"id": row["id"], "status": row["status"], "iban": iban_mod.normalize(f.get("iban") or row.get("iban") or ""),
            "amount_cents": int(f.get("amount_cents") or row.get("amount_cents") or 0),
            "ref_digits": matching.reference_of(f.get("model", ""), f.get("reference", "")),
            "name_tokens": matching.name_tokens(f.get("payee_name", "")),
            "created_day": _day(row.get("created_at")) or date.today(), "paid_day": _day(row.get("paid_at"))}


def _hr_day(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y.")
    except (TypeError, ValueError):
        return iso or ""


def _payee(row: dict) -> str:
    f = row.get("fields") or {}
    return f"{f.get('payee_name') or 'This bill'} · {checks.format_amount(int(f.get('amount_cents') or 0), f.get('currency', 'EUR'))}"


async def _bell(owner: str, title: str, body: str, intent_id: str) -> None:
    try:
        from captain_claw.flight_deck.auth import get_db

        await get_db().add_notification(owner, "payment", title, body, ref_type="payment", ref_id=intent_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Statement notification failed", error=str(exc))


def _with_warning(row: dict, code: str, severity: str, message: str) -> list[dict]:
    return [*(w for w in row.get("warnings") or [] if w.get("code") != code),
            {"code": code, "severity": severity, "message": message}]


def _status_after_warnings(row: dict, warnings: list[dict]) -> str:
    """A bill that can still be handed over moves between ready and needs_review
    with its blocking warnings; any other status stays."""
    if row["status"] not in ("ready", "needs_review"):
        return row["status"]
    return checks.status_for(row.get("fields") or {}, warnings, frozenset((row.get("acks") or {}).keys()))


async def settle(row: dict, tx: dict, label: str, match: str, actor: str = "statement") -> dict:
    """Book *row* as paid from the bank statement (or add the statement as evidence)."""
    owner = row["owner_id"]
    booked = matching.tx_day(tx)
    st = dict(row.get("statement") or {})
    st["settled_by"] = {"fp": tx["fp"], "booked_on": booked, "account_label": label, "match": match}
    st.pop("pending", None)
    st["suggestions"] = [x for x in st.get("suggestions") or [] if x.get("fp") != tx["fp"]]
    changes: dict = {"statement": st,
                     "warnings": [w for w in row.get("warnings") or [] if w.get("code") not in _STATEMENT_CODES]}
    newly = row["status"] != "settled"
    if newly:
        changes["status"] = "settled"
        if row["status"] != "user_paid" or not row.get("paid_at"):
            try:
                changes["paid_at"] = datetime.fromisoformat(f"{booked}T12:00:00").timestamp()
            except ValueError:
                changes["paid_at"] = time.time()
            changes["paid_via"] = "statement"
        if row.get("page_token_hash"):
            changes["page_expires_at"] = min(float(row.get("page_expires_at") or 0), time.time() + 86400)
    updated = await store().update(row["id"], **changes)
    await store().add_event(row["id"], owner, actor, "settled" if newly else "statement_evidence",
                            {"booked_on": booked, "account": label, "match": match})
    return updated


async def _apply(owner: str, d: matching.Decision, row: dict, tx: dict, label: str) -> dict:
    s = store()
    if d.kind == "settle":
        await s.set_statement_link(owner, d.fp, d.intent_id, "auto", "exact")
        await s.assign_transaction(owner, d.fp, d.intent_id, "auto")
        return await settle(row, tx, label, "auto")
    if d.kind == "evidence":
        await s.set_statement_link(owner, d.fp, d.intent_id, "auto", "evidence")
        await s.assign_transaction(owner, d.fp, d.intent_id, "auto")
        return await settle(row, tx, label, "evidence")
    if d.kind == "pending":
        await s.set_statement_link(owner, d.fp, d.intent_id, "pending")
        return row
    if d.kind == "suggest":
        await s.set_statement_link(owner, d.fp, d.intent_id, "suggested", d.reason)
        return row
    if d.kind == "duplicate":
        await s.set_statement_link(owner, d.fp, d.intent_id, "duplicate")
        await s.assign_transaction(owner, d.fp, d.intent_id, "duplicate")
        first = ((row.get("statement") or {}).get("settled_by") or {}).get("booked_on", "")
        message = (f"This bill seems to have left your account twice: {_hr_day(first)} and "
                   f"{_hr_day(matching.tx_day(tx))} ({label}). If the second one is a mistake, ask your bank "
                   "to recall it.")
        row = await s.update(row["id"], warnings=_with_warning(row, "paid_twice", checks.INFO, message))
        await s.add_event(row["id"], owner, "statement", "paid_twice", {"booked_on": matching.tx_day(tx)})
        await _bell(owner, f"Paid twice? {_payee(row)}", message, row["id"])
        return row
    if d.kind == "reversed":
        if any(w.get("code") == "bank_reversed" for w in row.get("warnings") or []):
            return row
        message = (f"Your bank reversed the payment booked {_hr_day(matching.tx_day(tx))} ({label}). "
                   "Check whether this bill is still unpaid.")
        row = await s.update(row["id"], warnings=_with_warning(row, "bank_reversed", checks.BLOCKING, message))
        await s.add_event(row["id"], owner, "statement", "bank_reversed", {"fp": d.fp})
        await _bell(owner, f"Payment reversed: {_payee(row)}", message, row["id"])
        return row
    return row


async def _refresh(owner: str, rows: dict[str, dict], links: list[dict], tx_by_fp: dict[str, dict],
                   labels: dict[str, str], not_seen: set[str], watched_labels: list[str]) -> None:
    """Bring each bill's statement summary and statement warnings up to date."""
    by_intent: dict[str, list[dict]] = {}
    for link in links:
        by_intent.setdefault(link["intent_id"], []).append(link)
    for iid, row in rows.items():
        st = dict(row.get("statement") or {})
        own = by_intent.get(iid, [])
        settled = st.get("settled_by")
        pending = next((link for link in own if link["state"] == "pending" and link["fp"] in tx_by_fp), None)
        if pending and not settled:
            tx = tx_by_fp[pending["fp"]]
            st["pending"] = {"fp": pending["fp"], "since": tx.get("value_date") or "",
                             "account_label": labels.get(tx.get("account_key"), "")}
        else:
            st.pop("pending", None)
        st["suggestions"] = [] if settled else [
            {"fp": link["fp"], "day": matching.tx_day(tx_by_fp[link["fp"]]),
             "amount_cents": tx_by_fp[link["fp"]]["amount_cents"],
             "creditor_name": tx_by_fp[link["fp"]].get("creditor_name", ""),
             "account_label": labels.get(tx_by_fp[link["fp"]].get("account_key"), ""), "reason": link["reason"]}
            for link in own if link["state"] == "suggested" and link["fp"] in tx_by_fp]
        if not st["suggestions"]:
            st.pop("suggestions")
        warnings = [w for w in row.get("warnings") or [] if w.get("code") not in _STATEMENT_CODES]
        before = next((x for x in st.get("suggestions") or [] if x["reason"] == "before_prepared"), None)
        if before and row["status"] in ("ready", "needs_review", "handed_off"):
            warnings.append({"code": "paid_before_prepared", "severity": checks.BLOCKING, "message": (
                f"You seem to have paid this on {_hr_day(before['day'])} ({before['account_label']}), before Captain "
                "prepared it. Confirm or reject that payment on the card before paying again.")})
        if iid in not_seen:
            warnings.append({"code": "not_on_statement", "severity": checks.INFO, "message": (
                f"Captain can't see this payment on {', '.join(watched_labels)} yet — paid from another account? "
                "You can ignore this.")})
        if st == (row.get("statement") or {}) and warnings == (row.get("warnings") or []):
            continue
        changes: dict = {"statement": st, "warnings": warnings}
        status = _status_after_warnings(row, warnings)
        if status != row["status"]:
            changes["status"] = status
        rows[iid] = await store().update(iid, **changes)
        if status != row["status"]:
            await store().add_event(iid, owner, "statement", status, {"reason": "statement warning"})


async def match_owner(owner: str, *, today: date | None = None) -> dict:
    """Match one owner's statement against their bills; returns decision counts."""
    from captain_claw.flight_deck import bank_statements as bs

    lock = _LOCKS.setdefault(owner, asyncio.Lock())
    async with lock:
        s = store()
        today = today or date.today()
        rows = await s.list(owner, statuses=(*matching.CANDIDATE_STATES, "settled"),
                            since=time.time() - bs.RETENTION_DAYS * 86400, limit=2000)
        txs = await s.bank_transactions(owner, since=(today - timedelta(days=bs.RETENTION_DAYS)).isoformat(),
                                        limit=10000)
        bank_sessions = await s.list_bank_sessions(owner)
        if not txs and not bank_sessions and not any(r.get("statement") for r in rows):
            return {}                    # no statement at all: nothing to say about any bill
        accounts = await s.bank_accounts(owner)
        labels = {a["account_key"]: a["label"] for a in accounts}
        by_id = {r["id"]: r for r in rows}
        tx_by_fp = {t["fp"]: t for t in txs}
        links = {(link["fp"], link["intent_id"]): link["state"] for link in await s.statement_links(owner)}
        counts: Counter = Counter()
        for d in matching.match([_bill(r) for r in rows], txs, links, today=today):
            row, tx = by_id.get(d.intent_id), tx_by_fp.get(d.fp)
            if not row or not tx:
                continue
            by_id[d.intent_id] = await _apply(owner, d, row, tx, labels.get(tx.get("account_key"), ""))
            counts[d.kind] += 1
        # Bills the user said they paid that the statement still doesn't show.
        link_rows = await s.statement_links(owner)
        links = {(link["fp"], link["intent_id"]): link["state"] for link in link_rows}
        sessions = {(x["provider"], x["session_id"]) for x in bank_sessions if x["status"] == "active"}
        watched = [a for a in accounts if a["watched"] and (a["provider"], a["session_id"]) in sessions]
        not_seen: set[str] = set()
        if watched and await s.get_pref(owner, WARNINGS_PREF) is not False:
            read_through = min(a["synced_through"] for a in watched)
            not_seen = set(matching.not_seen([_bill(r) for r in by_id.values()], links, read_through=read_through,
                                             today=today))
        await _refresh(owner, by_id, link_rows, tx_by_fp, labels, not_seen, [a["label"] for a in watched])
        counts["not_seen"] = len(not_seen)
        return dict(counts)


async def safe_match(owner: str) -> dict:
    """match_owner that only logs (a failing match never breaks a sync or a request)."""
    try:
        return await match_owner(owner)
    except Exception as exc:  # noqa: BLE001
        log.warning("Statement matching failed", error=f"{type(exc).__name__}: {exc}")
        return {}


async def match_all() -> int:
    """Every owner with a statement connection (the loop, hourly: "not on the statement" ages)."""
    owners = {x["owner_id"] for x in await store().list_bank_sessions()}
    for owner in owners:
        await safe_match(owner)
    return len(owners)


async def answer(row: dict, fp: str, accept: bool, actor: str) -> dict:
    """The owner confirms or rejects one suggested transaction for this bill."""
    from fastapi import HTTPException

    s = store()
    owner = row["owner_id"]
    lock = _LOCKS.setdefault(owner, asyncio.Lock())
    async with lock:
        links = await s.statement_links(owner)
        if not any(link["fp"] == fp and link["intent_id"] == row["id"] and link["state"] == "suggested"
                   for link in links):
            raise HTTPException(409, "That payment isn't waiting for your answer on this bill")
        tx = next((t for t in await s.bank_transactions(owner, limit=10000) if t["fp"] == fp), None)
        if not tx:
            raise HTTPException(404, "That bank payment isn't stored any more")
        if accept:
            await s.set_statement_link(owner, fp, row["id"], "confirmed")
            await s.assign_transaction(owner, fp, row["id"], "confirmed")
            for other in links:          # one transaction settles one bill
                if other["fp"] == fp and other["intent_id"] != row["id"] and other["state"] == "suggested":
                    await s.set_statement_link(owner, fp, other["intent_id"], "rejected")
            label = next((a["label"] for a in await s.bank_accounts(owner) if a["account_key"] == tx["account_key"]), "")
            await settle(row, tx, label, "confirmed" if row["status"] != "settled" else "evidence", actor)
        else:
            await s.set_statement_link(owner, fp, row["id"], "rejected")
            await s.add_event(row["id"], owner, actor, "statement_rejected", {"booked_on": matching.tx_day(tx)})
    await match_owner(owner)
    return await s.get(row["id"])
