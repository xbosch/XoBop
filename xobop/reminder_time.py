from __future__ import annotations

import re
from datetime import datetime, timedelta

_RELATIVE_RE = re.compile(
    r"^in\s+(?:(?P<days>\d+)\s*d\s*)?(?:(?P<hours>\d+)\s*h\s*)?(?:(?P<minutes>\d+)\s*m\s*)?$",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(
    r"^(?P<prefix>today\s+at\s+|tomorrow\s+at\s+|at\s+)"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?$",
    re.IGNORECASE,
)


def parse_reminder_when(raw: str, now: datetime) -> datetime | None:
    """Deterministic parser for /remind's <when> argument.

    Supports:
      - "in 10m", "in 2h", "in 3d", "in 1h30m" (composable, any order of d/h/m)
      - "at 15:30", "at 3pm"                    -> next occurrence of that time
      - "today at HH:MM", "tomorrow at HH:MM"

    Returns None (not an exception) on anything unrecognized, so the caller
    can reject the command clearly instead of silently scheduling the wrong time.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None

    m = _RELATIVE_RE.match(text)
    if m and any(m.group(g) for g in ("days", "hours", "minutes")):
        delta = timedelta(
            days=int(m.group("days") or 0),
            hours=int(m.group("hours") or 0),
            minutes=int(m.group("minutes") or 0),
        )
        return now + delta

    m = _CLOCK_RE.match(text)
    if m:
        hour = int(m.group("hour"))
        minute = int(m.group("minute") or 0)
        ampm = m.group("ampm")
        if ampm:
            if not (1 <= hour <= 12):
                return None
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None

        prefix = m.group("prefix").strip()
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if prefix.startswith("tomorrow"):
            candidate += timedelta(days=1)
        elif prefix == "at" and candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    return None
