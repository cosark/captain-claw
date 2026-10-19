"""Bank statement ↔ payment matching (Payments Phase 4, docs/payments-phase4-plan.md).

Pure helpers, no I/O. 4a uses the normalising and fingerprint helpers to store
outgoing transactions; 4b adds the matcher.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

_NON_DIGIT = re.compile(r"\D+")


def amount_cents(value) -> int | None:
    """A bank amount ("84.20", "-84.2", 84.2) as positive cents; None if unreadable."""
    try:
        cents = (abs(Decimal(str(value).strip().replace(",", "."))) * 100).quantize(Decimal(1))
    except (InvalidOperation, ValueError):
        return None
    return int(cents)


def reference_digits(*texts: str) -> str:
    """The digits of each reference text, one group per text: "HR01 7269-68499637766-00019"
    → "0172696849963776600019". A bill's reference is found when its own digits
    (model + reference) appear inside one group."""
    groups = [_NON_DIGIT.sub("", t or "") for t in texts]
    return " ".join(g for g in groups if g)[:200]


def bill_digits(model: str, reference: str) -> str:
    """A bill's model + reference as digits ("HR01", "7269-68499637766-00019" → "0172696849963776600019")."""
    return _NON_DIGIT.sub("", f"{model or ''}{reference or ''}")


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()
