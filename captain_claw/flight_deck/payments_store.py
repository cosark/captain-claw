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
TERMINAL_STATES = PAID_STATES + ("skipped", "cancelled", "expired", "failed")
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
    paid_via        TEXT NOT NULL DEFAULT '',
    handoff_rail    TEXT NOT NULL DEFAULT '',
    rails_state     TEXT NOT NULL DEFAULT '{}',
    rail_nonce      TEXT
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
-- A bank connection (Phase 1: Revolut Business). Tokens are scoped READ,WRITE:
-- they can create drafts, never pay.
CREATE TABLE IF NOT EXISTS payment_connections (
    owner_id          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    environment       TEXT NOT NULL DEFAULT 'production',
    client_id         TEXT NOT NULL DEFAULT '',
    redirect_uri      TEXT NOT NULL DEFAULT '',
    private_key       TEXT NOT NULL DEFAULT '',
    certificate       TEXT NOT NULL DEFAULT '',
    refresh_token     TEXT NOT NULL DEFAULT '',
    access_token      TEXT NOT NULL DEFAULT '',
    access_expires_at REAL NOT NULL DEFAULT 0,
    account_id        TEXT NOT NULL DEFAULT '',
    account_label     TEXT NOT NULL DEFAULT '',
    connected_at      REAL,
    updated_at        REAL NOT NULL,
    PRIMARY KEY (owner_id, provider)
);
-- Purchases (Phase 3): Captain buys from an online shop with a one-time
-- credential the user approved (Link Agent Wallet, a Revolut card made for the
-- purchase) or through a checkout protocol (ACP, AP2).
CREATE TABLE IF NOT EXISTS purchases (
    id                  TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    agent               TEXT NOT NULL DEFAULT '',
    session_id          TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL,
    source              TEXT NOT NULL,
    merchant_name       TEXT NOT NULL DEFAULT '',
    merchant_url        TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    items               TEXT NOT NULL DEFAULT '[]',
    amount_cents        INTEGER NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'EUR',
    approval_url        TEXT NOT NULL DEFAULT '',
    provider_state      TEXT NOT NULL DEFAULT '{}',
    card_last4          TEXT NOT NULL DEFAULT '',
    credential_released INTEGER NOT NULL DEFAULT 0,
    result              TEXT NOT NULL DEFAULT '{}',
    order_ref           TEXT NOT NULL DEFAULT '',
    warnings            TEXT NOT NULL DEFAULT '[]',
    automated           INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    completed_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_purchases_owner ON purchases(owner_id, created_at);
-- Small per-owner preferences (Phase 2: the bank a pay-by-bank link opens).
CREATE TABLE IF NOT EXISTS payment_prefs (
    owner_id   TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner_id, key)
);
-- Statements (Phase 4): a read-only bank connection — an Enable Banking AIS
-- session or the owner's Revolut Business statement — and the outgoing
-- payments read from it. Incoming payments and balances are never stored.
CREATE TABLE IF NOT EXISTS bank_sessions (
    owner_id        TEXT NOT NULL,
    provider        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    aspsp_name      TEXT NOT NULL DEFAULT '',
    aspsp_country   TEXT NOT NULL DEFAULT '',
    psu_type        TEXT NOT NULL DEFAULT 'personal',
    required_psu_headers TEXT NOT NULL DEFAULT '[]',
    valid_until     REAL,
    status          TEXT NOT NULL,
    last_sync_at    REAL,
    last_attempt_at REAL,
    unattended_at   TEXT NOT NULL DEFAULT '[]',
    last_error      TEXT NOT NULL DEFAULT '',
    notified        TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (owner_id, provider, session_id)
);
CREATE TABLE IF NOT EXISTS bank_accounts (
    owner_id        TEXT NOT NULL,
    provider        TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    uid             TEXT NOT NULL,
    account_key     TEXT NOT NULL,
    label           TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'EUR',
    watched         INTEGER NOT NULL DEFAULT 1,
    synced_through  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner_id, uid)
);
CREATE TABLE IF NOT EXISTS bank_transactions (
    owner_id         TEXT NOT NULL,
    account_key      TEXT NOT NULL,
    fp               TEXT NOT NULL,
    status           TEXT NOT NULL,
    booking_date     TEXT NOT NULL DEFAULT '',
    value_date       TEXT NOT NULL DEFAULT '',
    amount_cents     INTEGER NOT NULL,
    currency         TEXT NOT NULL,
    creditor_name    TEXT NOT NULL DEFAULT '',
    creditor_iban    TEXT NOT NULL DEFAULT '',
    reference_digits TEXT NOT NULL DEFAULT '',
    remittance       TEXT NOT NULL DEFAULT '',
    intent_id        TEXT NOT NULL DEFAULT '',
    match            TEXT NOT NULL DEFAULT '',
    seen_at          REAL NOT NULL,
    updated_at       REAL NOT NULL,
    PRIMARY KEY (owner_id, fp)
);
CREATE INDEX IF NOT EXISTS idx_bank_transactions_date ON bank_transactions(owner_id, booking_date);
-- Phase 4b: which bank transaction belongs to which bill, and how sure we are.
-- state: auto | confirmed (it settled the bill) | pending | suggested | rejected | duplicate.
CREATE TABLE IF NOT EXISTS statement_links (
    owner_id   TEXT NOT NULL,
    fp         TEXT NOT NULL,
    intent_id  TEXT NOT NULL,
    state      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner_id, fp, intent_id)
);
"""

# Columns added after a table first shipped: (table, column, DDL).
_MIGRATIONS = (
    ("payment_intents", "handoff_rail", "TEXT NOT NULL DEFAULT ''"),
    ("payment_intents", "rails_state", "TEXT NOT NULL DEFAULT '{}'"),
    ("payment_intents", "rail_nonce", "TEXT"),
    ("bank_sessions", "last_attempt_at", "REAL"),
    ("payment_intents", "statement", "TEXT NOT NULL DEFAULT '{}'"),
    ("payment_intents", "notify_origin", "TEXT NOT NULL DEFAULT '{}'"),
    ("payment_intents", "reminders", "TEXT NOT NULL DEFAULT '{}'"),
)
# Indexes on migrated columns, created after the migrations ran.
_POST_MIGRATION_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_intents_rail_nonce"
    " ON payment_intents(rail_nonce) WHERE rail_nonce IS NOT NULL"
)

_JSON_COLS = {"fields": {}, "provenance": {}, "warnings": [], "acks": {}, "barcodes": [], "rails_state": {},
              "statement": {}, "notify_origin": {}, "reminders": {}}


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


def _dump(values: dict, json_cols: dict = _JSON_COLS) -> dict:
    return {k: (json.dumps(v, ensure_ascii=False) if k in json_cols else v) for k, v in values.items()}


_PURCHASE_JSON = {"items": [], "provider_state": {}, "result": {}, "warnings": []}
PURCHASE_LIVE = ("awaiting_approval", "approved", "paying", "needs_action")
PURCHASE_DONE = ("ordered", "completed")
PURCHASE_TERMINAL = PURCHASE_DONE + ("failed", "denied", "expired", "cancelled")


def _purchase_row(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col, default in _PURCHASE_JSON.items():
        out[col] = _loads(out.get(col), type(default)())
    out["automated"] = bool(out.get("automated"))
    out["credential_released"] = bool(out.get("credential_released"))
    return out


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
        for table, column, ddl in _MIGRATIONS:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except aiosqlite.OperationalError:
                pass   # already there
        await db.execute(_POST_MIGRATION_DDL)
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

    async def owners_with_due_bills(self) -> list[str]:
        """Owners with a live bill that has a due date (Phase 4c reminders)."""
        db = await self._ensure_db()
        live = ", ".join("?" for _ in LIVE_STATES)
        async with db.execute(
            f"SELECT DISTINCT owner_id FROM payment_intents WHERE status IN ({live}) AND due_date != ''", LIVE_STATES,
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def latest_origin(self, owner_id: str) -> dict:
        """The chat the owner most recently prepared a bill from ({} if none)."""
        db = await self._ensure_db()
        async with db.execute(
            "SELECT notify_origin FROM payment_intents WHERE owner_id = ? AND notify_origin NOT IN ('', '{}')"
            " ORDER BY created_at DESC LIMIT 1", (owner_id,),
        ) as cur:
            row = await cur.fetchone()
        return _loads(row[0], {}) if row else {}

    async def get_by_nonce(self, nonce: str) -> dict | None:
        """The payment a bank redirect's one-time state belongs to."""
        db = await self._ensure_db()
        async with db.execute("SELECT * FROM payment_intents WHERE rail_nonce = ?", (nonce,)) as cur:
            return _row(await cur.fetchone())

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
        await db.execute("DELETE FROM payment_connections WHERE owner_id = ?", (owner_id,))
        await db.execute("DELETE FROM payment_prefs WHERE owner_id = ?", (owner_id,))
        await db.execute("DELETE FROM purchases WHERE owner_id = ?", (owner_id,))
        for table in ("bank_sessions", "bank_accounts", "bank_transactions", "statement_links"):
            await db.execute(f"DELETE FROM {table} WHERE owner_id = ?", (owner_id,))
        await db.commit()

    # ── purchases (Phase 3) ──────────────────────────────────────────

    async def create_purchase(self, row: dict) -> dict:
        db = await self._ensure_db()
        now = time.time()
        values = _dump({**row, "created_at": now, "updated_at": now}, _PURCHASE_JSON)
        await db.execute(f"INSERT INTO purchases ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                         tuple(values.values()))
        await db.commit()
        return await self.get_purchase(row["id"])

    async def update_purchase(self, purchase_id: str, **changes: Any) -> dict | None:
        db = await self._ensure_db()
        values = _dump({**changes, "updated_at": time.time()}, _PURCHASE_JSON)
        await db.execute(f"UPDATE purchases SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                         (*values.values(), purchase_id))
        await db.commit()
        return await self.get_purchase(purchase_id)

    async def get_purchase(self, purchase_id: str) -> dict | None:
        db = await self._ensure_db()
        async with db.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)) as cur:
            return _purchase_row(await cur.fetchone())

    async def list_purchases(self, owner_id: str, *, limit: int = 100) -> list[dict]:
        db = await self._ensure_db()
        async with db.execute("SELECT * FROM purchases WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
                              (owner_id, int(limit))) as cur:
            return [_purchase_row(r) for r in await cur.fetchall()]

    async def release_credential(self, purchase_id: str) -> bool:
        """Mark the purchase's one-time credential released; False if it already was
        (a credential leaves Flight Deck once, for one checkout)."""
        db = await self._ensure_db()
        cur = await db.execute(
            "UPDATE purchases SET credential_released = 1, status = 'paying', updated_at = ?"
            " WHERE id = ? AND credential_released = 0 AND status = 'approved'", (time.time(), purchase_id))
        await db.commit()
        return (cur.rowcount or 0) == 1

    # ── preferences ──────────────────────────────────────────────────

    async def get_pref(self, owner_id: str, key: str) -> Any:
        db = await self._ensure_db()
        async with db.execute("SELECT value FROM payment_prefs WHERE owner_id = ? AND key = ?",
                              (owner_id, key)) as cur:
            row = await cur.fetchone()
            return _loads(row[0], None) if row else None

    async def set_pref(self, owner_id: str, key: str, value: Any) -> None:
        db = await self._ensure_db()
        await db.execute(
            "INSERT INTO payment_prefs (owner_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(owner_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (owner_id, key, json.dumps(value, ensure_ascii=False), time.time()))
        await db.commit()

    async def delete_pref(self, owner_id: str, key: str) -> None:
        db = await self._ensure_db()
        await db.execute("DELETE FROM payment_prefs WHERE owner_id = ? AND key = ?", (owner_id, key))
        await db.commit()

    # ── bank connections ─────────────────────────────────────────────

    async def get_connection(self, owner_id: str, provider: str) -> dict | None:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT * FROM payment_connections WHERE owner_id = ? AND provider = ?", (owner_id, provider)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def save_connection(self, owner_id: str, provider: str, **values: Any) -> dict:
        """Upsert *values* into the owner's connection for *provider*."""
        db = await self._ensure_db()
        values = {**values, "updated_at": time.time()}
        cols = ["owner_id", "provider", *values]
        marks = ", ".join("?" for _ in cols)
        sets = ", ".join(f"{k} = excluded.{k}" for k in values)
        await db.execute(
            f"INSERT INTO payment_connections ({', '.join(cols)}) VALUES ({marks})"
            f" ON CONFLICT(owner_id, provider) DO UPDATE SET {sets}",
            (owner_id, provider, *values.values()),
        )
        await db.commit()
        return await self.get_connection(owner_id, provider)

    async def delete_connection(self, owner_id: str, provider: str) -> None:
        db = await self._ensure_db()
        await db.execute("DELETE FROM payment_connections WHERE owner_id = ? AND provider = ?",
                         (owner_id, provider))
        await db.commit()

    # ── statements (Phase 4) ─────────────────────────────────────────

    async def save_bank_session(self, owner_id: str, provider: str, session_id: str, **values: Any) -> dict:
        db = await self._ensure_db()
        now = time.time()
        values = {k: (json.dumps(v) if k in ("required_psu_headers", "unattended_at") else v)
                  for k, v in {**values, "updated_at": now}.items()}
        key = (owner_id, provider, session_id)
        if await self.get_bank_session(*key):
            # A plain UPDATE: SQLite checks NOT NULL on an upsert's insert row first.
            sets = ", ".join(f"{k} = ?" for k in values)
            await db.execute(f"UPDATE bank_sessions SET {sets} WHERE owner_id = ? AND provider = ? AND session_id = ?",
                             (*values.values(), *key))
        else:
            values.setdefault("status", "active")
            cols = ["owner_id", "provider", "session_id", "created_at", *values]
            await db.execute(f"INSERT INTO bank_sessions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                             (*key, now, *values.values()))
        await db.commit()
        return await self.get_bank_session(owner_id, provider, session_id)

    @staticmethod
    def _session_row(row: Any) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        out["required_psu_headers"] = _loads(out.get("required_psu_headers"), [])
        out["unattended_at"] = _loads(out.get("unattended_at"), [])
        return out

    async def get_bank_session(self, owner_id: str, provider: str, session_id: str) -> dict | None:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT * FROM bank_sessions WHERE owner_id = ? AND provider = ? AND session_id = ?",
            (owner_id, provider, session_id),
        ) as cur:
            return self._session_row(await cur.fetchone())

    async def list_bank_sessions(self, owner_id: str | None = None) -> list[dict]:
        """One owner's sessions, or every owner's (the background loop)."""
        db = await self._ensure_db()
        q, args = "SELECT * FROM bank_sessions", ()
        if owner_id is not None:
            q, args = q + " WHERE owner_id = ?", (owner_id,)
        async with db.execute(q + " ORDER BY created_at", args) as cur:
            return [self._session_row(r) for r in await cur.fetchall()]

    async def delete_bank_session(self, owner_id: str, provider: str, session_id: str, *,
                                  keep_transactions: bool = False) -> None:
        """The session, its accounts and (unless a renewal carries them on) every
        transaction read from them."""
        db = await self._ensure_db()
        if not keep_transactions:
            await db.execute(
                "DELETE FROM bank_transactions WHERE owner_id = ? AND account_key IN"
                " (SELECT account_key FROM bank_accounts WHERE owner_id = ? AND provider = ? AND session_id = ?)",
                (owner_id, owner_id, provider, session_id))
        await db.execute("DELETE FROM bank_accounts WHERE owner_id = ? AND provider = ? AND session_id = ?",
                         (owner_id, provider, session_id))
        await db.execute("DELETE FROM bank_sessions WHERE owner_id = ? AND provider = ? AND session_id = ?",
                         (owner_id, provider, session_id))
        await db.commit()

    async def save_bank_accounts(self, owner_id: str, provider: str, session_id: str, accounts: list[dict]) -> None:
        """Upsert the session's accounts; a re-read keeps each account's watch choice and sync point."""
        db = await self._ensure_db()
        for a in accounts:
            await db.execute(
                "INSERT INTO bank_accounts (owner_id, provider, session_id, uid, account_key, label, currency,"
                " watched, synced_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(owner_id, uid) DO UPDATE SET"
                " provider = excluded.provider, session_id = excluded.session_id, label = excluded.label,"
                " currency = excluded.currency, account_key = excluded.account_key",
                (owner_id, provider, session_id, a["uid"], a.get("account_key") or a["uid"], a["label"],
                 a.get("currency") or "EUR", 1 if a.get("watched", True) else 0, a.get("synced_through", "")))
        await db.commit()

    async def bank_accounts(self, owner_id: str, provider: str = "", session_id: str = "") -> list[dict]:
        db = await self._ensure_db()
        q, args = "SELECT * FROM bank_accounts WHERE owner_id = ?", [owner_id]
        if provider:
            q, args = q + " AND provider = ? AND session_id = ?", [*args, provider, session_id]
        async with db.execute(q + " ORDER BY label", args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_bank_account(self, owner_id: str, uid: str, **values: Any) -> None:
        db = await self._ensure_db()
        sets = ", ".join(f"{k} = ?" for k in values)
        await db.execute(f"UPDATE bank_accounts SET {sets} WHERE owner_id = ? AND uid = ?",
                         (*values.values(), owner_id, uid))
        await db.commit()

    async def upsert_bank_transactions(self, owner_id: str, rows: list[dict]) -> tuple[int, int]:
        """(new, changed). A re-read updates the bank status and dates; the
        match (Phase 4b) is kept."""
        db = await self._ensure_db()
        now = time.time()
        new = changed = 0
        for r in rows:
            async with db.execute("SELECT status, booking_date FROM bank_transactions WHERE owner_id = ? AND fp = ?",
                                  (owner_id, r["fp"])) as cur:
                old = await cur.fetchone()
            if old is None:
                await db.execute(
                    "INSERT INTO bank_transactions (owner_id, account_key, fp, status, booking_date, value_date,"
                    " amount_cents, currency, creditor_name, creditor_iban, reference_digits, remittance,"
                    " seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (owner_id, r["account_key"], r["fp"], r["status"], r.get("booking_date", ""),
                     r.get("value_date", ""), r["amount_cents"], r["currency"], r.get("creditor_name", ""),
                     r.get("creditor_iban", ""), r.get("reference_digits", ""), r.get("remittance", ""), now, now))
                new += 1
            elif (old["status"], old["booking_date"]) != (r["status"], r.get("booking_date", "")):
                await db.execute("UPDATE bank_transactions SET status = ?, booking_date = ?, value_date = ?,"
                                 " updated_at = ? WHERE owner_id = ? AND fp = ?",
                                 (r["status"], r.get("booking_date", ""), r.get("value_date", ""), now,
                                  owner_id, r["fp"]))
                changed += 1
        await db.commit()
        return new, changed

    async def bank_transactions(self, owner_id: str, *, since: str = "", limit: int = 2000) -> list[dict]:
        db = await self._ensure_db()
        async with db.execute(
            "SELECT * FROM bank_transactions WHERE owner_id = ? AND (booking_date >= ? OR booking_date = '')"
            " ORDER BY booking_date DESC LIMIT ?", (owner_id, since, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def count_bank_transactions(self, owner_id: str, account_key: str) -> int:
        db = await self._ensure_db()
        async with db.execute("SELECT COUNT(*) FROM bank_transactions WHERE owner_id = ? AND account_key = ?",
                              (owner_id, account_key)) as cur:
            return int((await cur.fetchone())[0])

    async def statement_links(self, owner_id: str) -> list[dict]:
        db = await self._ensure_db()
        async with db.execute("SELECT * FROM statement_links WHERE owner_id = ?", (owner_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def set_statement_link(self, owner_id: str, fp: str, intent_id: str, state: str, reason: str = "") -> None:
        db = await self._ensure_db()
        now = time.time()
        await db.execute(
            "INSERT INTO statement_links (owner_id, fp, intent_id, state, reason, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(owner_id, fp, intent_id) DO UPDATE SET"
            " state = excluded.state, reason = CASE WHEN excluded.reason = '' THEN reason ELSE excluded.reason END,"
            " updated_at = excluded.updated_at",
            (owner_id, fp, intent_id, state, reason, now, now))
        await db.commit()

    async def assign_transaction(self, owner_id: str, fp: str, intent_id: str, match: str) -> None:
        """Record the bill a transaction paid (kept past the retention purge)."""
        db = await self._ensure_db()
        await db.execute("UPDATE bank_transactions SET intent_id = ?, match = ?, updated_at = ?"
                         " WHERE owner_id = ? AND fp = ?", (intent_id, match, time.time(), owner_id, fp))
        await db.commit()

    async def purge_bank_transactions(self, before: str) -> int:
        """Unmatched outgoing payments booked before *before* (YYYY-MM-DD) go."""
        db = await self._ensure_db()
        cur = await db.execute("DELETE FROM bank_transactions WHERE intent_id = '' AND booking_date != ''"
                               " AND booking_date < ?", (before,))
        await db.commit()
        return cur.rowcount or 0

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
