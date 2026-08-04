"""Formatting is where centavos become characters. No database needed."""

from __future__ import annotations

import datetime as dt

import pytest

from bot.formatting import (
    account_label,
    esc,
    format_date,
    format_datetime,
    format_minor,
    format_signed_minor,
    to_manila,
)


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        (0, "₱0.00"),
        (1, "₱0.01"),
        (50, "₱0.50"),
        (100, "₱1.00"),
        (12_050, "₱120.50"),
        (120_050, "₱1,200.50"),
        (100_000_000, "₱1,000,000.00"),
        # A card you owe on has a negative balance. The sign belongs in front
        # of the currency, not between it and the digits.
        (-300_000, "-₱3,000.00"),
        (-1, "-₱0.01"),
    ],
)
def test_format_minor(minor: int, expected: str):
    assert format_minor(minor) == expected


def test_format_minor_never_loses_a_centavo():
    """The exact failure a float would introduce.

    `1234.56 * 100` is 123455.99999999999 in binary floating point. Formatting
    goes the other way, but any round trip through float would show up here as
    an off-by-one centavo.
    """
    for minor in range(0, 10_000):
        text = format_minor(minor)
        pesos, _, centavos = text.removeprefix("₱").partition(".")
        assert int(pesos.replace(",", "")) * 100 + int(centavos) == minor


def test_format_signed_minor_shows_direction():
    assert format_signed_minor(12_000) == "+₱120.00"
    assert format_signed_minor(-12_000) == "-₱120.00"
    assert format_signed_minor(0) == "+₱0.00"


def test_manila_is_utc_plus_eight():
    """23:00 UTC is already tomorrow in Manila.

    An expense logged at 07:30 Manila is stored as 23:30 UTC the previous day.
    Displaying the stored date without converting would tell the user they
    spent it yesterday, which is the whole reason this conversion exists.
    """
    late = dt.datetime(2026, 8, 3, 23, 30, tzinfo=dt.UTC)
    local = to_manila(late)
    assert (local.year, local.month, local.day) == (2026, 8, 4)
    assert local.hour == 7
    assert format_date(late) == "4 Aug 2026"


def test_format_datetime_uses_twelve_hour_manila_time():
    # 06:15 UTC is 14:15 Manila.
    moment = dt.datetime(2026, 8, 4, 6, 15, tzinfo=dt.UTC)
    assert format_datetime(moment) == "4 Aug 2026, 2:15 PM"


def test_format_datetime_renders_midnight_and_noon():
    """12-hour clocks are where off-by-twelve bugs live."""
    # 16:00 UTC on the 3rd is Manila midnight on the 4th.
    midnight = dt.datetime(2026, 8, 3, 16, 0, tzinfo=dt.UTC)
    assert format_datetime(midnight) == "4 Aug 2026, 12:00 AM"

    noon = dt.datetime(2026, 8, 4, 4, 0, tzinfo=dt.UTC)
    assert format_datetime(noon) == "4 Aug 2026, 12:00 PM"


def test_format_date_has_no_leading_zero():
    assert format_date(dt.datetime(2026, 8, 4, 4, 0, tzinfo=dt.UTC)) == "4 Aug 2026"


def test_esc_neutralises_html_in_user_text():
    """Note text is whatever someone typed, and it is rendered as HTML.

    An unescaped "<b>" would either style the message or make Telegram reject
    it outright, and the note is the one field the user controls completely.

    Apostrophes are left alone: Telegram's HTML mode needs only `<`, `>` and
    `&` escaped in text, and turning every "Jerry's" into "Jerry&#x27;s" would
    be visible in the message.
    """
    assert (
        esc("Ben & Jerry's <b>sale</b>") == "Ben &amp; Jerry's &lt;b&gt;sale&lt;/b&gt;"
    )


def test_account_label_carries_emoji():
    """Which is exactly why a label never becomes callback_data."""
    label = account_label("BPI", "bank")
    assert "BPI" in label
    assert len(label.encode("utf-8")) > len(label)


def test_account_label_survives_an_unknown_type():
    assert account_label("Mystery", "not_a_type") == "Mystery"
