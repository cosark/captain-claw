"""Microsoft (Outlook / Microsoft 365) Mail tool — read, search and draft.

The Outlook twin of ``google_mail``, through Microsoft Graph with the owner's
Microsoft connection (:mod:`captain_claw.microsoft_oauth_manager`):

* Read: list_messages, search (Outlook KQL), read_message, get_thread (an
  Outlook conversation), list_folders, get_attachment (saved under
  ``saved/downloads/<session>/mail-<id>/`` with the tool that reads it).
* Write: create_draft (new or a reply that stays in its conversation),
  update_draft, list_drafts. **No sending** — the agent never sends Outlook
  mail; the user reviews the draft and sends it from Outlook. (Gmail sends go
  through Flight Deck's per-user policy, audit and limits; Outlook has no such
  gate yet, so no send action exists.)
* Mail writes in automated turns go through :mod:`captain_claw.mail_authority`
  exactly like google_mail's, and create_draft refuses a repeat (same
  recipient + subject already in Drafts, or in Sent Items within
  ``tools.microsoft_mail.repeat_check_days``; for a reply, a draft or newer
  reply in the conversation) unless ``allow_repeat`` — the refusal carries
  :data:`gmail_compose.REPEAT_MARK` so the loop and Flight Deck treat it as
  "already done".

Scopes: ``Mail.Read`` to read, ``Mail.ReadWrite`` to draft.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from captain_claw import gmail_compose, mail_authority
from captain_claw.config import get_config
from captain_claw.logging import get_logger
from captain_claw.microsoft_graph import (
    GRAPH_API,
    MAIL_READ,
    MAIL_WRITE,
    flag,
    graph_error,
    graph_token,
    limit,
    odata_string,
    pop_runtime,
)
from captain_claw.tools.registry import Tool, ToolResult

log = get_logger(__name__)

_WRITE_ACTIONS = frozenset({"create_draft", "update_draft"})

_MAX_BODY_CHARS = 30_000
_MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
_MAX_ATTACHMENTS_PER_CALL = 10

_MSG_SELECT = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "sentDateTime,isRead,isDraft,hasAttachments,bodyPreview,webLink"
)
_FULL_SELECT = _MSG_SELECT + ",bccRecipients,replyTo,body,lastModifiedDateTime"

# Friendly folder names → Graph well-known folder names.
_WELL_KNOWN = {
    "inbox": "inbox", "drafts": "drafts", "draft": "drafts", "sent": "sentitems",
    "sentitems": "sentitems", "sent items": "sentitems", "deleted": "deleteditems",
    "deleteditems": "deleteditems", "deleted items": "deleteditems", "trash": "deleteditems",
    "junk": "junkemail", "junkemail": "junkemail", "spam": "junkemail", "archive": "archive",
    "outbox": "outbox",
}

_FOLLOWUP_HINT = (
    "\nNext steps — use microsoft_mail actions only (never filesystem tools):\n"
    "  • Read full email body: microsoft_mail action=read_message message_id=<Newest msg ID above>\n"
    "  • Read whole conversation: microsoft_mail action=get_thread thread_id=<Conversation ID above>\n"
    "  • Reply to one — ONLY if the user asked you to reply: first check get_thread / "
    "list_drafts for a reply already drafted or sent (never make a second one), then "
    "microsoft_mail action=create_draft reply_to_message_id=<Newest msg ID above> body=...\n"
    "  • Narrow the list: microsoft_mail action=search query='from:alice subject:report'"
)
_FOLLOWUP_HINT_NO_REPLY = (
    "\nNext steps — use microsoft_mail actions only (never filesystem tools):\n"
    "  • Read full email body: microsoft_mail action=read_message message_id=<Newest msg ID above>\n"
    "  • Read whole conversation: microsoft_mail action=get_thread thread_id=<Conversation ID above>\n"
    "  • Narrow the list: microsoft_mail action=search query='from:alice subject:report'"
)


def _followup_hint() -> str:
    if mail_authority.check_mail_write("microsoft_mail", "create_draft") is None:
        return _FOLLOWUP_HINT
    return _FOLLOWUP_HINT_NO_REPLY


def _seg(value: str) -> str:
    return quote(str(value).strip(), safe="=-_.!~")


def _recipients(field: str) -> list[dict[str, Any]]:
    """A To / Cc / Bcc string as Graph recipients (display names kept)."""
    import email.utils

    out: list[dict[str, Any]] = []
    for name, addr in email.utils.getaddresses([field or ""]):
        addr = addr.strip()
        if addr:
            entry: dict[str, Any] = {"address": addr}
            if name.strip():
                entry["name"] = name.strip()
            out.append({"emailAddress": entry})
    return out


def _addr_list(recips: list[dict[str, Any]] | None) -> list[str]:
    return [str((r.get("emailAddress") or {}).get("address") or "").lower()
            for r in recips or [] if (r.get("emailAddress") or {}).get("address")]


def _who(recip: dict[str, Any] | None) -> str:
    em = (recip or {}).get("emailAddress") or {}
    name, addr = em.get("name") or "", em.get("address") or ""
    return f"{name} <{addr}>" if name and name != addr else (addr or name or "?")


def _who_list(recips: list[dict[str, Any]] | None) -> str:
    return ", ".join(_who(r) for r in recips or []) or "(none)"


def _stamp(value: str | None) -> str:
    return str(value or "")[:16].replace("T", " ")


class MicrosoftMailTool(Tool):
    """Read Outlook mail and create drafts (never sends)."""

    name = "microsoft_mail"
    description = (
        "Outlook / Microsoft 365 mail — read, search and create drafts. It NEVER sends: drafts are "
        "saved in the user's Outlook Drafts folder for them to review and send; if the user asks you "
        "to send, create the draft and tell them it's waiting in Outlook Drafts. "
        "WRITE ONLY WHEN ASKED: create_draft / update_draft only when the user asked for that email "
        "in this conversation (or a scheduled job's own text explicitly says to draft it). Reading, "
        "listing, summarizing or triaging mail NEVER implies drafting replies — say who is waiting "
        "and offer to draft. If this tool answers '[not-authorized: mail-write]', don't retry: tell "
        "the user. When the user asks for emails, call create_draft once per recipient they named — "
        "do NOT output email text for them to copy. "
        "NO REPEATS: before create_draft check whether the email already exists — list_drafts with "
        "query='to:<recipient>', and for a reply get_thread on its conversation. If the same email "
        "is already drafted or sent, don't create another: report it (update_draft revises a draft). "
        "create_draft refuses a repeat itself ('Not created — repeat of …'): that is not a failure, "
        "don't retry; pass allow_repeat=true only if the user explicitly wants another copy. "
        "Replies pass reply_to_message_id (reply_all=true for reply-all) so the draft stays in the "
        "conversation. ATTACHMENTS: read_message lists them numbered; get_attachment saves them and "
        "names the tool that reads each. ROUTING: any request about an Outlook email, inbox, "
        "conversation, sender or subject MUST use microsoft_mail — never filesystem tools. "
        "Actions: list_messages (newest first; folder default Inbox; grouped by conversation), "
        "search (Outlook search: from:, to:, subject:, hasattachments:true, received>=2026-01-01, "
        "free text; folder default Inbox, folder='all' for every folder), read_message, "
        "get_thread (whole conversation), list_folders, create_draft, update_draft (replace a "
        "draft's body; unset to/cc/bcc/subject keep the draft's own), list_drafts, get_attachment."
    )
    timeout_seconds = 120.0
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_messages", "search", "read_message", "get_thread", "list_folders",
                    "create_draft", "update_draft", "list_drafts", "get_attachment",
                ],
                "description": "The action to perform.",
            },
            "folder": {
                "type": "string",
                "description": (
                    "list_messages / search: Inbox (default), Sent, Drafts, Archive, Deleted, Junk, a "
                    "folder name or ID from list_folders; search also takes 'all'."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "search: Outlook search text (from:alice subject:report hasattachments:true "
                    "received>=2026-01-01 …). list_drafts: 'to:<address>' or words in the subject."
                ),
            },
            "unread_only": {"type": "boolean", "description": "list_messages: only unread mail."},
            "focused_only": {
                "type": "boolean",
                "description": "list_messages: only the Focused Inbox (when the user uses Focused Inbox).",
            },
            "message_id": {"type": "string", "description": "Message ID (read_message, get_attachment)."},
            "thread_id": {
                "type": "string",
                "description": "Conversation ID (get_thread) — the 'Conversation:' ID list/search print.",
            },
            "attachment": {
                "type": "string",
                "description": "get_attachment: number from read_message ('1', '2', …), name, or 'all'.",
            },
            "to": {"type": "string", "description": "Recipients for create_draft / update_draft (comma-separated)."},
            "cc": {"type": "string", "description": "CC recipients (comma-separated)."},
            "bcc": {"type": "string", "description": "BCC recipients (comma-separated)."},
            "subject": {"type": "string", "description": "Subject for create_draft / update_draft."},
            "body": {
                "type": "string",
                "description": (
                    "Plain-text body (create_draft; update_draft: the full new text, it replaces "
                    "the draft's). For a reply it goes above the quoted original."
                ),
            },
            "html_body": {"type": "string", "description": "Optional HTML body instead of body (new drafts)."},
            "reply_to_message_id": {
                "type": "string",
                "description": (
                    "create_draft: the message you are replying to. The draft lands in its "
                    "conversation with 'RE:' and the original quoted; recipients default to the "
                    "sender (or, for your own sent email, its recipients)."
                ),
            },
            "reply_all": {"type": "boolean", "description": "create_draft reply: include everyone (reply all)."},
            "draft_id": {"type": "string", "description": "update_draft: the Draft ID create_draft / list_drafts printed."},
            "allow_repeat": {
                "type": "boolean",
                "description": (
                    "create_draft: true ONLY when the user explicitly asked for another copy of an "
                    "email already drafted or sent. Default false."
                ),
            },
            "max_results": {"type": "number", "description": "Maximum results (default 10, max 50)."},
            "include_body": {"type": "boolean", "description": "list_messages / search: add each preview text."},
            "group_by_thread": {
                "type": "boolean",
                "description": (
                    "list_messages / search: group by conversation (default true) — count conversations, "
                    "not messages, for 'how many new emails'."
                ),
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            headers={"User-Agent": "Captain Claw/0.1.0 (Microsoft Mail Tool)"},
        )
        self._own_cache: dict[str, set[str]] = {}
        self._folder_cache: dict[str, dict[str, str]] = {}

    async def execute(self, action: str, **kwargs: Any) -> ToolResult:
        runtime = pop_runtime(kwargs)
        if action == "get_attachment":
            kwargs["_runtime"] = runtime
        handlers = {
            "list_messages": self._action_list_messages,
            "search": self._action_search,
            "read_message": self._action_read_message,
            "get_thread": self._action_get_thread,
            "list_folders": self._action_list_folders,
            "create_draft": self._action_create_draft,
            "update_draft": self._action_update_draft,
            "list_drafts": self._action_list_drafts,
            "get_attachment": self._action_get_attachment,
        }
        if action in ("send", "send_draft", "reply", "forward"):
            return ToolResult(success=False, error=(
                "microsoft_mail doesn't send email. Create a draft with action=create_draft "
                "and tell the user it is waiting in their Outlook Drafts folder to send."
            ))
        handler = handlers.get(action)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"Unknown action '{action}'. Use one of: {', '.join(handlers)}",
            )

        # Mail writes in an automated turn need the job's own explicit words
        # (or a human's approval) — checked before any token fetch.
        if mail_authority.is_mail_write("microsoft_mail", action):
            if mail_authority.needs_recipient_check():
                rcpt = (
                    None if action != "create_draft" or kwargs.get("reply_to_message_id")
                    else mail_authority.parse_recipients(kwargs.get("to"), kwargs.get("cc"), kwargs.get("bcc"))
                )
                refusal = mail_authority.check_mail_write(
                    "microsoft_mail", action, recipients=rcpt, own_addresses=await self._own_addresses(),
                )
            else:
                refusal = mail_authority.check_mail_write("microsoft_mail", action)
            if refusal:
                log.info("microsoft_mail write refused (automated turn)",
                         action=action, kind=mail_authority.current().kind)
                return ToolResult(success=False, error=refusal)

        write = action in _WRITE_ACTIONS
        try:
            token, _granted = await graph_token(
                MAIL_WRITE if write else MAIL_READ,
                "mail draft access (Mail.ReadWrite)" if write else "mail access (Mail.Read)",
            )
        except RuntimeError as exc:
            return ToolResult(success=False, error=str(exc))
        try:
            return await handler(token, **kwargs)
        except httpx.HTTPStatusError as exc:
            return graph_error(exc, "Outlook mail", not_found="Message, draft or folder not found. Check the ID.")
        except httpx.HTTPError as exc:
            log.error("Outlook mail HTTP error", action=action, error=str(exc))
            return ToolResult(success=False, error=f"HTTP error: {exc}")
        except Exception as exc:
            log.error("Outlook mail tool error", action=action, error=str(exc))
            return ToolResult(success=False, error=str(exc))

    # ── HTTP helpers ────────────────────────────────────────────────

    @staticmethod
    def _headers(token: str, *, text_body: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}"}
        if text_body:
            headers["Prefer"] = 'outlook.body-content-type="text"'
        return headers

    async def _get(self, token: str, path: str, params: dict[str, Any] | None = None,
                   *, text_body: bool = False) -> dict[str, Any]:
        url = path if path.startswith("https://") else f"{GRAPH_API}{path}"
        resp = await self._client.get(url, params=params, headers=self._headers(token, text_body=text_body))
        resp.raise_for_status()
        return resp.json()

    async def _collect(self, token: str, path: str, params: dict[str, Any], want: int,
                       *, text_body: bool = False) -> list[dict[str, Any]]:
        """Up to *want* items, following @odata.nextLink."""
        items: list[dict[str, Any]] = []
        url: str | None = path
        p: dict[str, Any] | None = params
        while url and len(items) < want:
            data = await self._get(token, url, p, text_body=text_body)
            p = None
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items[:want]

    async def _own_addresses(self) -> set[str]:
        """The mailbox's own addresses (mail + UPN); empty on any error."""
        try:
            token, _ = await graph_token(MAIL_READ, "mail access (Mail.Read)")
        except Exception:
            return set()
        if token in self._own_cache:
            return set(self._own_cache[token])
        try:
            me = await self._get(token, "/me", {"$select": "mail,userPrincipalName"})
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.info("Outlook profile lookup failed", error=str(exc))
            return set()
        addrs = {str(me.get(k) or "").strip().lower() for k in ("mail", "userPrincipalName")} - {""}
        self._own_cache.clear()  # tokens rotate; keep the latest only
        self._own_cache[token] = addrs
        return set(addrs)

    async def _folder_path(self, token: str, folder: str | None) -> str:
        """``/me/mailFolders/<id or well-known>`` for a folder argument."""
        name = str(folder or "").strip()
        if not name:
            return "/me/mailFolders/inbox"
        known = _WELL_KNOWN.get(name.lower())
        if known:
            return f"/me/mailFolders/{known}"
        if len(name) > 40 and " " not in name:  # a folder ID
            return f"/me/mailFolders/{_seg(name)}"
        cache = self._folder_cache.setdefault(token, {})
        if name.lower() not in cache:
            data = await self._get(token, "/me/mailFolders", {
                "$filter": f"displayName eq '{odata_string(name)}'", "$select": "id,displayName",
            })
            hits = data.get("value", [])
            if not hits:
                raise LookupError(f"No mail folder named '{name}'. list_folders shows the folders.")
            cache[name.lower()] = str(hits[0]["id"])
        return f"/me/mailFolders/{_seg(cache[name.lower()])}"

    # ── list / search ───────────────────────────────────────────────

    async def _action_list_messages(
        self, token: str, folder: str | None = None, max_results: Any = None,
        unread_only: Any = None, focused_only: Any = None, include_body: Any = None,
        group_by_thread: Any = None, **kwargs: Any,
    ) -> ToolResult:
        try:
            path = await self._folder_path(token, folder)
        except LookupError as exc:
            return ToolResult(success=False, error=str(exc))
        n = limit(max_results, 10, 50)
        grouped = flag(group_by_thread, True)
        # Graph wants the $orderby property first in $filter (else
        # InefficientFilter), hence the always-true receivedDateTime clause.
        filters = []
        if flag(unread_only):
            filters.append("isRead eq false")
        if flag(focused_only):
            filters.append("inferenceClassification eq 'focused'")
        params: dict[str, Any] = {
            "$select": _MSG_SELECT, "$orderby": "receivedDateTime desc",
            "$top": min(n * 4 if grouped else n, 100),
        }
        if filters:
            params["$filter"] = " and ".join(["receivedDateTime ge 1900-01-01T00:00:00Z", *filters])
        msgs = await self._collect(token, f"{path}/messages", params, n * 4 if grouped else n)
        label = str(folder or "Inbox")
        if not msgs:
            return ToolResult(success=True, content=f"No messages in {label}.")
        return ToolResult(success=True, content=self._render(
            msgs, n, grouped, flag(include_body), f"{label} — newest first",
        ) + _followup_hint())

    async def _action_search(
        self, token: str, query: str = "", folder: str | None = None, max_results: Any = None,
        include_body: Any = None, group_by_thread: Any = None, **kwargs: Any,
    ) -> ToolResult:
        q = str(query or "").strip()
        if not q:
            return ToolResult(success=False, error="query is required for search.")
        if str(folder or "").strip().lower() in ("all", "anywhere", "*"):
            path = "/me/messages"
        else:
            try:
                path = f"{await self._folder_path(token, folder)}/messages"
            except LookupError as exc:
                return ToolResult(success=False, error=str(exc))
        n = limit(max_results, 10, 50)
        grouped = flag(group_by_thread, True)
        escaped = q.replace("\\", "\\\\").replace('"', '\\"')
        params = {"$search": f'"{escaped}"', "$select": _MSG_SELECT, "$top": min(n * 3 if grouped else n, 100)}
        msgs = await self._collect(token, path, params, n * 3 if grouped else n)
        if not msgs:
            where = "all folders" if path == "/me/messages" else str(folder or "Inbox")
            return ToolResult(success=True, content=f"No messages found for '{q}' in {where}.")
        msgs.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
        return ToolResult(success=True, content=self._render(
            msgs, n, grouped, flag(include_body), f"Search '{q}'",
        ) + _followup_hint())

    def _render(self, msgs: list[dict[str, Any]], n: int, grouped: bool,
                include_body: bool, title: str) -> str:
        if not grouped:
            shown = msgs[:n]
            lines = [f"{title} ({len(shown)} messages):\n"]
            lines.extend(self._format_summary(m, include_body) for m in shown)
            return "\n".join(lines)
        groups: dict[str, list[dict[str, Any]]] = {}
        for m in msgs:  # newest first, so each group's first is its newest
            groups.setdefault(str(m.get("conversationId") or m.get("id")), []).append(m)
        convs = list(groups.items())[:n]
        unread = sum(1 for _cid, ms in convs if any(not x.get("isRead") for x in ms))
        lines = [f"{title} ({len(convs)} conversations, {unread} with unread mail):\n"]
        for cid, ms in convs:
            newest = ms[0]
            people = []
            for x in ms:
                w = (x.get("from") or {}).get("emailAddress", {}).get("name") or \
                    (x.get("from") or {}).get("emailAddress", {}).get("address") or "?"
                if w not in people:
                    people.append(w)
            n_unread = sum(1 for x in ms if not x.get("isRead"))
            mark = "🔵 " if n_unread else ""
            lines.append(
                f"  {mark}{newest.get('subject') or '(no subject)'}"
                f"\n    From: {_who(newest.get('from'))}  |  {_stamp(newest.get('receivedDateTime'))}"
                + (f"  |  {len(ms)} msgs here, {n_unread} unread" if len(ms) > 1 or n_unread else "")
                + ("  |  📎" if any(x.get("hasAttachments") for x in ms) else "")
                + (f"\n    Participants: {', '.join(people[:6])}" if len(people) > 1 else "")
                + f"\n    Newest msg: {newest.get('id')}\n    Conversation: {cid}"
            )
            if include_body and newest.get("bodyPreview"):
                lines.append(f"    Preview: {str(newest['bodyPreview'])[:300]}")
        return "\n".join(lines)

    @staticmethod
    def _format_summary(m: dict[str, Any], include_body: bool) -> str:
        mark = "🔵 " if not m.get("isRead") else ""
        out = (
            f"  {mark}{m.get('subject') or '(no subject)'}"
            f"\n    From: {_who(m.get('from'))}  |  {_stamp(m.get('receivedDateTime'))}"
            + ("  |  📎" if m.get("hasAttachments") else "")
            + ("  |  DRAFT" if m.get("isDraft") else "")
            + f"\n    Message: {m.get('id')}\n    Conversation: {m.get('conversationId')}"
        )
        if include_body and m.get("bodyPreview"):
            out += f"\n    Preview: {str(m['bodyPreview'])[:300]}"
        return out

    # ── read ────────────────────────────────────────────────────────

    async def _attachments(self, token: str, message_id: str) -> list[dict[str, Any]]:
        data = await self._get(token, f"/me/messages/{_seg(message_id)}/attachments", {
            "$select": "id,name,contentType,size,isInline",
        })
        return data.get("value", [])

    async def _action_read_message(self, token: str, message_id: str = "", **kwargs: Any) -> ToolResult:
        if not message_id:
            return ToolResult(success=False, error="message_id is required.")
        m = await self._get(token, f"/me/messages/{_seg(message_id)}", {"$select": _FULL_SELECT}, text_body=True)
        atts = await self._attachments(token, message_id) if m.get("hasAttachments") else []
        return ToolResult(success=True, content=self._format_detail(m, atts))

    async def _action_get_thread(self, token: str, thread_id: str = "", **kwargs: Any) -> ToolResult:
        cid = str(thread_id or kwargs.get("conversation_id") or "").strip()
        if not cid:
            return ToolResult(success=False, error="thread_id (the Conversation ID) is required.")
        msgs = await self._collect(token, "/me/messages", {
            "$filter": f"conversationId eq '{odata_string(cid)}'", "$select": _FULL_SELECT, "$top": 50,
        }, 50, text_body=True)
        if not msgs:
            return ToolResult(success=True, content="Conversation not found or empty.")
        msgs.sort(key=lambda m: str(m.get("receivedDateTime") or m.get("lastModifiedDateTime") or ""))
        lines = [f"Conversation {cid} ({len(msgs)} messages, oldest first):\n"]
        for i, m in enumerate(msgs, 1):
            lines.append(f"--- Message {i}/{len(msgs)} ---")
            lines.append(self._format_detail(m, None))
            lines.append("")
        return ToolResult(success=True, content="\n".join(lines))

    @staticmethod
    def _format_detail(m: dict[str, Any], atts: list[dict[str, Any]] | None) -> str:
        lines = [
            f"Subject: {m.get('subject') or '(no subject)'}" + ("  [DRAFT]" if m.get("isDraft") else ""),
            f"From: {_who(m.get('from'))}",
            f"To: {_who_list(m.get('toRecipients'))}",
        ]
        if m.get("ccRecipients"):
            lines.append(f"Cc: {_who_list(m.get('ccRecipients'))}")
        if m.get("isDraft") and m.get("bccRecipients"):
            lines.append(f"Bcc: {_who_list(m.get('bccRecipients'))}")
        when = m.get("receivedDateTime") or m.get("lastModifiedDateTime")
        lines.append(f"Date: {_stamp(when)} UTC")
        lines.append(f"Message ID: {m.get('id')}")
        if m.get("isDraft"):
            lines.append(f"Draft ID: {m.get('id')}")
        lines.append(f"Conversation: {m.get('conversationId')}")
        if atts:
            lines.append(f"Attachments ({len(atts)}) — get_attachment saves them:")
            for i, a in enumerate(atts, 1):
                kind = " (inline)" if a.get("isInline") else ""
                lines.append(f"  [{i}] {a.get('name') or 'attachment'}{kind} — "
                             f"{a.get('contentType') or '?'}, {a.get('size') or 0} bytes")
        body = str(((m.get("body") or {}).get("content")) or "").strip()
        if (m.get("body") or {}).get("contentType") == "html":
            body = gmail_compose.html_to_text(body)
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS] + "\n\n... [body truncated]"
        lines.append("")
        lines.append(body or "(empty body)")
        return "\n".join(lines)

    async def _action_list_folders(self, token: str, **kwargs: Any) -> ToolResult:
        data = await self._get(token, "/me/mailFolders", {
            "$top": 100, "$select": "id,displayName,totalItemCount,unreadItemCount,childFolderCount",
        })
        folders = data.get("value", [])
        if not folders:
            return ToolResult(success=True, content="No mail folders found.")
        lines = [f"Mail folders ({len(folders)}):\n"]
        for f in folders:
            lines.append(
                f"  {f.get('displayName')}  ({f.get('unreadItemCount', 0)} unread / "
                f"{f.get('totalItemCount', 0)} total"
                + (f", {f['childFolderCount']} subfolders" if f.get("childFolderCount") else "")
                + f")\n    ID: {f.get('id')}"
            )
        return ToolResult(success=True, content="\n".join(lines))

    # ── drafts ──────────────────────────────────────────────────────

    async def _repeat_problem(
        self, token: str, *, to: list[str], subject: str, conversation_id: str = "",
        after: str = "",
    ) -> str:
        """The refusal when this email already exists, else "" (never raises)."""
        try:
            matches = await self._find_repeats(token, to=to, subject=subject,
                                               conversation_id=conversation_id, after=after)
        except Exception as exc:  # noqa: BLE001 — the check never blocks a draft on its own failure
            log.info("Outlook repeat check failed", error=str(exc))
            return ""
        if not matches:
            return ""
        first = matches[0]
        what = f'"{first.get("subject") or "(no subject)"}" to {", ".join(_addr_list(first.get("toRecipients"))) or "?"}'
        if first.get("isDraft"):
            desc = f"Draft ID {first.get('id')} ({what})"
            todo = f" To change it use microsoft_mail action=update_draft draft_id={first.get('id')}."
        else:
            desc = f"the email sent on {_stamp(first.get('sentDateTime'))} UTC, Message ID {first.get('id')} ({what})"
            todo = ""
        more = f" and {len(matches) - 1} more like it" if len(matches) > 1 else ""
        return (
            f"Not created {gmail_compose.REPEAT_MARK}{desc}{more}. This email is already drafted "
            f"or sent, so no new copy was made. Tell the user, with that Draft ID or sent date.{todo}"
            " Don't retry. Only if the user explicitly asks for another, separate copy, call "
            "again with allow_repeat=true."
        )

    async def _find_repeats(
        self, token: str, *, to: list[str], subject: str, conversation_id: str, after: str,
    ) -> list[dict[str, Any]]:
        sel = "id,subject,toRecipients,ccRecipients,isDraft,sentDateTime,receivedDateTime,from"
        wanted = set(to)
        if conversation_id:
            # A reply: a draft in the conversation, or a reply already sent after the original.
            own = await self._own_addresses()
            msgs = await self._collect(token, "/me/messages", {
                "$filter": f"conversationId eq '{odata_string(conversation_id)}'", "$select": sel, "$top": 50,
            }, 50)
            out = []
            for m in msgs:
                sender = str(((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
                if m.get("isDraft"):
                    out.append(m)
                elif (sender in own and str(m.get("sentDateTime") or "") > after
                      and (not wanted or wanted & set(_addr_list(m.get("toRecipients"))))):
                    out.append(m)
            return out
        if not wanted:
            return []
        out = []
        drafts = await self._collect(token, "/me/mailFolders/drafts/messages", {
            "$select": sel, "$top": 50, "$orderby": "lastModifiedDateTime desc",
        }, 50)
        days = get_config().tools.microsoft_mail.repeat_check_days
        sent: list[dict[str, Any]] = []
        if days > 0:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            sent = await self._collect(token, "/me/mailFolders/sentitems/messages", {
                "$select": sel, "$top": 100, "$filter": f"sentDateTime ge {since}",
                "$orderby": "sentDateTime desc",
            }, 200)
        for m in drafts + sent:
            if wanted & set(_addr_list(m.get("toRecipients")) + _addr_list(m.get("ccRecipients"))) \
                    and gmail_compose.subjects_match(subject, str(m.get("subject") or "")):
                out.append(m)
        return out

    async def _action_create_draft(
        self, token: str, to: str = "", cc: str = "", bcc: str = "", subject: str = "",
        body: str = "", html_body: str = "", reply_to_message_id: str = "",
        reply_all: Any = None, allow_repeat: Any = None, **kwargs: Any,
    ) -> ToolResult:
        bad = gmail_compose.invalid_recipients(to, cc, bcc)
        if bad:
            return ToolResult(success=False, error=gmail_compose.invalid_recipients_message(bad)
                              .replace("Nothing was sent.", "No draft was created."))
        if reply_to_message_id:
            return await self._reply_draft(
                token, reply_to_message_id, to=to, cc=cc, bcc=bcc, subject=subject,
                body=body or gmail_compose.html_to_text(html_body or ""),
                reply_all=flag(reply_all), allow_repeat=flag(allow_repeat),
            )
        if not to and not subject and not body and not html_body:
            return ToolResult(success=False, error=(
                "create_draft requires at least one of: to, subject, body (or reply_to_message_id)."
            ))
        if not flag(allow_repeat):
            problem = await self._repeat_problem(
                token, to=gmail_compose.parse_addresses(to, cc, bcc), subject=subject,
            )
            if problem:
                return ToolResult(success=False, error=problem)
        message: dict[str, Any] = {
            "subject": subject,
            "body": ({"contentType": "html", "content": html_body} if html_body
                     else {"contentType": "text", "content": body}),
            "toRecipients": _recipients(to),
        }
        if cc:
            message["ccRecipients"] = _recipients(cc)
        if bcc:
            message["bccRecipients"] = _recipients(bcc)
        resp = await self._client.post(f"{GRAPH_API}/me/messages", json=message, headers=self._headers(token))
        resp.raise_for_status()
        return ToolResult(success=True, content=self._draft_saved(resp.json(), "Draft created"))

    async def _reply_draft(
        self, token: str, original_id: str, *, to: str, cc: str, bcc: str, subject: str,
        body: str, reply_all: bool, allow_repeat: bool,
    ) -> ToolResult:
        orig = await self._get(token, f"/me/messages/{_seg(original_id)}", {
            "$select": "id,subject,from,replyTo,toRecipients,ccRecipients,conversationId,receivedDateTime,sentDateTime",
        })
        own = await self._own_addresses()
        sender = str(((orig.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        if to:
            to_r = _recipients(to)
        elif sender and sender in own:
            to_r = orig.get("toRecipients") or []  # replying to your own sent email
        else:
            to_r = orig.get("replyTo") or ([orig["from"]] if orig.get("from") else [])
        cc_r = _recipients(cc) if cc else []
        if reply_all and not cc:
            mine = set(own) | set(_addr_list(to_r))
            cc_r = [r for r in (orig.get("toRecipients") or []) + (orig.get("ccRecipients") or [])
                    if _addr_list([r]) and _addr_list([r])[0] not in mine]
        if not allow_repeat:
            problem = await self._repeat_problem(
                token, to=_addr_list(to_r), subject=subject or str(orig.get("subject") or ""),
                conversation_id=str(orig.get("conversationId") or ""),
                after=str(orig.get("receivedDateTime") or orig.get("sentDateTime") or ""),
            )
            if problem:
                return ToolResult(success=False, error=problem)
        message: dict[str, Any] = {"toRecipients": to_r}
        if cc_r:
            message["ccRecipients"] = cc_r
        if bcc:
            message["bccRecipients"] = _recipients(bcc)
        if subject:
            message["subject"] = subject
        verb = "createReplyAll" if reply_all else "createReply"
        resp = await self._client.post(
            f"{GRAPH_API}/me/messages/{_seg(original_id)}/{verb}",
            json={"comment": body or "", "message": message}, headers=self._headers(token),
        )
        resp.raise_for_status()
        return ToolResult(success=True, content=self._draft_saved(
            resp.json(), f"Reply draft created in conversation {orig.get('conversationId')}",
        ))

    @staticmethod
    def _draft_saved(d: dict[str, Any], lead: str) -> str:
        return (
            f"{lead} (saved in Outlook Drafts — not sent).\n"
            f"  Draft ID: {d.get('id', '?')}\n"
            f"  To: {_who_list(d.get('toRecipients'))}\n"
            f"  Subject: {d.get('subject') or '(no subject)'}"
            + (f"\n  Open in Outlook: {d['webLink']}" if d.get("webLink") else "")
        )

    async def _action_update_draft(
        self, token: str, draft_id: str = "", to: str = "", cc: str = "", bcc: str = "",
        subject: str = "", body: str = "", html_body: str = "", **kwargs: Any,
    ) -> ToolResult:
        draft_id = str(draft_id or "").strip()
        if not draft_id:
            return ToolResult(success=False, error="update_draft requires draft_id (from create_draft or list_drafts).")
        if not (str(body or "").strip() or str(html_body or "").strip()):
            return ToolResult(success=False, error=(
                "update_draft replaces the draft's text — pass the full new body. read_message "
                "with the Draft ID shows the current one. Nothing was changed."
            ))
        bad = gmail_compose.invalid_recipients(to, cc, bcc)
        if bad:
            return ToolResult(success=False, error=gmail_compose.invalid_recipients_message(bad)
                              .replace("Nothing was sent.", "Nothing was changed."))
        try:
            current = await self._get(token, f"/me/messages/{_seg(draft_id)}", {"$select": "id,isDraft"})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return ToolResult(success=False, error=(
                    f"Draft {draft_id} not found — it may have been sent or deleted. "
                    "list_drafts shows the current Draft IDs."
                ))
            raise
        if not current.get("isDraft"):
            return ToolResult(success=False, error=f"{draft_id} is not a draft (it was sent) — nothing was changed.")
        patch: dict[str, Any] = {
            "body": ({"contentType": "html", "content": html_body} if html_body
                     else {"contentType": "text", "content": body}),
        }
        if subject:
            patch["subject"] = subject
        if to:
            patch["toRecipients"] = _recipients(to)
        if cc:
            patch["ccRecipients"] = _recipients(cc)
        if bcc:
            patch["bccRecipients"] = _recipients(bcc)
        resp = await self._client.patch(
            f"{GRAPH_API}/me/messages/{_seg(draft_id)}", json=patch, headers=self._headers(token),
        )
        resp.raise_for_status()
        return ToolResult(success=True, content=self._draft_saved(
            resp.json(), "Draft updated (same draft — no new one was created)",
        ))

    async def _action_list_drafts(
        self, token: str, query: str = "", max_results: Any = None, **kwargs: Any,
    ) -> ToolResult:
        n = limit(max_results, 10, 50)
        drafts = await self._collect(token, "/me/mailFolders/drafts/messages", {
            "$select": "id,subject,toRecipients,ccRecipients,lastModifiedDateTime,conversationId,bodyPreview",
            "$orderby": "lastModifiedDateTime desc", "$top": 100,
        }, 200)
        q = str(query or "").strip().lower()
        if q.startswith("to:"):
            addr = q[3:].strip()
            drafts = [d for d in drafts
                      if any(addr in a for a in _addr_list(d.get("toRecipients")) + _addr_list(d.get("ccRecipients")))]
        elif q:
            drafts = [d for d in drafts if q in f"{d.get('subject') or ''} {d.get('bodyPreview') or ''}".lower()]
        drafts = drafts[:n]
        if not drafts:
            return ToolResult(success=True, content="No drafts found" + (f" for '{query}'." if q else "."))
        lines = [f"Drafts ({len(drafts)}):\n"]
        for d in drafts:
            lines.append(
                f"  {d.get('subject') or '(no subject)'}\n    To: {_who_list(d.get('toRecipients'))}"
                f"  |  edited {_stamp(d.get('lastModifiedDateTime'))} UTC\n    Draft ID: {d.get('id')}"
            )
        return ToolResult(success=True, content="\n".join(lines))

    # ── attachments ─────────────────────────────────────────────────

    async def _action_get_attachment(
        self, token: str, message_id: str = "", attachment: str = "",
        _runtime: dict[str, Any] | None = None, **kwargs: Any,
    ) -> ToolResult:
        from captain_claw.tools.google_mail import GoogleMailTool

        if not message_id:
            return ToolResult(success=False, error="message_id is required.")
        atts = await self._attachments(token, message_id)
        if not atts:
            return ToolResult(success=False, error=f"Message {message_id} has no attachments.")
        listing = ", ".join(f"[{i}] {a.get('name') or 'attachment'}" for i, a in enumerate(atts, 1))
        wanted = str(attachment or "").strip()
        if not wanted and len(atts) > 1:
            return ToolResult(success=False, error=(
                f"The message has {len(atts)} attachments ({listing}) — pass attachment=<number, name or 'all'>."
            ))
        name = wanted.lower()
        if not wanted or name == "all":
            chosen = list(enumerate(atts))[:_MAX_ATTACHMENTS_PER_CALL]
        elif wanted.isdigit() and 1 <= int(wanted) <= len(atts):
            chosen = [(int(wanted) - 1, atts[int(wanted) - 1])]
        else:
            exact = [(i, a) for i, a in enumerate(atts) if str(a.get("name") or "").lower() == name]
            partial = [] if wanted.isdigit() else [
                (i, a) for i, a in enumerate(atts) if name in str(a.get("name") or "").lower()]
            chosen = (exact or partial)[:1]
        if not chosen:
            return ToolResult(success=False, error=f"No attachment '{wanted}' — the message has {listing}.")

        kinds = [str(a.get("@odata.type") or "") for a in atts]
        names = GoogleMailTool._attachment_save_names([
            {"filename": (a.get("name") or "") + (".eml" if "itemAttachment" in k and not
                                                  str(a.get("name") or "").lower().endswith(".eml") else ""),
             "mime_type": a.get("contentType") or ""}
            for a, k in zip(atts, kinds)
        ])
        lines: list[str] = []
        saved = 0
        for index, att in chosen:
            ok, line = await self._save_attachment(token, message_id, att, kinds[index], names[index], _runtime or {})
            lines.append(line)
            saved += ok
        if not saved:
            return ToolResult(success=False, error="\n".join(lines))
        if len(atts) > len(chosen) and name == "all":
            lines.append(f"({len(atts) - len(chosen)} more not saved — ask for them by number.)")
        return ToolResult(success=True, content="\n".join(lines))

    async def _save_attachment(
        self, token: str, message_id: str, att: dict[str, Any], kind: str, save_name: str,
        runtime: dict[str, Any],
    ) -> tuple[bool, str]:
        from captain_claw import saved_attribution
        from captain_claw import speaker as _speaker
        from captain_claw.tools.google_drive import _READER_BY_SUFFIX, GoogleDriveTool, _DownloadPathError

        name = att.get("name") or save_name
        if "referenceAttachment" in kind:
            return False, (f"'{name}' is a link to a cloud file, not an attached file — open it "
                           "with microsoft_onedrive (its share URL) instead.")
        size = int(att.get("size") or 0)
        if size > _MAX_ATTACHMENT_BYTES:
            return False, f"'{name}' is too large ({size} bytes)."
        try:
            resp = await self._client.get(
                f"{GRAPH_API}/me/messages/{_seg(message_id)}/attachments/{_seg(att['id'])}/$value",
                headers=self._headers(token),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return False, f"'{name}' could not be fetched from Outlook (HTTP {exc.response.status_code})."
        except httpx.HTTPError as exc:
            return False, f"'{name}' could not be fetched from Outlook ({type(exc).__name__})."
        blob = resp.content
        if not blob:
            return False, f"'{name}' is empty."
        if len(blob) > _MAX_ATTACHMENT_BYTES:
            return False, f"'{name}' is too large ({len(blob)} bytes)."
        # Graph message ids share long mailbox prefixes: name the folder by a hash.
        folder = "mail-" + hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:16]
        try:
            dest = GoogleDriveTool._download_dest(None, f"{folder}/{save_name}", "", runtime)
        except _DownloadPathError as exc:
            return False, str(exc)
        prior = saved_attribution.prior_creator(dest)
        member = _speaker.current()
        if (member is not None and dest.exists()
                and not saved_attribution.member_may_change(dest, member.speaker_id)):
            return False, _speaker.PATH_REFUSED_PREFIX + _speaker.FILE_NOT_YOURS_WHY
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(dest.write_bytes, blob)
        except OSError as exc:
            return False, f"'{name}' could not be saved ({exc.strerror or type(exc).__name__})."
        saved_attribution.note_write(dest, prior)
        registry = runtime.get("_file_registry")
        if registry is not None:
            try:
                registry.register(logical_path=dest.name, physical_path=str(dest),
                                  task_id=str(runtime.get("_task_id") or ""))
            except Exception:
                pass
        reader = _READER_BY_SUFFIX.get(dest.suffix.lower(), "read")
        # "Saved to:", not "Path:" — chat bridges auto-send files named on a
        # "Path:" line, and an email's attachment is an input.
        return True, (
            f"Saved attachment '{name}' ({att.get('contentType') or 'unknown'}, {len(blob)} bytes)\n"
            f"  Saved to: {dest}\n"
            f"Use {reader}(path=\"{dest}\") to view it."
        )

    async def close(self) -> None:
        await self._client.aclose()
