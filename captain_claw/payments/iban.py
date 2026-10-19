"""IBAN checks: shape, per-country length, the mod-97 check digits, and the
Croatian bank an IBAN belongs to (from its 7-digit VBDI)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# IBAN length per country: the SEPA countries plus Croatia's non-SEPA neighbours
# (a bill from Bosnia or Serbia should fail as "not SEPA", not as "unknown").
IBAN_LENGTHS: dict[str, int] = {
    "AD": 24, "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GI": 23, "GR": 27,
    "HR": 21, "HU": 28, "IE": 22, "IS": 26, "IT": 27, "LI": 21, "LT": 20, "LU": 20,
    "LV": 21, "MC": 27, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24,
    "SE": 24, "SI": 19, "SK": 24, "SM": 27, "VA": 22,
    "AL": 28, "BA": 20, "ME": 22, "MK": 19, "RS": 22,
}

SEPA_COUNTRIES = frozenset({
    "AD", "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB",
    "GI", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MC", "MT", "NL",
    "NO", "PL", "PT", "RO", "SE", "SI", "SK", "SM", "VA",
})

_SHAPE_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{1,30}$")


def normalize(value: str) -> str:
    """``hr12 1001-0051 ...`` → ``HR1210010051...``."""
    return re.sub(r"[\s\-]", "", str(value or "")).upper()


def _mod97(iban: str) -> int:
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97


@dataclass(frozen=True)
class IbanCheck:
    iban: str
    ok: bool
    country: str = ""
    reason: str = ""


def check(value: str) -> IbanCheck:
    """Is *value* an IBAN whose check digits add up?"""
    iban = normalize(value)
    if not _SHAPE_RE.match(iban):
        return IbanCheck(iban, False, reason="this isn't shaped like an IBAN")
    country = iban[:2]
    expected = IBAN_LENGTHS.get(country)
    if expected is None:
        return IbanCheck(iban, False, country, f"IBANs from {country} aren't supported")
    if len(iban) != expected:
        return IbanCheck(iban, False, country,
                         f"{country} IBANs have {expected} characters, this one has {len(iban)}")
    if _mod97(iban) != 1:
        return IbanCheck(iban, False, country, "the check digits don't match (a typo?)")
    return IbanCheck(iban, True, country)


def grouped(value: str) -> str:
    """``HR1210010051863000160`` → ``HR12 1001 0051 8630 0016 0``."""
    iban = normalize(value)
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def is_sepa(value: str) -> bool:
    return normalize(value)[:2] in SEPA_COUNTRIES


@lru_cache(maxsize=1)
def _hr_banks() -> dict[str, str]:
    raw = json.loads((Path(__file__).with_name("hr_banks.json")).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def bank_name(value: str) -> str:
    """The Croatian bank a Croatian IBAN belongs to ('' when unknown or not HR)."""
    iban = normalize(value)
    if not iban.startswith("HR") or len(iban) != 21:
        return ""
    return _hr_banks().get(iban[4:11], "")
