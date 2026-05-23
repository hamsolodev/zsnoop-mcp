"""Tests for the human time-phrase parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zsnoop_mcp.timeparse import TimePhraseError, parse_phrase, to_iso

# Wednesday, 2026-05-13 14:30:00 UTC — a stable anchor used across tests.
NOW = datetime(2026, 5, 13, 14, 30, 0, tzinfo=UTC)


def test_now_returns_now() -> None:
    assert parse_phrase("now", now=NOW) == NOW


def test_today_is_midnight() -> None:
    assert parse_phrase("today", now=NOW) == NOW.replace(hour=0, minute=0, second=0)


def test_yesterday_is_previous_midnight() -> None:
    assert parse_phrase("yesterday", now=NOW) == datetime(2026, 5, 12, 0, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("phrase", "expected_delta"),
    [
        ("5 minutes ago", timedelta(minutes=5)),
        ("1 minute ago", timedelta(minutes=1)),
        ("3 hours ago", timedelta(hours=3)),
        ("2 days ago", timedelta(days=2)),
        ("1 week ago", timedelta(weeks=1)),
        ("90 seconds ago", timedelta(seconds=90)),
    ],
)
def test_ago_phrases(phrase: str, expected_delta: timedelta) -> None:
    assert parse_phrase(phrase, now=NOW) == NOW - expected_delta


def test_months_ago_uses_calendar_arithmetic() -> None:
    # NOW = May 13 2026. 2 months ago = March 13 2026.
    expected = datetime(2026, 3, 13, 14, 30, 0, tzinfo=UTC)
    assert parse_phrase("2 months ago", now=NOW) == expected


def test_years_ago_uses_calendar_arithmetic() -> None:
    assert parse_phrase("1 year ago", now=NOW) == datetime(2025, 5, 13, 14, 30, 0, tzinfo=UTC)


def test_last_hour_is_top_of_previous_hour() -> None:
    assert parse_phrase("last hour", now=NOW) == datetime(2026, 5, 13, 13, 0, 0, tzinfo=UTC)


def test_last_day_is_midnight_yesterday() -> None:
    assert parse_phrase("last day", now=NOW) == datetime(2026, 5, 12, 0, 0, 0, tzinfo=UTC)


def test_last_week_is_previous_monday_midnight() -> None:
    # NOW is Wednesday 2026-05-13. Monday of this week is 2026-05-11.
    # Previous Monday is 2026-05-04.
    assert parse_phrase("last week", now=NOW) == datetime(2026, 5, 4, 0, 0, 0, tzinfo=UTC)


def test_last_month_is_first_of_previous_month() -> None:
    assert parse_phrase("last month", now=NOW) == datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)


def test_last_year_is_jan_1_of_previous_year() -> None:
    assert parse_phrase("last year", now=NOW) == datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_iso_8601_pass_through() -> None:
    result = parse_phrase("2026-01-15T08:30:00+00:00", now=NOW)
    assert result == datetime(2026, 1, 15, 8, 30, 0, tzinfo=UTC)


def test_naive_iso_becomes_utc() -> None:
    result = parse_phrase("2026-01-15T08:30:00", now=NOW)
    assert result == datetime(2026, 1, 15, 8, 30, 0, tzinfo=UTC)


def test_case_insensitive() -> None:
    assert parse_phrase("YESTERDAY", now=NOW) == parse_phrase("yesterday", now=NOW)
    assert parse_phrase("5 Days Ago", now=NOW) == parse_phrase("5 days ago", now=NOW)


def test_whitespace_tolerated() -> None:
    assert parse_phrase("  yesterday  ", now=NOW) == parse_phrase("yesterday", now=NOW)


def test_unparseable_phrase_raises() -> None:
    with pytest.raises(TimePhraseError):
        parse_phrase("sometime around when the dog barked", now=NOW)


def test_non_string_input_raises() -> None:
    with pytest.raises(TimePhraseError):
        parse_phrase(12345, now=NOW)  # type: ignore[arg-type]


def test_naive_now_rejected() -> None:
    with pytest.raises(TimePhraseError, match="timezone-aware"):
        parse_phrase("now", now=datetime(2026, 1, 1))


def test_to_iso_returns_isoformat_string() -> None:
    iso = to_iso("yesterday", now=NOW)
    assert iso == "2026-05-12T00:00:00+00:00"
