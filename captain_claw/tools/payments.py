"""`payments` tool — get a bill ready for the user to pay in their own bank app.

Captain never moves money and never takes card data. The agent prepares a
payment from an invoice (Flight Deck reads the invoice's own HUB3 / EPC barcode
when there is one, checks the fields against the document, and keeps the
ledger), then hands over a barcode the user scans in their bank app plus a phone
page (/p/<token>). The user approves the payment in the bank app.

Guards:

* **No card data.** Card-like values are refused here and again in Flight Deck.
* **Warnings stay the user's.** A blocking warning (changed IBAN, duplicate,
  misread amount, over the limit) can only be cleared by the owner in Flight
  Deck; handoff is refused until then.
* **"Paid" is the user's word.** ``mark`` sends the person's own message of this
  turn (never a model argument); Flight Deck marks a payment paid only when that
  message says so, and never on an automated turn.
* **Owner-only.** A shared agent's member never reaches it.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from captain_claw import card_scrub, payments_client
from captain_claw.logging import get_logger
from captain_claw.tools.registry import Tool, ToolResult

log = get_logger(__name__)

MAX_SOURCE_BYTES = 10 * 1024 * 1024
_FIELDS = ("payee_name", "payee_street", "payee_city", "payee_oib", "iban", "amount", "currency",
           "model", "reference", "purpose_code", "description", "due_date")
_MARK = {"paid": "user_paid", "user_paid": "user_paid", "skip": "skipped", "skipped": "skipped",
         "cancel": "cancelled", "cancelled": "cancelled"}
_SOURCE_LABEL = {"barcode": "the invoice's own barcode", "document": "the invoice text",
                 "model": "read by you only", "user": "the user"}


class PaymentsTool(Tool):
    name = "payments"
    description = (
        "Get a bill ready for the user to pay in THEIR OWN bank app. Captain never moves money and "
        "never takes card numbers: you prepare the payment, the user scans a barcode in their bank "
        "app and approves it there. Use it when the user asks to pay or settle an invoice / bill / "
        "uplatnica / račun, or when a job's own text asks to prepare payments for invoices it finds.\n"
        "action='prepare': source = the invoice file (PDF or photo — e.g. a Gmail attachment's "
        "'Saved to:' path) plus the fields you read: payee_name, iban, amount (like '84.20'), model "
        "and reference (the poziv na broj, e.g. model 'HR01', reference '7269-68499637766-00019'), "
        "description, due_date (YYYY-MM-DD). Flight Deck reads the invoice's own barcode when it has "
        "one and checks every field. To correct a prepared payment, prepare again with supersedes=<id>.\n"
        "action='handoff' (id): only when prepare says READY — makes the barcode the user scans plus "
        "a link to a phone page; with rail='bank' (when prepare lists it) it returns ONE LINK to the "
        "user's own bank where they approve the payment; with rail='revolut' it puts the payment into "
        "the user's Revolut Business as a DRAFT they send in the Revolut app — Captain can't send or "
        "approve anything itself. action='mark' (id, status paid|skip|cancel): only when the user "
        "themselves says they paid or want to skip it. action='list' (status, query) / 'get' (id): "
        "what is prepared, paid or due.\n"
        "Rules: never ask for or accept card numbers, CVVs or bank logins. Never say anything was "
        "paid unless the user marked it paid — you can't pay. Tell the user every warning; you "
        "can't clear one, the user does that in Flight Deck → Payments."
    )
    timeout_seconds = 120.0

    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["prepare", "handoff", "mark", "list", "get"]},
            "source": {"type": "string",
                       "description": "prepare: the invoice file — path or file name (PDF, PNG, JPG)."},
            "payee_name": {"type": "string", "description": "prepare: who is paid."},
            "payee_street": {"type": "string", "description": "prepare: payee street and number."},
            "payee_city": {"type": "string", "description": "prepare: payee postcode and city."},
            "payee_oib": {"type": "string", "description": "prepare: payee OIB / tax number, if shown."},
            "iban": {"type": "string", "description": "prepare: the payee's IBAN."},
            "amount": {"type": "string", "description": "prepare: amount to pay, e.g. '84.20'."},
            "currency": {"type": "string", "description": "prepare: currency (EUR only for now)."},
            "model": {"type": "string", "description": "prepare: Croatian payment model, e.g. 'HR01'."},
            "reference": {"type": "string",
                          "description": "prepare: poziv na broj (or an RF creditor reference)."},
            "purpose_code": {"type": "string", "description": "prepare: purpose code, e.g. 'COST'."},
            "description": {"type": "string", "description": "prepare: payment description."},
            "due_date": {"type": "string", "description": "prepare: due date, YYYY-MM-DD."},
            "supersedes": {"type": "string",
                           "description": "prepare: id of an earlier payment this one corrects."},
            "id": {"type": "string", "description": "handoff / mark / get: the payment id (pay_…)."},
            "rail": {"type": "string", "enum": ["barcode", "bank", "revolut"],
                     "description": "handoff: 'barcode' (default); 'bank' — a link to approve it in the "
                                    "user's bank; 'revolut' — a draft in Revolut Business."},
            "status": {"type": "string",
                       "description": "mark: paid | skip | cancel. list: filter, e.g. 'handed_off'."},
            "query": {"type": "string", "description": "list: text to match in payee or description."},
        },
        "required": ["action"],
    }

    async def execute(self, action: str = "", **kwargs: Any) -> ToolResult:
        runtime = {k: kwargs.pop(k) for k in [k for k in kwargs if str(k).startswith("_")]}
        action = str(action or "").strip().lower()
        if payments_client.member_call():
            return ToolResult(success=False, error=payments_client.MEMBER_REFUSED)
        if not payments_client.under_flight_deck():
            return ToolResult(success=False, error=payments_client.NO_FLIGHT_DECK)
        try:
            if action == "prepare":
                return await self._prepare(kwargs, runtime)
            if action == "handoff":
                return await self._handoff(kwargs, runtime)
            if action == "mark":
                return await self._mark(kwargs, runtime)
            if action == "list":
                return await self._list(kwargs)
            if action == "get":
                return await self._get(kwargs)
        except Exception as exc:  # noqa: BLE001 — a failed call is the tool's answer, not a crash
            log.warning("payments tool call failed", action=action, error=str(exc))
            return ToolResult(success=False, error=f"Flight Deck couldn't be reached: {exc}")
        return ToolResult(success=False, error="Unknown action — use prepare, handoff, mark, list or get.")

    # ── actions ───────────────────────────────────────────────────────

    async def _prepare(self, kwargs: dict, runtime: dict) -> ToolResult:
        fields = {k: str(kwargs[k]).strip() for k in _FIELDS if kwargs.get(k) not in (None, "")}
        for name, value in fields.items():
            if card_scrub.looks_like_card_data(value):
                return ToolResult(success=False, error=(
                    f"Refused: {name} looks like payment-card data. Captain never takes card numbers "
                    "or CVVs — payments are approved in the user's bank app."))
        body: dict[str, Any] = {
            "fields": fields, "user_text": _user_text(runtime), "session_id": str(runtime.get("_session_id") or ""),
            "supersedes": str(kwargs.get("supersedes") or "").strip(),
            **_turn_kind(),
        }
        source = str(kwargs.get("source") or "").strip()
        if source:
            from captain_claw.tools.public_link import PublicLinkTool

            found = PublicLinkTool._resolve(source, runtime)
            if found is None:
                return ToolResult(success=False, error=(
                    f"No file '{source}' found among this agent's files. Pass the path it was saved "
                    "under (e.g. a Gmail attachment's 'Saved to:' path) or its exact file name."))
            path = found[1]
            size = path.stat().st_size
            if size > MAX_SOURCE_BYTES:
                return ToolResult(success=False, error="The invoice file is over 10 MB.")
            data = await asyncio.to_thread(path.read_bytes)
            body.update(source_b64=base64.b64encode(data).decode(), source_name=path.name)
        elif not fields:
            return ToolResult(success=False, error="Give `source` (the invoice file) and/or the fields you read.")
        data, err = await payments_client.call("POST", "/fd/payments/agent/prepare", body)
        if err:
            return ToolResult(success=False, error=err)
        intent = data.get("intent") or {}
        head = (f"This invoice was already prepared as {intent.get('id')} — nothing new was created."
                if data.get("existing") else "")
        return ToolResult(success=True, content=_describe(intent, head=head, next_step=True,
                                                          rails=data.get("rails") or []))

    async def _handoff(self, kwargs: dict, runtime: dict) -> ToolResult:
        intent_id = str(kwargs.get("id") or "").strip()
        if not intent_id:
            return ToolResult(success=False, error="Give `id` — the payment from prepare (pay_…).")
        rail = str(kwargs.get("rail") or "barcode").strip().lower()
        data, err = await payments_client.call("POST", f"/fd/payments/agent/{intent_id}/handoff", {"rail": rail})
        if err:
            return ToolResult(success=False, error=err)
        intent = data.get("intent") or {}
        if data.get("rail") == "bank":
            return ToolResult(success=True, content="\n".join([
                _status_line(intent),
                f"DONE — a payment link to the user's bank ({data.get('bank_name')}) is ready. Nothing has "
                "been paid: the user approves it in their bank.",
                f"Link: {data.get('url', '')}",
                f"Tell the user: {intent.get('payee_name')} · {intent.get('amount_text')} — open the link "
                "(it opens their bank), check payee and amount, and approve it there. The link works for "
                "about an hour; ask for a new one after that. Captain sees when the bank confirms. Do not "
                "say it is paid.",
                *_warning_lines(intent),
            ]))
        if data.get("rail") == "revolut":
            done = "already waiting" if data.get("existing") else "now waiting"
            return ToolResult(success=True, content="\n".join([
                _status_line(intent),
                f"DONE — a payment draft is {done} in the user's Revolut Business. Nothing has been paid: "
                "only the user can send it.",
                f"Tell the user: {intent.get('payee_name')} · {intent.get('amount_text')} is in Revolut "
                "Business as a draft — open the Revolut Business app, check it and send it there. Captain "
                "will see when it completes. Do not say it is paid.",
                *_warning_lines(intent),
            ]))
        paths = await asyncio.to_thread(self._save_images, intent_id, data.get("images") or [], runtime)
        lines = [
            _status_line(intent),
            "DONE — the payment is ready for the user to pay in their bank app. Nothing has been paid.",
            *[f"Path: {p}" for p in paths],
            f"Page: {data.get('page_url', '')}",
            f"Tell the user: {intent.get('payee_name')} · {intent.get('amount_text')}"
            + (f" · due {intent['due_date']}" if intent.get("due_date") else "") + ". "
            "Scan the barcode in your bank app (Pay → scan barcode), check payee and amount, and "
            "approve it there. Give them the Page link too (barcode, copy buttons and an 'I paid' "
            "button, handy on a phone). They can tap 'I paid' there or tell you once it's done. "
            "Do not say it is paid.",
        ]
        if data.get("local_only"):
            lines.append("The Page link only opens on this computer or network (this deck has no "
                         "public address) — say so.")
        return ToolResult(success=True, content="\n".join(lines))

    async def _mark(self, kwargs: dict, runtime: dict) -> ToolResult:
        intent_id = str(kwargs.get("id") or "").strip()
        status = _MARK.get(str(kwargs.get("status") or "").strip().lower())
        if not intent_id or not status:
            return ToolResult(success=False, error="Give `id` and `status` (paid, skip or cancel).")
        body = {"status": status, "user_text": _user_text(runtime), **_turn_kind()}
        data, err = await payments_client.call("POST", f"/fd/payments/agent/{intent_id}/mark", body)
        if err:
            return ToolResult(success=False, error=err)
        intent = data.get("intent") or {}
        said = {"user_paid": "The user marked it paid", "skipped": "Skipped", "cancelled": "Cancelled"}[status]
        return ToolResult(success=True, content=f"{_status_line(intent)}\n{said}: {intent.get('payee_name')} "
                                                f"· {intent.get('amount_text')}.")

    async def _list(self, kwargs: dict) -> ToolResult:
        params = {"status": str(kwargs.get("status") or ""), "q": str(kwargs.get("query") or "")}
        data, err = await payments_client.call("GET", "/fd/payments/agent/list", params=params)
        if err:
            return ToolResult(success=False, error=err)
        rows = data.get("payments") or []
        if not rows:
            return ToolResult(success=True, content="No payments match.")
        out = []
        for p in rows:
            due = f" · due {p['due_date']}" if p.get("due_date") else ""
            out.append(f"• {p['id']} · {p['status']} · {p.get('payee_name') or '—'} · "
                       f"{p.get('amount_text')}{due}")
        return ToolResult(success=True, content="\n".join(out))

    async def _get(self, kwargs: dict) -> ToolResult:
        intent_id = str(kwargs.get("id") or "").strip()
        if not intent_id:
            return ToolResult(success=False, error="Give `id` (pay_…).")
        data, err = await payments_client.call("GET", f"/fd/payments/agent/{intent_id}")
        if err:
            return ToolResult(success=False, error=err)
        return ToolResult(success=True, content=_describe(data.get("intent") or {}, next_step=False,
                                                          rails=data.get("rails") or []))

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _save_images(intent_id: str, images: list[dict], runtime: dict) -> list[str]:
        from captain_claw.tools.write import WriteTool

        saved_root = WriteTool._resolve_saved_root(runtime)
        session = WriteTool._normalize_session_id(str(runtime.get("_session_id", "")))
        paths: list[str] = []
        for image in images:
            logical = f"media/{session}/{intent_id}-{image.get('kind', 'code')}.png"
            target = WriteTool._normalize_under_saved(logical, saved_root, session)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(image.get("png_b64", "")))
            registry = runtime.get("_file_registry")
            if registry is not None:
                try:
                    registry.register(logical_path=logical, physical_path=str(target),
                                      task_id=str(runtime.get("_task_id", "")))
                except Exception:  # noqa: BLE001
                    pass
            paths.append(str(Path(target)))
        return paths


def _user_text(runtime: dict) -> str:
    agent = runtime.get("_agent")
    return str(getattr(agent, "_turn_user_text", "") or "")[:4000]


def _turn_kind() -> dict:
    try:
        from captain_claw import mail_authority

        authority = mail_authority.current()
        return {"automated": authority.mode == "automated", "job_kind": str(authority.kind or "")}
    except Exception:  # noqa: BLE001
        return {"automated": False, "job_kind": ""}


def _status_line(intent: dict) -> str:
    return f"payment_intent: {intent.get('id', '')} status: {intent.get('status', '')}"


def _warning_lines(intent: dict) -> list[str]:
    warnings = intent.get("warnings") or []
    if not warnings:
        return []
    lines = ["Warnings (tell the user every one):"]
    for w in warnings:
        tag = {"error": "must be fixed", "blocking": "the user must check it in Flight Deck",
               "info": "note"}.get(w.get("severity"), w.get("severity"))
        done = " — already checked by the user" if w.get("acked") else ""
        lines.append(f"- [{tag}] {w.get('message')}{done}")
    return lines


def _describe(intent: dict, *, head: str = "", next_step: bool, rails: list[str] = ()) -> str:
    lines = [_status_line(intent)]
    if head:
        lines.append(head)
    due = f" · due {intent['due_date']}" if intent.get("due_date") else ""
    lines.append(f"{intent.get('payee_name') or '(no payee)'} · {intent.get('amount_text')}{due}")
    bank = f" ({intent['bank']})" if intent.get("bank") else ""
    ref = " ".join(x for x in (intent.get("model"), intent.get("reference")) if x)
    lines.append(f"IBAN {intent.get('iban_grouped') or '—'}{bank}" + (f" · model/reference {ref}" if ref else ""))
    if intent.get("description"):
        lines.append(f"Description: {intent['description']}")
    prov = intent.get("provenance") or {}
    sources = sorted({_SOURCE_LABEL.get(v, v) for k, v in prov.items() if k in ("iban", "amount_cents", "reference")})
    if sources:
        lines.append("IBAN/amount/reference came from: " + ", ".join(sources))
    revolut = intent.get("revolut") or {}
    if revolut.get("draft_id"):
        lines.append(f"Revolut Business draft: {str(revolut.get('state') or 'CREATED').lower()}"
                     + (f" ({revolut['reason']})" if revolut.get("reason") else ""))
    bank_link = intent.get("bank_link") or {}
    if bank_link.get("status") or bank_link.get("url"):
        lines.append(f"Bank ({bank_link.get('bank_name') or 'the user’s bank'}): {bank_link.get('label')}"
                     + (f" ({bank_link['reason']})" if bank_link.get("reason") else "")
                     + (f" — link: {bank_link['url']}" if bank_link.get("url") else ""))
    if intent.get("status") == "settled":
        lines.append("Paid — confirmed by the bank.")
    lines.extend(_warning_lines(intent))
    if not next_step:
        return "\n".join(lines)
    status = intent.get("status")
    if status == "ready":
        extra = []
        if "bank" in rails:
            extra.append("rail='bank' for a link that opens the user's bank")
        if "revolut" in rails:
            extra.append("rail='revolut' to put it into the user's Revolut Business as a draft")
        lines.append(f"READY — next: payments handoff with id={intent.get('id')} to make the barcode"
                     + (", or " + ", or ".join(extra) + " (ask the user which they prefer)." if extra else "."))
    elif status == "needs_review":
        lines.append("NEEDS THE USER — tell them the warnings above and that they review and confirm "
                     f"them in Flight Deck → Payments ({intent.get('review_path')}). You can't clear a "
                     "warning; don't hand it off until they have.")
    elif status == "draft":
        lines.append("NOT READY — something must be fixed (see above) or a payee name, IBAN or "
                     f"amount is missing. Fix it and prepare again with supersedes={intent.get('id')}.")
    elif status == "handed_off":
        lines.append("Already handed over — the barcode and page exist; the user pays it in their bank app.")
    return "\n".join(lines)
