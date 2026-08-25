"""Pure 5-field cron parsing and next-fire computation.

[HAND] — the scheduler's correctness rests entirely on this module: a
silently-wrong ``next_fire`` either skips runs nobody notices or fires twice
nobody wanted. It is pure (datetime in, datetime out, no clock reads), which
is what makes it exhaustively testable — every rule below has a table-driven
test, and the scheduler composes these primitives instead of having its own
cron opinions.

Supported syntax (standard vixie-cron subset):

    *               any value
    */step          every ``step``-th value from the field minimum
    a-b             inclusive range (wrapping allowed: FRI-MON)
    a-b/step        stepped range
    a,b,c           lists of any of the above
    JAN/SUN/MON..   month and weekday names, case-insensitive
    7               accepted for Sunday (normalized to 0)

An optional timezone prefix selects the wall-clock the expression evaluates
in — ``TZ=Asia/Kathmandu 0 9 * * 1-5`` means 9am in Kathmandu. Default is
UTC, matching the Temporal scheduler's behavior on the full stack so a
schedule behaves the same in lite mode and full mode.

POSIX day-of-month / day-of-week semantics, implemented exactly: when BOTH
fields are restricted, a date matching EITHER fires (union); when only one is
restricted, it alone decides. Getting this backwards is the classic silent
scheduler bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

#: Search horizon for next_fire. A cron that never matches inside four years
#: (Feb 30, "0 0 31 2 *" ...) has no meaningful next fire; None is honest.
_MAX_SCAN_DAYS = 366 * 4

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_WEEKDAY_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

_FIELD_RANGES = (
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 6),
)

#: @macros from crontab(5), expanded exactly as vixie expands them.
_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_TZ_PREFIX = re.compile(r"^TZ[=\s]+(\S+)\s+(.*)$", re.IGNORECASE | re.DOTALL)


class CronError(ValueError):
    """The expression is not valid cron syntax. Message names the field."""


@dataclass(frozen=True, slots=True)
class CronSpec:
    """A parsed expression: the value sets plus the POSIX restriction flags."""

    expr: str
    tz_name: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days_of_month: tuple[int, ...]
    months: tuple[int, ...]
    days_of_week: tuple[int, ...]
    #: True when the field was something other than bare ``*`` — the inputs
    #: to the POSIX union/intersection rule for day matching.
    dom_restricted: bool
    dow_restricted: bool

    def describe(self) -> str:
        """One-line human description for dashboards and CLI output."""
        if self.minutes == (0,) and self.hours == (0,) and not self.dom_restricted \
                and not self.dow_restricted and self.months == tuple(range(1, 13)):
            return "daily at midnight (server time)"
        parts = [f"minute {', '.join(map(str, self.minutes))}"]
        if len(set(self.hours)) > 1 or self.hours != (0,):
            parts.append(f"hour {', '.join(map(str, self.hours))}")
        if self.months != tuple(range(1, 13)):
            parts.append(f"month {', '.join(map(str, self.months))}")
        if self.dom_restricted:
            parts.append(f"day-of-month {', '.join(map(str, self.days_of_month))}")
        if self.dow_restricted:
            parts.append(f"day-of-week {', '.join(map(str, self.days_of_week))}")
        return "; ".join(parts) + f" ({self.tz_name})"


def _expand_field(raw: str, index: int) -> tuple[set[int], bool]:
    """Expand one cron field into its value set. Returns (values, restricted).

    ``restricted`` is False only for a bare ``*`` (or ``*/1``, which is the
    same thing semantically but still counts as written — vixie treats
    ``*`` vs ``*/1`` identically for the union rule, so do the same).
    """
    lo_bound, hi_bound = _FIELD_RANGES[index]
    values: set[int] = set()
    restricted = False

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{_FIELD_NAMES[index]}: empty list element in {raw!r}")

        step = 1
        body = part
        if "/" in part:
            body, _, step_raw = part.partition("/")
            try:
                step = int(step_raw)
            except ValueError:
                raise CronError(
                    f"{_FIELD_NAMES[index]}: bad step {step_raw!r}"
                ) from None
            if step < 1:
                raise CronError(f"{_FIELD_NAMES[index]}: step must be >= 1")
            if not body:
                raise CronError(f"{_FIELD_NAMES[index]}: missing range before '/'")

        if body == "*":
            start, end = lo_bound, hi_bound
        elif "-" in body.lstrip("-"):
            range_body = body
            start_raw, sep, end_raw = range_body.partition("-")
            start = _parse_value(start_raw, index)
            # A trailing '-' with nothing after it is malformed, not "to max".
            if not sep:
                raise CronError(f"{_FIELD_NAMES[index]}: unterminated range {body!r}")
            end = _parse_value(end_raw, index)
        else:
            single = _parse_value(body, index)
            start, end = single, single

        if start <= end:
            values.update(range(start, end + 1, step))
        else:
            # Wrapping range (FRI-MON, DEC-FEB): vixie expands across the
            # boundary rather than rejecting.
            values.update(range(start, hi_bound + 1, step))
            offset = (start - lo_bound) % step
            values.update(range(lo_bound + ((offset - lo_bound) % step),
                                end + 1, step))

    if raw.strip() != "*" or "/" in raw:
        restricted = True

    # Only cron's documented "7 also means Sunday" alias is normalized;
    # anything else out of range must fail loudly, not silently wrap
    # (8 % 7 would have quietly meant Monday).
    normalized = {0 if v == 7 else v for v in values} if index == 4 else values
    for v in normalized:
        if not (lo_bound <= v <= hi_bound):
            raise CronError(f"{_FIELD_NAMES[index]}: {v} out of range "
                            f"[{lo_bound}-{hi_bound}]")
    return normalized, restricted


def _parse_value(token: str, index: int) -> int:
    """Parse one endpoint value: number, month name, or weekday name."""
    cleaned = token.strip()
    lowered = cleaned.lower()
    if index == 3 and lowered in _MONTH_NAMES:
        return _MONTH_NAMES[lowered]
    if index == 4 and lowered in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES[lowered]
    try:
        return int(cleaned)
    except ValueError:
        raise CronError(
            f"{_FIELD_NAMES[index]}: {cleaned!r} is not a number or known name"
        ) from None


def parse(expr: str) -> CronSpec:
    """Parse a cron expression (optionally ``TZ=<name>``-prefixed).

    Raises CronError with a field-specific message on anything malformed —
    callers surface that text directly to users.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise CronError("empty cron expression")

    text = _MACROS.get(expr.strip().lower(), expr.strip())

    tz_name = "UTC"
    tz_match = _TZ_PREFIX.match(text)
    if tz_match:
        tz_name = tz_match.group(1)
        text = tz_match.group(2).strip()
        try:
            ZoneInfo(tz_name)  # validation only; next_fire re-derives it
        except Exception as exc:
            raise CronError(f"unknown timezone {tz_name!r}") from exc

    fields = text.split()
    if len(fields) != 5:
        raise CronError(
            f"expected 5 fields (minute hour dom month dow), got {len(fields)}: "
            f"{text!r}"
        )

    minutes, m_res = _expand_field(fields[0], 0)
    hours, h_res = _expand_field(fields[1], 1)
    dom, dom_res = _expand_field(fields[2], 2)
    months, mon_res = _expand_field(fields[3], 3)
    dow, dow_res = _expand_field(fields[4], 4)
    del m_res, h_res, mon_res  # only day fields feed the union rule

    return CronSpec(
        expr=text,
        tz_name=tz_name,
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days_of_month=tuple(sorted(dom)),
        months=tuple(sorted(months)),
        days_of_week=tuple(sorted(dow)),
        dom_restricted=dom_res,
        dow_restricted=dow_res,
    )


