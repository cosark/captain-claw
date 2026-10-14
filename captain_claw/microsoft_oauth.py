"""Microsoft identity platform (Entra ID / Microsoft account) OAuth2 for the
Microsoft Graph tools (microsoft_mail, microsoft_calendar, microsoft_onedrive).

The Authorization Code flow with PKCE against the v2.0 endpoints of
``login.microsoftonline.com``. Flight Deck runs the flow and keeps each user's
refresh token (``flight_deck.microsoft_oauth_routes``); agents only ever see a
short-lived access token (``microsoft_oauth_manager``).

How it differs from Google (``captain_claw.google_oauth``):

* **Tenant.** The authority is ``/{tenant}/``. ``common`` (the default) takes
  work/school AND personal Microsoft accounts and needs an app registered for
  "Accounts in any organizational directory and personal Microsoft accounts";
  ``organizations`` / ``consumers`` narrow it; a tenant id or domain is for a
  single-tenant app registration.
* **Refresh tokens rotate.** Every refresh returns a new refresh token, which
  must replace the stored one (the old keeps working for a while, not forever).
* **No revoke endpoint** for one app's grant. Disconnect forgets the tokens;
  the user removes the app's access in their Microsoft account itself
  (:data:`MANAGE_ACCESS_HINT`).
* **Scopes come back in several spellings** — ``Mail.Read``,
  ``https://graph.microsoft.com/Mail.Read``, lower-cased — so compare them with
  :func:`granted_scopes`, never as raw strings.
* **No verification gate** for these delegated scopes: users consent
  themselves, unless their organization only allows admin consent.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from captain_claw.google_oauth import generate_pkce_pair  # noqa: F401 — re-exported
from captain_claw.logging import get_logger

log = get_logger(__name__)

# ── Endpoints ────────────────────────────────────────────────────────

MS_LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_API = "https://graph.microsoft.com/v1.0"
GRAPH_RESOURCE = "https://graph.microsoft.com/"

DEFAULT_TENANT = "common"

# Where a user takes back this app's access (no API revokes one app's grant).
MANAGE_ACCESS_HINT = (
    "To also remove the app's access from your Microsoft account: personal "
    "accounts at https://account.live.com/consent/Manage, work or school "
    "accounts at https://myapps.microsoft.com."
)

# ── Scopes ───────────────────────────────────────────────────────────

# Always requested: sign-in identity, a refresh token (offline_access) and
# /me (User.Read — every tool resolves the signed-in mailbox through it).
REQUIRED_SCOPES = ["openid", "email", "profile", "offline_access", "User.Read"]

# A working default for mail + calendar + OneDrive, minus sending (Mail.Send
# is opt-in; the tools don't send yet). Mail.ReadWrite covers drafts and
# organizing (archive, move, folders, categories). Unlike Google, none of
# these needs app verification — users consent themselves unless their
# organization requires admin consent.
DEFAULT_SCOPES = [
    *REQUIRED_SCOPES,
    "Mail.ReadWrite",
    "Calendars.ReadWrite",
    "Files.ReadWrite.All",
]

# What the Connections UI offers. ``admin`` marks scopes an organization may
# reserve for admin consent (most tenants let users consent to all of these).
SCOPE_CATALOG: list[dict[str, str]] = [
    {"scope": "openid", "label": "OpenID", "description": "Sign in (required).",
     "sensitivity": "none", "group": "identity"},
    {"scope": "email", "label": "Email address", "description": "Your account's email (required).",
     "sensitivity": "none", "group": "identity"},
    {"scope": "profile", "label": "Basic profile", "description": "Your name (required).",
     "sensitivity": "none", "group": "identity"},
    {"scope": "offline_access", "label": "Stay connected",
     "description": "A refresh token, so agents keep access without you signing in again (required).",
     "sensitivity": "none", "group": "identity"},
    {"scope": "User.Read", "label": "Your profile",
     "description": "Read the signed-in user's profile (required).",
     "sensitivity": "none", "group": "identity"},
    {"scope": "Mail.Read", "label": "Mail (read)",
     "description": "List, search and read Outlook mail.",
     "sensitivity": "sensitive", "group": "mail"},
    {"scope": "Mail.ReadWrite", "label": "Mail (read + drafts)",
     "description": ("Read mail, create / edit drafts, and organize it (archive, move, folders, "
                     "categories). Drafts are never sent by agents."),
     "sensitivity": "sensitive", "group": "mail"},
    {"scope": "Mail.Send", "label": "Mail (send)",
     "description": "Send mail as you. Not used by the agent tools yet — leave off unless needed.",
     "sensitivity": "sensitive", "group": "mail"},
    {"scope": "Calendars.Read", "label": "Calendar (read)",
     "description": "Read your calendars and events.",
     "sensitivity": "sensitive", "group": "calendar"},
    {"scope": "Calendars.ReadWrite", "label": "Calendar (read + write)",
     "description": "Read, create, change and delete events; answer invitations.",
     "sensitivity": "sensitive", "group": "calendar"},
    {"scope": "Files.Read", "label": "OneDrive (read your files)",
     "description": "Read your own OneDrive files.",
     "sensitivity": "sensitive", "group": "files"},
    {"scope": "Files.ReadWrite", "label": "OneDrive (read + write your files)",
     "description": "Read and write your own OneDrive files.",
     "sensitivity": "sensitive", "group": "files"},
    {"scope": "Files.Read.All", "label": "OneDrive (read all you can open)",
     "description": "Read your files and every file shared with you.",
     "sensitivity": "sensitive", "group": "files"},
    {"scope": "Files.ReadWrite.All", "label": "OneDrive (read + write all you can open)",
     "description": "Read and write your files and files shared with you.",
     "sensitivity": "sensitive", "group": "files"},
]

_CATALOG_BY_KEY: dict[str, str] = {s["scope"].lower(): s["scope"] for s in SCOPE_CATALOG}


def scope_key(scope: str) -> str:
    """One scope as a comparable key: the Graph resource prefix dropped,
    lower-cased (``https://graph.microsoft.com/Mail.Read`` → ``mail.read``)."""
    s = str(scope or "").strip()
    if s.lower().startswith(GRAPH_RESOURCE):
        s = s[len(GRAPH_RESOURCE):]
    return s.lower()


def granted_scopes(scope_string: str) -> frozenset[str]:
    """A token response's ``scope`` as a set of :func:`scope_key` keys."""
    return frozenset(k for k in (scope_key(s) for s in str(scope_string or "").split()) if k)


def sanitize_scopes(scopes: list[str] | None) -> list[str]:
    """*scopes* filtered to the catalogue (canonical spelling, no duplicates),
    with :data:`REQUIRED_SCOPES` always in. Empty / None → :data:`DEFAULT_SCOPES`."""
    if not scopes:
        return list(DEFAULT_SCOPES)
    out: list[str] = []
    seen: set[str] = set()
    for s in scopes:
        if not isinstance(s, str):
            continue
        canonical = _CATALOG_BY_KEY.get(scope_key(s))
        if canonical is None or canonical in seen:
            continue  # unknown or repeated — dropped, never passed through
        seen.add(canonical)
        out.append(canonical)
    missing = [r for r in REQUIRED_SCOPES if r not in seen]
    return missing + out


def valid_tenant(tenant: str) -> bool:
    """A tenant segment safe to put in the authority URL: ``common`` /
    ``organizations`` / ``consumers``, a GUID, or a domain name."""
    t = str(tenant or "").strip()
    if not t or len(t) > 128:
        return False
    return all(c.isalnum() or c in "-." for c in t) and not t.startswith(".")


def _tenant(tenant: str | None) -> str:
    t = str(tenant or "").strip()
    return t if valid_tenant(t) else DEFAULT_TENANT


def authorize_url(tenant: str | None = None) -> str:
    return f"{MS_LOGIN_BASE}/{quote(_tenant(tenant))}/oauth2/v2.0/authorize"


def token_url(tenant: str | None = None) -> str:
    return f"{MS_LOGIN_BASE}/{quote(_tenant(tenant))}/oauth2/v2.0/token"


# ── Data ─────────────────────────────────────────────────────────────


@dataclass
class MicrosoftOAuthTokens:
    """Tokens from the Microsoft identity platform's token endpoint."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_at: float = 0.0  # Unix timestamp
    scope: str = ""
    id_token: str = ""

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        """True if the access token expires within *buffer_seconds*."""
        return time.time() >= (self.expires_at - buffer_seconds)

    def granted(self) -> frozenset[str]:
        return granted_scopes(self.scope)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MicrosoftOAuthTokens:
        known = {"access_token", "refresh_token", "token_type", "expires_at", "scope", "id_token"}
        return cls(**{k: v for k, v in data.items() if k in known})


# ── URL builder ──────────────────────────────────────────────────────


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    scopes: list[str] | None = None,
    state: str | None = None,
    code_challenge: str | None = None,
    tenant: str | None = None,
) -> str:
    """The sign-in URL.

    ``prompt=select_account`` always shows the account picker: a browser
    already signed into one Microsoft account would otherwise go straight on
    with THAT account, so on a shared machine the next person would silently
    connect the previous person's mailbox (the Google flow guards the same
    way). Microsoft takes one prompt value only; ``offline_access`` in the
    scopes is what yields the refresh token.
    """
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
        "prompt": "select_account",
    }
    if state:
        params["state"] = state
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{authorize_url(tenant)}?{urlencode(params)}"


# ── Token exchange / refresh ─────────────────────────────────────────


def _tokens_from(data: dict[str, Any], *, previous_refresh: str = "") -> MicrosoftOAuthTokens:
    return MicrosoftOAuthTokens(
        access_token=data["access_token"],
        # Microsoft rotates refresh tokens: keep the new one when sent.
        refresh_token=str(data.get("refresh_token") or previous_refresh),
        token_type=data.get("token_type", "Bearer"),
        expires_at=time.time() + int(data.get("expires_in", 3600)),
        scope=str(data.get("scope", "")),
        id_token=str(data.get("id_token", "")),
    )


def _token_error(resp: httpx.Response) -> str:
    """The identity platform's own reason (``AADSTS…: …``) for a failed call."""
    try:
        body = resp.json()
    except Exception:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        desc = str(body.get("error_description") or body.get("error") or "").strip()
        if desc:
            return desc.splitlines()[0][:400]
    return f"HTTP {resp.status_code}"


