"""The payments background loop — Payments Phase 4 (docs/payments-phase4-plan.md).

One loop for the work no request triggers: stale bills expire, bank statements
are read on their daily slots (4a), bills are matched against them hourly so
"not on the statement" ages (4b; every sync matches too), and old unmatched
statement rows are purged. Reminders (4c) and recurring bills (4d) come here
next. Each step is idempotent, so a restart or a double tick repeats nothing;
one failure is logged and never stops the loop.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date

from captain_claw.logging import get_logger

log = get_logger(__name__)

TICK_SECONDS = 300
MATCH_EVERY = 3600
_last_purge: dict[str, date] = {}
_last_match: dict[str, float] = {}


async def tick(now: float | None = None) -> dict:
    from captain_claw.flight_deck import bank_statements, payments_routes, statements_routes

    out = {"expired": 0, "synced": 0, "purged": 0, "matched": 0}
    if not payments_routes.enabled():
        return out
    out["expired"] = await payments_routes.store().expire_stale(now)
    if bank_statements.enabled():
        out["synced"] = await statements_routes.background_sync(now)
        # "Not on the statement" ages with the clock, not only with syncs.
        clock = time.time() if now is None else now
        if clock - _last_match.get("all", 0) >= MATCH_EVERY:
            from captain_claw.flight_deck import statement_matching

            out["matched"] = await statement_matching.match_all()
            _last_match["all"] = clock
        today = date.today()
        if _last_purge.get("statements") != today:
            out["purged"] = await statements_routes.purge(today)
            _last_purge["statements"] = today
    return out


async def payments_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await tick()
        except Exception as exc:  # noqa: BLE001 — never let one bad tick end the loop
            log.warning("Payments loop tick failed", error=f"{type(exc).__name__}: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
