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
        "SHOPPING (buying from an online shop): action='purchase' (source: 'link' = Stripe Link wallet, "
        "'revolut_card' = a Revolut card made for this purchase; merchant_name, merchant_url = the shop's "
        "https address, items [{name, quantity, unit_amount}], amount = the checkout's final total incl. "
        "shipping, currency) asks the USER to approve this seller and amount — nothing can be paid before. "
        "When get says APPROVED: fill the cart, shipping and contact details in the browser and stop at the "
        "card form, then action='checkout_pay' (id, pay_button = the exact text of the shop's pay button): "
        "it types the one-time card into the form and clicks pay in one step — you never see the card. "
        "Don't screenshot or read the page between checkout_pay's fill and its result. If the bank asks the "
        "user to confirm (3-D Secure), ask them, then action='checkout_check' (id).\n"
        "Shops that speak a checkout protocol need no browser: source='acp' (protocol_endpoint = the shop's "
        "domain, items with the shop's item `id`, shipping {name, line_one, city, state, country, "
        "postal_code, email}) — the user approves in Link and Flight Deck completes the order; "
        "source='ap2' (protocol_endpoint = the merchant agent's A2A URL, description = what to look for) — "
        "the user approves in Flight Deck → Payments and answers any one-time code there. For both: no "
        "checkout_pay, just check with get.\n"
        "Rules: never ask for or accept card numbers, CVVs or bank logins. Never say anything was "
        "paid unless the user marked it paid — you can't pay. Tell the user every warning; you "
        "can't clear one, the user does that in Flight Deck → Payments."
    )
    timeout_seconds = 120.0

    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["prepare", "handoff", "mark", "list", "get",
                                                  "purchase", "checkout_pay", "checkout_check"]},
            "source": {"type": "string",
                       "description": "prepare: the invoice file — path or file name (PDF, PNG, JPG). "
                                      "purchase: how it's paid — link, revolut_card, acp or ap2."},
            "merchant_name": {"type": "string", "description": "purchase: the shop's name."},
            "merchant_url": {"type": "string", "description": "purchase: the shop's https:// page for this order."},
            "items": {"type": "array", "description": "purchase: what is bought.",
                      "items": {"type": "object", "properties": {
                          "name": {"type": "string"}, "quantity": {"type": "integer"},
                          "unit_amount": {"type": "string"}, "url": {"type": "string"},
                          "id": {"type": "string"}}}},
            "credential_type": {"type": "string", "enum": ["card", "link_pay_token"],
                                "description": "purchase with source link: 'link_pay_token' only on a Stripe "
                                               "checkout showing a Link pay-token field."},
            "merchant_account_id": {"type": "string",
                                    "description": "purchase with link_pay_token: the acct_… on the checkout page."},
            "protocol_endpoint": {"type": "string",
                                  "description": "purchase with source acp: the shop's domain; ap2: the "
                                                 "merchant agent's A2A URL."},
            "shipping": {"type": "object",
                         "description": "purchase with source acp / ap2: where it's delivered — {name, line_one, "
                                        "line_two, city, state, country, postal_code, email, phone_number}."},
            "pay_button": {"type": "string",
                           "description": "checkout_pay: the exact visible text of the shop's pay button "
                                          "(or a CSS selector)."},
            "fields": {"type": "object",
                       "description": "checkout_pay, only if the card fields aren't found: CSS selectors "
                                      "{number, exp, exp_month, exp_year, cvc}."},
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
            if action == "purchase":
                return await self._purchase(kwargs, runtime)
            if action == "checkout_pay":
                return await self._checkout_pay(kwargs, runtime)
            if action == "checkout_check":
                return await self._checkout_check(kwargs, runtime)
        except Exception as exc:  # noqa: BLE001 — a failed call is the tool's answer, not a crash
            log.warning("payments tool call failed", action=action, error=str(exc))
            return ToolResult(success=False, error=f"Flight Deck couldn't be reached: {exc}")
        return ToolResult(success=False, error=(
            "Unknown action — use prepare, handoff, mark, list, get, purchase, checkout_pay or checkout_check."))

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
        if intent_id.startswith("pur_"):
            if str(kwargs.get("status") or "").strip().lower() not in ("cancel", "cancelled", "skip"):
                return ToolResult(success=False, error="A purchase can only be cancelled here (status cancel); "
                                                       "it counts as paid only when its checkout confirms it.")
            data, err = await payments_client.call("POST", f"/fd/payments/agent/purchases/{intent_id}/cancel", {})
            if err:
                return ToolResult(success=False, error=err)
            return ToolResult(success=True, content=_describe_purchase(data.get("purchase") or {}))
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
            return ToolResult(success=False, error="Give `id` (pay_… or pur_…).")
        if intent_id.startswith("pur_"):
            data, err = await payments_client.call("GET", f"/fd/payments/agent/purchases/{intent_id}")
            if err:
                return ToolResult(success=False, error=err)
            return ToolResult(success=True, content=_describe_purchase(data.get("purchase") or {}))
        data, err = await payments_client.call("GET", f"/fd/payments/agent/{intent_id}")
        if err:
            return ToolResult(success=False, error=err)
        return ToolResult(success=True, content=_describe(data.get("intent") or {}, next_step=False,
                                                          rails=data.get("rails") or []))

    # ── purchases (Phase 3) ───────────────────────────────────────────

    async def _purchase(self, kwargs: dict, runtime: dict) -> ToolResult:
        keys = ("source", "merchant_name", "merchant_url", "description", "amount", "currency",
                "credential_type", "merchant_account_id", "protocol_endpoint")
        body: dict[str, Any] = {k: str(kwargs[k]).strip() for k in keys if kwargs.get(k) not in (None, "")}
        items = kwargs.get("items") or []
        body["items"] = [{k: (str(v) if k != "quantity" else int(v or 1)) for k, v in dict(i).items()
                          if k in ("name", "quantity", "unit_amount", "url", "id") and v not in (None, "")}
                         for i in items if isinstance(i, dict) and i.get("name")]
        shipping = kwargs.get("shipping")
        if isinstance(shipping, dict) and shipping:
            body["shipping"] = {str(k): str(v).strip() for k, v in shipping.items() if v not in (None, "")}
        for value in [*body.values(), *(i.get("name", "") for i in body["items"]),
                      *body.get("shipping", {}).values()]:
            if card_scrub.looks_like_card_data(str(value)):
                return ToolResult(success=False, error="Refused: that looks like payment-card data. Captain "
                                                       "never takes card numbers — the user approves the purchase.")
        missing = [k for k in ("source", "merchant_name", "merchant_url", "amount") if not body.get(k)]
        if missing:
            return ToolResult(success=False, error=f"Give {', '.join(missing)}.")
        body.update(session_id=str(runtime.get("_session_id") or ""), **{k: v for k, v in _turn_kind().items()
                                                                          if k == "automated"})
        data, err = await payments_client.call("POST", "/fd/payments/agent/purchases", body)
        if err:
            return ToolResult(success=False, error=err)
        return ToolResult(success=True, content=_describe_purchase(data.get("purchase") or {}))

    async def _checkout_pay(self, kwargs: dict, runtime: dict) -> ToolResult:
        from captain_claw.payments import card_fill

        purchase_id = str(kwargs.get("id") or "").strip()
        pay_button = str(kwargs.get("pay_button") or "").strip()
        if not purchase_id.startswith("pur_") or not pay_button:
            return ToolResult(success=False, error="Give `id` (pur_…) and `pay_button` (the pay button's text).")
        page = await _agent_page(runtime)
        if page is None:
            return ToolResult(success=False, error=(
                "No checkout page is open. Open the shop's checkout in the browser tool (cart, shipping and "
                "contact filled, stopped at the card form), then call checkout_pay again."))
        cred, err = await payments_client.call("POST", f"/fd/payments/agent/purchases/{purchase_id}/credential", {})
        if err:
            return ToolResult(success=False, error=err)
        card = None
        token = ""
        if cred.get("kind") == "card":
            c = cred.get("card") or {}
            card = card_fill.Card(number=str(c.get("number") or ""), exp_month=int(c.get("exp_month") or 0),
                                  exp_year=int(c.get("exp_year") or 0), cvc=str(c.get("cvc") or ""),
                                  name=str(c.get("name") or ""))
        else:
            token = str(cred.get("token") or "")
        cred = {}
        overrides = {k: str(v) for k, v in dict(kwargs.get("fields") or {}).items()
                     if k in ("number", "exp", "exp_month", "exp_year", "cvc", "name") and v}
        try:
            result = await card_fill.fill_and_pay(page, card=card, link_pay_token=token, pay_button=pay_button,
                                                  overrides=overrides)
        finally:
            card, token = None, ""            # the card lives only for the fill
        data, err = await payments_client.call("POST", f"/fd/payments/agent/purchases/{purchase_id}/result",
                                               result.as_dict() | {"filled": result.filled})
        if err:
            return ToolResult(success=False, error=f"The checkout said {result.outcome}, but recording it failed: {err}")
        return ToolResult(success=True, content=_describe_checkout(result.as_dict(), data.get("purchase") or {}))

    async def _checkout_check(self, kwargs: dict, runtime: dict) -> ToolResult:
        from captain_claw.payments import card_fill

        purchase_id = str(kwargs.get("id") or "").strip()
        page = await _agent_page(runtime)
        if not purchase_id.startswith("pur_") or page is None:
            return ToolResult(success=False, error="Give `id` (pur_…) with the checkout page still open.")
        outcome, url, title, excerpt = await card_fill.read_outcome(page)
        if outcome in ("confirmed", "declined"):
            data, err = await payments_client.call("POST", f"/fd/payments/agent/purchases/{purchase_id}/result",
                                                   {"outcome": outcome, "url": url, "title": title, "excerpt": excerpt})
            if err:
                return ToolResult(success=False, error=err)
            purchase = data.get("purchase") or {}
        else:
            data, err = await payments_client.call("GET", f"/fd/payments/agent/purchases/{purchase_id}")
            purchase = data.get("purchase") or {} if not err else {}
        return ToolResult(success=True, content=_describe_checkout(
            {"outcome": outcome, "url": url, "title": title, "excerpt": excerpt, "filled": []}, purchase))

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


