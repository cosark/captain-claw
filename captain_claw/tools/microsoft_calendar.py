"""Microsoft (Outlook) Calendar tool — list, search, read, create, update,
delete and answer events through Microsoft Graph.

Same shape as ``google_calendar``, with one difference that matters: Outlook
emails other people ITSELF the moment an event changes — invitations when an
event with attendees is created or changed, cancellations when the organizer
deletes it, a reply to the organizer when an invitation is answered. So those
calls need ``notify_attendees=true`` (the model passes it only when the user
asked), and an automated turn is refused them unless the action was approved
(``mail_authority.check_notify_others``). Without attendees nothing is sent.

Token: the owner's Microsoft connection through Flight Deck
(:mod:`captain_claw.microsoft_oauth_manager`); scopes ``Calendars.Read`` /
``Calendars.ReadWrite``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from captain_claw import mail_authority
from captain_claw.logging import get_logger
from captain_claw.microsoft_graph import (
    CALENDAR_READ,
    CALENDAR_WRITE,
    GRAPH_API,
    flag,
    graph_error,
    graph_token,
    limit,
    pop_runtime,
)
from captain_claw.tools.registry import Tool, ToolResult

log = get_logger(__name__)

_WRITE_ACTIONS = frozenset({"create_event", "update_event", "delete_event", "respond_event"})

_EVENT_SELECT = (
    "id,subject,start,end,isAllDay,location,organizer,attendees,isOrganizer,"
    "isCancelled,showAs,responseStatus,webLink,onlineMeeting,isOnlineMeeting,"
    "recurrence,type,seriesMasterId,bodyPreview,createdDateTime,lastModifiedDateTime,"
    "reminderMinutesBeforeStart,isReminderOn,categories"
)
_LIST_SELECT = (
    "id,subject,start,end,isAllDay,location,organizer,isOrganizer,isCancelled,"
    "showAs,responseStatus,type,attendees,bodyPreview"
)

# search_events looks through this window (and at most this many events).
_SEARCH_BACK = timedelta(days=365)
_SEARCH_AHEAD = timedelta(days=365)
_SEARCH_MAX_EVENTS = 2000

_DAYS = {"MO": "monday", "TU": "tuesday", "WE": "wednesday", "TH": "thursday",
         "FR": "friday", "SA": "saturday", "SU": "sunday"}
_INDEX = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}

_NOTIFY_REQUIRED = (
    "Not done: {what}. Outlook emails them itself, so this needs "
    "notify_attendees=true — pass it only if the user asked for that. Nothing "
    "was changed."
)


def _seg(value: str) -> str:
    """An id as one URL path segment (Graph ids keep '=', '-', '_')."""
    return quote(str(value).strip(), safe="=-_.!~")


class MicrosoftCalendarTool(Tool):
    """Outlook / Microsoft 365 calendar through Microsoft Graph."""

    name = "microsoft_calendar"
    description = (
        "Outlook / Microsoft 365 Calendar. Actions: list_events (upcoming events in a time "
        "window), search_events (find events by text), get_event (full details), create_event, "
        "update_event, delete_event, respond_event (accept / decline / tentative an invitation), "
        "list_calendars. Outlook EMAILS PEOPLE ITSELF: creating or changing an event that has "
        "attendees sends them invitations/updates, deleting a meeting you organize sends "
        "cancellations, answering an invitation can notify the organizer — so those calls need "
        "notify_attendees=true, which you pass ONLY when the user asked you to invite / update / "
        "cancel / reply. Events without attendees never notify anyone. Times: ISO 8601 "
        "('2026-02-24T10:00:00+01:00'), or a date ('2026-02-24') for all-day events; pass "
        "`timezone` (IANA, e.g. 'Europe/Zagreb') for local times and for recurring events."
    )
    timeout_seconds = 120.0
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_events", "search_events", "get_event", "create_event",
                    "update_event", "delete_event", "respond_event", "list_calendars",
                ],
                "description": "The action to perform.",
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (from list_calendars). Default: the user's default calendar.",
            },
            "event_id": {
                "type": "string",
                "description": "Event ID (get_event, update_event, delete_event, respond_event).",
            },
            "query": {"type": "string", "description": "Text to look for (search_events)."},
            "summary": {"type": "string", "description": "Event title (create_event, update_event)."},
            "description": {"type": "string", "description": "Event notes / body (create_event, update_event)."},
            "location": {"type": "string", "description": "Event location (create_event, update_event)."},
            "start": {
                "type": "string",
                "description": (
                    "Event start: ISO 8601 datetime, or a date (YYYY-MM-DD) for an all-day event."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "Event end (same format as start; for all-day events the EXCLUSIVE end date). "
                    "Default: start + 1 hour (timed) or + 1 day (all-day)."
                ),
            },
            "timezone": {
                "type": "string",
                "description": (
                    "IANA timezone (e.g. 'Europe/Berlin'): start/end without an offset are in it, "
                    "a recurring event repeats in it, and listed times are shown in it. Default UTC."
                ),
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Attendee email addresses (create_event, update_event — replaces the list). "
                    "Outlook sends them invitations: needs notify_attendees=true."
                ),
            },
            "notify_attendees": {
                "type": "boolean",
                "description": (
                    "true ONLY when the user asked you to invite / update / cancel / reply: Outlook "
                    "will email the attendees (or, for respond_event, the organizer). Required for "
                    "any change that emails other people; default false."
                ),
            },
            "response": {
                "type": "string",
                "enum": ["accept", "decline", "tentative"],
                "description": "respond_event: your answer to the invitation.",
            },
            "comment": {"type": "string", "description": "respond_event: optional note to the organizer."},
            "reminder_minutes": {
                "type": "number",
                "description": "Reminder this many minutes before the start (create_event, update_event).",
            },
            "recurrence": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "One RRULE, e.g. ['RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10'] (FREQ DAILY / WEEKLY / "
                    "MONTHLY / YEARLY; INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY, BYMONTH)."
                ),
            },
            "online_meeting": {
                "type": "boolean",
                "description": "create_event: add a Teams meeting link (work / school accounts).",
            },
            "max_results": {
                "type": "number",
                "description": "Maximum events to return (default 10, max 100).",
            },
            "time_min": {
                "type": "string",
                "description": "Window start (ISO 8601). list_events default: now.",
            },
            "time_max": {
                "type": "string",
                "description": "Window end (ISO 8601). list_events default: one year after time_min.",
            },
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            headers={"User-Agent": "Captain Claw/0.1.0 (Microsoft Calendar Tool)"},
        )

    async def execute(self, action: str, **kwargs: Any) -> ToolResult:
        pop_runtime(kwargs)
        handlers = {
            "list_events": self._action_list_events,
            "search_events": self._action_search_events,
            "get_event": self._action_get_event,
            "create_event": self._action_create_event,
            "update_event": self._action_update_event,
            "delete_event": self._action_delete_event,
            "respond_event": self._action_respond_event,
            "list_calendars": self._action_list_calendars,
        }
        handler = handlers.get(action)
        if handler is None:
            return ToolResult(
                success=False,
                error=f"Unknown action '{action}'. Use one of: {', '.join(handlers)}",
            )
        write = action in _WRITE_ACTIONS
        try:
            token, _granted = await graph_token(
                CALENDAR_WRITE if write else CALENDAR_READ,
                "calendar write access (Calendars.ReadWrite)" if write
                else "calendar access (Calendars.Read)",
            )
        except RuntimeError as exc:
            return ToolResult(success=False, error=str(exc))
        try:
            return await handler(token, **kwargs)
        except httpx.HTTPStatusError as exc:
            return graph_error(exc, "Microsoft Calendar", not_found="Event or calendar not found. Check the ID.")
        except httpx.HTTPError as exc:
            log.error("Microsoft Calendar HTTP error", action=action, error=str(exc))
            return ToolResult(success=False, error=f"HTTP error: {exc}")
        except ValueError as exc:  # bad times / recurrence — the model's input
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            log.error("Microsoft Calendar tool error", action=action, error=str(exc))
            return ToolResult(success=False, error=str(exc))

    # ── HTTP ────────────────────────────────────────────────────────

    @staticmethod
    def _headers(token: str, tz: str | None = None, *, text_body: bool = False) -> dict[str, str]:
        prefer = [f'outlook.timezone="{_display_tz(tz)}"']
        if text_body:
            prefer.append('outlook.body-content-type="text"')
        return {"Authorization": f"Bearer {token}", "Prefer": ", ".join(prefer)}

    async def _get(self, token: str, path: str, params: dict[str, Any] | None = None,
                   tz: str | None = None, *, text_body: bool = False) -> dict[str, Any]:
        url = path if path.startswith("https://") else f"{GRAPH_API}{path}"
        resp = await self._client.get(url, params=params, headers=self._headers(token, tz, text_body=text_body))
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _calendar_path(calendar_id: str | None) -> str:
        cid = str(calendar_id or "").strip()
        if not cid or cid.lower() in ("primary", "default"):
            return "/me/calendar"
        return f"/me/calendars/{_seg(cid)}"

    # ── list_calendars ──────────────────────────────────────────────

    async def _action_list_calendars(self, token: str, **kwargs: Any) -> ToolResult:
        data = await self._get(token, "/me/calendars", {
            "$select": "id,name,isDefaultCalendar,canEdit,owner,color", "$top": 100,
        })
        cals = data.get("value", [])
        if not cals:
            return ToolResult(success=True, content="No calendars found.")
        lines = [f"Calendars ({len(cals)} found):\n"]
        for cal in cals:
            default = " [DEFAULT]" if cal.get("isDefaultCalendar") else ""
            owner = (cal.get("owner") or {}).get("address", "")
            lines.append(
                f"  {cal.get('name', '(no name)')}{default}\n    ID: {cal.get('id')}"
                f"  |  {'can edit' if cal.get('canEdit') else 'read-only'}"
                + (f"  |  Owner: {owner}" if owner else "")
            )
        return ToolResult(success=True, content="\n".join(lines))

    # ── list / search ───────────────────────────────────────────────

    async def _calendar_view(
        self, token: str, calendar_id: str | None, start: datetime, end: datetime,
        tz: str | None, max_events: int, select: str,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "startDateTime": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$orderby": "start/dateTime",
            "$top": min(max_events, 100),
            "$select": select,
        }
        url: str | None = f"{self._calendar_path(calendar_id)}/calendarView"
        events: list[dict[str, Any]] = []
        while url and len(events) < max_events:
            data = await self._get(token, url, params, tz)
            params = None  # the nextLink carries them
            events.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return events[:max_events]

    async def _action_list_events(
        self, token: str, calendar_id: str | None = None, max_results: Any = None,
        time_min: str | None = None, time_max: str | None = None,
        timezone: str | None = None, **kwargs: Any,
    ) -> ToolResult:
        n = limit(max_results, 10, 100)
        start = _parse_bound(time_min, timezone) or datetime.now(tz=_utc())
        end = _parse_bound(time_max, timezone) or (start + timedelta(days=365))
        events = await self._calendar_view(token, calendar_id, start, end, timezone, n, _LIST_SELECT)
        if not events:
            return ToolResult(success=True, content="No upcoming events found.")
        lines = [f"Upcoming events ({len(events)}, times in {_display_tz(timezone)}):\n"]
        lines.extend(_format_event(ev) for ev in events)
        return ToolResult(success=True, content="\n".join(lines))

    async def _action_search_events(
        self, token: str, query: str = "", calendar_id: str | None = None,
        max_results: Any = None, time_min: str | None = None, time_max: str | None = None,
        timezone: str | None = None, **kwargs: Any,
    ) -> ToolResult:
        """Graph has no text search over events: look through a window
        (default a year back and a year ahead) and match subject, location,
        attendees, organizer and preview."""
        words = [w for w in str(query or "").lower().split() if w]
        if not words:
            return ToolResult(success=False, error="Search query is required.")
        n = limit(max_results, 10, 100)
        now = datetime.now(tz=_utc())
        start = _parse_bound(time_min, timezone) or (now - _SEARCH_BACK)
        end = _parse_bound(time_max, timezone) or (now + _SEARCH_AHEAD)
        events = await self._calendar_view(
            token, calendar_id, start, end, timezone, _SEARCH_MAX_EVENTS, _LIST_SELECT,
        )
        hits = [ev for ev in events if all(w in _event_haystack(ev) for w in words)][:n]
        if not hits:
            return ToolResult(success=True, content=(
                f"No events found matching '{query}' between {start.date()} and {end.date()}."
            ))
        lines = [f"Search results for '{query}' ({len(hits)} found, times in {_display_tz(timezone)}):\n"]
        lines.extend(_format_event(ev) for ev in hits)
        return ToolResult(success=True, content="\n".join(lines))

    # ── get ─────────────────────────────────────────────────────────

    async def _action_get_event(
        self, token: str, event_id: str = "", timezone: str | None = None, **kwargs: Any,
    ) -> ToolResult:
        if not event_id:
            return ToolResult(success=False, error="event_id is required for get_event.")
        ev = await self._get(
            token, f"/me/events/{_seg(event_id)}",
            {"$select": _EVENT_SELECT + ",body"}, timezone, text_body=True,
        )
        return ToolResult(success=True, content=_format_event_detail(ev, timezone))

    # ── create / update ─────────────────────────────────────────────

    async def _action_create_event(
        self, token: str, summary: str = "", calendar_id: str | None = None,
        start: str | None = None, end: str | None = None, timezone: str | None = None,
        description: str | None = None, location: str | None = None,
        attendees: list[str] | None = None, notify_attendees: Any = None,
        reminder_minutes: Any = None, recurrence: list[str] | None = None,
        online_meeting: Any = None, **kwargs: Any,
    ) -> ToolResult:
        if not summary:
            return ToolResult(success=False, error="summary is required for create_event.")
        if not start:
            return ToolResult(success=False, error="start is required for create_event.")
        people = _addresses(attendees)
        if people:
            refusal = self._notify_refusal(
                notify_attendees, f"invitations to {', '.join(people)}",
                "creating an event with attendees sends them invitations",
            )
            if refusal:
                return refusal
        body: dict[str, Any] = {"subject": summary}
        body.update(_times(start, end, timezone))
        if description:
            body["body"] = {"contentType": "text", "content": description}
        if location:
            body["location"] = {"displayName": location}
        if people:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in people
            ]
        if reminder_minutes not in (None, ""):
            body["isReminderOn"] = True
            body["reminderMinutesBeforeStart"] = int(float(reminder_minutes))
        if recurrence:
            body["recurrence"] = _recurrence(recurrence, start, timezone)
        if flag(online_meeting):
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"

        resp = await self._client.post(
            f"{GRAPH_API}{self._calendar_path(calendar_id)}/events",
            json=body, headers=self._headers(token, timezone),
        )
        resp.raise_for_status()
        ev = resp.json()
        msg = f"Created event '{ev.get('subject', summary)}' in Outlook Calendar.\n  ID: {ev.get('id', '?')}"
        msg += f"\n  When: {_when(ev)}"
        if people:
            msg += f"\n  Invitations sent to: {', '.join(people)}"
        join = (ev.get("onlineMeeting") or {}).get("joinUrl")
        if join:
            msg += f"\n  Teams link: {join}"
        if ev.get("webLink"):
            msg += f"\n  Link: {ev['webLink']}"
        return ToolResult(success=True, content=msg)

    async def _action_update_event(
        self, token: str, event_id: str = "", summary: str | None = None,
        start: str | None = None, end: str | None = None, timezone: str | None = None,
        description: str | None = None, location: str | None = None,
        attendees: list[str] | None = None, notify_attendees: Any = None,
        reminder_minutes: Any = None, recurrence: list[str] | None = None, **kwargs: Any,
    ) -> ToolResult:
        if not event_id:
            return ToolResult(success=False, error="event_id is required for update_event.")
        body: dict[str, Any] = {}
        if summary is not None:
            body["subject"] = summary
        if description is not None:
            body["body"] = {"contentType": "text", "content": description}
        if location is not None:
            body["location"] = {"displayName": location}
        if start is not None:
            body.update(_times(start, end, timezone))
        elif end is not None:
            return ToolResult(success=False, error="Pass start together with end to move an event.")
        people = _addresses(attendees) if attendees is not None else None
        if people is not None:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in people
            ]
        if reminder_minutes not in (None, ""):
            body["isReminderOn"] = True
            body["reminderMinutesBeforeStart"] = int(float(reminder_minutes))
        if recurrence is not None:
            body["recurrence"] = _recurrence(recurrence, start, timezone) if recurrence else None
        if not body:
            return ToolResult(success=False, error="No fields to update. Provide at least one field.")

        # An organizer's change to a meeting emails its attendees an update.
        current = await self._get(token, f"/me/events/{_seg(event_id)}",
                                  {"$select": "id,subject,isOrganizer,attendees"})
        others = _addresses(
            [(a.get("emailAddress") or {}).get("address", "") for a in current.get("attendees") or []]
        )
        notified = sorted(set(others) | set(people or [])) if current.get("isOrganizer") else []
        if notified:
            refusal = self._notify_refusal(
                notify_attendees, f"an update to {', '.join(notified)}",
                "this meeting has attendees, and changing it emails them an update",
            )
            if refusal:
                return refusal

        resp = await self._client.patch(
            f"{GRAPH_API}/me/events/{_seg(event_id)}",
            json=body, headers=self._headers(token, timezone),
        )
        resp.raise_for_status()
        ev = resp.json()
        msg = f"Updated event '{ev.get('subject', event_id)}'.\n  ID: {ev.get('id', event_id)}\n  When: {_when(ev)}"
        if notified:
            msg += f"\n  Update sent to: {', '.join(notified)}"
        return ToolResult(success=True, content=msg)

    # ── delete / respond ────────────────────────────────────────────

    async def _action_delete_event(
        self, token: str, event_id: str = "", notify_attendees: Any = None, **kwargs: Any,
    ) -> ToolResult:
        if not event_id:
            return ToolResult(success=False, error="event_id is required for delete_event.")
        current = await self._get(token, f"/me/events/{_seg(event_id)}",
                                  {"$select": "id,subject,isOrganizer,attendees"})
        others = _addresses(
            [(a.get("emailAddress") or {}).get("address", "") for a in current.get("attendees") or []]
        )
        if current.get("isOrganizer") and others:
            refusal = self._notify_refusal(
                notify_attendees, f"a cancellation to {', '.join(others)}",
                "you organize this meeting, and deleting it emails the attendees a cancellation",
            )
            if refusal:
                return refusal
        resp = await self._client.delete(
            f"{GRAPH_API}/me/events/{_seg(event_id)}", headers=self._headers(token),
        )
        resp.raise_for_status()
        msg = f"Deleted event '{current.get('subject') or event_id}'."
        if current.get("isOrganizer") and others:
            msg += f"\n  Cancellation sent to: {', '.join(others)}"
        return ToolResult(success=True, content=msg)

    async def _action_respond_event(
        self, token: str, event_id: str = "", response: str = "", comment: str = "",
        notify_attendees: Any = None, **kwargs: Any,
    ) -> ToolResult:
        if not event_id:
            return ToolResult(success=False, error="event_id is required for respond_event.")
        verb = {"accept": "accept", "decline": "decline", "tentative": "tentativelyAccept"}.get(
            str(response or "").strip().lower())
        if verb is None:
            return ToolResult(success=False, error="response must be accept, decline or tentative.")
        send = flag(notify_attendees)
        if send:
            refusal = mail_authority.check_notify_others("your reply to the organizer")
            if refusal:
                return ToolResult(success=False, error=refusal)
        payload: dict[str, Any] = {"sendResponse": send}
        if comment and send:
            payload["comment"] = comment
        resp = await self._client.post(
            f"{GRAPH_API}/me/events/{_seg(event_id)}/{verb}",
            json=payload, headers=self._headers(token),
        )
        resp.raise_for_status()
        said = {"accept": "Accepted", "decline": "Declined", "tentativelyAccept": "Tentatively accepted"}[verb]
        return ToolResult(success=True, content=(
            f"{said} event {event_id}."
            + (" The organizer was notified." if send else " The organizer was not notified.")
        ))

    @staticmethod
    def _notify_refusal(notify: Any, what: str, why: str) -> ToolResult | None:
        """None when this change may email other people; else the refusal."""
        if not flag(notify):
            return ToolResult(success=False, error=_NOTIFY_REQUIRED.format(what=why))
        refusal = mail_authority.check_notify_others(what)
        return ToolResult(success=False, error=refusal) if refusal else None

    async def close(self) -> None:
        await self._client.aclose()


# ── time helpers ────────────────────────────────────────────────────


def _utc() -> timezone:
    return timezone.utc


def _zone(tz: str | None) -> ZoneInfo | None:
    if not tz:
        return None
    try:
        return ZoneInfo(str(tz).strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _display_tz(tz: str | None) -> str:
    return str(tz).strip() if tz and _zone(tz) else "UTC"


def _is_date(value: str) -> bool:
    return len(str(value).strip()) <= 10


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Can't read the time {value!r}: use ISO 8601 such as '2026-02-24T10:00:00+01:00' "
            "or a date '2026-02-24'."
        ) from None


def _parse_bound(value: str | None, tz: str | None) -> datetime | None:
    """A window bound as an aware datetime (naive → *tz*, else UTC)."""
    if not value:
        return None
    dt = _parse_dt(value if not _is_date(value) else f"{str(value).strip()}T00:00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz) or _utc())
    return dt


def _graph_time(value: str, tz: str | None) -> dict[str, str]:
    """A dateTimeTimeZone: local wall time in *tz* when one is given (so a
    recurring event keeps its local time across DST), else UTC."""
    dt = _parse_dt(value)
    zone = _zone(tz)
    if zone is not None:
        if dt.tzinfo is not None:
            dt = dt.astimezone(zone)
        return {"dateTime": dt.replace(tzinfo=None).isoformat(timespec="seconds"), "timeZone": str(tz).strip()}
    if dt.tzinfo is not None:
        dt = dt.astimezone(_utc())
    return {"dateTime": dt.replace(tzinfo=None).isoformat(timespec="seconds"), "timeZone": "UTC"}


def _times(start: str, end: str | None, tz: str | None) -> dict[str, Any]:
    """start / end (+ isAllDay) for an event body."""
    if _is_date(start):
        try:
            first = date.fromisoformat(str(start).strip())
            last = date.fromisoformat(str(end).strip()[:10]) if end else first + timedelta(days=1)
        except ValueError:
            raise ValueError("All-day dates must look like '2026-02-24'.") from None
        if last <= first:
            last = first + timedelta(days=1)
        zone = _display_tz(tz)
        return {
            "isAllDay": True,
            "start": {"dateTime": f"{first.isoformat()}T00:00:00", "timeZone": zone},
            "end": {"dateTime": f"{last.isoformat()}T00:00:00", "timeZone": zone},
        }
    begin = _graph_time(start, tz)
    if end:
        finish = _graph_time(end, tz)
    else:
        later = (_parse_dt(start) + timedelta(hours=1)).isoformat()
        finish = _graph_time(later, tz)
    return {"isAllDay": False, "start": begin, "end": finish}


# ── recurrence ──────────────────────────────────────────────────────


def _recurrence(rules: list[str], start: str | None, tz: str | None) -> dict[str, Any]:
    """An RRULE as Graph's patternedRecurrence (the common subset)."""
    rule = next((r for r in rules if "FREQ=" in str(r).upper()), "")
    if not rule:
        raise ValueError("recurrence needs an RRULE such as 'RRULE:FREQ=WEEKLY;COUNT=10'.")
    if not start:
        raise ValueError("A recurring event needs its start (pass start).")
    parts: dict[str, str] = {}
    for item in str(rule).split(":", 1)[-1].split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()
    first = _parse_dt(start if not _is_date(start) else f"{start}T00:00:00").date()
    freq = parts.get("FREQ", "")
    interval = int(parts.get("INTERVAL", "1") or 1)
    byday = [d for d in parts.get("BYDAY", "").split(",") if d]
    pattern: dict[str, Any] = {"interval": max(1, interval)}

    def day_name(token: str) -> tuple[int | None, str]:
        m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", token)
        if not m:
            raise ValueError(f"Unsupported BYDAY value {token!r}.")
        return (int(m.group(1)) if m.group(1) else None), _DAYS[m.group(2)]

    if freq == "DAILY":
        pattern["type"] = "daily"
    elif freq == "WEEKLY":
        pattern["type"] = "weekly"
        days = [day_name(d)[1] for d in byday] or [list(_DAYS.values())[first.weekday()]]
        pattern["daysOfWeek"] = days
        pattern["firstDayOfWeek"] = "monday"
    elif freq in ("MONTHLY", "YEARLY"):
        yearly = freq == "YEARLY"
        if yearly:
            pattern["month"] = int(parts.get("BYMONTH", first.month))
        if byday:
            idx, day = day_name(byday[0])
            if idx not in _INDEX:
                raise ValueError("BYDAY with an ordinal must be 1-4 or -1 (last), e.g. '2TU'.")
            pattern["type"] = "relativeYearly" if yearly else "relativeMonthly"
            pattern["daysOfWeek"] = [day]
            pattern["index"] = _INDEX[idx]
        else:
            pattern["type"] = "absoluteYearly" if yearly else "absoluteMonthly"
            pattern["dayOfMonth"] = int(parts.get("BYMONTHDAY", first.day))
    else:
        raise ValueError(f"Unsupported FREQ {freq!r}: use DAILY, WEEKLY, MONTHLY or YEARLY.")

    rng: dict[str, Any] = {"startDate": first.isoformat(), "recurrenceTimeZone": _display_tz(tz)}
    if parts.get("COUNT"):
        rng["type"] = "numbered"
        rng["numberOfOccurrences"] = int(parts["COUNT"])
    elif parts.get("UNTIL"):
        until = parts["UNTIL"]
        rng["type"] = "endDate"
        rng["endDate"] = f"{until[0:4]}-{until[4:6]}-{until[6:8]}" if "-" not in until else until[:10]
    else:
        rng["type"] = "noEnd"
    return {"pattern": pattern, "range": rng}


