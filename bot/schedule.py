"""Local wall clock for the daily summary: SUMMARY_TIMEZONE (an IANA name such as Europe/Berlin) via zoneinfo.

Without a time zone database (Windows without the `tzdata` package) or with an unknown name, zone() falls back
to UTC and says so, and the daemon logs a warning.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo

from .store import utc_iso


def zone(name: str | None) -> tuple[tzinfo, bool]:
    """(tzinfo, True) for 'UTC' or a loadable IANA name; (UTC, False) otherwise."""
    name = (name or "").strip()
    if not name or name.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc, True
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), True
    except Exception:                                   # no tz database here, or an unknown name
        return timezone.utc, False


def _utc(now) -> datetime:
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def to_local(now, tz: tzinfo) -> datetime:
    return _utc(now).astimezone(tz)


def local_day(now, tz: tzinfo) -> date:
    return to_local(now, tz).date()


def day_bounds(day: date, tz: tzinfo) -> tuple[str, str]:
    """UTC ISO [start, end) of one local calendar day."""
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
    return utc_iso(start), utc_iso(end)


def summary_due(now, last_day: str | None, hour: int | None, tz: tzinfo) -> str | None:
    """The local date to summarise when it is >= hour:00 there and today's summary is not sent yet."""
    if hour is None:
        return None
    local = to_local(now, tz)
    today = local.date().isoformat()
    return today if local.hour >= hour and last_day != today else None