async def _agent_page(runtime: dict):
    """The agent's own browser page (the browser tool's session), or None."""
    try:
        from captain_claw.tools import browser as browser_tool
    except Exception:  # noqa: BLE001 — no browser support installed
        return None
    key = str(runtime.get("_session_id") or "").strip() or "default"
    session = browser_tool._BROWSER_SESSIONS.get(key)
    if session is None or not session.is_alive:
        return None
    return await session.ensure_page()


_PAID_WITH = {"link": "a one-time Link card", "revolut_card": "a Revolut card made for this purchase",
              "acp": "the shop's ACP checkout", "ap2": "an AP2 checkout"}


def _purchase_line(p: dict) -> str:
    return f"purchase_intent: {p.get('id', '')} status: {p.get('status', '')}"


def _describe_purchase(p: dict) -> str:
    lines = [_purchase_line(p),
             f"{p.get('merchant_name')} ({p.get('merchant_domain')}) · {p.get('amount_text')} · paid with "
             f"{_PAID_WITH.get(p.get('source'), p.get('source'))}" + (" (TEST mode)" if p.get("test") else "")]
    for w in p.get("warnings") or []:
        lines.append(f"- note: {w.get('message')}")
    status = p.get("status")
    if status == "awaiting_approval":
        if p.get("source") in ("link", "acp"):
            lines.append(f"WAITING FOR THE USER — give them this link to approve it in Link: {p.get('approval_url')} "
                         "(it works for 30 minutes). Then check with get.")
        else:
            lines.append("WAITING FOR THE USER — they approve it in Flight Deck → Payments "
                         f"({p.get('review_path')}). You can't approve it. Then check with get.")
    elif status in ("approved", "paying") and p.get("source") in ("acp", "ap2"):
        lines.append("APPROVED — Flight Deck is completing the order with the shop. Check again with get.")
    elif status == "needs_action" and p.get("source") == "ap2":
        lines.append("THE SHOP WANTS A ONE-TIME CODE — ask the user to enter it in Flight Deck → Payments "
                     f"({p.get('review_path')}): " + str((p.get("next_action") or {}).get("display_message") or "")
                     + ". Don't ask them to tell you the code. Then check with get.")
    elif status == "approved":
        lines.append("APPROVED — open the checkout in the browser (cart, shipping, contact filled; stop at the card "
                     f"form), then payments checkout_pay id={p.get('id')} pay_button='<the pay button text>'.")
    elif status == "needs_action":
        action = p.get("next_action") or {}
        lines.append("THE BANK WANTS THE USER TO CONFIRM (3-D Secure) — ask them to approve it in their bank or Link "
                     "app" + (f" ({action.get('action_url')})" if action.get("action_url") else "")
                     + (", then check with get." if p.get("source") == "acp" else ", then call checkout_check."))
    elif status in ("ordered", "completed"):
        lines.append(f"ORDERED{' and paid' if status == 'completed' else ''}"
                     + (f" — order {p.get('order_ref')}" if p.get("order_ref") else "") + ".")
    elif status in ("failed", "denied", "expired", "cancelled"):
        lines.append(f"Not bought ({status}). Nothing was paid" + (" — the user denied it." if status == "denied" else "."))
    return "\n".join(lines)


def _describe_checkout(result: dict, purchase: dict) -> str:
    outcome = result.get("outcome")
    said = {"confirmed": "The shop confirmed the order.",
            "declined": "The shop declined the card. Nothing was bought.",
            "needs_action": "The bank wants the user to confirm the payment (3-D Secure).",
            "not_filled": "The card form wasn't found, so nothing was entered or paid. Give `fields` selectors "
                          "or check that the page is at the card step, then try again.",
            "no_button": "The pay button wasn't found, so nothing was paid (the card was cleared from the form). "
                         "Check the button's exact text and try again.",
            "unknown": "The page didn't say clearly whether it worked — check it, then checkout_check."}.get(outcome, outcome)
    lines = [_purchase_line(purchase) if purchase else "", said]
    if result.get("filled"):
        lines.append("Filled: " + ", ".join(result["filled"]) + " (values not shown).")
    if result.get("title") or result.get("url"):
        lines.append(f"Page now: {result.get('title') or ''} {result.get('url') or ''}".strip())
    if result.get("excerpt"):
        lines.append(f"It says: {result['excerpt'][:600]}")
    if purchase:
        lines.append(_describe_purchase(purchase).split("\n", 1)[-1])
    return "\n".join(line for line in lines if line)


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
