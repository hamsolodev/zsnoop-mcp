"""Human-friendly relative time parsing -> absolute ISO 8601 strings.

The agent only understands ISO 8601 timestamps. The MCP tool layer accepts
phrases like ``"yesterday"``, ``"3 days ago"``, ``"last week"`` and converts
them here so the agent stays simple and consistent.

Supported phrases (case-insensitive, leading/trailing whitespace ignored):

- ``now``
- ``today`` (00:00:00 UTC of the current day)
- ``yesterday`` (00:00:00 UTC of the previous day)
- ``N {seconds|minutes|hours|days|weeks|months|years} ago``
- ``last {hour|day|week|month|year}`` (start of the previous unit, UTC)
- An ISO 8601 timestamp (passed through; naive values are treated as UTC)

All anchors are UTC because the agent compares against ZFS ``creation``
timestamps which are stored as UTC seconds since the epoch. In TZs west
of UTC, "today" therefore starts a few hours before local midnight —
acceptable for filtering since the agent only knows UTC anyway.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Final

from dateutil.relativedelta import relativedelta

# Match "<n> <unit> ago".
_AGO_RE: Final = re.compile(
    r"^\s*(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago\s*$",
    re.IGNORECASE,
)

# Match "last <unit>".
_LAST_RE: Final = re.compile(
    r"^\s*last\s+(hour|day|week|month|year)\s*$",
    re.IGNORECASE,
)


class TimePhraseError(ValueError):
    """Raised when a phrase cannot be parsed as a time."""


def parse_phrase(phrase: str, *, now: datetime | None = None) -> datetime:
    """Parse *phrase* into an aware :class:`datetime`.

    *now* defaults to ``datetime.now(timezone.utc)`` (UTC); inject it in tests
    for deterministic results.
    """
    if not isinstance(phrase, str):
        raise TimePhraseError(f"phrase must be a string, got {type(phrase).__name__}")
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        raise TimePhraseError("now must be timezone-aware")
    text = phrase.strip().lower()
    if text == "now":
        return now
    if text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "yesterday":
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=1)
    if m := _AGO_RE.match(phrase):
        n, unit = int(m.group(1)), m.group(2).lower()
        return now - _unit_delta(n, unit)
    if m := _LAST_RE.match(phrase):
        return _start_of_previous(now, m.group(1).lower())
    # Final fallback: try ISO 8601.
    try:
        dt = datetime.fromisoformat(phrase)
    except ValueError as e:
        raise TimePhraseError(f"could not parse time phrase: {phrase!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def to_iso(phrase: str, *, now: datetime | None = None) -> str:
    """Parse *phrase* and return an ISO 8601 string suitable for the agent."""
    return parse_phrase(phrase, now=now).isoformat()


def maybe_to_iso(phrase: str | None, *, now: datetime | None = None) -> str | None:
    """Convenience: pass ``None`` through, otherwise :func:`to_iso`."""
    return None if phrase is None else to_iso(phrase, now=now)


_TIMEDELTA_UNITS: Final[dict[str, str]] = {
    "second": "seconds",
    "minute": "minutes",
    "hour": "hours",
    "day": "days",
    "week": "weeks",
}


def _unit_delta(n: int, unit: str) -> timedelta | relativedelta:
    if unit in _TIMEDELTA_UNITS:
        return timedelta(**{_TIMEDELTA_UNITS[unit]: n})
    # relativedelta has positional-then-kwarg overloads that mypy can't
    # narrow through dict unpacking, so spell these two cases explicitly.
    if unit == "month":
        return relativedelta(months=n)
    if unit == "year":
        return relativedelta(years=n)
    raise TimePhraseError(f"unsupported unit: {unit!r}")  # pragma: no cover


def _start_of_previous(now: datetime, unit: str) -> datetime:
    if unit == "hour":
        anchor = now.replace(minute=0, second=0, microsecond=0)
        return anchor - timedelta(hours=1)
    if unit == "day":
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=1)
    if unit == "week":
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Snap to Monday of the current week, then step back 7 days.
        this_monday = midnight - timedelta(days=midnight.weekday())
        return this_monday - timedelta(days=7)
    if unit == "month":
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return first_of_this_month - relativedelta(months=1)
    if unit == "year":
        first_of_this_year = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return first_of_this_year - relativedelta(years=1)
    raise TimePhraseError(f"unsupported unit: {unit!r}")  # pragma: no cover
