"""Resolve named periods into half-open UTC intervals.

Two rules, both load-bearing:

1. Boundaries resolve in Asia/Manila FIRST, then convert to UTC. Doing it the
   other way round lands the boundary at 08:00 Manila instead of midnight, and
   silently misfiles eight hours of entries into the neighbouring period.
2. Intervals are half-open, [start, end). The last instant of a period belongs
   to that period; the first instant of the next does not. Closed intervals
   double-count anything landing exactly on a boundary.

Asia/Manila has observed no DST since 1978 and is a fixed UTC+08:00, so there
are no transitions to straddle. `tests/test_periods.py` keeps a canary on that
assumption rather than trusting it forever.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from core.errors import PeriodError

MANILA = ZoneInfo("Asia/Manila")

PeriodKind = Literal["day", "week", "month", "quarter", "year", "custom"]


def _manila_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    """The instant local midnight of `day` begins, expressed in UTC."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def _add_months(day: date, months: int) -> date:
    """Shift `day` by whole months, clamping to the 1st.

    Only ever called with day-of-month 1, so no end-of-month clamping is needed.
    """
    total = (day.year * 12 + day.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def day_bounds(anchor: date, *, tz: ZoneInfo = MANILA) -> tuple[datetime, datetime]:
    return _manila_midnight_utc(anchor, tz), _manila_midnight_utc(
        anchor + timedelta(days=1), tz
    )


def week_bounds(anchor: date, *, tz: ZoneInfo = MANILA) -> tuple[datetime, datetime]:
    """ISO-8601 week: Monday through Sunday."""
    start = anchor - timedelta(days=anchor.weekday())
    return _manila_midnight_utc(start, tz), _manila_midnight_utc(
        start + timedelta(days=7), tz
    )


def month_bounds(anchor: date, *, tz: ZoneInfo = MANILA) -> tuple[datetime, datetime]:
    start = anchor.replace(day=1)
    return _manila_midnight_utc(start, tz), _manila_midnight_utc(
        _add_months(start, 1), tz
    )


def quarter_bounds(anchor: date, *, tz: ZoneInfo = MANILA) -> tuple[datetime, datetime]:
    """Calendar quarters: Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec."""
    start = date(anchor.year, 3 * ((anchor.month - 1) // 3) + 1, 1)
    return _manila_midnight_utc(start, tz), _manila_midnight_utc(
        _add_months(start, 3), tz
    )


def year_bounds(anchor: date, *, tz: ZoneInfo = MANILA) -> tuple[datetime, datetime]:
    return _manila_midnight_utc(date(anchor.year, 1, 1), tz), _manila_midnight_utc(
        date(anchor.year + 1, 1, 1), tz
    )


def custom_bounds(
    start: date, end: date, *, tz: ZoneInfo = MANILA
) -> tuple[datetime, datetime]:
    """Half-open interval from local midnight of `start` to local midnight of
    the day AFTER `end`, so `end` is fully included as a user would expect.
    """
    if end < start:
        raise PeriodError(f"end {end} precedes start {start}")
    return _manila_midnight_utc(start, tz), _manila_midnight_utc(
        end + timedelta(days=1), tz
    )


_BOUNDS = {
    "day": day_bounds,
    "week": week_bounds,
    "month": month_bounds,
    "quarter": quarter_bounds,
    "year": year_bounds,
}


def resolve(
    kind: PeriodKind,
    *,
    anchor: date | None = None,
    start: date | None = None,
    end: date | None = None,
    tz: ZoneInfo = MANILA,
) -> tuple[datetime, datetime]:
    """Resolve a period into a half-open [start_utc, end_utc).

    `anchor` is any date inside the wanted period. `custom` takes `start` and
    `end` instead, both inclusive in local terms.
    """
    if kind == "custom":
        if start is None or end is None:
            raise PeriodError("custom periods need both start and end")
        return custom_bounds(start, end, tz=tz)

    if anchor is None:
        raise PeriodError(f"{kind} periods need an anchor date")

    try:
        return _BOUNDS[kind](anchor, tz=tz)
    except KeyError:
        raise PeriodError(f"unknown period kind: {kind!r}") from None


def manila_today(now: datetime, *, tz: ZoneInfo = MANILA) -> date:
    """The current date in Manila, given an aware instant.

    The parser needs a Manila-local `today` for @yesterday; this is the only
    supported way to derive it. Rejects naive datetimes rather than assuming
    they are UTC.
    """
    if now.tzinfo is None:
        raise PeriodError("naive datetime — every instant must carry a timezone")
    return now.astimezone(tz).date()


def to_utc(day: date, *, tz: ZoneInfo = MANILA) -> datetime:
    """Local midnight of `day` as a UTC instant.

    This is how a parsed `@2026-07-28` becomes a storable `occurred_at`:
    2026-07-28 in Manila is 2026-07-27T16:00Z.
    """
    return _manila_midnight_utc(day, tz)


def occurred_at_utc(
    day: date | None, now: datetime, *, tz: ZoneInfo = MANILA
) -> datetime:
    """When the money moved, as a UTC instant.

    The rule every adapter needs and none of them may own: a parsed `@date` —
    or the absence of one — plus the current instant in, an `occurred_at` out.

    No date, or TODAY's date, means `now`. The user is logging something that
    just happened, and midnight is between eight and twenty-four hours in the
    past: an entry typed at 22:00 would be filed at 00:00 that morning, and
    nothing on screen would say so, because a date line is only shown for an
    entry that is not for today.

    Any other date lands on local midnight of that day. The user named a DAY,
    not a second, and midnight is the only instant that does not invent a time
    they did not give. It is also stable — the same `@2026-08-01` always
    resolves to the same instant, so period boundaries cannot wobble.

    `@today` and an explicit `@<today's date>` are indistinguishable once
    `core.parser` is done with them, and are deliberately treated alike. Both
    stay inside the same Manila day, so no total moves either way.

    "Today" is a Manila question. Comparing UTC dates would call anything
    logged after 08:00 Manila "not today" for the last eight hours of every
    day. `manila_today` answers it, and rejects a naive `now` on the way — it
    is resolved even in the undated case, which needs no comparison, so that a
    naive `now` can never be returned as an `occurred_at` and stored in a
    TIMESTAMPTZ column as if it were UTC.
    """
    today = manila_today(now, tz=tz)
    if day is None or day == today:
        return now
    return to_utc(day, tz=tz)