def validate_cron(expr: str) -> str | None:
    """Return an error string for an invalid expression, else None."""
    try:
        parse(expr)
    except CronError as exc:
        return str(exc)
    return None


def _date_matches(spec: CronSpec, day: datetime) -> bool:
    """POSIX day matching: union when both day fields are restricted."""
    if day.month not in spec.months:
        return False
    cron_dow = (day.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0
    dom_ok = day.day in spec.days_of_month
    dow_ok = cron_dow in spec.days_of_week
    if spec.dom_restricted and spec.dow_restricted:
        return dom_ok or dow_ok
    if spec.dom_restricted:
        return dom_ok
    if spec.dow_restricted:
        return dow_ok
    return True


def next_fire(expr: str | CronSpec, after: datetime) -> datetime | None:
    """The next instant the expression fires strictly AFTER ``after``.

    Evaluated in the expression's timezone (default UTC); the result carries
    that tzinfo. Returns None when nothing matches within four years — the
    caller decides whether that is an error for their purposes.
    """
    spec = expr if isinstance(expr, CronSpec) else parse(expr)
    tz = ZoneInfo(spec.tz_name)

    local_after = after.astimezone(tz) if after.tzinfo else after.replace(tzinfo=tz)
    # Candidates start at the next whole minute: ``after`` itself is excluded
    # (strictly-after contract) and sub-minute precision does not exist in cron.
    cursor_date = (local_after + timedelta(minutes=1)).date()
    first_time = (local_after + timedelta(minutes=1)).timetz()

    minute_slots = [
        (h, m) for h in spec.hours for m in spec.minutes
    ]

    for day_offset in range(_MAX_SCAN_DAYS):
        day = cursor_date + timedelta(days=day_offset)
        probe = datetime(day.year, day.month, day.day, tzinfo=tz)
        if not _date_matches(spec, probe):
            continue
        for h, m in minute_slots:
            candidate = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
            floor = first_time.replace(tzinfo=tz) if day_offset == 0 else None
            if floor is not None and candidate.timetz() < floor:
                continue
            return candidate
    return None


def matches(expr: str | CronSpec, moment: datetime) -> bool:
    """True when the expression fires AT ``moment`` (minute precision)."""
    spec = expr if isinstance(expr, CronSpec) else parse(expr)
    tz = ZoneInfo(spec.tz_name)
    local = moment.astimezone(tz) if moment.tzinfo else moment.replace(tzinfo=tz)
    if local.second not in (0, None):
        return False
    if local.minute not in spec.minutes or local.hour not in spec.hours:
        return False
    return _date_matches(spec, local)
