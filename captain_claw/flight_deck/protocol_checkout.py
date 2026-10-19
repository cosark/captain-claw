"""Checkout protocols for purchases — ACP (OpenAI/Stripe) and AP2 (Google).

Payments Phase 3 (docs/payments-phase3-plan.md). Filled in by Phase 3b (ACP)
and 3c (AP2); until a protocol is switched on, purchases through it are refused
with a plain answer.
"""

from __future__ import annotations

from fastapi import HTTPException

PROTOCOLS = ("acp", "ap2")


def enabled(kind: str) -> bool:
    return False


async def start(row: dict, endpoint: str) -> dict:
    raise HTTPException(409, f"{row['source'].upper()} checkout isn't available on this deck yet.")


async def approve(row: dict, owner: str) -> dict:
    raise HTTPException(409, f"{row['source'].upper()} checkout isn't available on this deck yet.")
