"""Parser tests — one case per planned edge case, every rejection included.

Every non-ASCII character in this file is written as an escape sequence on
purpose. Raw NBSPs, full-width digits and emoji are invisible in a diff and
editors silently normalize some of them, which would quietly disarm the exact
tests that exist to catch them.
"""

from datetime import date

import pytest

from core.parser import (
    MAX_AMOUNT_MINOR,
    ParsedEntry,
    ParseError,
    ParseErrorCode,
    parse,
)

TODAY = date(2026, 8, 3)

NBSP = " "
FULLWIDTH_100 = "１００"
ARABIC_INDIC_100 = "١٠٠"
PESO = "₱"
COFFEE = "☕"


def ok(raw: str) -> ParsedEntry:
    result = parse(raw, today=TODAY)
    assert isinstance(result, ParsedEntry), f"expected success, got {result}"
    return result


def err(raw: str) -> ParseErrorCode:
    result = parse(raw, today=TODAY)
    assert isinstance(result, ParseError), f"expected failure, got {result}"
    return result.code


# --- the six examples from the brief -------------------------------------


@pytest.mark.parametrize(
    ("raw", "amount", "note", "occurred_on", "tags"),
    [
        ("100 coffee", 10_000, "coffee", None, ()),
        ("/expense 100 coffee", 10_000, "coffee", None, ()),
        ("1,250.50 groceries", 125_050, "groceries", None, ()),
        ("320 lunch @yesterday", 32_000, "lunch", date(2026, 8, 2), ()),
        ("320 lunch @2026-07-28", 32_000, "lunch", date(2026, 7, 28), ()),
        ("45000 salary #bonus", 4_500_000, "salary", None, ("bonus",)),
    ],
)
def test_brief_examples(raw, amount, note, occurred_on, tags):
    entry = ok(raw)
    assert entry.amount_minor == amount
    assert entry.note == note
    assert entry.occurred_on == occurred_on
    assert entry.tags == tags
    assert entry.raw == raw


# --- amounts --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1250", 125_000),
        ("1250.5", 125_050),  # one decimal digit pads to centavos
        ("100.00", 10_000),
        (".50 coffee", 50),  # leading dot
        ("1,250.50", 125_050),
        ("100", 10_000),  # no note at all
        ("P100 x", 10_000),
        ("PHP 100 x", 10_000),  # currency as its own token
        ("php100 x", 10_000),
        (PESO + "100 x", 10_000),
    ],
)
def test_amount_forms(raw, expected):
    assert ok(raw).amount_minor == expected


def test_amount_is_positional_not_searched():
    """The first token is the amount. A later number stays in the note."""
    entry = ok("100 coffee 200")
    assert entry.amount_minor == 10_000
    assert entry.note == "coffee 200"


def test_bare_amount_defaults_to_expense():
    assert ok("100 coffee").kind == "expense"


def test_income_requires_the_command():
    """'45000 salary' is an EXPENSE — nothing in the text marks it as income."""
    assert ok("45000 salary #bonus").kind == "expense"
    assert ok("/income 45000 salary #bonus").kind == "income"


def test_largest_accepted_amount():
    pesos = MAX_AMOUNT_MINOR // 100
    assert ok(str(pesos)).amount_minor == pesos * 100


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("1250.505", ParseErrorCode.SUB_CENTAVO),  # never round
        ("100.", ParseErrorCode.BAD_AMOUNT),
        ("-100 coffee", ParseErrorCode.NEGATIVE_AMOUNT),
        ("0 coffee", ParseErrorCode.ZERO_AMOUNT),
        ("0.00 coffee", ParseErrorCode.ZERO_AMOUNT),
        ("coffee 100", ParseErrorCode.NO_AMOUNT),  # never guess position
        ("100k", ParseErrorCode.NO_AMOUNT),  # never expand shorthand
        ("1.5k", ParseErrorCode.NO_AMOUNT),
        ("", ParseErrorCode.EMPTY),
        ("   ", ParseErrorCode.EMPTY),
    ],
)
def test_amount_rejections(raw, code):
    assert err(raw) == code


def test_amount_overflow_rejected():
    assert err(str(MAX_AMOUNT_MINOR)) == ParseErrorCode.AMOUNT_OVERFLOW


@pytest.mark.parametrize(
    "raw",
    [
        FULLWIDTH_100 + " coffee",
        ARABIC_INDIC_100 + " coffee",
    ],
)
def test_non_ascii_digits_rejected(raw):
    """int() accepts these silently — a money-corruption path, so reject.

    Note this is also why the parser does NOT run NFKC normalization: NFKC
    rewrites full-width digits to ASCII, which would launder the input past
    this check.
    """
    assert err(raw) == ParseErrorCode.NON_ASCII_DIGITS


