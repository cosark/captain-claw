"""The `payments` tool: card data never leaves, "paid" comes from the user's own
message, barcodes land where the chat bridges deliver them, and the tool is
hidden unless the owner's Flight Deck has payments on."""

from __future__ import annotations

import asyncio
import json
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from captain_claw import payments_client
from captain_claw.tools.payments import PaymentsTool

INTENT = {
    "id": "pay_0123456789ab", "status": "ready", "payee_name": "HEP Elektra d.o.o.",
    "amount_text": "84.20 EUR", "iban_grouped": "HR12 1001 0051 8630 0016 0", "bank": "",
    "model": "HR01", "reference": "7269-1", "due_date": "2026-10-25", "warnings": [],
    "provenance": {"iban": "barcode", "amount_cents": "barcode"}, "review_path": "/payments/pay_0123456789ab",
}


@pytest.fixture()
def fd(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []
    replies: dict[str, dict] = {}

    async def call(method, path, body=None, *, params=None, timeout=90.0):
        calls.append((method, path, body if body is not None else params))
        for suffix, reply in replies.items():
            if path.endswith(suffix):
                return reply, ""
        return {}, "unexpected call"

    monkeypatch.setattr(payments_client, "call", call)
    monkeypatch.setattr(payments_client, "under_flight_deck", lambda: True)
    monkeypatch.setattr(payments_client, "member_call", lambda: False)
    return calls, replies


def _run(**kwargs):
    return asyncio.run(PaymentsTool().execute(**kwargs))


def test_card_data_never_leaves(fd):
    calls, _ = fd
    r = _run(action="prepare", payee_name="Shop", description="4111 1111 1111 1111 cvv 123")
    assert not r.success and "card" in r.error
    assert calls == []


def test_members_are_refused(fd, monkeypatch):
    monkeypatch.setattr(payments_client, "member_call", lambda: True)
    r = _run(action="list")
    assert not r.success and "member" in r.error


def test_prepare_sends_the_users_own_words(fd, tmp_path):
    calls, replies = fd
    replies["/prepare"] = {"intent": INTENT, "existing": False}
    invoice = tmp_path / "saved" / "downloads" / "s1" / "racun.pdf"
    invoice.parent.mkdir(parents=True)
    invoice.write_bytes(b"%PDF-1.4 fake")
    agent = SimpleNamespace(_turn_user_text="Plati ovaj račun molim te")
    r = _run(action="prepare", source=str(invoice), amount="84.20", iban="HR1210010051863000160",
             _agent=agent, _session_id="s1", _saved_base_path=str(tmp_path / "saved"))
    assert r.success, r.error
    body = calls[0][2]
    assert body["user_text"] == "Plati ovaj račun molim te"
    assert body["source_name"] == "racun.pdf"
    assert base64.b64decode(body["source_b64"]) == b"%PDF-1.4 fake"
    assert body["fields"] == {"amount": "84.20", "iban": "HR1210010051863000160"}
    assert "payment_intent: pay_0123456789ab status: ready" in r.content
    assert "READY" in r.content and "handoff" in r.content


def test_needs_review_tells_the_model_it_cannot_clear_it(fd):
    _, replies = fd
    replies["/prepare"] = {"intent": {**INTENT, "status": "needs_review", "warnings": [
        {"code": "iban_changed", "severity": "blocking", "message": "IBAN changed.", "acked": False}]}}
    r = _run(action="prepare", payee_name="HEP", iban="HR1210010051863000160", amount="1")
    assert "NEEDS THE USER" in r.content and "can't clear" in r.content
    assert "[the user must check it in Flight Deck] IBAN changed." in r.content


def test_handoff_saves_barcodes_for_the_bridges(fd, tmp_path):
    _, replies = fd
    png = base64.b64encode(b"\x89PNG fake").decode()
    replies["/handoff"] = {"intent": {**INTENT, "status": "handed_off"}, "page_url": "https://deck/p/tok",
                           "local_only": False, "images": [{"kind": "hub3", "png_b64": png}]}
    r = _run(action="handoff", id="pay_0123456789ab", _session_id="s1", _saved_base_path=str(tmp_path / "saved"))
    assert r.success, r.error
    path_line = next(line for line in r.content.splitlines() if line.startswith("Path: "))
    saved = Path(path_line[6:])
    assert saved.read_bytes() == b"\x89PNG fake"
    assert saved.relative_to(tmp_path / "saved").parts[:2] == ("media", "s1")
    assert "Nothing has been paid" in r.content and "https://deck/p/tok" in r.content


def test_mark_uses_the_turns_text_not_an_argument(fd):
    calls, replies = fd
    replies["/mark"] = {"intent": {**INTENT, "status": "user_paid"}}
    agent = SimpleNamespace(_turn_user_text="platio sam")
    r = _run(action="mark", id="pay_0123456789ab", status="paid", user_text="ignored", _agent=agent)
    assert r.success
    assert calls[0][2]["user_text"] == "platio sam" and calls[0][2]["status"] == "user_paid"


def test_bridges_deliver_payment_barcodes():
    from captain_claw.tools import whatsapp_send_file

    assert "payments" in whatsapp_send_file._DELIVERED_IMAGE_TOOLS


def test_registry_hides_the_tool_unless_payments_are_on(monkeypatch):
    from captain_claw.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(PaymentsTool(), metadata={"requires_payments": True})
    monkeypatch.setattr(payments_client, "payments_usable", lambda: False)
    assert "payments" not in [t.name for t in registry._resolve_tools()]
    monkeypatch.setattr(payments_client, "payments_usable", lambda: True)
    assert "payments" in [t.name for t in registry._resolve_tools()]


def test_handoff_to_revolut_says_it_is_a_draft(fd):
    calls, replies = fd
    replies["/handoff"] = {"rail": "revolut", "existing": False, "intent": {
        **INTENT, "status": "handed_off", "handoff_rail": "revolut",
        "revolut": {"draft_id": "draft-1", "state": "CREATED"}}}
    r = _run(action="handoff", id="pay_0123456789ab", rail="revolut")
    assert r.success and calls[0][2] == {"rail": "revolut"}
    assert "draft" in r.content and "Nothing has been paid" in r.content and "Path:" not in r.content


def test_ready_mentions_revolut_only_when_connected(fd):
    _, replies = fd
    replies["/prepare"] = {"intent": INTENT, "rails": ["barcode", "revolut"]}
    assert "rail='revolut'" in _run(action="prepare", payee_name="HEP", iban="HR1210010051863000160",
                                    amount="1").content
    replies["/prepare"] = {"intent": INTENT, "rails": ["barcode"]}
    assert "revolut" not in _run(action="prepare", payee_name="HEP", iban="HR1210010051863000160",
                                 amount="1").content


def test_a_settled_payment_says_bank_confirmed(fd):
    _, replies = fd
    replies["/pay_0123456789ab"] = {"intent": {**INTENT, "status": "settled",
                                               "revolut": {"draft_id": "draft-1", "state": "COMPLETED"}}}
    r = _run(action="get", id="pay_0123456789ab")
    assert "status: settled" in r.content and "confirmed by the bank" in r.content


def test_handoff_to_the_bank_gives_one_link(fd):
    calls, replies = fd
    replies["/handoff"] = {"rail": "bank", "url": "https://bank.example/auth?p=1", "bank_name": "Zagrebačka banka",
                           "intent": {**INTENT, "status": "handed_off", "handoff_rail": "bank"}}
    r = _run(action="handoff", id="pay_0123456789ab", rail="bank")
    assert r.success and calls[0][2] == {"rail": "bank"}
    assert "Link: https://bank.example/auth?p=1" in r.content and "Nothing has been paid" in r.content


def test_ready_lists_every_rail_on_offer(fd):
    _, replies = fd
    replies["/prepare"] = {"intent": INTENT, "rails": ["barcode", "revolut", "bank"]}
    out = _run(action="prepare", payee_name="HEP", iban="HR1210010051863000160", amount="1").content
    assert "rail='bank'" in out and "rail='revolut'" in out


# ── Phase 3: purchases ──────────────────────────────────────────────

_SHOP_PAGE = """<html><body><div id="form"><input autocomplete="cc-number" id="num"> <input autocomplete="cc-exp">
<input autocomplete="cc-csc" id="cvc"></div>
<button onclick="document.getElementById('out').textContent = document.getElementById('num').value.replace(/\\D/g,'')
 === '4111111111117788' ? 'Thank you! Order number KB-77.' : 'Payment declined.'">Kupi</button><p id="out"></p></body></html>"""


def test_purchase_refuses_card_data_and_sends_the_order(fd):
    calls, replies = fd
    r = _run(action="purchase", source="link", merchant_name="Shop", merchant_url="https://shop.example",
             amount="10", items=[{"name": "4111 1111 1111 1111"}])
    assert not r.success and calls == []
    replies["/purchases"] = {"purchase": {"id": "pur_0123456789ab", "status": "awaiting_approval", "source": "link",
                                          "merchant_name": "Shop", "merchant_domain": "shop.example",
                                          "amount_text": "10.00 EUR", "approval_url": "https://app.link.com/a/1"}}
    r = _run(action="purchase", source="link", merchant_name="Shop", merchant_url="https://shop.example",
             amount="10", items=[{"name": "Book", "quantity": 2}])
    assert r.success and "purchase_intent: pur_0123456789ab status: awaiting_approval" in r.content
    assert "https://app.link.com/a/1" in r.content and calls[0][2]["items"] == [{"name": "Book", "quantity": 2}]


def test_checkout_pay_never_shows_the_card(fd, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    from captain_claw.tools import browser as browser_tool

    calls, replies = fd
    replies["/credential"] = {"kind": "card", "card": {"number": "4111111111117788", "cvc": "321",
                                                       "exp_month": 10, "exp_year": 2027, "name": ""}}
    replies["/result"] = {"purchase": {"id": "pur_0123456789ab", "status": "ordered", "source": "revolut_card",
                                       "merchant_name": "Knjižara", "merchant_domain": "knjizara.example",
                                       "amount_text": "42.50 EUR", "order_ref": "KB-77"}}

    async def go():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.set_content(_SHOP_PAGE)

            class Session:
                is_alive = True

                async def ensure_page(self):
                    return page

            monkeypatch.setitem(browser_tool._BROWSER_SESSIONS, "s1", Session())
            try:
                return await PaymentsTool().execute(action="checkout_pay", id="pur_0123456789ab",
                                                    pay_button="Kupi", _session_id="s1")
            finally:
                await browser.close()

    r = asyncio.run(go())
    assert r.success, r.error
    assert "4111111111117788" not in r.content and "321" not in r.content
    assert "purchase_intent: pur_0123456789ab status: ordered" in r.content and "KB-77" in r.content
    posted = next(body for _, path, body in calls if path.endswith("/result"))
    assert posted["outcome"] == "confirmed" and "4111111111117788" not in json.dumps(posted)


def test_checkout_pay_needs_an_open_page(fd):
    r = _run(action="checkout_pay", id="pur_0123456789ab", pay_button="Pay", _session_id="nobody")
    assert not r.success and "No checkout page is open" in r.error


def test_protocol_purchases_send_shipping_and_never_ask_for_the_code(fd):
    calls, replies = fd
    assert "acp" in PaymentsTool.parameters["properties"]["source"]["description"]
    replies["/purchases"] = {"purchase": {"id": "pur_0123456789ab", "status": "awaiting_approval", "source": "acp",
                                          "merchant_name": "Shop", "merchant_domain": "shop.example",
                                          "amount_text": "3.00 USD", "approval_url": "https://app.link.com/a/2"}}
    r = _run(action="purchase", source="acp", merchant_name="Shop", merchant_url="https://shop.example",
             protocol_endpoint="shop.example", amount="3", items=[{"name": "Socks", "id": "item_123"}],
             shipping={"name": "Ana", "city": "Zagreb", "country": "HR", "phone_number": None})
    assert r.success and "https://app.link.com/a/2" in r.content
    assert calls[0][2]["shipping"] == {"name": "Ana", "city": "Zagreb", "country": "HR"}
    assert calls[0][2]["items"] == [{"name": "Socks", "id": "item_123"}]
    replies.clear()
    replies["/pur_0123456789ab"] = {"purchase": {"id": "pur_0123456789ab", "status": "needs_action", "source": "ap2",
                                                 "review_path": "/payments/purchases/pur_0123456789ab",
                                                 "next_action": {"display_message": "Enter the code"}}}
    r = _run(action="get", id="pur_0123456789ab")
    assert "Enter the code" in r.content and "Don't ask them to tell you the code" in r.content
    assert "checkout_pay" not in r.content


def test_statement_wording_and_no_transaction_details(fd):
    _, replies = fd
    base = {**INTENT, "review_path": "/payments/pay_0123456789ab"}
    replies["/pay_0123456789ab"] = {"intent": {**base, "status": "settled", "statement": {
        "booked_on": "2026-10-24", "account_label": "Erste ••4565", "match": "auto", "pending_since": "",
        "suggestions": 0}}}
    r = _run(action="get", id="pay_0123456789ab")
    assert "BANK CONFIRMED — on the user's statement, booked 24.10.2026. on Erste ••4565." in r.content
    replies["/pay_0123456789ab"] = {"intent": {**base, "status": "handed_off", "statement": {
        "booked_on": "", "pending_since": "2026-10-24", "suggestions": 1}}}
    r = _run(action="get", id="pay_0123456789ab")
    assert "pending since 24.10.2026." in r.content and "confirm or reject it in Flight Deck" in r.content


def test_reminders_go_where_the_conversation_came_from_and_can_be_switched(fd):
    calls, replies = fd
    replies["/prepare"] = {"intent": INTENT, "existing": False}
    agent = SimpleNamespace(_turn_user_text="plati",
                            session=SimpleNamespace(metadata={"origin": {"kind": "whatsapp", "address": "385911234567"}}))
    _run(action="prepare", payee_name="HEP", amount="84.20", iban="HR1210010051863000160", _agent=agent)
    assert calls[-1][2]["origin"] == {"kind": "whatsapp", "address": "385911234567"}
    _run(action="prepare", payee_name="HEP", amount="84.20", iban="HR1210010051863000160")
    assert calls[-1][2]["origin"] == {}
    replies["/reminders"] = {"intent": {**INTENT, "reminders": {"off": True}}}
    r = _run(action="remind", id="pay_0123456789ab", reminders="off")
    assert r.success and calls[-1][1].endswith("/pay_0123456789ab/reminders") and calls[-1][2] == {"on": False}
    assert "reminders for HEP Elektra d.o.o. are off" in r.content
    assert not _run(action="remind", id="pay_0123456789ab").success
    assert "status='due'" in PaymentsTool.description


def test_recurring_bills_list_and_propose(fd):
    calls, replies = fd
    replies["/agent/recurring"] = {"bills": [
        {"payee_name": "Stan d.o.o.", "status": "active", "cadence": "monthly", "amount_text": "650.00 EUR",
         "next_due": "2026-11-01", "prepares_itself": True},
        {"payee_name": "HEP", "status": "suggested", "cadence": "monthly", "amount_text": "79.00 EUR – 91.00 EUR",
         "next_due": "", "prepares_itself": False}]}
    r = _run(action="recurring")
    assert "Stan d.o.o. · monthly · 650.00 EUR · next around 01.11.2026. · Flight Deck prepares it" in r.content
    assert "HEP · monthly · 79.00 EUR – 91.00 EUR · suggested — the user confirms it in Flight Deck" in r.content
    replies.clear()
    replies["/pay_0123456789ab/recurring"] = {"bill": {"payee_name": "Gym", "status": "suggested", "cadence": "monthly"}}
    r = _run(action="recurring", id="pay_0123456789ab", cadence="monthly")
    assert calls[-1][2] == {"cadence": "monthly"} and "The user confirms it" in r.content
    replies["/agent/list"] = {"payments": [], "expected": [
        {"payee_name": "HEP", "expected_on": "2026-10-10", "amount_text": "~84 EUR", "status": "missing"}]}
    r = _run(action="list", status="due")
    assert "expected, no bill yet · HEP" in r.content and "MISSING" in r.content
