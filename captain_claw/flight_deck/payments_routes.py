"""Payments, Phase 0 — bill pay by barcode (docs/payments-phase0-plan.md).

The agent prepares a payment from an invoice; Flight Deck decodes the invoice's
own barcode, checks the fields against the document, runs the warnings, keeps
the ledger, and renders the HUB3 PDF417 / EPC QR the user scans in their bank
app. Captain never moves money and never takes card data.

  agent   /fd/payments/agent/*   status · prepare · {id}/handoff · {id}/mark · list · {id}
  owner   /fd/payments*          list · config · settings · {id} · barcode · ack · mark
  public  /p/{token}             the phone page a handoff link opens (capability link)

Off unless ``FD_PAYMENTS`` is on. Blocking warnings are cleared only by the
signed-in owner (``POST /fd/payments/{id}/ack``) — never by an agent.
"""

from __future__ import annotations

import base64
import hashlib
import html
import importlib.util
import os
import re
import secrets
import time
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from captain_claw.flight_deck.auth import get_current_user
from captain_claw.flight_deck.payments_store import (
    LIVE_STATES, PAID_STATES, TERMINAL_STATES, PaymentsStore,
)
from captain_claw.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["payments"])
public_router = APIRouter(tags=["payments-public"])

MAX_SOURCE_BYTES = 10 * 1024 * 1024
PREPARES_PER_HOUR = 30
PAGE_DAYS = 14
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_ID_RE = re.compile(r"^pay_[0-9a-f]{12}$")
_DEPS = ("segno", "pdf417gen", "zxingcpp", "pypdfium2", "cryptography")

_data_dir: Path | None = None
_store: PaymentsStore | None = None


def configure(*, data_dir: Path) -> None:
    global _data_dir
    _data_dir = Path(data_dir)


def enabled() -> bool:
    return os.environ.get("FD_PAYMENTS", "").strip().lower() in ("1", "true", "yes", "on")


def deps_ok() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in _DEPS)


def store() -> PaymentsStore:
    global _store
    if _store is None:
        if _data_dir is None:
            raise HTTPException(503, "Payments are not configured on this deck")
        _store = PaymentsStore(_data_dir / "payments.db")
    return _store


def set_store(value: PaymentsStore | None) -> None:
    """Tests: point the routes at another store."""
    global _store
    _store = value


async def close() -> None:
    if _store is not None:
        await _store.close()


async def forget_user(user_id: str) -> None:
    """A deleted user's payments go with them."""
    if _data_dir is None and _store is None:
        return
    try:
        await store().delete_owner(user_id)
    except Exception as exc:   # never block a user deletion on this
        log.warning("Payments cleanup failed for a deleted user", error=str(exc))


def _require_on() -> None:
    if not enabled():
        raise HTTPException(404, "Payments are turned off on this deck")


def _require_deps() -> None:
    if not deps_ok():
        raise HTTPException(503, "This deck is missing the payments libraries (pip install .[payments])")


# ── field handling ────────────────────────────────────────────────────

def parse_amount(value) -> int:
    """'84,20' · '84.2' · '1.234,55' · '1,234.55' · '€ 12' → cents."""
    if isinstance(value, bool):
        raise ValueError("not an amount")
    if isinstance(value, int):
        return value * 100
    s = re.sub(r"[^\d.,\-]", "", str(value or ""))
    if not s or s.startswith("-"):
        raise ValueError("not an amount")
    m = re.search(r"[.,](\d{1,2})$", s)
    whole, frac = (s[:m.start()], m.group(1).ljust(2, "0")) if m else (s, "00")
    whole = re.sub(r"[.,]", "", whole) or "0"
    if not whole.isdigit():
        raise ValueError("not an amount")
    return int(whole) * 100 + int(frac)


def parse_due_date(value) -> str:
    s = str(value or "").strip()
    m = re.fullmatch(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})\.?", s)
    if m:
        s = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    try:
        return date.fromisoformat(s[:10]).isoformat() if s else ""
    except ValueError:
        return ""


def model_fields(raw: dict) -> dict:
    """The fields an agent sent, cleaned; unknown keys are dropped."""
    from captain_claw.payments import hr_ref, iban as iban_mod

    out: dict = {}
    for key in ("payee_name", "payee_street", "payee_city", "description", "purpose_code"):
        if raw.get(key):
            out[key] = " ".join(str(raw[key]).split())
    if raw.get("payee_oib"):
        out["payee_oib"] = re.sub(r"\D", "", str(raw["payee_oib"]))
    if raw.get("iban"):
        out["iban"] = iban_mod.normalize(raw["iban"])
    amount = raw.get("amount_cents") if raw.get("amount_cents") not in (None, "") else raw.get("amount")
    if amount not in (None, ""):
        try:
            out["amount_cents"] = (int(amount) if raw.get("amount_cents") not in (None, "")
                                   else parse_amount(amount))
        except (TypeError, ValueError):
            raise HTTPException(422, f"'{amount}' isn't an amount — send it like 84.20")
    if raw.get("currency"):
        out["currency"] = str(raw["currency"]).strip().upper()[:3]
    if raw.get("model"):
        out["model"] = hr_ref.normalize_model(raw["model"])
    if raw.get("reference"):
        out["reference"] = hr_ref.normalize_reference(raw["reference"])
    if raw.get("due_date"):
        out["due_date"] = parse_due_date(raw["due_date"])
    return {k: v for k, v in out.items() if v not in ("", None)}


def _barcode_fields(found) -> dict:
    keep = ("payee_name", "payee_street", "payee_city", "iban", "amount_cents", "currency",
            "model", "reference", "purpose_code", "description")
    return {k: v for k, v in found.fields.items() if k in keep and v not in ("", None, 0)}


_PAID_WORD_RE = re.compile(
    r"(?i)\b(?:paid|platio|platila|platili|platih|pla[cć]eno|pla[cć]en|uplatio|uplatila|"
    r"uplatili|upla[cć]eno|upla[cć]en)\b")
_NEGATION_RE = re.compile(
    r"(?i)\b(?:not|never|haven'?t|hasn'?t|didn'?t|nisam|nije|nismo|niste|nisu|ne|neću|necu|jo[sš]\s+nisam)\b"
    r"[\s\w']{0,14}$")


def paid_said(text: str) -> bool:
    """Does the user's own message say they paid? (Not a question, not negated.)"""
    text = str(text or "")
    for m in _PAID_WORD_RE.finditer(text):
        if _NEGATION_RE.search(text[max(0, m.start() - 24):m.start()]):
            continue
        rest = re.split(r"[.!\n]", text[m.end():], maxsplit=1)[0]
        if "?" in rest:
            continue
        return True
    return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso_day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else ""


def view(row: dict) -> dict:
    """What the agent and the UI get for one payment."""
    from captain_claw.payments import checks, iban as iban_mod

    f = row["fields"]
    acks = row.get("acks") or {}
    now = time.time()
    return {
        "id": row["id"], "status": row["status"],
        "payee_name": f.get("payee_name", ""), "payee_street": f.get("payee_street", ""),
        "payee_city": f.get("payee_city", ""), "payee_oib": f.get("payee_oib", ""),
        "iban": f.get("iban", ""), "iban_grouped": iban_mod.grouped(f.get("iban", "")),
        "bank": iban_mod.bank_name(f.get("iban", "")),
        "amount_cents": int(f.get("amount_cents") or 0),
        "amount_text": checks.format_amount(int(f.get("amount_cents") or 0), f.get("currency", "EUR")),
        "currency": f.get("currency", "EUR"),
        "model": f.get("model", ""), "reference": f.get("reference", ""),
        "purpose_code": f.get("purpose_code", ""), "description": f.get("description", ""),
        "due_date": f.get("due_date", ""),
        "provenance": row.get("provenance") or {},
        "warnings": [{**w, "acked": w.get("code") in acks} for w in row.get("warnings") or []],
        "barcodes": row.get("barcodes") or [],
        "source_name": row.get("source_name", ""), "source_kind": row.get("source_kind", ""),
        "automated": bool(row.get("automated")), "supersedes": row.get("supersedes", ""),
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "paid_at": row.get("paid_at"), "paid_via": row.get("paid_via", ""),
        "page_active": bool(row.get("page_token_hash")) and float(row.get("page_expires_at") or 0) > now,
        "review_path": f"/payments/{row['id']}",
        "handoff_rail": row.get("handoff_rail", ""),
        "revolut": _revolut_public((row.get("rails_state") or {}).get("revolut") or {}),
        "bank_link": _bank_public(row),
        "statement": _statement_public(row),
    }


