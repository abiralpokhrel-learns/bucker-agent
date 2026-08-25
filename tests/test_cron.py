"""Cron engine tests (bucker.core.cron).

The lite scheduler's correctness rests on this module, so the tests are
table-driven and exhaustive about the rules that silently break real
schedulers: POSIX day-field union semantics, wrapping ranges, step values,
timezone handling, and the strictly-after contract of next_fire.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bucker.core.cron import CronError, matches, next_fire, parse, validate_cron

# ------------------------------------------------------------ parsing ----


def test_parse_simple_daily():
    spec = parse("0 9 * * *")
    assert spec.minutes == (0,)
    assert spec.hours == (9,)
    assert spec.months == tuple(range(1, 13))
    assert not spec.dom_restricted and not spec.dow_restricted


def test_parse_every_five_minutes():
    spec = parse("*/5 * * * *")
    assert spec.minutes == tuple(range(0, 60, 5))


def test_parse_lists_and_ranges():
    spec = parse("0,30 8-10 * * 1,3,5")
    assert spec.minutes == (0, 30)
    assert spec.hours == (8, 9, 10)
    assert spec.days_of_week == (1, 3, 5)
    assert spec.dow_restricted is True


def test_parse_names_case_insensitive():
    spec = parse("0 0 1 JAN MON")
    # JAN -> 1; MON -> 1
    assert spec.days_of_week == (1,)
    assert spec.months == (1,)


def test_parse_sunday_seven_normalizes_to_zero():
    spec = parse("0 0 * * 7")
    assert spec.days_of_week == (0,)


def test_parse_wrapping_range():
    # FRI-MON = Fri,Sat,Sun,Mon
    spec = parse("0 0 * * FRI-MON")
    assert spec.days_of_week == (0, 1, 5, 6)


def test_parse_stepped_range():
    spec = parse("10-30/10 * * * *")
    assert spec.minutes == (10, 20, 30)


def test_macros_expand():
    assert parse("@daily").expr == "0 0 * * *"
    assert parse("@WEEKLY").days_of_week == (0,)
    assert parse("@yearly").months == (1,)


def _invalid(expr: str, fragment: str) -> None:
    with pytest.raises(CronError, match=fragment):
        parse(expr)


@pytest.mark.parametrize("expr,fragment", [
    ("", "empty"),
    ("* * * *", "expected 5 fields"),
    ("* * * * * *", "expected 5 fields"),
    ("60 * * * *", "minute"),
    ("* 24 * * *", "hour"),
    ("0 0 32 * *", "day-of-month"),
    ("0 0 * 13 *", "month"),
    ("0 0 * * 8", "day-of-week"),
    ("*/0 * * * *", "step must be >= 1"),
    ("abc * * * *", "not a number"),
    ("0 0 * NOVX *", "not a number"),
])
def test_invalid_expressions_raise_with_field_names(expr, fragment):
    _invalid(expr, fragment)


def test_validate_cron_returns_none_or_message():
    assert validate_cron("*/10 * * * *") is None
    error = validate_cron("61 * * * *")
    assert error and "minute" in error


def test_timezone_prefix_parses():
    spec = parse("TZ=Asia/Kathmandu 0 9 * * 1-5")
    assert spec.tz_name == "Asia/Kathmandu"
    with pytest.raises(CronError, match="unknown timezone"):
        parse("TZ=Mars/Olympus 0 9 * * *")


# ------------------------------------------------------------ matching ----

UTC = UTC  # readability in the table below


@pytest.mark.parametrize("expr,moment,expected", [
    ("0 9 * * *", datetime(2026, 8, 22, 9, 0, tzinfo=UTC), True),
    ("0 9 * * *", datetime(2026, 8, 22, 9, 1, tzinfo=UTC), False),
    ("0 9 * * *", datetime(2026, 8, 22, 8, 59, tzinfo=UTC), False),
    ("*/15 * * * *", datetime(2026, 8, 22, 10, 45, tzinfo=UTC), True),
    ("*/15 * * * *", datetime(2026, 8, 22, 10, 46, tzinfo=UTC), False),
    ("0 0 * * 1", datetime(2026, 8, 17, 0, 0, tzinfo=UTC), True),   # a Monday
    ("0 0 * * 1", datetime(2026, 8, 18, 0, 0, tzinfo=UTC), False),  # Tuesday
])
def test_matches(expr, moment, expected):
    assert matches(expr, moment) is expected


def test_matches_posix_union_rule_both_day_fields():
    # dom=13 restricted AND dow=Friday restricted -> fires on EITHER.
    expr = "0 0 13 * 5"
    friday_the_13th = datetime(2026, 11, 13, 0, 0, tzinfo=UTC)  # Friday
    plain_friday = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)      # Friday, not 13th
    thirteenth = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)       # Tuesday the 13th
    assert matches(expr, friday_the_13th)
    assert matches(expr, plain_friday)
    assert matches(expr, thirteenth)
    neither = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)  # Tue Nov 3
    assert not matches(expr, neither)


def test_matches_intersection_when_only_one_day_field():
    expr = "0 0 1 * *"  # day-of-month only
    assert matches(expr, datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert not matches(expr, datetime(2026, 11, 2, 0, 0, tzinfo=UTC))


# ----------------------------------------------------------- next_fire ----


def test_next_fire_same_hour_later_minute():
    after = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
    nxt = next_fire("30 10 * * *", after)
    assert nxt == datetime(2026, 8, 22, 10, 30, tzinfo=UTC)


def test_next_fire_rolls_to_tomorrow():
    after = datetime(2026, 8, 22, 23, 59, tzinfo=UTC)
    nxt = next_fire("0 8 * * *", after)
    assert nxt == datetime(2026, 8, 23, 8, 0, tzinfo=UTC)


def test_next_fire_is_strictly_after():
    """A fire exactly AT `after` must not be returned — the caller just
    fired it; returning it again means double-running."""
    fire_time = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    nxt = next_fire("0 9 * * *", fire_time)
    assert nxt == datetime(2026, 8, 23, 9, 0, tzinfo=UTC)


def test_next_fire_skips_non_matching_days():
    after = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)  # Wednesday
    nxt = next_fire("0 0 * * 1", after)               # Mondays only
    assert nxt.weekday() == 0
    assert nxt > after


def test_next_fire_respects_timezone():
    kathmandu = "Asia/Kathmandu"  # UTC+05:45, no DST
    after = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
    nxt = next_fire(f"TZ={kathmandu} 30 9 * * *", after)
    assert nxt is not None
    # 09:30 NPT == 03:45 UTC same day
    assert nxt.astimezone(UTC).hour == 3
    assert nxt.astimezone(UTC).minute == 45


def test_next_fire_naive_input_assumed_utc():
    after = datetime(2026, 8, 22, 10, 0)
    nxt = next_fire("0 11 * * *", after)
    assert nxt is not None and nxt.utcoffset().total_seconds() == 0
    assert nxt.hour == 11


def test_next_fire_impossible_date_returns_none():
    # Feb 31 never exists.
    assert next_fire("0 0 31 2 *", datetime(2026, 1, 1, tzinfo=UTC)) is None


def test_next_fire_chain_is_monotonic():
    """Repeatedly advancing from each fire yields an increasing sequence —
    the exact loop the scheduler runs."""
    cursor = datetime(2026, 8, 1, tzinfo=UTC)
    seen = []
    for _ in range(10):
        cursor = next_fire("*/30 * * * *", cursor)
        seen.append(cursor)
    assert all(b > a for a, b in zip(seen, seen[1:], strict=False))
    assert all(m.minute in (0, 30) for m in seen)
