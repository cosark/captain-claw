"""IBAN and reference checks, grounding, and the warnings a payment carries."""

from datetime import date

from captain_claw.payments import checks, grounding, hr_ref, iban

HR = "HR1210010051863000160"
TODAY = date(2026, 10, 19)


def _codes(warnings):
    return [w.code for w in warnings]


def _fields(**over):
    f = dict(payee_name="HEP Elektra d.o.o.", iban=HR, amount_cents=8420, currency="EUR",
             model="HR01", reference="7269-68499637766-00019", due_date="2026-10-25")
    f.update(over)
    return f


def _prov(source="document"):
    return {k: source for k in ("payee_name", "iban", "amount_cents", "reference")}


# ── IBAN / references ────────────────────────────────────────────────

def test_iban_check():
    assert iban.check("hr12 1001 0051 8630 0016 0").ok
    assert iban.check("DE89 3704 0044 0532 0130 00").ok
    assert "check digits" in iban.check("HR1310010051863000160").reason
    assert "21 characters" in iban.check("HR121001005186300016").reason
    assert not iban.check("hello").ok
    assert iban.grouped(HR) == "HR12 1001 0051 8630 0016 0"


def test_bank_name():
    assert iban.bank_name(HR) == "Hrvatska narodna banka"
    assert iban.bank_name("DE89370400440532013000") == ""


def test_references():
    assert hr_ref.check("HR01", "7269-68499637766-00019").ok
    assert hr_ref.check("01", "7269 - 684996").ok
    assert hr_ref.check("HR99", "").ok
    assert not hr_ref.check("HR99", "123").ok
    assert not hr_ref.check("HR01", "").ok
    assert not hr_ref.check("", "12345").ok
    assert not hr_ref.check("HR01", "1-2-3-4").ok
    assert hr_ref.rf_ok("RF18539007547034")
    assert not hr_ref.rf_ok("RF19539007547034")
    assert hr_ref.check("", "RF18 5390 0754 7034").ok


# ── grounding ─────────────────────────────────────────────────────────

def test_amount_and_iban_in_text():
    assert grounding.amount_in("Ukupno: 1.234,55 EUR", 123455)
    assert grounding.amount_in("Total 1,234.55", 123455)
    assert grounding.amount_in("Za platiti 84,20 €", 8420)
    assert not grounding.amount_in("Za platiti 184,20 €", 8420)
    assert grounding.iban_in("IBAN: HR12 1001 0051 8630 0016 0", HR)
    assert grounding.reference_in("Poziv na broj: HR01 7269-68499637766-00019", "7269-68499637766-00019")


def test_merge_barcode_wins_and_flags_mismatch():
    merged = grounding.merge(
        {"iban": HR, "amount_cents": 9999, "payee_name": "HEP"},
        {"iban": HR, "amount_cents": 8420, "payee_name": "HEP ELEKTRA"},
        "")
    assert merged.fields["amount_cents"] == 8420
    assert merged.provenance["amount_cents"] == "barcode"
    assert sorted(m[0] for m in merged.mismatches) == ["amount_cents", "payee_name"]


def test_merge_model_fields_checked_against_text():
    merged = grounding.merge({"iban": HR, "amount_cents": 8420}, None, "IBAN HR1210010051863000160 iznos 84,21")
    assert merged.provenance["iban"] == "document"
    assert merged.provenance["amount_cents"] == "model"
    assert merged.not_in_document == ["amount_cents"]
    photo = grounding.merge({"iban": HR}, None, "")
    assert photo.provenance["iban"] == "model" and photo.not_in_document == []


# ── warnings ──────────────────────────────────────────────────────────

def test_clean_payment_is_ready():
    w = checks.evaluate(_fields(), _prov(), today=TODAY)
    assert _codes(w) == ["new_payee"]
    assert checks.status_for(_fields(), [x.as_dict() for x in w]) == "ready"