def _statement_public(row: dict) -> dict:
    """What the bank statement says about this bill — no transaction details (the agent sees this)."""
    st = row.get("statement") or {}
    settled, pending = st.get("settled_by") or {}, st.get("pending") or {}
    return {"booked_on": settled.get("booked_on", ""), "account_label": settled.get("account_label", ""),
            "match": settled.get("match", ""), "pending_since": pending.get("since", ""),
            "pending_account": pending.get("account_label", ""), "suggestions": len(st.get("suggestions") or [])}


def _bank_public(row: dict) -> dict:
    from captain_claw.flight_deck import bank_link

    bk = (row.get("rails_state") or {}).get("bank") or {}
    if not bk:
        return {}
    fresh = (row.get("status") == "handed_off" and bk.get("url")
             and time.time() - float(bk.get("created_at") or 0) < bank_link.LINK_LIFETIME
             and (bk.get("status") or "RCVD") in ("RCVD", "PDNG"))
    return {"bank_name": (bk.get("aspsp") or {}).get("name", ""), "status": bk.get("status", ""),
            "label": bank_link.describe(bk.get("status", "")), "reason": bk.get("reason", ""),
            "url": bk["url"] if fresh else ""}


def _revolut_public(rv: dict) -> dict:
    return {k: rv[k] for k in ("draft_id", "state", "reason", "vop") if k in rv}


def render_barcodes(row: dict) -> list[dict]:
    """[{kind, png}] for the barcodes this payment gets."""
    from captain_claw.payments import checks, epc, hub3

    f = row["fields"]
    out: list[dict] = []
    for kind in checks.barcodes_for(f):
        if kind == "hub3":
            payload = hub3.build_payload(
                amount_cents=int(f.get("amount_cents") or 0), iban=f.get("iban", ""),
                payee_name=f.get("payee_name", ""), payee_street=f.get("payee_street", ""),
                payee_city=f.get("payee_city", ""), model=f.get("model", ""),
                reference=f.get("reference", ""), purpose_code=f.get("purpose_code", ""),
                description=f.get("description", "")).payload
            out.append({"kind": "hub3", "png": hub3.render_png(payload)})
        else:
            payload = epc.build_payload(
                name=f.get("payee_name", ""), iban=f.get("iban", ""),
                amount_cents=int(f.get("amount_cents") or 0), reference=f.get("reference", ""),
                text=f.get("description", ""), purpose=f.get("purpose_code", "")).payload
            out.append({"kind": "epc", "png": epc.render_png(payload)})
    return out


async def _evaluate(owner: str, intent_id: str, fields: dict, provenance: dict, *,
                    source_sha256: str = "", mismatches=(), not_in_document=()) -> list[dict]:
    from captain_claw.payments import checks, hub3

    s = store()
    recent = await s.list(owner, since=time.time() - 120 * 86400, limit=500)
    history = [{"payee_key": r["payee_key"], "iban": r["iban"], "amount_cents": r["amount_cents"],
                "paid_on": _iso_day(r.get("paid_at"))}
               for r in recent if r["status"] in PAID_STATES]
    others = [{"id": r["id"], "status": r["status"], "iban": r["iban"], "amount_cents": r["amount_cents"],
               "reference": r["reference"], "source_sha256": r["source_sha256"],
               "created_on": _iso_day(r.get("created_at"))} for r in recent]
    cap = await s.get_cap(owner)
    notes: list[str] = []
    if "hub3" in checks.barcodes_for(fields) and fields.get("iban") and fields.get("amount_cents"):
        try:
            notes = list(hub3.build_payload(
                amount_cents=int(fields["amount_cents"]), iban=fields["iban"],
                payee_name=fields.get("payee_name", ""), payee_street=fields.get("payee_street", ""),
                payee_city=fields.get("payee_city", ""), model=fields.get("model", ""),
                reference=fields.get("reference", ""), purpose_code=fields.get("purpose_code", ""),
                description=fields.get("description", "")).notes)
        except ValueError:
            notes = []
    warnings = checks.evaluate(
        fields, provenance, intent_id=intent_id, source_sha256=source_sha256, history=history,
        others=others, cap_cents=checks.DEFAULT_CAP_CENTS if cap is None else cap,
        mismatches=list(mismatches), not_in_document=list(not_in_document), text_notes=notes)
    return [w.as_dict() for w in warnings]


async def _owned(intent_id: str, owner: str) -> dict:
    row = await store().get(intent_id) if _ID_RE.match(intent_id or "") else None
    if not row or row["owner_id"] != owner:
        raise HTTPException(404, "No such payment")
    return row


async def _set_status(row: dict, status: str, actor: str, *, via: str = "", detail: dict | None = None) -> dict:
    if status in ("skipped", "cancelled"):
        await _drop_revolut_draft(row)
    changes: dict = {"status": status}
    if status in PAID_STATES:
        changes.update(paid_at=time.time(), paid_via=via)
    if status in TERMINAL_STATES and row.get("page_token_hash"):
        # The page stays readable a day (it shows the new status), then goes.
        changes["page_expires_at"] = min(float(row.get("page_expires_at") or 0), time.time() + 86400)
    updated = await store().update(row["id"], **changes)
    await store().add_event(row["id"], row["owner_id"], actor, status, detail or {"via": via})
    return updated


# ── agent routes ──────────────────────────────────────────────────────

def _agent_owner(request: Request) -> tuple[str, str]:
    from captain_claw.flight_deck.agent_identity import require_agent_caller
    from captain_claw.flight_deck.auth import _fd_auth_enabled

    caller = require_agent_caller(request, what="payments")
    owner = caller.owner or ("" if _fd_auth_enabled() else "local")
    if not owner:
        raise HTTPException(403, "This agent has no owner on record, so it can't prepare payments")
    return owner, caller.slug


class PrepareRequest(BaseModel):
    source_b64: str = ""
    source_name: str = ""
    fields: dict = {}
    user_text: str = ""          # the person's own message this turn (the tool sends it, not the model)
    session_id: str = ""
    automated: bool = False
    job_kind: str = ""
    supersedes: str = ""


class MarkRequest(BaseModel):
    status: str
    user_text: str = ""
    automated: bool = False


@router.get("/fd/payments/agent/status")
async def agent_status(request: Request):
    owner, _ = _agent_owner(request)
    return {"enabled": enabled(), "deps_ok": deps_ok(),
            "rails": await _rails(owner) if enabled() else []}


