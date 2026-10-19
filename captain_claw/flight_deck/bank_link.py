"""Pay-by-bank links through Enable Banking (PSD2 payment initiation) —
Payments Phase 2 (docs/payments-phase2-plan.md).

Captain creates the payment at Enable Banking and hands the user one link; the
link opens the user's own bank, where they approve the payment with the bank's
strong customer authentication. Captain never holds a bank login or a
long-lived token — each payment is its own one-time authorisation.

Deck setup (admin): an Enable Banking application (sandbox or production) whose
allowed redirect URLs include ``<deck>/fd/payments/bank/callback``;
``FD_ENABLEBANKING_APP_ID`` = its id, ``FD_ENABLEBANKING_KEY`` = path to its
private key PEM. Auth per enablebanking.com/docs/api/reference: an RS256 JWT
with header ``kid`` = application id, ``iss`` = enablebanking.com, ``aud`` =
api.enablebanking.com, lifetime at most 24 h.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

from captain_claw.logging import get_logger

log = get_logger(__name__)

API = "https://api.enablebanking.com"
# ISO 20022 statuses Enable Banking reports for a payment.
ACCEPTED = frozenset({"ACTC", "ACCP", "ACSP", "ACWC", "ACWP", "ACPT", "ACCR", "ACSC", "ACCC"})
SETTLED = frozenset({"ACSC", "ACCC"})          # settlement completed: paid, whatever final_status says
REJECTED = frozenset({"RJCT"})
CANCELLED = frozenset({"CNCL", "RJCR", "PACR", "PDCR", "NULL"})
LINK_LIFETIME = 3600                            # treat a bank link as fresh for an hour
_PAYMENT_TYPES = ("SEPA", "DOMESTIC", "INST_SEPA")

# Tests swap in an httpx.MockTransport.
transport: httpx.AsyncBaseTransport | None = None
_KEY_CACHE: dict[str, tuple[float, str]] = {}
_ASPSP_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_APP_CACHE: tuple[float, dict] = (0.0, {})
_ASPSP_TTL = 6 * 3600


class BankLinkError(Exception):
    def __init__(self, status: int, message: str, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def app_id() -> str:
    return os.environ.get("FD_ENABLEBANKING_APP_ID", "").strip()


def _key() -> str:
    path = os.environ.get("FD_ENABLEBANKING_KEY", "").strip()
    if not path:
        return ""
    p = Path(path).expanduser()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return ""
    cached = _KEY_CACHE.get(str(p))
    if cached and cached[0] == mtime:
        return cached[1]
    pem = p.read_text(encoding="utf-8")
    _KEY_CACHE[str(p)] = (mtime, pem)
    return pem


def configured() -> bool:
    return bool(app_id() and _key())


def auth_token(lifetime: int = 3600) -> str:
    import jwt

    now = int(time.time())
    claims = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now,
              "exp": now + min(int(lifetime), 86400)}
    return jwt.encode(claims, _key(), algorithm="RS256", headers={"kid": app_id(), "typ": "JWT"})


async def _request(method: str, path: str, *, json: Any = None, params: dict | None = None,
                   headers: dict | None = None) -> Any:
    if not configured():
        raise BankLinkError(503, "Bank links aren't set up on this deck (FD_ENABLEBANKING_APP_ID / _KEY)")
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            resp = await client.request(method, f"{API}{path}", json=json, params=params,
                                        headers={**(headers or {}), "Authorization": f"Bearer {auth_token()}"})
    except httpx.HTTPError as exc:
        raise BankLinkError(502, f"Couldn't reach Enable Banking ({type(exc).__name__})")
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        message = str((body or {}).get("message") or (body or {}).get("detail") or resp.text[:200])
        raise BankLinkError(resp.status_code, message[:300], str((body or {}).get("error") or ""))
    try:
        return resp.json() if resp.content else None
    except ValueError:
        raise BankLinkError(502, "Enable Banking's answer isn't JSON")


async def application() -> dict:
    """The application's record (environment, redirect URLs); cached 10 minutes."""
    global _APP_CACHE
    at, data = _APP_CACHE
    if data and time.time() - at < 600:
        return data
    data = await _request("GET", "/application") or {}
    _APP_CACHE = (time.time(), data)
    return data


def payment_type_for(aspsp: dict) -> dict | None:
    """The bank's payment type to use for a EUR transfer (SEPA first)."""
    entries = [p for p in aspsp.get("payments") or []
               if not p.get("currencies") or "EUR" in (p.get("currencies") or [])]
    for wanted in _PAYMENT_TYPES:
        for entry in entries:
            if entry.get("payment_type") == wanted:
                return entry
    return None


