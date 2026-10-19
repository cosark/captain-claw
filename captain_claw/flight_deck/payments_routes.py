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
_DEPS = ("segno", "pdf417gen", "zxingcpp", "pypdfium2")

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
    }


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
    _agent_owner(request)
    return {"enabled": enabled(), "deps_ok": deps_ok()}


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
            return {"intent": view(existing), "existing": True}

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
    log.info("Payment prepared", id=intent_id, owner=owner, status=status, source=source_kind)
    return {"intent": view(row), "existing": False}


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


@router.post("/fd/payments/agent/{intent_id}/handoff")
async def agent_handoff(intent_id: str, request: Request):
    _require_on()
    _require_deps()
    owner, slug = _agent_owner(request)
    row = await _owned(intent_id, owner)
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
    images = render_barcodes(row)
    token = secrets.token_urlsafe(24)
    row = await store().update(row["id"], status="handed_off", page_token_hash=_token_hash(token),
                               page_expires_at=time.time() + PAGE_DAYS * 86400)
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
    rows = await store().list(owner, statuses=statuses, limit=max(1, min(limit, 100)))
    if q:
        needle = q.casefold()
        rows = [r for r in rows if needle in str(r["fields"].get("payee_name", "")).casefold()
                or needle in str(r["fields"].get("description", "")).casefold()]
    return {"payments": [view(r) for r in rows]}


@router.get("/fd/payments/agent/{intent_id}")
async def agent_get(intent_id: str, request: Request):
    _require_on()
    owner, _ = _agent_owner(request)
    return {"intent": view(await _owned(intent_id, owner))}


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
    rows = await store().list(user["id"], statuses=statuses)
    return {"payments": [view(r) for r in rows], **(await _config(user["id"]))}


@router.get("/fd/payments/{intent_id}")
async def owner_get(intent_id: str, user: dict = Depends(get_current_user)):
    _require_on()
    row = await _owned(intent_id, user["id"])
    return {"intent": view(row), "events": await store().events(intent_id)}


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
async def owner_mark(intent_id: str, req: OwnerMarkRequest, user: dict = Depends(get_current_user)):
    _require_on()
    if req.status not in ("user_paid", "skipped", "cancelled"):
        raise HTTPException(422, "status is one of user_paid, skipped, cancelled")
    row = await _owned(intent_id, user["id"])
    if row["status"] in TERMINAL_STATES:
        raise HTTPException(409, f"This payment is already {row['status'].replace('_', ' ')}")
    row = await _set_status(row, req.status, f"user:{user['id']}", via="card")
    return {"intent": view(row)}


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