@router.post("/fd/payments/agent/prepare")
async def agent_prepare(req: PrepareRequest, request: Request):
    _require_on()
    _require_deps()
    owner, slug = _agent_owner(request)
    from captain_claw import card_scrub
    from captain_claw.payments import checks, decode, grounding

    s = store()
    await s.expire_stale()
    if await s.count_since(owner, time.time() - 3600) >= PREPARES_PER_HOUR:
        raise HTTPException(429, f"At most {PREPARES_PER_HOUR} payments an hour can be prepared")
    for value in req.fields.values():
        if card_scrub.looks_like_card_data(str(value)):
            raise HTTPException(422, "That looks like payment-card data. Captain never takes card "
                                     "numbers or CVVs: payments are approved in the user's bank app.")
    fields_in = model_fields(req.fields)

    data = b""
    if req.source_b64:
        try:
            data = base64.b64decode(req.source_b64, validate=True)
        except ValueError:
            raise HTTPException(422, "The source file isn't valid base64")
        if len(data) > MAX_SOURCE_BYTES:
            raise HTTPException(413, "The invoice file is over 10 MB")
    found, text = decode.read(data, req.source_name) if data else ([], "")
    sha = hashlib.sha256(data).hexdigest() if data else ""
    hub = next((f for f in found if f.kind == "hub3"), None)
    barcode = hub or (found[0] if found else None)
    source_kind = (f"barcode_{barcode.kind}" if barcode else
                   "document" if text.strip() else "image" if data else "none")
    document = "\n".join(t for t in (text, req.user_text) if t and t.strip())
    merged = grounding.merge(fields_in, _barcode_fields(barcode) if barcode else None, document)
    fields = {"currency": "EUR", **merged.fields}

    if not req.supersedes:
        existing = await s.find_live(owner, source_sha256=sha, iban=fields.get("iban", ""),
                                     amount_cents=int(fields.get("amount_cents") or 0),
                                     reference=fields.get("reference", ""))
        if existing:
            return {"intent": view(existing), "existing": True, "rails": await _rails(owner)}

    intent_id = f"pay_{secrets.token_hex(6)}"
    warnings = await _evaluate(owner, intent_id, fields, merged.provenance, source_sha256=sha,
                               mismatches=merged.mismatches, not_in_document=merged.not_in_document)
    status = checks.status_for(fields, warnings)
    row = await s.create({
        "id": intent_id, "owner_id": owner, "agent": slug, "session_id": req.session_id[:200],
        "status": status, "fields": fields, "provenance": merged.provenance, "warnings": warnings,
        "acks": {}, "payee_key": checks.payee_key(fields.get("payee_name", ""), fields.get("payee_oib", "")),
        "iban": fields.get("iban", ""), "amount_cents": int(fields.get("amount_cents") or 0),
        "reference": fields.get("reference", ""), "due_date": fields.get("due_date", ""),
        "source_name": os.path.basename(req.source_name)[:200], "source_sha256": sha,
        "source_kind": source_kind, "barcodes": checks.barcodes_for(fields),
        "supersedes": req.supersedes if _ID_RE.match(req.supersedes or "") else "",
        "automated": 1 if req.automated else 0,
    })
    await s.add_event(intent_id, owner, f"agent:{slug}", "prepared",
                      {"automated": req.automated, "job_kind": req.job_kind[:60], "status": status})
    if row["supersedes"]:
        old = await s.get(row["supersedes"])
        if old and old["owner_id"] == owner and old["status"] in LIVE_STATES:
            await _set_status(old, "cancelled", f"agent:{slug}", detail={"replaced_by": intent_id})
    if req.automated:
        await _notify(owner, row)
    row = await _statement_check(owner, row)
    log.info("Payment prepared", id=intent_id, owner=owner, status=status, source=source_kind)
    return {"intent": view(row), "existing": False, "rails": await _rails(owner)}


async def _statement_check(owner: str, row: dict) -> dict:
    """Phase 4b: match a new bill against the stored statement (it may be paid already)."""
    from captain_claw.flight_deck import bank_statements, statement_matching

    if not bank_statements.enabled():
        return row
    await statement_matching.safe_match(owner)
    return await store().get(row["id"]) or row


async def _notify(owner: str, row: dict) -> None:
    """A payment an automated job prepared (mail triage, cron) shows up in the bell."""
    try:
        from captain_claw.flight_deck.auth import get_db

        v = view(row)
        await get_db().add_notification(
            owner, "payment", f"Payment ready: {v['payee_name'] or 'invoice'} · {v['amount_text']}",
            "Prepared from an invoice — review it on the Payments page before paying.",
            ref_type="payment", ref_id=row["id"])
    except Exception as exc:
        log.warning("Payment notification failed", error=str(exc))


async def _link_base(request: Request, owner: str) -> tuple[str, bool]:
    import urllib.parse

    from captain_claw.flight_deck import public_file_shares

    base = await public_file_shares._agent_link_base(request, owner)
    host = urllib.parse.urlsplit(base).hostname or ""
    return base, public_file_shares._private_host(host)


class HandoffRequest(BaseModel):
    rail: str = "barcode"        # barcode | revolut | bank


def _handoff_gate(row: dict) -> None:
    """The same gate for every rail: complete, and no blocking warning open."""
    if row["status"] in TERMINAL_STATES:
        raise HTTPException(409, f"This payment is already {row['status'].replace('_', ' ')}")
    if row["status"] == "draft":
        problems = [w["message"] for w in row["warnings"] if w.get("severity") == "error"]
        raise HTTPException(409, "This payment can't be handed over yet: "
                            + (" ".join(problems) or "a payee name, IBAN or amount is missing."))
    if row["status"] == "needs_review":
        open_ = [w["message"] for w in row["warnings"]
                 if w.get("severity") == "blocking" and w.get("code") not in (row.get("acks") or {})]
        raise HTTPException(409, "The user has to review this payment in Flight Deck first "
                            f"({row['id']}, Payments page): " + " ".join(open_))


@router.post("/fd/payments/agent/{intent_id}/handoff")
async def agent_handoff(intent_id: str, request: Request, req: HandoffRequest | None = None):
    _require_on()
    owner, slug = _agent_owner(request)
    row = await _owned(intent_id, owner)
    _handoff_gate(row)
    rail = (req or HandoffRequest()).rail
    if rail == REVOLUT:
        return await _handoff_revolut(row, owner, f"agent:{slug}")
    if rail == BANK:
        return await _handoff_bank(row, owner, f"agent:{slug}", request)
    _require_deps()
    images = render_barcodes(row)
    token = secrets.token_urlsafe(24)
    row = await store().update(row["id"], status="handed_off", page_token_hash=_token_hash(token),
                               page_expires_at=time.time() + PAGE_DAYS * 86400,
                               handoff_rail=row.get("handoff_rail") or "barcode")
    await store().add_event(row["id"], owner, f"agent:{slug}", "handed_off",
                            {"barcodes": [i["kind"] for i in images]})
    base, local_only = await _link_base(request, owner)
    return {
        "intent": view(row), "page_url": f"{base}/p/{token}", "local_only": local_only,
        "images": [{"kind": i["kind"], "png_b64": base64.b64encode(i["png"]).decode()} for i in images],
    }


@router.post("/fd/payments/agent/{intent_id}/mark")
async def agent_mark(intent_id: str, req: MarkRequest, request: Request):
    _require_on()
    owner, slug = _agent_owner(request)
    if req.automated:
        raise HTTPException(403, "Only the user can mark a payment; this is an automated turn")
    row = await _owned(intent_id, owner)
    if req.status not in ("user_paid", "skipped", "cancelled"):
        raise HTTPException(422, "status is one of user_paid, skipped, cancelled")
    if row["status"] in TERMINAL_STATES:
        raise HTTPException(409, f"This payment is already {row['status'].replace('_', ' ')}")
    if req.status == "user_paid" and not paid_said(req.user_text):
        raise HTTPException(403, "The user's message doesn't say they paid. Only mark it paid when "
                                 "they tell you so — or they can tap 'I paid' on the payment card.")
    row = await _set_status(row, req.status, f"agent:{slug}", via="chat")
    return {"intent": view(row)}


@router.get("/fd/payments/agent/list")
async def agent_list(request: Request, status: str = "", q: str = "", limit: int = 20):
    _require_on()
    owner, _ = _agent_owner(request)
    await store().expire_stale()
    statuses = tuple(x for x in status.split(",") if x) if status else ()
    rows = await _sync_rows(await store().list(owner, statuses=statuses, limit=max(1, min(limit, 100))))
    if q:
        needle = q.casefold()
        rows = [r for r in rows if needle in str(r["fields"].get("payee_name", "")).casefold()
                or needle in str(r["fields"].get("description", "")).casefold()]
    return {"payments": [view(r) for r in rows]}


