"""The `payments` tool: card data never leaves, "paid" comes from the user's own
message, barcodes land where the chat bridges deliver them, and the tool is
hidden unless the owner's Flight Deck has payments on."""

from __future__ import annotations

import asyncio
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
