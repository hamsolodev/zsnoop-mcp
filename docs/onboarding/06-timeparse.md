# 6. Time parsing

## What

[`src/zsnoop_mcp/timeparse.py`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/src/zsnoop_mcp/timeparse.py) — turns
human time phrases (`"yesterday"`, `"3 days ago"`, `"last week"`) into
absolute ISO 8601 timestamps the agent understands.

## Why parse locally

We considered shipping the parser to the agent and decided against it:

| Reason | Decision |
| --- | --- |
| The agent must stay **stdlib-only**. `python-dateutil` is a dep. | Keep it local. |
| Time-phrase semantics are LLM-facing — should be **consistent across hosts**. | Single implementation, no per-host drift. |
| The agent should have a **minimal, contractual schema** (ISO 8601). | Easier to test, easier to swap the agent for a Rust binary later. |

## How — guided tour

### The supported grammar

Three kinds of input:

```python
"now"                            # current wall-clock
"today"                          # 00:00:00 of the current day
"yesterday"                      # 00:00:00 of the previous day
"N seconds|minutes|hours|days|weeks|months|years ago"
"last hour|day|week|month|year"  # the previous bucket of that size
"2026-05-12T14:30:00+00:00"      # raw ISO 8601 passes through
```

All case-insensitive, whitespace-tolerant. Anything else raises
`TimePhraseError`.

### Implementation shape

```python
def parse_phrase(phrase: str, *, now: datetime | None = None) -> datetime:
    if not isinstance(phrase, str):
        raise TimePhraseError(...)
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        raise TimePhraseError("now must be timezone-aware")
    text = phrase.strip().lower()
    if text == "now":       return now
    if text == "today":     return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "yesterday": return ... - timedelta(days=1)
    if m := _AGO_RE.match(phrase):       return now - _unit_delta(int(m.group(1)), m.group(2).lower())
    if m := _LAST_RE.match(phrase):      return _start_of_previous(now, m.group(1).lower())
    # Final fallback: ISO 8601.
    try:
        dt = datetime.fromisoformat(phrase)
    except ValueError as e:
        raise TimePhraseError(...) from e
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
```

The dependency-injectable `now` parameter is **critical for testing**.
Every test in
[tests/test_timeparse.py]({{ config.repo_url }}/src/branch/{{ repo_branch }}/tests/test_timeparse.py) passes a fixed
`NOW = datetime(2026, 5, 13, 14, 30, 0, tzinfo=UTC)` so results don't
drift with the wall clock.

### Calendar arithmetic — months and years

For sub-month units, `timedelta` is fine (`days=`, `hours=`, etc.). But
"2 months ago" can't be `timedelta(days=60)` — months vary. We pull in
`dateutil.relativedelta`:

```python
def _unit_delta(n: int, unit: str) -> timedelta | relativedelta:
    if unit in _TIMEDELTA_UNITS:
        return timedelta(**{_TIMEDELTA_UNITS[unit]: n})
    if unit == "month":
        return relativedelta(months=n)
    if unit == "year":
        return relativedelta(years=n)
    raise TimePhraseError(...)
```

(The explicit `if unit == "month"` / `"year"` branches are because mypy
can't narrow `relativedelta`'s overloads through `**dict` unpacking — see
the comment in the file.)

### The `last <unit>` semantics

A subtle one: "last week" doesn't mean "7 days ago". It means "the
previous calendar week, anchored at Monday 00:00".

```python
def _start_of_previous(now: datetime, unit: str) -> datetime:
    if unit == "week":
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        this_monday = midnight - timedelta(days=midnight.weekday())
        return this_monday - timedelta(days=7)
    ...
```

For Wednesday 2026-05-13, "last week" = Monday 2026-05-04 00:00. Test:
[`test_last_week_is_previous_monday_midnight`]({{ config.repo_url }}/src/branch/{{ repo_branch }}/tests/test_timeparse.py).

## What to read next

→ [Testing patterns](07-testing.md) — including how the dependency-injected
`now` pattern keeps these tests deterministic.
