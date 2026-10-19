"""Read-only bank statements — Payments Phase 4a (docs/payments-phase4-plan.md).

Two sources of the owner's *outgoing* payments, so a bill can be confirmed
from the bank rather than from the user's word:

- **Enable Banking AIS** (the same application as Phase 2's pay-by-bank links):
  the user authorises read access at their bank (POST /auth → bank SCA →
  POST /sessions), then Flight Deck reads transactions per account.
- **Revolut Business** (the Phase 1 connection; READ is already in its scope):
  GET /transactions, transfers only.

Everything is reduced to outgoing rows here, while parsing: incoming payments
are dropped and never stored; balances are never asked for. PSD2 allows at
most four reads a day per account without the user present, so callers keep
count (``UNATTENDED_PER_DAY``) and send the user's PSU headers when they are
present.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from captain_claw.flight_deck import bank_link
from captain_claw.flight_deck.bank_link import BankLinkError
from captain_claw.logging import get_logger
from captain_claw.payments import iban as iban_mod
from captain_claw.payments import matching

log = get_logger(__name__)

ENABLEBANKING = "enablebanking"
REVOLUT = "revolut"
CONSENT_MAX_SECONDS = 180 * 86400        # amended RTS: AIS re-authentication every 180 days
UNATTENDED_PER_DAY = 4                   # PSD2 RTS Art. 36(5)(b)
FIRST_SYNC_DAYS = 395                    # 13 months — enough to spot yearly bills (decision 2)
OVERLAP_DAYS = 5                         # banks book late; re-read the last days
RETENTION_DAYS = 400
MAX_PAGES = 30
ENDED = frozenset({"EXPIRED_SESSION", "REVOKED_SESSION", "CLOSED_SESSION"})
RATE_LIMITED = frozenset({"ASPSP_RATE_LIMIT_EXCEEDED"})
_ASPSP_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_ASPSP_TTL = 6 * 3600


def enabled() -> bool:
    return os.environ.get("FD_PAYMENTS_STATEMENTS", "").strip().lower() in ("1", "true", "yes", "on")


def session_ended(exc: BankLinkError) -> bool:
    return exc.code in ENDED or any(code.lower().replace("_", " ") in exc.message.lower() for code in ENDED)


def rate_limited(exc: BankLinkError) -> bool:
    return exc.status == 429 or exc.code in RATE_LIMITED


# ── Enable Banking AIS ───────────────────────────────────────────────

async def aspsps(country: str, psu_type: str) -> list[dict]:
    """Banks in *country* that offer account information for *psu_type*; cached 6 h."""
    key = (country.upper(), psu_type)
    cached = _ASPSP_CACHE.get(key)
    if cached and time.time() - cached[0] < _ASPSP_TTL:
        return cached[1]
    data = await bank_link._request("GET", "/aspsps", params={"country": key[0], "service": "AIS",
                                                              "psu_type": psu_type})
    banks = list((data or {}).get("aspsps") or [])
    _ASPSP_CACHE[key] = (time.time(), banks)
    return banks


def consent_seconds(aspsp: dict) -> int:
    """How long to ask for: the bank's maximum, never more than 180 days."""
    try:
        bank_max = int(aspsp.get("maximum_consent_validity") or 0)
    except (TypeError, ValueError):
        bank_max = 0
    return min(bank_max, CONSENT_MAX_SECONDS) if bank_max > 0 else 90 * 86400


async def start_auth(*, aspsp: dict, psu_type: str, state: str, redirect_url: str) -> dict:
    """{url, authorization_id, valid_until} — the user opens *url* and authorises at their bank."""
    valid_until = datetime.now(timezone.utc) + timedelta(seconds=consent_seconds(aspsp))
    body = {"access": {"valid_until": valid_until.isoformat(timespec="seconds")},
            "aspsp": {"name": aspsp["name"], "country": aspsp["country"]},
            "state": state, "redirect_url": redirect_url, "psu_type": psu_type,
            "language": "hr" if aspsp.get("country") == "HR" else "en"}
    data = await bank_link._request("POST", "/auth", json=body) or {}
    if not data.get("url"):
        raise BankLinkError(502, "Enable Banking didn't return a link to your bank")
    return {**data, "valid_until": valid_until.timestamp()}


