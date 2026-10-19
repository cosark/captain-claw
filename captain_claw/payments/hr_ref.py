"""Payment references: the Croatian model + poziv na broj, and the ISO 11649
RF creditor reference.

Phase 0 checks the format only; the per-model check digits (FINA's model list)
are a later step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MODEL_RE = re.compile(r"^HR\d{2}$")
# Up to three dash-separated groups of digits, at most 22 characters in all.
_POZIV_RE = re.compile(r"^\d+(?:-\d+){0,2}$")
_RF_RE = re.compile(r"^RF\d{2}[A-Z0-9]{1,21}$")
NO_REFERENCE_MODEL = "HR99"


def normalize_model(value: str) -> str:
    """``hr 01`` / ``01`` → ``HR01``; anything else is returned cleaned."""
    m = re.sub(r"\s", "", str(value or "")).upper()
    if re.fullmatch(r"\d{2}", m):
        m = "HR" + m
    return m


def normalize_reference(value: str) -> str:
    """Collapse spaces, drop the ones around dashes, upper-case an RF reference."""
    r = re.sub(r"\s*-\s*", "-", " ".join(str(value or "").split()))
    compact = r.replace(" ", "").upper()
    return compact if compact.startswith("RF") else r.replace(" ", "")


def is_rf(reference: str) -> bool:
    return bool(_RF_RE.match(normalize_reference(reference)))


def rf_ok(reference: str) -> bool:
    ref = normalize_reference(reference)
    if not _RF_RE.match(ref):
        return False
    rearranged = ref[4:] + ref[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


@dataclass(frozen=True)
class RefCheck:
    ok: bool
    model: str
    reference: str
    reason: str = ""


def check(model: str, reference: str) -> RefCheck:
    """Does the model + reference pair make sense?"""
    m, r = normalize_model(model), normalize_reference(reference)
    if not m:
        if not r:
            return RefCheck(True, m, r)
        if is_rf(r):
            return RefCheck(rf_ok(r), m, r, "" if rf_ok(r) else "the RF reference's check digits don't match")
        return RefCheck(False, m, r, "a poziv na broj needs a model (HR00, HR01, …)")
    if not _MODEL_RE.match(m):
        return RefCheck(False, m, r, f"'{m}' isn't a Croatian model (HR followed by two digits)")
    if m == NO_REFERENCE_MODEL:
        return RefCheck(not r, m, r, "" if not r else "model HR99 means no poziv na broj")
    if not r:
        return RefCheck(False, m, r, f"model {m} needs a poziv na broj")
    if len(r) > 22:
        return RefCheck(False, m, r, "a poziv na broj has at most 22 characters")
    if not _POZIV_RE.match(r):
        return RefCheck(False, m, r, "a poziv na broj is digits in up to three dash-separated parts")
    return RefCheck(True, m, r)