# ── formatting ──────────────────────────────────────────────────────


def _addresses(values: list[str] | None) -> list[str]:
    from captain_claw.gmail_compose import parse_addresses

    out: list[str] = []
    for v in values or []:
        for addr in parse_addresses(str(v or "")):
            if addr and addr not in out:
                out.append(addr)
    return out


def _when(ev: dict[str, Any]) -> str:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    s = str(start.get("dateTime") or "")[:16].replace("T", " ")
    e = str(end.get("dateTime") or "")[:16].replace("T", " ")
    tz = start.get("timeZone") or ""
    if ev.get("isAllDay"):
        first, last = s[:10], e[:10]
        try:
            shown_last = (date.fromisoformat(last) - timedelta(days=1)).isoformat()
        except ValueError:
            shown_last = last
        return f"{first} (all day)" if shown_last <= first else f"{first} → {shown_last} (all day)"
    if s[:10] == e[:10]:
        return f"{s} → {e[11:]}" + (f" {tz}" if tz else "")
    return f"{s} → {e}" + (f" {tz}" if tz else "")


def _event_haystack(ev: dict[str, Any]) -> str:
    parts = [
        ev.get("subject") or "", (ev.get("location") or {}).get("displayName") or "",
        ev.get("bodyPreview") or "",
        ((ev.get("organizer") or {}).get("emailAddress") or {}).get("name") or "",
        ((ev.get("organizer") or {}).get("emailAddress") or {}).get("address") or "",
    ]
    for a in ev.get("attendees") or []:
        em = a.get("emailAddress") or {}
        parts += [em.get("name") or "", em.get("address") or ""]
    return " ".join(parts).lower()


