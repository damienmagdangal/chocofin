"""Period tests.

These are the tests that catch the two classic ledger bugs: resolving the
boundary in UTC instead of Manila (misfiles 8 hours of entries), and using a
closed interval instead of a half-open one (double-counts the boundary).
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from core.errors import PeriodError
from core.periods import MANILA, manila_today, resolve, to_utc

# Manila is UTC+08:00, so local midnight is 16:00Z on the PREVIOUS day.
MIDNIGHT_OFFSET = timedelta(hours=16)


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# --- the fixed-offset assumption -------------------------------------------


def test_manila_has_no_dst_in_either_half_of_the_year():
    """Canary. Asia/Manila has observed no DST since 1978.

    Every boundary here assumes a fixed +08:00. If the PH ever reintroduces
    DST, or tzdata changes, this fails loudly instead of quietly shifting
    period boundaries by an hour.
    """
    january = datetime(2026, 1, 15, tzinfo=MANILA).utcoffset()
    july = datetime(2026, 7, 15, tzinfo=MANILA).utcoffset()
    assert january == july == timedelta(hours=8)


# --- Manila-first conversion ------------------------------------------------


def test_day_starts_at_manila_midnight_not_utc_midnight():
    start, end = resolve("day", anchor=date(2026, 7, 28))
    assert start == utc(2026, 7, 27, 16, 0)
    assert end == utc(2026, 7, 28, 16, 0)


def test_parsed_date_becomes_manila_midnight():
    """@2026-07-28 is a Manila day, so it stores as 2026-07-27T16:00Z."""
    assert to_utc(date(2026, 7, 28)) == utc(2026, 7, 27, 16, 0)


def test_every_boundary_is_manila_midnight():
    """Whatever the period, both ends land on 16:00Z — never 00:00Z."""
    for kind, kwargs in [
        ("day", {"anchor": date(2026, 3, 9)}),
        ("week", {"anchor": date(2026, 3, 9)}),
        ("month", {"anchor": date(2026, 3, 9)}),
        ("quarter", {"anchor": date(2026, 3, 9)}),
        ("year", {"anchor": date(2026, 3, 9)}),
        ("custom", {"start": date(2026, 3, 1), "end": date(2026, 3, 31)}),
    ]:
        start, end = resolve(kind, **kwargs)
        assert (start.hour, start.minute) == (16, 0), kind
        assert (end.hour, end.minute) == (16, 0), kind


# --- half-open intervals ----------------------------------------------------


def test_month_interval_is_half_open_at_manila_midnight():
    """The defining test: the last instant of January belongs to January, and
    the first instant of February does not."""
    jan_start, jan_end = resolve("month", anchor=date(2026, 1, 15))
    feb_start, _ = resolve("month", anchor=date(2026, 2, 15))

    assert jan_end == feb_start  # no gap, no overlap

    last_instant_of_january = jan_end - timedelta(microseconds=1)
    assert jan_start <= last_instant_of_january < jan_end
    assert not (feb_start <= last_instant_of_january)

    # And the boundary instant itself is February's, not January's.
    assert not (jan_start <= jan_end < jan_end)
    assert feb_start <= jan_end


def test_adjacent_periods_never_overlap():
    for kind, a, b in [
        ("day", date(2026, 1, 31), date(2026, 2, 1)),
        ("month", date(2026, 1, 15), date(2026, 2, 15)),
        ("quarter", date(2026, 3, 31), date(2026, 4, 1)),
        ("year", date(2026, 6, 1), date(2027, 6, 1)),
    ]:
        _, first_end = resolve(kind, anchor=a)
        second_start, _ = resolve(kind, anchor=b)
        assert first_end == second_start, kind


# --- period shapes ----------------------------------------------------------


def test_week_starts_monday():
    # 2026-07-28 is a Tuesday; its week starts Monday 2026-07-27 Manila.
    start, end = resolve("week", anchor=date(2026, 7, 28))
    assert start == utc(2026, 7, 26, 16, 0)
    assert end == utc(2026, 8, 2, 16, 0)
    assert end - start == timedelta(days=7)


def test_week_anchored_on_its_own_monday_is_stable():
    monday = date(2026, 7, 27)
    assert resolve("week", anchor=monday) == resolve(
        "week", anchor=monday + timedelta(days=6)
    )


@pytest.mark.parametrize(
    ("anchor", "expected_start_month"),
    [
        (date(2026, 1, 5), 1),
        (date(2026, 3, 31), 1),
        (date(2026, 4, 1), 4),
        (date(2026, 9, 30), 7),
        (date(2026, 12, 25), 10),
    ],
)
def test_quarters_are_calendar_quarters(anchor, expected_start_month):
    start, _ = resolve("quarter", anchor=anchor)
    assert start.astimezone(MANILA).month == expected_start_month
    assert start.astimezone(MANILA).day == 1


def test_december_month_rolls_into_january():
    start, end = resolve("month", anchor=date(2026, 12, 9))
    assert start == utc(2026, 11, 30, 16, 0)
    assert end == utc(2026, 12, 31, 16, 0)


def test_leap_february_covers_29_days():
    start, end = resolve("month", anchor=date(2028, 2, 10))
    assert end - start == timedelta(days=29)


def test_year_covers_the_whole_year():
    start, end = resolve("year", anchor=date(2026, 6, 6))
    assert start == utc(2025, 12, 31, 16, 0)
    assert end == utc(2026, 12, 31, 16, 0)


def test_custom_includes_its_end_date():
    """An inclusive end date means the interval runs to the following midnight."""
    start, end = resolve("custom", start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert start == utc(2026, 6, 30, 16, 0)
    assert end == utc(2026, 7, 31, 16, 0)


def test_custom_single_day_is_one_day_long():
    start, end = resolve("custom", start=date(2026, 7, 1), end=date(2026, 7, 1))
    assert end - start == timedelta(days=1)


# --- rejections -------------------------------------------------------------


def test_custom_rejects_reversed_range():
    with pytest.raises(PeriodError):
        resolve("custom", start=date(2026, 7, 31), end=date(2026, 7, 1))


def test_custom_requires_both_ends():
    with pytest.raises(PeriodError):
        resolve("custom", start=date(2026, 7, 1))


def test_named_periods_require_an_anchor():
    with pytest.raises(PeriodError):
        resolve("month")


def test_unknown_kind_rejected():
    with pytest.raises(PeriodError):
        resolve("fortnight", anchor=date(2026, 7, 1))


# --- naive datetimes never escape or enter ----------------------------------


def test_every_boundary_is_timezone_aware():
    for kind, kwargs in [
        ("day", {"anchor": date(2026, 7, 28)}),
        ("week", {"anchor": date(2026, 7, 28)}),
        ("month", {"anchor": date(2026, 7, 28)}),
        ("quarter", {"anchor": date(2026, 7, 28)}),
        ("year", {"anchor": date(2026, 7, 28)}),
        ("custom", {"start": date(2026, 7, 1), "end": date(2026, 7, 2)}),
    ]:
        for boundary in resolve(kind, **kwargs):
            assert boundary.tzinfo is not None, kind
            assert boundary.utcoffset() == timedelta(0), f"{kind} not in UTC"


def test_manila_today_rejects_naive_datetime():
    with pytest.raises(PeriodError):
        manila_today(datetime(2026, 8, 3, 15, 30))


def test_manila_today_crosses_at_16_00_utc():
    """23:30 Manila and 00:30 Manila are different days despite being 1h apart."""
    assert manila_today(utc(2026, 8, 3, 15, 30)) == date(2026, 8, 3)
    assert manila_today(utc(2026, 8, 3, 16, 30)) == date(2026, 8, 4)


def test_manila_today_is_not_utc_today():
    """The bug this guards: using UTC's date for a Manila user's 'yesterday'."""
    late_evening_manila = utc(2026, 8, 3, 16, 30)
    assert manila_today(late_evening_manila) != late_evening_manila.date()