def test_errors_make_a_draft():
    w = checks.evaluate(_fields(iban="HR1310010051863000160", currency="USD",
                                description="card 4111 1111 1111 1111"), _prov(), today=TODAY)
    assert {"iban_invalid", "non_eur", "card_data"} <= set(_codes(w))
    assert checks.status_for(_fields(), [x.as_dict() for x in w]) == "draft"
    assert checks.status_for(_fields(iban=""), []) == "draft"


def test_iban_changed_blocks_until_acked():
    history = [{"payee_key": checks.payee_key("HEP ELEKTRA d.o.o."), "iban": "HR2123400091110000000",
                "amount_cents": 8000, "paid_on": "2026-09-20"}]
    w = checks.evaluate(_fields(), _prov(), history=history, today=TODAY)
    assert "iban_changed" in _codes(w) and "new_payee" not in _codes(w)
    as_dicts = [x.as_dict() for x in w]
    assert checks.status_for(_fields(), as_dicts) == "needs_review"
    assert checks.status_for(_fields(), as_dicts, {"iban_changed"}) == "ready"


def test_known_payee_same_iban_is_quiet():
    history = [{"payee_key": checks.payee_key("HEP Elektra"), "iban": HR, "amount_cents": 8000}]
    assert _codes(checks.evaluate(_fields(), _prov(), history=history, today=TODAY)) == []


def test_duplicates():
    same_source = [{"id": "pay_old", "status": "handed_off", "source_sha256": "abc", "created_on": "2026-10-01"}]
    assert "possible_duplicate" in _codes(checks.evaluate(
        _fields(), _prov(), source_sha256="abc", others=same_source, today=TODAY))
    same_payment = [{"id": "pay_old", "status": "user_paid", "iban": HR, "amount_cents": 8420,
                     "reference": "7269-68499637766-00019", "created_on": "2026-10-01"}]
    assert "possible_duplicate" in _codes(checks.evaluate(_fields(), _prov(), others=same_payment, today=TODAY))
    old = [dict(same_payment[0], created_on="2026-06-01")]
    assert "possible_duplicate" not in _codes(checks.evaluate(_fields(), _prov(), others=old, today=TODAY))
    cancelled = [dict(same_payment[0], status="cancelled")]
    assert "possible_duplicate" not in _codes(checks.evaluate(_fields(), _prov(), others=cancelled, today=TODAY))
    itself = [dict(same_payment[0], id="pay_me")]
    assert "possible_duplicate" not in _codes(checks.evaluate(
        _fields(), _prov(), intent_id="pay_me", others=itself, today=TODAY))


def test_cap_unusual_overdue_and_provenance():
    history = [{"payee_key": checks.payee_key("HEP Elektra"), "iban": HR, "amount_cents": a} for a in (2000, 2500)]
    w = checks.evaluate(_fields(amount_cents=900_000, due_date="2026-10-01"), _prov("model"),
                        history=history, cap_cents=500_000, today=TODAY, text_notes=["payee: 'a' → 'b'"])
    assert {"over_cap", "amount_unusual", "overdue", "model_read_only", "text_adjusted"} <= set(_codes(w))


def test_mismatch_and_not_in_document_block():
    w = checks.evaluate(_fields(), _prov(), mismatches=[("amount_cents", 8420, 9999)],
                        not_in_document=["iban"], today=TODAY)
    assert _codes(w)[:2] == ["barcode_mismatch", "not_in_document"]
    assert all(x.severity == "blocking" for x in w if x.code in ("barcode_mismatch", "not_in_document"))


def test_barcodes_for():
    assert checks.barcodes_for(_fields()) == ["hub3"]
    assert checks.barcodes_for(_fields(model="HR99", reference="")) == ["hub3", "epc"]
    assert checks.barcodes_for(_fields(iban="DE89370400440532013000")) == ["epc"]
    assert checks.barcodes_for(_fields(currency="USD")) == []


def test_payee_key():
    assert checks.payee_key("HEP Elektra d.o.o.") == checks.payee_key("hep elektra")
    assert checks.payee_key("x", "123-456-789-01") == "oib:12345678901"
