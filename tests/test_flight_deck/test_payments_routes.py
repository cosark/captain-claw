"""Payments routes — the agent prepares and hands over, only the owner clears a
warning, only the user's own words (or tap) mark a payment paid, and the /p page
is a capability link that goes away."""

from __future__ import annotations

import asyncio
import base64
import io
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from captain_claw.flight_deck import auth, payments_routes as pr
from captain_claw.flight_deck.db import FlightDeckDB
from captain_claw.flight_deck.payments_store import PaymentsStore
from captain_claw.payments import hub3

pytest.importorskip("zxingcpp")
pytest.importorskip("pdf417gen")

OWNER = {"id": "user-owner", "email": "o@x", "display_name": "Owner", "role": "user"}
OTHER = {"id": "user-other", "email": "t@x", "display_name": "Other", "role": "user"}
HR = "HR1210010051863000160"
AGENT = {"X-Agent-Auth": "tok-A"}


def _slip_png(amount_cents=8420, iban=HR, payee="HEP Elektra d.o.o.", reference="7269-68499637766-00019"):
    payload = hub3.build_payload(amount_cents=amount_cents, iban=iban, payee_name=payee, model="HR01",
                                 reference=reference, purpose_code="ELEC", description="Struja 09/2026").payload
    return base64.b64encode(hub3.render_png(payload)).decode()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from captain_claw.flight_deck import server

    monkeypatch.setenv("FD_PAYMENTS", "1")
    monkeypatch.delenv("FD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("FD_ALLOWED_HOSTS", raising=False)
    db = FlightDeckDB(tmp_path / "flight-deck.db")
    asyncio.run(db.init())
    auth.set_auth_db(db)
    pr.configure(data_dir=tmp_path)
    store = PaymentsStore(tmp_path / "payments.db")
    pr.set_store(store)
    owners = {"tok-A": OWNER["id"], "tok-other": OTHER["id"]}
    monkeypatch.setattr(server, "_agent_caller_ok", lambda request: True)
    monkeypatch.setattr(server, "_find_agent_by_auth", lambda t: (t in owners, owners.get(t, ""), "slug"))
    app = FastAPI()
    app.include_router(pr.router)
    app.include_router(pr.public_router)
    current = {"user": OWNER}
    app.dependency_overrides[auth.get_current_user] = lambda: current["user"]
    yield TestClient(app), current, db, store
    asyncio.run(store.close())
    pr.set_store(None)
    asyncio.run(db.close())
    auth.set_auth_db(None)  # type: ignore[arg-type]


def _prepare(client, headers=AGENT, **body):
    return client.post("/fd/payments/agent/prepare", json=body, headers=headers)


def _codes(intent):
    return [w["code"] for w in intent["warnings"]]


def test_off_by_default(env, monkeypatch):
    client, *_ = env
    monkeypatch.delenv("FD_PAYMENTS")
    assert client.get("/fd/payments/agent/status", headers=AGENT).json() == {
        "enabled": False, "deps_ok": True, "rails": []}
    assert _prepare(client, source_b64=_slip_png(), source_name="r.png").status_code == 404
    assert client.get("/fd/payments").status_code == 404


def test_browsers_and_strangers_cannot_use_agent_routes(env):
    client, *_ = env
    assert _prepare(client, headers={"X-Agent-Auth": "nope"}).status_code == 403
    r = client.post("/fd/payments/agent/prepare", json={}, headers={**AGENT, "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403


def test_barcode_on_the_invoice_wins(env):
    client, *_ = env
    r = _prepare(client, source_b64=_slip_png(), source_name="racun.png",
                 fields={"payee_name": "HEP", "amount": "84.20"})
    intent = r.json()["intent"]
    assert intent["status"] == "ready", intent
    assert intent["iban"] == HR and intent["amount_cents"] == 8420
    assert intent["payee_name"] == "HEP Elektra d.o.o."   # the barcode's name, no fuss over "HEP"
    assert intent["provenance"]["iban"] == "barcode" and intent["source_kind"] == "barcode_hub3"
    assert intent["barcodes"] == ["hub3"] and intent["bank"] == "Hrvatska narodna banka"
    assert "barcode_mismatch" not in _codes(intent)
    # Same file again: the same payment, not a second one.
    again = _prepare(client, source_b64=_slip_png(), source_name="racun.png").json()
    assert again["existing"] and again["intent"]["id"] == intent["id"]


def test_misread_amount_needs_the_owner(env):
    client, current, *_ = env
    intent = _prepare(client, source_b64=_slip_png(), source_name="r.png",
                      fields={"payee_name": "HEP Elektra d.o.o.", "amount": "48,20"}).json()["intent"]
    assert intent["status"] == "needs_review" and intent["amount_cents"] == 8420
    pid = intent["id"]
    blocked = client.post(f"/fd/payments/agent/{pid}/handoff", headers=AGENT)
    assert blocked.status_code == 409 and "review" in blocked.json()["detail"]
    # There is no agent route that clears a warning.
    assert client.post(f"/fd/payments/agent/{pid}/ack", json={"codes": ["barcode_mismatch"]},
                       headers=AGENT).status_code in (404, 405)
    # Another user can't see it or clear it.
    current["user"] = OTHER
    assert client.post(f"/fd/payments/{pid}/ack", json={"codes": ["barcode_mismatch"]}).status_code == 404
    current["user"] = OWNER
    acked = client.post(f"/fd/payments/{pid}/ack", json={"codes": ["barcode_mismatch"]}).json()["intent"]
    assert acked["status"] == "ready"
    assert client.post(f"/fd/payments/agent/{pid}/handoff", headers=AGENT).status_code == 200


def test_card_data_is_refused_and_not_stored(env):
    client, _, _, store = env
    r = _prepare(client, fields={"payee_name": "Shop", "description": "card 4111 1111 1111 1111"})
    assert r.status_code == 422
    assert asyncio.run(store.list(OWNER["id"])) == []


def test_model_only_fields_are_flagged(env):
    client, *_ = env
    intent = _prepare(client, fields={"payee_name": "Vodovod d.o.o.", "iban": HR, "amount": "12,50",
                                      "model": "HR00", "reference": "123-456"}).json()["intent"]
    assert intent["status"] == "ready"
    assert "model_read_only" in _codes(intent)
    text_backed = _prepare(client, fields={"payee_name": "Plin", "iban": HR, "amount": "33,10"},
                           user_text="Plati plin, IBAN HR1210010051863000160, 33,10 EUR").json()["intent"]
    assert text_backed["provenance"]["iban"] == "document"
    assert "model_read_only" not in _codes(text_backed)


def test_handoff_gives_barcodes_and_a_page(env):
    client, *_ = env
    pid = _prepare(client, source_b64=_slip_png(payee="HEP"), source_name="r.png",
                   fields={"payee_name": "HEP"}).json()["intent"]["id"]
    out = client.post(f"/fd/payments/agent/{pid}/handoff", headers=AGENT).json()
    assert out["intent"]["status"] == "handed_off"
    assert [i["kind"] for i in out["images"]] == ["hub3"]
    from PIL import Image
    import zxingcpp

    decoded = zxingcpp.read_barcodes(Image.open(io.BytesIO(base64.b64decode(out["images"][0]["png_b64"]))))
    assert decoded[0].text.startswith("HRVHUB30\nEUR\n000000000008420\n")
    token = out["page_url"].rsplit("/p/", 1)[1]
    page = client.get(f"/p/{token}")
    assert page.status_code == 200 and "data:image/png;base64," in page.text
    assert "HR12 1001 0051 8630 0016 0" in page.text and "I paid" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'sha256-" in csp and "form-action 'self'" in csp and "img-src data:" in csp
    assert page.headers["cache-control"] == "no-store"
    # Not no-referrer: that makes the browser send the "I paid" POST with
    # Origin: null, which origin_guard refuses as cross-site.
    assert page.headers["referrer-policy"] == "same-origin"
    assert 'content="same-origin"' in page.text
    # Tapping "I paid" on the page records it; the page then says so.
    done = client.post(f"/p/{token}/paid", follow_redirects=False)
    assert done.status_code == 303
    assert "Marked as paid" in client.get(f"/p/{token}").text
    assert client.get(f"/fd/payments/{pid}").json()["intent"]["paid_via"] == "page"
    assert client.get("/p/not-a-real-token-at-all-xxxx").status_code == 404


def test_page_expires(env):
    client, _, _, store = env
    pid = _prepare(client, source_b64=_slip_png(payee="HEP"), source_name="r.png",
                   fields={"payee_name": "HEP"}).json()["intent"]["id"]
    token = client.post(f"/fd/payments/agent/{pid}/handoff", headers=AGENT).json()["page_url"].rsplit("/p/", 1)[1]
    asyncio.run(store.update(pid, page_expires_at=time.time() - 1))
    assert client.get(f"/p/{token}").status_code == 404


def test_page_hides_the_barcode_while_review_is_open(env):
    client, _, _, store = env
    pid = _prepare(client, source_b64=_slip_png(payee="HEP"), source_name="r.png",
                   fields={"payee_name": "HEP"}).json()["intent"]["id"]
    token = client.post(f"/fd/payments/agent/{pid}/handoff", headers=AGENT).json()["page_url"].rsplit("/p/", 1)[1]
    asyncio.run(store.update(pid, status="needs_review"))
    page = client.get(f"/p/{token}").text
    assert "Review needed first" in page and "data:image/png" not in page
    assert client.post(f"/p/{token}/paid", follow_redirects=False).status_code == 303
    assert asyncio.run(store.get(pid))["status"] == "needs_review"


def test_only_the_users_words_mark_it_paid(env):
    client, *_ = env
    pid = _prepare(client, source_b64=_slip_png(payee="HEP"), source_name="r.png",
                   fields={"payee_name": "HEP"}).json()["intent"]["id"]
    url = f"/fd/payments/agent/{pid}/mark"
    assert client.post(url, json={"status": "user_paid", "user_text": "Platio sam", "automated": True},
                       headers=AGENT).status_code == 403
    assert client.post(url, json={"status": "user_paid", "user_text": "nisam još platio"},
                       headers=AGENT).status_code == 403
    assert client.post(url, json={"status": "user_paid", "user_text": "is it paid?"},
                       headers=AGENT).status_code == 403
    ok = client.post(url, json={"status": "user_paid", "user_text": "Platio sam, hvala"}, headers=AGENT)
    assert ok.status_code == 200 and ok.json()["intent"]["status"] == "user_paid"
    assert client.post(url, json={"status": "skipped"}, headers=AGENT).status_code == 409


def test_changed_iban_for_a_known_payee(env):
    client, *_ = env
    first = _prepare(client, fields={"payee_name": "Vodovod d.o.o.", "iban": HR, "amount": "20,00"},
                     user_text="HR1210010051863000160 20,00").json()["intent"]
    client.post(f"/fd/payments/{first['id']}/mark", json={"status": "user_paid"})
    other_iban = "HR5623400091110000000"
    second = _prepare(client, fields={"payee_name": "VODOVOD", "iban": other_iban, "amount": "21,00"},
                      user_text=f"{other_iban} 21,00").json()["intent"]
    assert "iban_changed" in _codes(second) and second["status"] == "needs_review"


def test_cap_is_per_user(env):
    client, current, *_ = env
    assert client.get("/fd/payments/config").json()["cap_cents"] == 500_000
    assert client.put("/fd/payments/settings", json={"cap_cents": 1000}).json()["cap_cents"] == 1000
    intent = _prepare(client, fields={"payee_name": "X", "iban": HR, "amount": "20,00"}).json()["intent"]
    assert "over_cap" in _codes(intent)
    current["user"] = OTHER
    assert client.get("/fd/payments/config").json()["cap_cents"] == 500_000


def test_supersede_cancels_the_old_one(env):
    client, _, _, store = env
    old = _prepare(client, fields={"payee_name": "X", "iban": HR, "amount": "20,00"}).json()["intent"]
    new = _prepare(client, fields={"payee_name": "X", "iban": HR, "amount": "21,00"},
                   supersedes=old["id"]).json()["intent"]
    assert new["supersedes"] == old["id"]
    assert asyncio.run(store.get(old["id"]))["status"] == "cancelled"


def test_automated_prepare_rings_the_bell(env):
    client, _, db, _ = env
    intent = _prepare(client, fields={"payee_name": "X", "iban": HR, "amount": "20,00"},
                      automated=True, job_kind="mail_triage").json()["intent"]
    notes = asyncio.run(db.list_notifications(OWNER["id"]))
    assert notes and notes[0]["ref_id"] == intent["id"] and notes[0]["type"] == "payment"


def test_rate_limit(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr(pr, "PREPARES_PER_HOUR", 2)
    for amount in ("1,00", "2,00"):
        assert _prepare(client, fields={"payee_name": "X", "iban": HR, "amount": amount}).status_code == 200
    assert _prepare(client, fields={"payee_name": "X", "iban": HR, "amount": "3,00"}).status_code == 429


def test_stale_payments_expire_and_users_are_forgotten(env):
    client, _, _, store = env
    pid = _prepare(client, fields={"payee_name": "X", "iban": HR, "amount": "1,00"}).json()["intent"]["id"]
    asyncio.run(store.update(pid, due_date="2026-01-01"))
    assert asyncio.run(store.expire_stale()) == 1
    assert asyncio.run(store.get(pid))["status"] == "expired"
    asyncio.run(pr.forget_user(OWNER["id"]))
    assert asyncio.run(store.list(OWNER["id"])) == []


@pytest.mark.parametrize("text,said", [
    ("Platio sam", True), ("paid, thanks", True), ("Uplaćeno.", True), ("I paid it", True),
    ("nisam platio", False), ("I haven't paid yet", False), ("je li plaćeno?", False),
    ("is it paid?", False), ("pay it please", False), ("", False),
])
def test_paid_said(text, said):
    assert pr.paid_said(text) is said


@pytest.mark.parametrize("raw,cents", [("84,20", 8420), ("84.2", 8420), ("1.234,55", 123455),
                                       ("1,234.55", 123455), ("€ 12", 1200), ("1.234", 123400)])
def test_parse_amount(raw, cents):
    assert pr.parse_amount(raw) == cents