class MicrosoftTokenError(RuntimeError):
    """The token endpoint refused (bad code, revoked grant, wrong secret…)."""


async def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: str | None = None,
    scopes: list[str] | None = None,
    tenant: str | None = None,
) -> MicrosoftOAuthTokens:
    """Exchange an authorization code for access + refresh tokens."""
    payload: dict[str, str] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(token_url(tenant), data=payload)
    if resp.status_code != 200:
        raise MicrosoftTokenError(_token_error(resp))
    return _tokens_from(resp.json())


async def refresh_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    scopes: list[str] | None = None,
    tenant: str | None = None,
) -> MicrosoftOAuthTokens:
    """A fresh access token (and, usually, a rotated refresh token)."""
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": " ".join(scopes or DEFAULT_SCOPES),
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(token_url(tenant), data=payload)
    if resp.status_code != 200:
        raise MicrosoftTokenError(_token_error(resp))
    return _tokens_from(resp.json(), previous_refresh=refresh_token)


# ── User info ────────────────────────────────────────────────────────


async def fetch_user_info(access_token: str) -> dict[str, Any]:
    """The signed-in account from Graph ``/me``, in the shape Flight Deck
    stores: ``sub`` (the stable account id), ``email``, ``name``, ``upn``.

    ``mail`` is empty for many personal accounts and for work accounts without
    a mailbox, so ``email`` falls back to the user principal name.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GRAPH_API}/me",
            params={"$select": "id,displayName,mail,userPrincipalName"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        me = resp.json()
    upn = str(me.get("userPrincipalName") or "")
    return {
        "sub": str(me.get("id") or ""),
        "email": str(me.get("mail") or upn),
        "name": str(me.get("displayName") or ""),
        "upn": upn,
    }