async def create_session(code: str) -> dict:
    """{session_id, accounts, aspsp, psu_type, access} for the code the bank sent back."""
    data = await bank_link._request("POST", "/sessions", json={"code": code}) or {}
    if not data.get("session_id"):
        raise BankLinkError(502, "Enable Banking didn't open a session")
    return data


async def delete_session(session_id: str) -> None:
    try:
        await bank_link._request("DELETE", f"/sessions/{session_id}")
    except BankLinkError as exc:
        if exc.status not in (404, 410) and not session_ended(exc):
            raise


def account_label(account: dict, bank_name: str) -> str:
    """ "Erste ••1234" — the full IBAN isn't kept."""
    short = re.split(r"[\s&,]+", (bank_name or "").strip())[0] or "Bank"
    iban = iban_mod.normalize(str(((account.get("account_id") or {}).get("iban")) or ""))
    tail = iban[-4:] if len(iban) >= 4 else str(account.get("uid") or "")[-4:]
    return f"{short} ••{tail}"[:60]


def watch_by_default(account: dict) -> bool:
    """EUR current accounts are pre-selected; cards, savings and loans aren't."""
    kind = str(account.get("cash_account_type") or "").upper()
    return str(account.get("currency") or "EUR").upper() == "EUR" and kind in ("", "CACC", "TRAN", "OTHR")


async def transactions(uid: str, *, date_from: str, psu_headers: dict | None = None,
                       longest: bool = False) -> list[dict]:
    """Every transaction the bank returns from *date_from*, following continuation keys."""
    out: list[dict] = []
    params: dict[str, Any] = {"date_from": date_from}
    if longest:
        params["strategy"] = "longest"
    for _ in range(MAX_PAGES):
        data = await bank_link._request("GET", f"/accounts/{uid}/transactions", params=params,
                                        headers=psu_headers) or {}
        out.extend(data.get("transactions") or [])
        key = data.get("continuation_key")
        if not key:
            break
        params = {**params, "continuation_key": key}
    return out


def account_key(account: dict) -> str:
    """Stable across sessions (a renewal gets new uids): Enable Banking's identification hash."""
    return str(account.get("identification_hash") or account.get("uid") or "")


def outgoing(tx: dict, account_key: str) -> dict | None:
    """One Enable Banking transaction as a stored row — None unless it's money going out.
    *account_key* is the account's identification hash, so entry_reference (stable for
    the same hash) gives the same fingerprint after a renewal."""
    if str(tx.get("credit_debit_indicator") or "").upper() != "DBIT":
        return None
    money = tx.get("transaction_amount") or {}
    cents = matching.amount_cents(money.get("amount"))
    if cents is None:
        return None
    remittance = [str(x) for x in (tx.get("remittance_information") or []) if x]
    reference_number = str(tx.get("reference_number") or "")
    creditor_iban = iban_mod.normalize(str((tx.get("creditor_account") or {}).get("iban") or ""))
    entry = str(tx.get("entry_reference") or "")
    stable_day = tx.get("value_date") or tx.get("transaction_date") or tx.get("booking_date") or ""
    fp = (matching.fingerprint(ENABLEBANKING, account_key, entry) if entry else
          matching.fingerprint(ENABLEBANKING, account_key, stable_day, cents, money.get("currency"),
                               creditor_iban, reference_number, *remittance))
    return {
        "account_key": account_key, "fp": fp, "status": str(tx.get("status") or "BOOK").upper(),
        "booking_date": str(tx.get("booking_date") or "")[:10], "value_date": str(tx.get("value_date") or "")[:10],
        "amount_cents": cents, "currency": str(money.get("currency") or "EUR").upper(),
        "creditor_name": str((tx.get("creditor") or {}).get("name") or "")[:140],
        "creditor_iban": creditor_iban,
        "reference_digits": matching.reference_digits(reference_number, *remittance),
        "remittance": " ".join([reference_number, *remittance]).strip()[:140],
    }


