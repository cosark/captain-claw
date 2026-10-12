"""Shared bits of the Microsoft Graph tools (microsoft_mail /
microsoft_calendar / microsoft_onedrive): the token with a scope check, and
Graph errors turned into messages the model can act on."""

from __future__ import annotations

from typing import Any

import httpx

from captain_claw.microsoft_oauth import GRAPH_API  # noqa: F401 — re-exported
from captain_claw.tools.registry import ToolResult

RECONNECT = "Flight Deck → Connections → Microsoft"

# Grants, as microsoft_oauth.scope_key keys (lower-case, no resource prefix).
MAIL_READ = frozenset({"mail.read", "mail.readwrite"})
MAIL_WRITE = frozenset({"mail.readwrite"})
CALENDAR_READ = frozenset({"calendars.read", "calendars.readwrite"})
CALENDAR_WRITE = frozenset({"calendars.readwrite"})
FILES_READ = frozenset({"files.read", "files.readwrite", "files.read.all", "files.readwrite.all"})
FILES_WRITE = frozenset({"files.readwrite", "files.readwrite.all"})
FILES_ALL = frozenset({"files.read.all", "files.readwrite.all"})

# Injected by the registry into every tool call; the Graph tools don't use them
# (mail / OneDrive pick out the saved-area ones before popping the rest).
RUNTIME_KEYS = ("_runtime_base_path", "_saved_base_path", "_session_id",
                "_file_registry", "_task_id")


def pop_runtime(kwargs: dict[str, Any]) -> dict[str, Any]:
    runtime = {key: kwargs.pop(key, None) for key in RUNTIME_KEYS}
    kwargs.pop("_abort_event", None)
    return runtime


async def graph_token(need: frozenset[str], what: str) -> tuple[str, frozenset[str]]:
    """The owner's Graph access token and its granted scopes; a RuntimeError
    (returned to the model as-is) when Microsoft isn't connected, Flight Deck
    refused, or the grant doesn't cover *need* (*what* names it)."""
    from captain_claw.microsoft_oauth_manager import MicrosoftOAuthManager

    tokens = await MicrosoftOAuthManager().access_token()
    granted = tokens.granted()
    # An empty scope string means the token endpoint didn't report scopes —
    # let Graph be the judge (403) rather than block on it.
    if granted and not granted & need:
        raise RuntimeError(
            f"The Microsoft connection doesn't include {what}. An admin enables it "
            f"in {RECONNECT} (scopes), then the user reconnects Microsoft."
        )
    return tokens.access_token, granted


def graph_message(exc: httpx.HTTPStatusError) -> tuple[str, str]:
    """Graph's ``(code, message)`` for a failed call."""
    try:
        body = exc.response.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "").strip()
    except Exception:
        pass
    return "", (exc.response.text or str(exc)).strip()[:300]


def graph_error(exc: httpx.HTTPStatusError, service: str, *, not_found: str) -> ToolResult:
    status = exc.response.status_code
    code, message = graph_message(exc)
    if status == 401:
        return ToolResult(
            success=False,
            error=f"Microsoft authentication failed. Reconnect Microsoft in {RECONNECT}.",
        )
    if code == "MailboxNotEnabledForRESTAPI" or "MailboxNotEnabledForRESTAPI" in message:
        return ToolResult(success=False, error=(
            f"{service}: this Microsoft account has no Outlook mailbox Graph can reach "
            f"({message or code}). Personal accounts need an Outlook.com mailbox; work "
            "accounts need an Exchange Online license."
        ))
    if status == 403:
        return ToolResult(success=False, error=(
            f"Permission denied by Microsoft: {message or code}. If this is a scope the "
            f"connection lacks, an admin adds it in {RECONNECT} and the user reconnects."
        ))
    if status == 404:
        return ToolResult(success=False, error=not_found)
    if status == 429 or status == 503:
        retry = exc.response.headers.get("Retry-After", "")
        return ToolResult(success=False, error=(
            f"{service} is throttling requests"
            + (f" (retry after {retry}s)" if retry else "")
            + ". Try again in a moment."
        ))
    detail = f"{code}: {message}" if code and message else (message or code or str(exc))
    return ToolResult(success=False, error=f"{service} error ({status}): {detail}")


def odata_string(value: str) -> str:
    """*value* as an OData string literal body (single quotes doubled)."""
    return str(value).replace("'", "''")


def flag(value: Any, default: bool = False) -> bool:
    """A boolean argument as models send it (true, "false", 1, None)."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def limit(value: Any, default: int, maximum: int) -> int:
    try:
        n = int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, maximum))