@router.get("/fd/payments/agent/{intent_id}")
async def agent_get(intent_id: str, request: Request):
    _require_on()
    owner, _ = _agent_owner(request)
    row = await _sync_rail(await _owned(intent_id, owner))
    return {"intent": view(row), "rails": await _rails(owner)}


# ── owner routes ──────────────────────────────────────────────────────

class AckRequest(BaseModel):
    codes: list[str]


class OwnerMarkRequest(BaseModel):
    status: str


class SettingsRequest(BaseModel):
    cap_cents: int


async def _config(owner: str) -> dict:
    from captain_claw.payments import checks

    cap = await store().get_cap(owner) if enabled() else None
    return {"enabled": enabled(), "deps_ok": deps_ok(),
            "cap_cents": checks.DEFAULT_CAP_CENTS if cap is None else cap,
            "default_cap_cents": checks.DEFAULT_CAP_CENTS}


@router.get("/fd/payments/config")
async def owner_config(user: dict = Depends(get_current_user)):
    return await _config(user["id"])


@router.put("/fd/payments/settings")
async def owner_settings(req: SettingsRequest, user: dict = Depends(get_current_user)):
    _require_on()
    if req.cap_cents < 0 or req.cap_cents > 10 ** 11:
        raise HTTPException(422, "The limit is between 0 (no limit) and 1,000,000,000 EUR")
    await store().set_cap(user["id"], req.cap_cents)
    return await _config(user["id"])


@router.get("/fd/payments")
async def owner_list(status: str = "", user: dict = Depends(get_current_user)):
    _require_on()
    await store().expire_stale()
    statuses = tuple(x for x in status.split(",") if x) if status else ()
    rows = await _sync_rows(await store().list(user["id"], statuses=statuses))
    return {"payments": [view(r) for r in rows], **(await _config(user["id"]))}


@router.get("/fd/payments/{intent_id}")
async def owner_get(intent_id: str, user: dict = Depends(get_current_user)):
    _require_on()
    row = await _sync_rail(await _owned(intent_id, user["id"]))
    return {"intent": view(row), "events": await store().events(intent_id), "rails": await _rails(user["id"]),
            "statement_suggestions": (row.get("statement") or {}).get("suggestions") or []}