# ── Revolut Business ─────────────────────────────────────────────────

_REVOLUT_STATE = {"completed": "BOOK", "pending": "PDNG", "created": "PDNG", "declined": "RJCT",
                  "failed": "RJCT", "reverted": "CNCL"}


async def revolut_transactions(client, account_id: str, *, date_from: str) -> list[dict]:
    """Transfers on *account_id* since *date_from*, newest first, paged by created_at."""
    out: list[dict] = []
    to = ""
    for _ in range(MAX_PAGES):
        params = {"from": date_from, "account": account_id, "type": "transfer", "count": 1000}
        if to:
            params["to"] = to
        page = await client.request("GET", "/transactions", params=params) or []
        out.extend(page)
        if len(page) < 1000:
            break
        to = page[-1].get("created_at") or ""
        if not to:
            break
    return out


async def revolut_outgoing(client, tx: dict, account_id: str, cache: dict[str, dict]) -> list[dict]:
    """The outgoing legs of one Revolut transfer as stored rows. The payee's IBAN
    comes from the counterparty record (looked up once per sync)."""
    rows = []
    for leg in tx.get("legs") or []:
        if leg.get("account_id") != account_id:
            continue
        cents = matching.amount_cents(leg.get("amount"))
        try:
            negative = float(leg.get("amount")) < 0
        except (TypeError, ValueError):
            negative = False
        if cents is None or not negative:
            continue
        cp = leg.get("counterparty") or {}
        creditor_iban = ""
        if cp.get("id"):
            if cp["id"] not in cache:
                try:
                    cache[cp["id"]] = await client.request("GET", f"/counterparty/{cp['id']}") or {}
                except Exception as exc:  # noqa: BLE001 — the IBAN is a bonus, not required
                    log.info("Revolut counterparty lookup failed", error=str(exc))
                    cache[cp["id"]] = {}
            for acc in cache[cp["id"]].get("accounts") or []:
                if acc.get("id") == cp.get("account_id") or not cp.get("account_id"):
                    creditor_iban = iban_mod.normalize(str(acc.get("iban") or ""))
                    if creditor_iban:
                        break
        day = str(tx.get("completed_at") or tx.get("created_at") or "")[:10]
        reference = str(tx.get("reference") or "")
        description = str(leg.get("description") or "")
        rows.append({
            "account_key": account_id, "fp": matching.fingerprint(REVOLUT, tx.get("id"), leg.get("leg_id")),
            "status": _REVOLUT_STATE.get(str(tx.get("state") or "").lower(), "OTHR"),
            "booking_date": day if str(tx.get("state")) == "completed" else "", "value_date": day,
            "amount_cents": cents, "currency": str(leg.get("currency") or "EUR").upper(),
            "creditor_name": re.sub(r"^To\s+", "", description)[:140], "creditor_iban": creditor_iban,
            "reference_digits": matching.reference_digits(reference),
            "remittance": reference[:140],
        })
    return rows


# ── shared ───────────────────────────────────────────────────────────

def sync_from(synced_through: str, *, today: date | None = None) -> tuple[str, bool]:
    """(date_from, first) — 13 months back the first time, else a few days before the newest booking."""
    today = today or date.today()
    if not synced_through:
        return (today - timedelta(days=FIRST_SYNC_DAYS)).isoformat(), True
    try:
        start = date.fromisoformat(synced_through) - timedelta(days=OVERLAP_DAYS)
    except ValueError:
        return (today - timedelta(days=FIRST_SYNC_DAYS)).isoformat(), True
    return min(start, today).isoformat(), False


def unattended_left(stamps: list[float], now: float | None = None) -> int:
    now = time.time() if now is None else now
    return UNATTENDED_PER_DAY - sum(1 for t in stamps if now - float(t) < 86400)