def _format_event(ev: dict[str, Any]) -> str:
    flags = []
    if ev.get("isCancelled"):
        flags.append("cancelled")
    response = (ev.get("responseStatus") or {}).get("response") or ""
    if not ev.get("isOrganizer") and response in ("notResponded", "none") and ev.get("attendees"):
        flags.append("not answered")
    elif response == "tentativelyAccepted":
        flags.append("tentative")
    flag_str = f"  [{', '.join(flags)}]" if flags else ""
    loc = (ev.get("location") or {}).get("displayName") or ""
    return (
        f"  📅 {ev.get('subject') or '(no title)'}{flag_str}"
        f"\n    When: {_when(ev)}" + (f"  |  📍 {loc}" if loc else "")
        + f"\n    ID: {ev.get('id', '?')}"
    )


def _format_event_detail(ev: dict[str, Any], tz: str | None) -> str:
    lines = [f"Event: {ev.get('subject') or '(no title)'}", f"  ID: {ev.get('id', '?')}"]
    lines.append(f"  When: {_when(ev)}")
    if ev.get("isCancelled"):
        lines.append("  Status: cancelled")
    loc = (ev.get("location") or {}).get("displayName")
    if loc:
        lines.append(f"  Location: {loc}")
    org = (ev.get("organizer") or {}).get("emailAddress") or {}
    if org:
        lines.append(f"  Organizer: {org.get('name') or ''} <{org.get('address') or '?'}>"
                     + (" (you)" if ev.get("isOrganizer") else ""))
    response = (ev.get("responseStatus") or {}).get("response")
    if response and not ev.get("isOrganizer"):
        lines.append(f"  Your response: {response}")
    atts = ev.get("attendees") or []
    if atts:
        lines.append(f"  Attendees ({len(atts)}):")
        for a in atts:
            em = a.get("emailAddress") or {}
            status = (a.get("status") or {}).get("response") or "?"
            label = f"{em.get('name')} <{em.get('address')}>" if em.get("name") else em.get("address", "?")
            lines.append(f"    - {label} ({a.get('type', 'required')}, {status})")
    join = (ev.get("onlineMeeting") or {}).get("joinUrl")
    if join:
        lines.append(f"  Online meeting: {join}")
    rec = ev.get("recurrence") or {}
    if rec.get("pattern"):
        p = rec["pattern"]
        lines.append(
            f"  Repeats: {p.get('type')} every {p.get('interval', 1)}"
            + (f" on {', '.join(p.get('daysOfWeek') or [])}" if p.get("daysOfWeek") else "")
            + f" ({(rec.get('range') or {}).get('type', '?')})"
        )
    if ev.get("type") == "occurrence" and ev.get("seriesMasterId"):
        lines.append(f"  Part of series: {ev['seriesMasterId']}")
    if ev.get("isReminderOn"):
        lines.append(f"  Reminder: {ev.get('reminderMinutesBeforeStart')} min before")
    body = str(((ev.get("body") or {}).get("content")) or "").strip()
    if body:
        lines.append(f"  Description: {body[:4000]}")
    if ev.get("webLink"):
        lines.append(f"  Link: {ev['webLink']}")
    lines.append(f"  Times shown in: {_display_tz(tz)}")
    return "\n".join(lines)