@router.get("/fd/payments/{intent_id}/barcode/{kind}.png")
async def owner_barcode(intent_id: str, kind: str, user: dict = Depends(get_current_user)):
    _require_on()
    _require_deps()
    row = await _owned(intent_id, user["id"])
    if row["status"] not in ("ready", "handed_off"):
        raise HTTPException(409, "There's no barcode while the payment is " + row["status"].replace("_", " "))
    image = next((i for i in render_barcodes(row) if i["kind"] == kind), None)
    if image is None:
        raise HTTPException(404, "This payment has no such barcode")
    return Response(image["png"], media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/fd/payments/{intent_id}/ack")
async def owner_ack(intent_id: str, req: AckRequest, user: dict = Depends(get_current_user)):
    """The owner checked a blocking warning: the only way past one."""
    from captain_claw.payments import checks

    _require_on()
    row = await _owned(intent_id, user["id"])
    if row["status"] in TERMINAL_STATES:
        raise HTTPException(409, f"This payment is already {row['status'].replace('_', ' ')}")
    blocking = {w["code"] for w in row["warnings"] if w.get("severity") == "blocking"}
    codes = [c for c in req.codes if c in blocking]
    if not codes:
        raise HTTPException(422, "None of those are open warnings on this payment")
    acks = dict(row.get("acks") or {})
    for code in codes:
        acks[code] = {"by": user["id"], "at": time.time()}
    status = row["status"]
    if status in ("needs_review", "ready"):
        status = checks.status_for(row["fields"], row["warnings"], set(acks))
    row = await store().update(intent_id, acks=acks, status=status)
    await store().add_event(intent_id, row["owner_id"], f"user:{user['id']}", "acknowledged", {"codes": codes})
    return {"intent": view(row)}


@router.post("/fd/payments/{intent_id}/mark")
async def owner_mark(intent_id: str, req: OwnerMarkRequest, request: Request, user: dict = Depends(get_current_user)):
    _require_on()
    if req.status not in ("user_paid", "skipped", "cancelled"):
        raise HTTPException(422, "status is one of user_paid, skipped, cancelled")
    row = await _owned(intent_id, user["id"])
    if row["status"] in TERMINAL_STATES:
        raise HTTPException(409, f"This payment is already {row['status'].replace('_', ' ')}")
    row = await _set_status(row, req.status, f"user:{user['id']}", via="card")
    if req.status == "user_paid":
        from captain_claw.flight_deck import statements_routes

        await statements_routes.after_user_paid(user["id"], request)
    return {"intent": view(row)}


# ── Revolut Business payment drafts (Phase 1) ─────────────────────────
#
# docs/payments-phase1-plan.md. The token is scoped READ,WRITE: it creates
# counterparties and drafts, never payments. The user sends the draft in the
# Revolut Business app; the draft's state flows back here on every read.

REVOLUT = "revolut"
_CONNECT_TTL = 600
_CONNECT_STATES: dict[str, tuple[str, float]] = {}    # state → (owner, expires)
_SYNC_MAX = 10                                         # drafts re-read per list


async def _revolut_conn(owner: str) -> dict | None:
    conn = await store().get_connection(owner, REVOLUT)
    return conn if conn and conn.get("refresh_token") else None


async def _rails(owner: str) -> list[str]:
    from captain_claw.flight_deck import bank_link

    rails = ["barcode"]
    conn = await _revolut_conn(owner)
    if conn and conn.get("account_id"):
        rails.append(REVOLUT)
    if bank_link.configured() and await store().get_pref(owner, BANK_PREF):
        rails.append(BANK)
    return rails


def _client(conn: dict):
    from captain_claw.flight_deck import revolut_business as rb

    owner = conn["owner_id"]

    async def save(tokens: dict) -> None:
        await store().save_connection(owner, REVOLUT, **tokens)

    return rb.RevolutClient(dict(conn), save)


def _warn(row: dict, code: str, severity: str, message: str) -> list[dict]:
    """The row's warnings with *code* (re)set."""
    out = [w for w in row.get("warnings") or [] if w.get("code") != code]
    out.append({"code": code, "severity": severity, "message": message})
    return out


async def _sync_revolut(row: dict) -> dict:
    """Read the payment's Revolut draft and move the ledger on: COMPLETED →
    settled, DECLINED/FAILED/REVERTED → failed, gone → cancelled."""
    from captain_claw.flight_deck import revolut_business as rb

    rv = dict((row.get("rails_state") or {}).get(REVOLUT) or {})
    if row.get("status") != "handed_off" or not rv.get("draft_id"):
        return row
    conn = await _revolut_conn(row["owner_id"])
    if not conn:
        return row
    try:
        draft = await _client(conn).get_draft(rv["draft_id"])
    except Exception as exc:  # the card shows the last known state
        log.info("Revolut draft read failed", id=row["id"], error=str(exc))
        return row
    payment = ((draft or {}).get("payments") or [{}])[0]
    state = str(payment.get("state") or ("" if draft else "DELETED")).upper()
    reason = str(payment.get("reason") or payment.get("error_message") or "")
    if state == rv.get("state") and state not in rb.DRAFT_STATES_DONE + rb.DRAFT_STATES_FAILED + rb.DRAFT_STATES_GONE:
        return row
    rv.update(state=state, reason=reason[:200], checked_at=time.time())
    rails_state = {**(row.get("rails_state") or {}), REVOLUT: rv}
    row = await store().update(row["id"], rails_state=rails_state)
    if state in rb.DRAFT_STATES_DONE:
        row = await _set_status(row, "settled", "revolut", via=REVOLUT, detail={"draft_id": rv["draft_id"]})
    elif state in rb.DRAFT_STATES_FAILED:
        row = await store().update(row["id"], warnings=_warn(
            row, "revolut_failed", "info", f"Revolut didn't complete the payment ({state.lower()}"
            + (f": {reason}" if reason else "") + "). Nothing was paid; prepare it again to retry."))
        row = await _set_status(row, "failed", "revolut", via=REVOLUT, detail={"state": state, "reason": reason})
    elif state in rb.DRAFT_STATES_GONE:
        row = await _set_status(row, "cancelled", "revolut", via=REVOLUT, detail={"state": state})
    return row


async def _sync_rows(rows: list[dict]) -> list[dict]:
    out, left = [], _SYNC_MAX
    for row in rows:
        if left and row.get("status") == "handed_off" and row.get("handoff_rail") in (REVOLUT, BANK):
            left -= 1
            row = await _sync_rail(row)
        out.append(row)
    return out


async def _drop_revolut_draft(row: dict) -> None:
    """A payment skipped or cancelled in Captain takes its unsent draft with it."""
    rv = (row.get("rails_state") or {}).get(REVOLUT) or {}
    if not rv.get("draft_id") or rv.get("state") not in ("CREATED", None, ""):
        return
    conn = await _revolut_conn(row["owner_id"])
    if conn:
        try:
            await _client(conn).delete_draft(rv["draft_id"])
        except Exception as exc:
            log.warning("Revolut draft delete failed", id=row["id"], error=str(exc))


def _draft_reference(fields: dict) -> str:
    from captain_claw.payments import hr_ref

    model, ref = fields.get("model", ""), fields.get("reference", "")
    if ref and model and model != hr_ref.NO_REFERENCE_MODEL:
        return f"{model} {ref}"
    return ref or fields.get("description", "") or fields.get("payee_name", "")


async def _handoff_revolut(row: dict, owner: str, actor: str) -> dict:
    """Put the payment into Revolut Business as a draft (gate already passed)."""
    from captain_claw.flight_deck import revolut_business as rb
    from captain_claw.payments import checks, iban as iban_mod

    conn = await _revolut_conn(owner)
    if not conn:
        raise HTTPException(409, "Revolut Business isn't connected — connect it on the Payments page.")
    if not conn.get("account_id"):
        raise HTTPException(409, "Pick the Revolut account to pay from on the Payments page first.")
    fields = row["fields"]
    account = iban_mod.normalize(fields.get("iban", ""))
    if (fields.get("currency") or "EUR") != "EUR" or not iban_mod.is_sepa(account):
        raise HTTPException(409, "Revolut drafts here are for EUR payments to SEPA IBANs.")
    rv = dict((row.get("rails_state") or {}).get(REVOLUT) or {})
    if rv.get("draft_id") and rv.get("state") in ("CREATED", "PENDING"):
        return {"intent": view(row), "rail": REVOLUT, "existing": True}
    client = _client(conn)
    name = fields.get("payee_name", "")
    warnings = list(row.get("warnings") or [])

    try:
        # Verification of Payee, once per payment: a payee name that doesn't
        # match the IBAN's holder is the owner's call, not the agent's.
        if "vop" not in rv:
            try:
                result = await client.validate_name(name=name, iban=account, individual=False)
            except rb.RevolutError as exc:
                result = {"result_code": "error", "message": exc.message}
            code = str(result.get("result_code") or "error")
            actual = str(result.get("company_name") or "")
            rv["vop"] = {"result_code": code, "name": actual, "at": time.time()}
            temp = {**row, "warnings": warnings}
            if code in ("close_match", "not_matched"):
                said = (f"the bank says the account holder is '{actual}'" if actual and code == "close_match"
                        else "the bank says the name doesn't match the account")
                warnings = _warn(temp, f"vop_{code}", "blocking", (
                    f"Verification of Payee: you're paying '{name}', but {said}. Check the invoice "
                    "and the payee before paying."))
                acks = row.get("acks") or {}
                row = await store().update(row["id"], warnings=warnings,
                                           rails_state={**(row.get("rails_state") or {}), REVOLUT: rv},
                                           status=checks.status_for(fields, warnings, set(acks)))
                await store().add_event(row["id"], owner, actor, "vop", rv["vop"])
                raise HTTPException(409, "Verification of Payee didn't confirm the payee's name — the "
                                         f"user has to review it in Flight Deck first ({row['id']}).")
            if code != "matched":
                warnings = _warn(temp, "vop_unavailable", "info",
                                 "The payee's name couldn't be checked against the IBAN (Verification of Payee).")

        counterparty = await client.find_counterparty(account) or await client.create_counterparty(
            name=name, iban=account, individual=False)
        cp_account = next((a.get("id") for a in counterparty.get("accounts") or []
                           if iban_mod.normalize(a.get("iban", "")) == account), "")
        if fields.get("model") and account.startswith("HR"):
            warnings = _warn({"warnings": warnings}, "reference_as_text", "info", (
                "Revolut sends the model and poziv na broj as plain text, so the payee may take longer "
                "to match this payment than one made from the barcode."))
        draft_id = await client.create_draft(
            account_id=conn["account_id"], counterparty_id=counterparty["id"],
            counterparty_account_id=cp_account, amount_cents=int(fields.get("amount_cents") or 0),
            reference=_draft_reference(fields), title=f"Captain · {name}")
    except rb.RevolutError as exc:
        raise HTTPException(502, f"Revolut Business refused it: {exc.message}")
    rv.update(draft_id=draft_id, counterparty_id=counterparty["id"], account_id=conn["account_id"],
              state="CREATED", created_at=time.time())
    row = await store().update(row["id"], status="handed_off", handoff_rail=REVOLUT, warnings=warnings,
                               rails_state={**(row.get("rails_state") or {}), REVOLUT: rv})
    await store().add_event(row["id"], owner, actor, "revolut_draft", {"draft_id": draft_id})
    log.info("Revolut payment draft created", id=row["id"], owner=owner)
    return {"intent": view(row), "rail": REVOLUT, "existing": False}


def _connection_view(conn: dict | None) -> dict:
    conn = conn or {}
    return {
        "connected": bool(conn.get("refresh_token")), "environment": conn.get("environment") or "production",
        "client_id": conn.get("client_id", ""), "redirect_uri": conn.get("redirect_uri", ""),
        "has_key": bool(conn.get("private_key")), "certificate": conn.get("certificate", ""),
        "account_id": conn.get("account_id", ""), "account_label": conn.get("account_label", ""),
        "connected_at": conn.get("connected_at"), "scope": "READ,WRITE",
    }


class RevolutKeyRequest(BaseModel):
    environment: str = "production"


class RevolutClientRequest(BaseModel):
    client_id: str
    environment: str = "production"


class RevolutCodeRequest(BaseModel):
    code: str


class RevolutAccountRequest(BaseModel):
    account_id: str


def _callback_uri(request: Request) -> str:
    origin = (request.headers.get("origin") or str(request.base_url)).rstrip("/")
    return f"{origin}/fd/payments/revolut/callback"


@router.get("/fd/payments/revolut/status")   # not /revolut: /fd/payments/{id} is declared first
async def revolut_status(user: dict = Depends(get_current_user)):
    _require_on()
    return {**_connection_view(await store().get_connection(user["id"], REVOLUT)),
            "cards": bool(await store().get_pref(user["id"], "revolut_cards"))}


@router.post("/fd/payments/revolut/key")
async def revolut_key(req: RevolutKeyRequest, request: Request, user: dict = Depends(get_current_user)):
    """A fresh key + certificate to upload in Revolut Business. A new key means
    a new Client ID, so any earlier connection is dropped."""
    from captain_claw.flight_deck import revolut_business as rb

    _require_on()
    key, cert = rb.generate_key_and_certificate()
    conn = await store().save_connection(
        user["id"], REVOLUT, environment=rb.environment(req.environment), private_key=key, certificate=cert,
        redirect_uri=_callback_uri(request), client_id="", refresh_token="", access_token="",
        access_expires_at=0, account_id="", account_label="", connected_at=None)
    return _connection_view(conn)


@router.put("/fd/payments/revolut/client")
async def revolut_client_id(req: RevolutClientRequest, user: dict = Depends(get_current_user)):
    from captain_claw.flight_deck import revolut_business as rb

    _require_on()
    conn = await store().get_connection(user["id"], REVOLUT)
    if not conn or not conn.get("private_key"):
        raise HTTPException(409, "Make a key first")
    client_id = req.client_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", client_id):
        raise HTTPException(422, "That doesn't look like a Revolut Client ID")
    conn = await store().save_connection(user["id"], REVOLUT, client_id=client_id,
                                         environment=rb.environment(req.environment))
    return _connection_view(conn)


@router.post("/fd/payments/revolut/connect")
async def revolut_connect(user: dict = Depends(get_current_user)):
    """The Revolut consent URL (scope READ,WRITE — never PAY)."""
    from captain_claw.flight_deck import revolut_business as rb

    _require_on()
    conn = await store().get_connection(user["id"], REVOLUT)
    if not conn or not conn.get("private_key") or not conn.get("client_id"):
        raise HTTPException(409, "Make a key and add the Client ID first")
    now = time.time()
    for state, (_owner, expires) in list(_CONNECT_STATES.items()):
        if expires < now:
            _CONNECT_STATES.pop(state, None)
    state = secrets.token_urlsafe(16)
    _CONNECT_STATES[state] = (user["id"], now + _CONNECT_TTL)
    return {"url": rb.consent_url(conn, state)}


async def _finish_connect(owner: str, code: str) -> dict:
    from captain_claw.flight_deck import revolut_business as rb

    conn = await store().get_connection(owner, REVOLUT)
    if not conn or not conn.get("client_id"):
        raise HTTPException(409, "Start the connection on the Payments page first")
    try:
        tokens = await rb.exchange_code(conn, code.strip())
    except rb.RevolutError as exc:
        raise HTTPException(502, f"Revolut refused the code: {exc.message}")
    conn = await store().save_connection(owner, REVOLUT, connected_at=time.time(), **tokens)
    # Pay from the first active EUR account until the owner picks another.
    try:
        accounts = await _client(conn).accounts()
        eur = next((a for a in accounts if a.get("currency") == "EUR" and a.get("state") == "active"), None)
        if eur and not conn.get("account_id"):
            conn = await store().save_connection(owner, REVOLUT, account_id=eur["id"],
                                                 account_label=str(eur.get("name") or "EUR account"))
    except Exception as exc:
        log.info("Revolut accounts read failed after connect", error=str(exc))
    log.info("Revolut Business connected", owner=owner, environment=conn.get("environment"))
    return _connection_view(conn)


@router.post("/fd/payments/revolut/code")
async def revolut_code(req: RevolutCodeRequest, user: dict = Depends(get_current_user)):
    """Fallback: the code pasted by hand (valid 2 minutes)."""
    _require_on()
    return await _finish_connect(user["id"], req.code)


@router.get("/fd/payments/revolut/accounts")
async def revolut_accounts(user: dict = Depends(get_current_user)):
    from captain_claw.flight_deck import revolut_business as rb

    _require_on()
    conn = await _revolut_conn(user["id"])
    if not conn:
        raise HTTPException(409, "Revolut Business isn't connected")
    try:
        accounts = await _client(conn).accounts()
    except rb.RevolutError as exc:
        raise HTTPException(502, f"Revolut: {exc.message}")
    return {"accounts": [{k: a.get(k) for k in ("id", "name", "currency", "balance", "state")}
                         for a in accounts if a.get("currency") == "EUR"]}


@router.put("/fd/payments/revolut/account")
async def revolut_account(req: RevolutAccountRequest, user: dict = Depends(get_current_user)):
    _require_on()
    accounts = (await revolut_accounts(user))["accounts"]
    chosen = next((a for a in accounts if a["id"] == req.account_id), None)
    if not chosen:
        raise HTTPException(422, "That isn't one of your EUR accounts")
    conn = await store().save_connection(user["id"], REVOLUT, account_id=chosen["id"],
                                         account_label=str(chosen.get("name") or "EUR account"))
    return _connection_view(conn)


@router.delete("/fd/payments/revolut/connection")
async def revolut_disconnect(user: dict = Depends(get_current_user)):
    _require_on()
    await store().delete_connection(user["id"], REVOLUT)
    return _connection_view(None)


@router.post("/fd/payments/{intent_id}/revolut")
async def owner_send_to_revolut(intent_id: str, user: dict = Depends(get_current_user)):
    _require_on()
    row = await _owned(intent_id, user["id"])
    _handoff_gate(row)
    return await _handoff_revolut(row, user["id"], f"user:{user['id']}")


@public_router.get("/fd/payments/revolut/callback")
async def revolut_callback(code: str = "", state: str = "", error: str = ""):
    """Revolut's redirect after consent. The one-time state names the owner;
    without it, the only connect started in the last 10 minutes."""
    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>{html.escape(title)}</title><body style="font:16px system-ui;max-width:520px;'
            f'margin:40px auto;padding:0 16px"><h2>{html.escape(title)}</h2>{body}</body>',
            status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    if not enabled():
        return page("Payments are off", "<p>Payments aren't turned on for this Flight Deck.</p>", 404)
    if error or not code:
        return page("Not connected", f"<p>Revolut didn't grant access{': ' + html.escape(error) if error else ''}."
                    " Try again from the Payments page.</p>", 400)
    now = time.time()
    entry = _CONNECT_STATES.pop(state, None) if state else None
    if entry is None and not state:
        live = [(s, e) for s, e in _CONNECT_STATES.items() if e[1] > now]
        if len(live) == 1:
            entry = _CONNECT_STATES.pop(live[0][0])
    if entry is None or entry[1] < now:
        return page("Almost there", "<p>This Flight Deck couldn't tell whose connection this is. Copy "
                    "this code into the Payments page within two minutes:</p>"
                    f"<pre style='padding:8px;background:#eee;word-break:break-all'>{html.escape(code)}</pre>")
    try:
        await _finish_connect(entry[0], code)
    except HTTPException as exc:
        return page("Not connected", f"<p>{html.escape(str(exc.detail))}</p>", 502)
    return page("Revolut Business connected", "<p>Captain can now put payments into Revolut Business as "
                "drafts for you to send. It can't send them itself. You can close this tab.</p>")


# ── Pay-by-bank links: PSD2 payment initiation (Phase 2) ──────────────
#
# docs/payments-phase2-plan.md. Flight Deck creates the payment at Enable
# Banking (the deck's application, FD_ENABLEBANKING_*) and hands the user one
# link to their own bank, where they approve it with the bank's SCA. No bank
# login or long-lived token is ever held here.

BANK = "bank"
BANK_PREF = "bank"


async def _sync_rail(row: dict) -> dict:
    if row.get("handoff_rail") == BANK:
        return await _sync_bank(row)
    return await _sync_revolut(row)


async def _bank_redirect(request: Request, owner: str) -> str:
    override = os.environ.get("FD_ENABLEBANKING_REDIRECT_URL", "").strip()
    if override:
        return override
    base, _ = await _link_base(request, owner)
    return f"{base}/fd/payments/bank/callback"


async def _sync_bank(row: dict) -> dict:
    """Read the bank's status and move the ledger on (ACSC/ACCC or final
    accepted → settled, RJCT → failed, final cancellation → cancelled)."""
    from captain_claw.flight_deck import bank_link

    bk = dict((row.get("rails_state") or {}).get(BANK) or {})
    if row.get("status") != "handed_off" or not bk.get("payment_id") or not bank_link.configured():
        return row
    try:
        data = await bank_link.get_payment(bk["payment_id"])
    except bank_link.BankLinkError as exc:
        log.info("Bank payment read failed", id=row["id"], error=exc.message)
        return row
    status = str(data.get("status") or "").upper()
    final = bool(data.get("final_status"))
    reason_info = data.get("status_reason_information") or {}
    reason = str(reason_info.get("status_reason_description") or reason_info.get("status_reason_code") or "")
    if status == bk.get("status") and final == bool(bk.get("final")):
        return row
    bk.update(status=status, final=final, reason=reason[:200], checked_at=time.time())
    row = await store().update(row["id"], rails_state={**(row.get("rails_state") or {}), BANK: bk})
    target = bank_link.ledger_status(status, final)
    if target == "settled":
        row = await _set_status(row, "settled", "bank", via=BANK, detail={"payment_id": bk["payment_id"],
                                                                            "status": status})
    elif target == "failed":
        row = await store().update(row["id"], warnings=_warn(
            row, "bank_rejected", "info", f"Your bank rejected the payment"
            + (f": {reason}" if reason else "") + ". Nothing was paid; hand it over again to retry."))
        row = await _set_status(row, "failed", "bank", via=BANK, detail={"status": status, "reason": reason})
    elif target == "cancelled":
        row = await _set_status(row, "cancelled", "bank", via=BANK, detail={"status": status})
    return row


async def _handoff_bank(row: dict, owner: str, actor: str, request: Request) -> dict:
    """Create the payment at the user's bank (gate already passed) → the link."""
    from captain_claw.flight_deck import bank_link
    from captain_claw.payments import iban as iban_mod

    if not bank_link.configured():
        raise HTTPException(409, "Pay-by-bank links aren't set up on this deck.")
    choice = await store().get_pref(owner, BANK_PREF)
    if not choice:
        raise HTTPException(409, "Choose your bank on the Payments page first.")
    fields = row["fields"]
    if (fields.get("currency") or "EUR") != "EUR" or not iban_mod.is_sepa(fields.get("iban", "")):
        raise HTTPException(409, "Pay-by-bank links here are for EUR payments to SEPA IBANs.")
    try:
        banks = await bank_link.aspsps(choice["country"], choice["psu_type"])
        aspsp = next((b for b in banks if b.get("name") == choice["name"]), None)
        if aspsp is None:
            raise HTTPException(409, f"{choice['name']} can't take payments through the bank link right now.")
        nonce = secrets.token_urlsafe(18)
        created = await bank_link.create_payment(
            aspsp=aspsp, psu_type=choice["psu_type"], fields=fields, state=nonce,
            redirect_url=await _bank_redirect(request, owner))
    except bank_link.BankLinkError as exc:
        raise HTTPException(502, f"The bank link couldn't be made: {exc.message}")
    bk = {"payment_id": created["payment_id"], "url": created["url"], "status": created.get("status") or "RCVD",
          "aspsp": {"name": aspsp["name"], "country": aspsp["country"]}, "created_at": time.time()}
    row = await store().update(row["id"], status="handed_off", handoff_rail=BANK, rail_nonce=nonce,
                               rails_state={**(row.get("rails_state") or {}), BANK: bk})
    await store().add_event(row["id"], owner, actor, "bank_link",
                            {"payment_id": created["payment_id"], "bank": aspsp["name"]})
    log.info("Bank payment link created", id=row["id"], bank=aspsp["name"])
    return {"intent": view(row), "rail": BANK, "url": created["url"], "bank_name": aspsp["name"]}


class BankChoiceRequest(BaseModel):
    name: str
    country: str = "HR"
    psu_type: str = "personal"


@router.get("/fd/payments/bank/status")
async def bank_status(request: Request, user: dict = Depends(get_current_user)):
    from captain_claw.flight_deck import bank_link

    _require_on()
    out = {"configured": bank_link.configured(), "environment": "", "redirect_url": "",
           "bank": await store().get_pref(user["id"], BANK_PREF)}
    if out["configured"]:
        out["redirect_url"] = await _bank_redirect(request, user["id"])
        try:
            out["environment"] = str((await bank_link.application()).get("environment") or "").lower()
        except bank_link.BankLinkError as exc:
            out["error"] = exc.message
    return out


@router.get("/fd/payments/bank/aspsps")
async def bank_aspsps(country: str = "HR", psu_type: str = "personal", user: dict = Depends(get_current_user)):
    from captain_claw.flight_deck import bank_link

    _require_on()
    if psu_type not in ("personal", "business") or not re.fullmatch(r"[A-Za-z]{2}", country):
        raise HTTPException(422, "country is a two-letter code; psu_type is personal or business")
    try:
        banks = await bank_link.aspsps(country, psu_type)
    except bank_link.BankLinkError as exc:
        raise HTTPException(exc.status if exc.status in (409, 503) else 502, exc.message)
    return {"banks": [{"name": b.get("name"), "country": b.get("country"), "logo": b.get("logo", ""),
                       "beta": bool(b.get("beta")), "bic": b.get("bic", "")} for b in banks]}


@router.put("/fd/payments/bank/choice")
async def bank_choice(req: BankChoiceRequest, user: dict = Depends(get_current_user)):
    _require_on()
    banks = (await bank_aspsps(req.country.upper(), req.psu_type, user))["banks"]
    if not any(b["name"] == req.name for b in banks):
        raise HTTPException(422, "That bank can't take payments through the bank link")
    choice = {"name": req.name, "country": req.country.upper(), "psu_type": req.psu_type}
    await store().set_pref(user["id"], BANK_PREF, choice)
    return {"bank": choice}


@router.delete("/fd/payments/bank/choice")
async def bank_choice_clear(user: dict = Depends(get_current_user)):
    _require_on()
    await store().delete_pref(user["id"], BANK_PREF)
    return {"bank": None}


@router.post("/fd/payments/{intent_id}/bank")
async def owner_pay_by_bank(intent_id: str, request: Request, user: dict = Depends(get_current_user)):
    _require_on()
    row = await _owned(intent_id, user["id"])
    _handoff_gate(row)
    return await _handoff_bank(row, user["id"], f"user:{user['id']}", request)


@public_router.get("/fd/payments/bank/callback")
async def bank_callback(state: str = "", error: str = "", error_description: str = ""):
    """The bank (via Enable Banking) sends the user back here after approving
    — or not. The one-time state names the payment."""
    from captain_claw.flight_deck import bank_link

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>{html.escape(title)}</title><body style="font:16px system-ui;max-width:520px;'
            f'margin:40px auto;padding:0 16px"><h2>{html.escape(title)}</h2>{body}</body>',
            status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    row = await store().get_by_nonce(state) if enabled() and state else None
    if not row:
        return page("Link unavailable", "<p>This payment link isn't known here any more.</p>", 404)
    bk = dict((row.get("rails_state") or {}).get(BANK) or {})
    if error:
        bk["last_error"] = (error_description or error)[:200]
        row = await store().update(row["id"], rails_state={**(row.get("rails_state") or {}), BANK: bk})
        await store().add_event(row["id"], row["owner_id"], "bank", "bank_not_approved", {"error": error})
    row = await _sync_bank(row)
    v = view(row)
    who = f"{html.escape(v['payee_name'] or 'the payee')} · {html.escape(v['amount_text'])}"
    if row["status"] == "settled":
        return page("Paid", f"<p>{who} — your bank completed the payment. You can close this tab.</p>")
    if row["status"] in ("failed", "cancelled") or error:
        reason = bk.get("last_error") or (row.get("rails_state") or {}).get(BANK, {}).get("reason") or ""
        return page("Not paid", f"<p>{who} wasn't approved at your bank"
                    + (f" ({html.escape(reason)})" if reason else "") + ". Nothing was paid. Ask Captain "
                    "for a new link when you're ready.</p>")
    return page("Approved at your bank", f"<p>{who} — {html.escape(bank_link.describe(bk.get('status') or ''))}. "
                "Captain shows it as paid once your bank confirms. You can close this tab.</p>")


# ── the phone page: /p/{token} ────────────────────────────────────────

_COPY_JS = ("document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',"
            "function(){navigator.clipboard.writeText(b.getAttribute('data-copy')).then(function(){"
            "var t=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=t},1400)})})})")
_COPY_HASH = base64.b64encode(hashlib.sha256(_COPY_JS.encode()).digest()).decode()
_PAGE_CSP = (f"default-src 'none'; style-src 'unsafe-inline'; img-src data:; script-src 'sha256-{_COPY_HASH}'; "
             "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
_HEADERS = {
    # same-origin, not no-referrer: with no-referrer the browser sends the
    # "I paid" form POST with Origin: null, which the deck's cross-site guard
    # (origin_guard) refuses. Nothing leaves the deck either way.
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
    "X-Robots-Tag": "noindex, nofollow, noarchive", "Cache-Control": "no-store",
    "Content-Security-Policy": _PAGE_CSP, "X-Frame-Options": "DENY",
}


async def _page_row(token: str) -> dict | None:
    if not enabled() or not _TOKEN_RE.match(token or ""):
        return None
    row = await store().get_by_page(_token_hash(token))
    if not row or float(row.get("page_expires_at") or 0) <= time.time():
        return None
    return row


def _shell(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive"><meta name="referrer" content="same-origin">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body><main>{body}</main><script>{_COPY_JS}</script></body></html>"""
    return HTMLResponse(page, status_code=status, headers=_HEADERS)


def _gone() -> HTMLResponse:
    return _shell("Link unavailable", '<section class="card"><h1>This link isn\'t available</h1>'
                  "<p class=\"muted\">It may have expired or been replaced.</p></section>", 404)


def _row_html(label: str, value: str, copy: str | None = None, note: str = "") -> str:
    if not value:
        return ""
    button = (f'<button type="button" class="copy" data-copy="{html.escape(copy)}">Copy</button>'
              if copy else "")
    extra = f'<span class="note">{html.escape(note)}</span>' if note else ""
    return (f'<div class="row"><div><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{html.escape(value)}</div>{extra}</div>{button}</div>')


def _render_page(row: dict, token: str) -> str:
    v = view(row)
    esc = html.escape
    head = (f'<header><div class="muted">Payment to</div><h1>{esc(v["payee_name"] or "—")}</h1>'
            f'<div class="amount">{esc(v["amount_text"])}</div>'
            + (f'<div class="muted">Due {esc(v["due_date"])}</div>' if v["due_date"] else "")
            + "</header>")
    status = v["status"]
    if status in PAID_STATES:
        when = _iso_day(row.get("paid_at"))
        return head + (f'<section class="card done"><h2>Marked as paid{" on " + when if when else ""}</h2>'
                       '<p class="muted">Nothing more to do here.</p></section>')
    if status in TERMINAL_STATES:
        return head + (f'<section class="card"><h2>This payment was {esc(status)}</h2></section>')
    if status in ("draft", "needs_review"):
        return head + ('<section class="card warn"><h2>Review needed first</h2><p>This payment has a '
                       'warning only you can clear. Open Flight Deck and look at it on the Payments page '
                       f'before paying.</p><a class="btn" href="/payments/{esc(v["id"])}">Open in Flight Deck</a>'
                       "</section>")
    images = render_barcodes(row)
    codes = ""
    for image in images:
        src = "data:image/png;base64," + base64.b64encode(image["png"]).decode()
        caption = ("Scan in your bank app: Pay → scan barcode (HUB3)" if image["kind"] == "hub3"
                   else "Or scan this QR code (SEPA bank apps)")
        codes += f'<figure class="code k-{image["kind"]}"><img src="{src}" alt="{esc(caption)}"><figcaption>{esc(caption)}</figcaption></figure>'
    euros = f'{v["amount_cents"] // 100},{v["amount_cents"] % 100:02d}'
    rows = "".join([
        _row_html("Payee", v["payee_name"]),
        _row_html("IBAN", v["iban_grouped"], copy=v["iban"], note=v["bank"]),
        _row_html("Amount", v["amount_text"], copy=euros),
        _row_html("Model", v["model"], copy=v["model"]),
        _row_html("Poziv na broj", v["reference"], copy=v["reference"]),
        _row_html("Description", v["description"]),
    ])
    info = "".join(f'<li>{esc(w["message"])}</li>' for w in v["warnings"] if w.get("severity") == "info")
    bank = v.get("bank_link") or {}
    if bank.get("url"):
        head += (f'<section class="actions"><a class="btn primary" href="{esc(bank["url"])}" '
                 f'rel="noreferrer">Pay in {esc(bank["bank_name"] or "your bank")} →</a></section>'
                 '<p class="muted small">Opens your bank, where you approve the payment. Or scan the '
                 'barcode below.</p>')
    return (head + f'<section class="codes">{codes}</section>'
            '<p class="muted small">On this phone? Save the barcode and use your bank app\'s "scan from '
            'gallery", if it has one — or copy the details below.</p>'
            f'<section class="card fields">{rows}</section>'
            + (f'<section class="card info"><ul>{info}</ul></section>' if info else "")
            + f'<section class="actions"><form method="post" action="/p/{esc(token)}/paid">'
              '<button class="btn primary" type="submit">I paid</button></form>'
              f'<form method="post" action="/p/{esc(token)}/skip">'
              '<button class="btn" type="submit">Skip this one</button></form></section>'
              '<p class="muted small">Captain never moves money. You approve the payment in your bank app.</p>')


@public_router.get("/p/{token}")
async def public_payment_page(token: str):
    row = await _page_row(token)
    if not row:
        return _gone()
    return _shell("Payment", _render_page(row, token))


async def _page_mark(token: str, status: str):
    row = await _page_row(token)
    if not row:
        return _gone()
    if row["status"] in ("ready", "handed_off"):
        await _set_status(row, status, "page", via="page")
    return RedirectResponse(f"/p/{token}", status_code=303, headers={"Cache-Control": "no-store"})


@public_router.post("/p/{token}/paid")
async def public_payment_paid(token: str):
    return await _page_mark(token, "user_paid")


@public_router.post("/p/{token}/skip")
async def public_payment_skip(token: str):
    return await _page_mark(token, "skipped")


_CSS = """
:root{--bg:#fafafa;--panel:#fff;--fg:#18181b;--muted:#71717a;--line:#e4e4e7;--accent:#7c3aed;
--accent-fg:#fff;--warn:#b45309;--warn-bg:#fffbeb;--ok:#15803d}
@media (prefers-color-scheme:dark){:root{--bg:#09090b;--panel:#18181b;--fg:#e4e4e7;--muted:#a1a1aa;
--line:#27272a;--accent:#a78bfa;--accent-fg:#09090b;--warn:#fbbf24;--warn-bg:#292114;--ok:#4ade80}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:560px;margin:0 auto;padding:20px 16px 40px}
header{margin:8px 0 18px}h1{font-size:22px;margin:2px 0}h2{font-size:18px;margin:0 0 6px}
.amount{font-size:34px;font-weight:700;letter-spacing:-.01em;margin:4px 0}
.muted{color:var(--muted)}.small{font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:14px 0}
.card.warn{border-color:var(--warn);background:var(--warn-bg)}.card.done h2{color:var(--ok)}
.codes{display:flex;flex-direction:column;gap:14px}
.code{margin:0;background:#fff;border-radius:14px;padding:14px;text-align:center;border:1px solid var(--line)}
.code img{max-width:100%;height:auto;image-rendering:pixelated}.k-epc img{width:240px}
.code figcaption{color:#52525b;font-size:13px;margin-top:6px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;
border-bottom:1px solid var(--line)}.row:last-child{border-bottom:0}
.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.value{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.note{display:block;font-size:12px;color:var(--muted)}
.copy{flex:none;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:8px;
padding:6px 10px;font:inherit;font-size:13px;cursor:pointer}
.info ul{margin:0;padding-left:18px;color:var(--muted);font-size:14px}
.actions{display:flex;gap:10px;margin:18px 0}.actions form{flex:1}
.btn{display:block;width:100%;text-align:center;border:1px solid var(--line);background:var(--panel);
color:var(--fg);border-radius:12px;padding:12px 14px;font:inherit;font-weight:600;cursor:pointer;
text-decoration:none}.btn.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
"""
