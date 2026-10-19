"""Payments — the per-owner ledger of payments Captain prepared.

A dedicated SQLite DB (``payments.db``) in Flight Deck's data dir, deck-isolated
via ``FD_DATA_DIR``, like ``bat.db``. Captain never moves money: a row records a
payment the agent got ready and handed over as a barcode, and what the user did
with it (docs/payments-phase0-plan.md).

Tables:
  payment_intents   — one row per prepared payment (fields as JSON + the
                      columns the duplicate and history checks query)
  payment_events    — append-only audit trail (who did what, when)
  payment_settings  — per-owner limit (cap)

``owner_id`` has no foreign key: an auth-off deck's owner is the synthetic
``local`` user, which isn't a row in ``users`` (same as public_file_shares).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

LIVE_STATES = ("draft", "needs_review", "ready", "handed_off")
PAID_STATES = ("user_paid", "settled")
TERMINAL_STATES = PAID_STATES + ("skipped", "cancelled", "expired")
EXPIRE_AFTER_SECONDS = 30 * 86400

_DDL = """
CREATE TABLE IF NOT EXISTS payment_intents (
    id              TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL,
    agent           TEXT NOT NULL DEFAULT '',
    session_id      TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    fields          TEXT NOT NULL DEFAULT '{}',
    provenance      TEXT NOT NULL DEFAULT '{}',
    warnings        TEXT NOT NULL DEFAULT '[]',
    acks            TEXT NOT NULL DEFAULT '{}',
    payee_key       TEXT NOT NULL DEFAULT '',
    iban            TEXT NOT NULL DEFAULT '',
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    reference       TEXT NOT NULL DEFAULT '',
    due_date        TEXT NOT NULL DEFAULT '',
    source_name     TEXT NOT NULL DEFAULT '',
    source_sha256   TEXT NOT NULL DEFAULT '',
    source_kind     TEXT NOT NULL DEFAULT '',
    barcodes        TEXT NOT NULL DEFAULT '[]',
    page_token_hash TEXT,
    page_expires_at REAL,
    supersedes      TEXT NOT NULL DEFAULT '',
    automated       INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    paid_at         REAL,
    paid_via        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_payment_intents_owner ON payment_intents(owner_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_intents_page
    ON payment_intents(page_token_hash) WHERE page_token_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS payment_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id  TEXT NOT NULL,
    owner_id   TEXT NOT NULL,
    ts         REAL NOT NULL,
    actor      TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_payment_events_intent ON payment_events(intent_id, ts);
CREATE TABLE IF NOT EXISTS payment_settings (
    owner_id   TEXT PRIMARY KEY,
    cap_cents  INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""

_JSON_COLS = {"fields": {}, "provenance": {}, "warnings": [], "acks": {}, "barcodes": []}


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        return default


def _row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col, default in _JSON_COLS.items():
        out[col] = _loads(out.get(col), type(default)())
    out["automated"] = bool(out.get("automated"))
    return out


def _dump(values: dict) -> dict:
    return {k: (json.dumps(v, ensure_ascii=False) if k in _JSON_COLS else v) for k, v in values.items()}


class PaymentsStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        db = await aiosqlite.connect(str(self.db_path))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript(_DDL)
        await db.commit()
        self._db = db
        return db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ── intents ──────────────────────────────────────────────────────

    async def create(self, row: dict) -> dict:
        db = await self._ensure_db()
        now = time.time()
        values = _dump({**row, "created_at": now, "updated_at": now})
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        await db.execute(f"INSERT INTO payment_intents ({cols}) VALUES ({marks})", tuple(values.values()))
        await db.commit()
        return await self.get(row["id"])

    async def update(self, intent_id: str, **changes: Any) -> dict | None:
        db = await self._ensure_db()
        values = _dump({**changes, "updated_at": time.time()})
        sets = ", ".join(f"{k} = ?" for k in values)
        await db.execute(f"UPDATE payment_intents SET {sets} WHERE id = ?", (*values.values(), intent_id))
        await db.commit()
        return await self.get(intent_id)

    async def get(self, intent_id: str) -> dict | None:
        db = await self._ensure_db()
        async with db.execute("SELECT * FROM payment_intents WHERE id = ?", (intent_id,)) as cur:
            return _row(await cur.fetchone())

    async def get_by_page(self, token_hash: str) -> dict | None:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT * FROM payment_intents WHERE page_token_hash = ?", (token_hash,)
        ) as cur:
            return _row(await cur.fetchone())

    async def list(self, owner_id: str, *, statuses: tuple[str, ...] = (), limit: int = 200,
                   since: float = 0.0) -> list[dict]:
        db = await self._ensure_db()
        q = "SELECT * FROM payment_intents WHERE owner_id = ? AND created_at >= ?"
        args: list[Any] = [owner_id, since]
        if statuses:
            q += f" AND status IN ({', '.join('?' for _ in statuses)})"
            args += list(statuses)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        async with db.execute(q, args) as cur:
            return [_row(r) for r in await cur.fetchall()]

    async def find_live(self, owner_id: str, *, source_sha256: str = "", iban: str = "",
                        amount_cents: int = 0, reference: str = "") -> dict | None:
        """A live payment for the same invoice file, or the same IBAN + amount +
        reference (prepare is idempotent)."""
        db = await self._ensure_db()
        live = ", ".join("?" for _ in LIVE_STATES)
        if source_sha256:
            async with db.execute(
                f"SELECT * FROM payment_intents WHERE owner_id = ? AND source_sha256 = ?"
                f" AND status IN ({live}) ORDER BY created_at DESC LIMIT 1",
                (owner_id, source_sha256, *LIVE_STATES),
            ) as cur:
                row = await cur.fetchone()
                if row is not None:
                    return _row(row)
        if iban and amount_cents and reference:
            async with db.execute(
                f"SELECT * FROM payment_intents WHERE owner_id = ? AND iban = ? AND amount_cents = ?"
                f" AND reference = ? AND status IN ({live}) ORDER BY created_at DESC LIMIT 1",
                (owner_id, iban, int(amount_cents), reference, *LIVE_STATES),
            ) as cur:
                return _row(await cur.fetchone())
        return None

    async def count_since(self, owner_id: str, since: float) -> int:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT COUNT(*) FROM payment_intents WHERE owner_id = ? AND created_at >= ?",
            (owner_id, since),
        ) as cur:
            return int((await cur.fetchone())[0])

    async def expire_stale(self, now: float | None = None) -> int:
        """Live payments nobody touched for 30 days past their due date (or
        their creation, without one) become 'expired'."""
        db = await self._ensure_db()
        now = time.time() if now is None else now
        cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(now - EXPIRE_AFTER_SECONDS))
        live = ", ".join("?" for _ in LIVE_STATES)
        cur = await db.execute(
            f"UPDATE payment_intents SET status = 'expired', updated_at = ?"
            f" WHERE status IN ({live}) AND ("
            f"   (due_date != '' AND due_date < ?) OR (due_date = '' AND created_at < ?))",
            (now, *LIVE_STATES, cutoff_day, now - EXPIRE_AFTER_SECONDS),
        )
        await db.commit()
        return cur.rowcount or 0

    async def delete_owner(self, owner_id: str) -> None:
        db = await self._ensure_db()
        await db.execute("DELETE FROM payment_intents WHERE owner_id = ?", (owner_id,))
        await db.execute("DELETE FROM payment_events WHERE owner_id = ?", (owner_id,))
        await db.execute("DELETE FROM payment_settings WHERE owner_id = ?", (owner_id,))
        await db.commit()

    # ── audit trail ──────────────────────────────────────────────────

    async def add_event(self, intent_id: str, owner_id: str, actor: str, event: str,
                        detail: dict | None = None) -> None:
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO payment_events (intent_id, owner_id, ts, actor, event, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (intent_id, owner_id, time.time(), actor, event, json.dumps(detail or {}, ensure_ascii=False)),
        )
        await db.commit()

    async def events(self, intent_id: str) -> list[dict]:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT ts, actor, event, detail FROM payment_events WHERE intent_id = ? ORDER BY id",
            (intent_id,),
        ) as cur:
            return [{**dict(r), "detail": _loads(r["detail"], {})} for r in await cur.fetchall()]

    # ── settings ─────────────────────────────────────────────────────

    async def get_cap(self, owner_id: str) -> int | None:
        db = await self._ensure_db()
        async with db.execute("SELECT cap_cents FROM payment_settings WHERE owner_id = ?", (owner_id,)) as cur:
            row = await cur.fetchone()
            return None if row is None else int(row[0])

    async def set_cap(self, owner_id: str, cap_cents: int) -> None:
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO payment_settings (owner_id, cap_cents, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(owner_id) DO UPDATE SET cap_cents = excluded.cap_cents,"
            " updated_at = excluded.updated_at",
            (owner_id, int(cap_cents), time.time()),
        )
        await db.commit()