# --- command prefixes -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("/expense 100 coffee", "expense"),
        ("/income 100 salary", "income"),
        ("/Expense 100 coffee", "expense"),  # case-insensitive
        ("/expense@ChocoFinBot 100 coffee", "expense"),  # group-chat form
    ],
)
def test_command_prefix(raw, kind):
    assert ok(raw).kind == kind


def test_command_with_no_amount():
    assert err("/expense") == ParseErrorCode.NO_AMOUNT


def test_transfer_is_not_parseable_from_text():
    """A transfer needs two accounts; text alone cannot supply them."""
    assert err("/transfer 500 to savings") == ParseErrorCode.TRANSFER_NOT_PARSEABLE


def test_unknown_command_is_not_an_amount():
    assert err("/frobnicate 100") == ParseErrorCode.NO_AMOUNT


# --- dates ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("@today", date(2026, 8, 3)),
        ("@yesterday", date(2026, 8, 2)),
        ("@tomorrow", date(2026, 8, 4)),  # future-dating is legal
        ("@2026-07-28", date(2026, 7, 28)),
    ],
)
def test_date_tokens(token, expected):
    assert ok(f"100 lunch {token}").occurred_on == expected


def test_date_token_may_appear_mid_string():
    entry = ok("100 @yesterday lunch")
    assert entry.occurred_on == date(2026, 8, 2)
    assert entry.note == "lunch"


def test_no_date_means_now():
    assert ok("100 coffee").occurred_on is None


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("100 x @07/28", ParseErrorCode.AMBIGUOUS_DATE),
        ("100 x @28-07", ParseErrorCode.AMBIGUOUS_DATE),
        ("100 x @2026-02-30", ParseErrorCode.INVALID_DATE),
        ("100 x @2026-13-01", ParseErrorCode.INVALID_DATE),
        ("100 x @today @yesterday", ParseErrorCode.MULTIPLE_DATES),
    ],
)
def test_date_rejections(raw, code):
    assert err(raw) == code


def test_email_is_not_a_date_token():
    """'@' mid-word belongs to the text, not to the date grammar."""
    entry = ok("100 paid ana@gmail.com")
    assert entry.occurred_on is None
    assert entry.note == "paid ana@gmail.com"


# --- tags -----------------------------------------------------------------


def test_tags_are_lowercased_deduped_and_ordered():
    entry = ok("100 lunch #Bonus #bonus #Food")
    assert entry.tags == ("bonus", "food")
    assert entry.note == "lunch"


def test_non_ascii_tags_accepted():
    """The 64-byte callback_data limit is a bot concern, not a tag concern."""
    assert ok("100 x #kape" + COFFEE).tags == ("kape" + COFFEE,)


def test_tag_rejections():
    assert err("100 x #") == ParseErrorCode.EMPTY_TAG
    assert err("100 x #" + "a" * 33) == ParseErrorCode.TAG_TOO_LONG


def test_hash_mid_word_is_not_a_tag():
    assert ok("100 room#4").tags == ()


# --- whitespace and notes -------------------------------------------------


def test_non_breaking_space_is_normalized():
    """Telegram mobile emits U+00A0; untreated it breaks tokenizing."""
    entry = ok(f"100{NBSP}coffee{NBSP}beans")
    assert entry.amount_minor == 10_000
    assert entry.note == "coffee beans"
    assert NBSP not in entry.note


def test_surrounding_and_repeated_whitespace_collapses():
    assert ok("  100   flat   white \t ").note == "flat white"


def test_note_length_is_rejected_not_truncated():
    assert err("100 " + "x" * 201) == ParseErrorCode.NOTE_TOO_LONG
    assert len(ok("100 " + "x" * 200).note) == 200


def test_raw_is_preserved_verbatim():
    raw = "  100 coffee  "
    assert ok(raw).raw == raw


# --- the parser resolves nothing it cannot ---------------------------------


def test_parsed_entry_has_no_account_field():
    """The ledger takes account_id explicitly; the parser must not invent one."""
    entry = ok("100 coffee")
    assert not hasattr(entry, "account_id")
    assert not hasattr(entry, "account")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "@",
        "#",
        "/",
        "///",
        "@@@",
        "###",
        "-",
        ".",
        ",",
        "e",
        "1e5",
        " ",
        "\\",
        "\t",
        "\n",
        "\x00",
        "100 " + " " * 50,
        "@" * 100,
        "1" * 400,
        "/@",
        "#@",
        "100#@",
        "..",
        ",,",
        "-.",
        "1..2",
        "1,,2",
        "1.2.3",
    ],
)
def test_parser_never_raises(raw):
    """Every input returns a value. No input escapes as an exception."""
    assert isinstance(parse(raw, today=TODAY), ParsedEntry | ParseError)