async def aspsps(country: str, psu_type: str) -> list[dict]:
    """Banks in *country* that can initiate payments for *psu_type*; cached 6 h."""
    key = (country.upper(), psu_type)
    cached = _ASPSP_CACHE.get(key)
    if cached and time.time() - cached[0] < _ASPSP_TTL:
        return cached[1]
    data = await _request("GET", "/aspsps", params={"country": key[0], "service": "PIS", "psu_type": psu_type})
    banks = [a for a in (data or {}).get("aspsps") or [] if payment_type_for(a)]
    _ASPSP_CACHE[key] = (time.time(), banks)
    return banks


def remittance(fields: dict, max_length: int = 140) -> tuple[list[str], str]:
    """(remittance lines, reference_number). A Croatian model reference has no
    reference-number scheme here, so it leads the text; an RF reference goes
    in reference_number."""
    from captain_claw.payments import hr_ref

    model, ref, desc = fields.get("model", ""), fields.get("reference", ""), fields.get("description", "")
    reference_number = ""
    parts: list[str] = []
    if ref and hr_ref.is_rf(ref):
        reference_number = hr_ref.normalize_reference(ref)
    elif ref and model and model != hr_ref.NO_REFERENCE_MODEL:
        parts.append(f"{model} {ref}")
    elif ref:
        parts.append(ref)
    if desc:
        parts.append(desc)
    limit = max(1, int(max_length or 140))
    # The reference first and whole; the description only if it fits whole.
    line = parts[0][:limit].rstrip() if parts else ""
    for part in parts[1:]:
        if len(line) + 1 + len(part) <= limit:
            line = f"{line} {part}" if line else part
    return ([line] if line else []), reference_number


async def create_payment(*, aspsp: dict, psu_type: str, fields: dict, state: str, redirect_url: str) -> dict:
    """{payment_id, status, url} — *url* is the link the user opens."""
    entry = payment_type_for(aspsp) or {}
    lines_info = entry.get("remittance_information_lines") or [{}]
    lines, reference_number = remittance(fields, (lines_info[0] or {}).get("max_length") or 140)
    if entry.get("remittance_information_required") and not lines:
        lines = [str(fields.get("payee_name") or "Payment")[:140]]
    transaction: dict[str, Any] = {
        "instructed_amount": {"amount": f"{int(fields['amount_cents']) / 100:.2f}", "currency": "EUR"},
        "beneficiary": {"creditor": {"name": str(fields.get("payee_name") or "")[:70]},
                        "creditor_account": {"scheme_name": "IBAN", "identification": fields["iban"]}},
    }
    if lines:
        transaction["remittance_information"] = lines
    if reference_number:
        transaction["reference_number"] = reference_number
    body = {
        "payment_type": entry.get("payment_type") or "SEPA",
        "payment_request": {"credit_transfer_transaction": [transaction]},
        "aspsp": {"name": aspsp["name"], "country": aspsp["country"]},
        "psu_type": psu_type, "state": state, "redirect_url": redirect_url,
        "language": "hr" if aspsp.get("country") == "HR" else "en",
    }
    data = await _request("POST", "/payments", json=body) or {}
    if not data.get("url") or not data.get("payment_id"):
        raise BankLinkError(502, "Enable Banking didn't return a link for this payment")
    return data


async def get_payment(payment_id: str) -> dict:
    return await _request("GET", f"/payments/{payment_id}") or {}


def ledger_status(status: str, final: bool) -> str:
    """The ledger status a bank status moves a handed-off payment to ('' = stays)."""
    status = (status or "").upper()
    if status in SETTLED or (final and status in ACCEPTED):
        return "settled"
    if status in REJECTED:
        return "failed"
    if status in CANCELLED and final:
        return "cancelled"
    return ""


def describe(status: str) -> str:
    status = (status or "").upper()
    if status in SETTLED:
        return "Paid — your bank completed it"
    if status in ACCEPTED:
        return "Approved — your bank is processing it"
    if status in REJECTED:
        return "Rejected by your bank"
    if status in CANCELLED:
        return "Cancelled at your bank"
    if status in ("RCVD", "PDNG"):
        return "Waiting for you to approve it in your bank"
    return f"Bank status {status}" if status else "Waiting for you to approve it in your bank"
