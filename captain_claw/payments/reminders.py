"""Due-date reminder stages — Payments Phase 4c (docs/payments-phase4-plan.md).

Pure: which reminder a bill is due today, and the line that says so. A bill
gets a "before" reminder some days ahead, one on the due date, then "overdue"
the day after and weekly until it expires (30 days after the due date).
Missed days don't pile up: each stage is sent at most once (overdue once a
week), and a stage the clock has moved past is simply skipped.
"""

from __future__ import annotations

from datetime import date, timedelta

OVERDUE_EVERY_DAYS = 7
OVERDUE_FOR_DAYS = 30
DEFAULTS = {"enabled": True, "days_before": 3, "time": "09:00", "overdue": True}


def settings(raw) -> dict:
    """The owner's reminder settings with defaults filled in and values clamped."""
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out["enabled"] = bool(raw.get("enabled", True))
        out["overdue"] = bool(raw.get("overdue", True))
        try:
            out["days_before"] = max(0, min(14, int(raw.get("days_before", 3))))
        except (TypeError, ValueError):
            pass
        t = str(raw.get("time") or "")
        if len(t) == 5 and t[2] == ":" and t[:2].isdigit() and t[3:].isdigit() and int(t[:2]) < 24 and int(t[3:]) < 60:
            out["time"] = t
    return out


def stage(due: date, today: date, sent: dict, *, days_before: int = 3, overdue: bool = True) -> str:
    """'before' | 'due' | 'overdue' | '' — what today's digest says about one bill.
    *sent*: stage → ISO day it was last sent."""
    if today < due:
        if days_before > 0 and today >= due - timedelta(days=days_before) and not sent.get("before"):
            return "before"
        return ""
    if today == due:
        return "" if sent.get("due") else "due"
    if not overdue or today > due + timedelta(days=OVERDUE_FOR_DAYS):
        return ""
    last = sent.get("overdue")
    if not last:
        return "overdue"
    try:
        return "overdue" if today >= date.fromisoformat(last) + timedelta(days=OVERDUE_EVERY_DAYS) else ""
    except ValueError:
        return "overdue"


def when(due: date, today: date, stage_: str) -> str:
    """The words for when it's due: "due tomorrow (25.10.)", "due today", "overdue since 20.10."."""
    day = due.strftime("%d.%m.")
    if stage_ == "overdue":
        return f"overdue since {day}"
    days = (due - today).days
    if days <= 0:
        return "due today"
    if days == 1:
        return f"due tomorrow ({day})"
    return f"due in {days} days ({day})"
