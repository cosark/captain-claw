"""Phase 4c — reminder stages (pure): before / due / overdue weekly, each once,
missed days skipped rather than piled up."""

from __future__ import annotations

from datetime import date, timedelta

from captain_claw.payments import reminders as rem

DUE = date(2026, 10, 25)


def walk(days_before=3, overdue=True, start=-10, end=40):
    sent, out = {}, []
    for n in range(start, end):
        today = DUE + timedelta(days=n)
        st = rem.stage(DUE, today, sent, days_before=days_before, overdue=overdue)
        if st:
            sent[st] = today.isoformat()
            out.append((n, st))
    return out


def test_the_schedule():
    assert walk() == [(-3, "before"), (0, "due"), (1, "overdue"), (8, "overdue"), (15, "overdue"),
                      (22, "overdue"), (29, "overdue")]
    assert walk(days_before=0, overdue=False) == [(0, "due")]


def test_missed_days_dont_pile_up():
    # The deck was off until the day before the due date: one "before", then on with the schedule.
    assert walk(start=-1) == [(-1, "before"), (0, "due"), (1, "overdue"), (8, "overdue"), (15, "overdue"),
                              (22, "overdue"), (29, "overdue")]
    # Off until a week late: straight to overdue.
    assert walk(start=7)[0] == (7, "overdue")


def test_words():
    assert rem.when(DUE, DUE - timedelta(days=3), "before") == "due in 3 days (25.10.)"
    assert rem.when(DUE, DUE - timedelta(days=1), "before") == "due tomorrow (25.10.)"
    assert rem.when(DUE, DUE, "due") == "due today"
    assert rem.when(DUE, DUE + timedelta(days=2), "overdue") == "overdue since 25.10."


def test_settings_are_clamped():
    assert rem.settings(None) == rem.DEFAULTS
    assert rem.settings({"days_before": 99, "time": "25:00", "enabled": False}) == {
        "enabled": False, "days_before": 14, "time": "09:00", "overdue": True}
    assert rem.settings({"days_before": "x", "time": "07:15"})["time"] == "07:15"
