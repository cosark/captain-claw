"""Card numbers, CVVs and expiry dates never get into Captain (card_scrub)."""

import json
from pathlib import Path

import pytest

from captain_claw import card_scrub

_CASES = json.loads((Path(__file__).parent / "fixtures" / "card_scrub_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["text"][:40] for c in _CASES])
def test_shared_cases(case):
    found = card_scrub.find(case["text"])
    got = [{"kind": f.kind, **({"last4": f.last4} if f.kind == "pan" else {})} for f in found]
    assert got == case["expect"]
    if "scrubbed" in case:
        assert card_scrub.scrub(case["text"])[0] == case["scrubbed"]


def test_untouched_text_is_returned_as_is():
    text = "Pay HR1210010051863000160, HR01 7269-68499637766-00019, 123,55 EUR"
    assert card_scrub.scrub_user_text(text) is text


def test_user_text_gets_the_note():
    out = card_scrub.scrub_user_text("card 4111 1111 1111 1111")
    assert "4111" not in out
    assert out.endswith(card_scrub.NOTE)


def test_card_like_value():
    assert card_scrub.looks_like_card_data("4111-1111-1111-1111")
    assert not card_scrub.looks_like_card_data("7269-68499637766-00019")


def test_luhn():
    assert card_scrub.luhn_ok("4111111111111111")
    assert not card_scrub.luhn_ok("4111111111111112")
