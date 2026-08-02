"""Parse a raw message into a ParsedEntry.

Three rules govern everything here:

1. Never raise. Every failure is a returned `ParseError`.
2. Never guess an amount. Ambiguity is a rejection, not a best effort.
3. Never read a clock. The caller injects `today`, already resolved in Manila,
   so relative dates are deterministic and testable.

The parser also never resolves an account or a category — both require the
database, which would make this module impure. `ParsedEntry` therefore has no
account field; the caller supplies `account_id` explicitly from the inline
keyboard selection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Literal

EntryKind = Literal["income", "expense", "transfer"]

# BIGINT upper bound. Amounts are centavos, so this is the hard ceiling the
# database will accept.
MAX_AMOUNT_MINOR = 9_223_372_036_854_775_807

MAX_TAG_LENGTH = 32
MAX_NOTE_LENGTH = 200


class ParseErrorCode(StrEnum):
    """Why a parse failed.

    An enum rather than bare strings so the bot can map to localized copy
    without matching on message text.
    """

    EMPTY = "empty"
    NO_AMOUNT = "no_amount"
    BAD_AMOUNT = "bad_amount"
    NEGATIVE_AMOUNT = "negative_amount"
    ZERO_AMOUNT = "zero_amount"
    SUB_CENTAVO = "sub_centavo"
    AMOUNT_OVERFLOW = "amount_overflow"
    NON_ASCII_DIGITS = "non_ascii_digits"
    TRANSFER_NOT_PARSEABLE = "transfer_not_parseable"
    AMBIGUOUS_DATE = "ambiguous_date"
    INVALID_DATE = "invalid_date"
    MULTIPLE_DATES = "multiple_dates"
    EMPTY_TAG = "empty_tag"
    TAG_TOO_LONG = "tag_too_long"
    NOTE_TOO_LONG = "note_too_long"


@dataclass(frozen=True, slots=True)
class ParseError:
    code: ParseErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    kind: EntryKind
    amount_minor: int
    note: str
    occurred_on: date | None
    tags: tuple[str, ...]
    raw: str


# A bare "/expense" or the group-chat form "/expense@ChocoFinBot".
_COMMAND_RE = re.compile(r"^/(?P<name>[a-z_]+)(?:@[\w_]+)?$", re.IGNORECASE)

_COMMAND_KINDS: dict[str, EntryKind] = {
    "expense": "expense",
    "income": "income",
    "transfer": "transfer",
}

# Currency markers we accept in front of an amount. PHP only, per the invariant.
# Matches both the glued form ("P100") and, via _CURRENCY_WORDS, the spaced
# form ("PHP 100").
_CURRENCY_PREFIX_RE = re.compile(r"^(?:php|p|₱)", re.IGNORECASE)

_CURRENCY_WORDS = {"php", "p", "₱"}

# An amount is ASCII digits with optional comma grouping and an optional
# fractional part. Anchored, so trailing junk like "100k" or "100." fails.
_AMOUNT_RE = re.compile(r"^(?P<int>[0-9,]*)(?:\.(?P<frac>[0-9]+))?$")

# A date token: "@" that starts a word. "ana@gmail.com" has no whitespace
# before its "@", so it stays in the note where it belongs.
_DATE_TOKEN_RE = re.compile(r"(?:(?<=\s)|^)@(\S*)")

_TAG_TOKEN_RE = re.compile(r"(?:(?<=\s)|^)#(\S*)")

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_RELATIVE_DATES = {"today": 0, "yesterday": -1, "tomorrow": 1}


def _normalize_whitespace(text: str) -> str:
    """Collapse every kind of whitespace to single ASCII spaces.

    Telegram's mobile clients emit U+00A0 and other exotic spaces; left alone
    they break tokenizing in ways that are miserable to debug. `str.split()`
    already splits on the full Unicode whitespace set, including U+00A0.

    Deliberately NOT unicodedata.normalize("NFKC", ...): NFKC rewrites
    full-width digits to ASCII, which would launder the exact input
    `_has_non_ascii_digits` exists to reject.
    """
    return " ".join(text.split())


def _has_non_ascii_digits(text: str) -> bool:
    """True if `text` holds a digit that int() accepts but we must not.

    int("١٠٠") is 100 and str.isdigit() is True for Arabic-Indic and full-width
    forms. Silently accepting them is a money-corruption path, so they are a
    hard rejection rather than a normalization.
    """
    return any(ch.isdigit() and not ch.isascii() for ch in text)


def _parse_amount(token: str) -> int | ParseError:
    """Convert a leading token to centavos, or explain why it is not one."""
    if token.startswith("-"):
        return ParseError(
            ParseErrorCode.NEGATIVE_AMOUNT,
            "Amounts are always positive — the sign is decided by the entry kind.",
        )

    stripped = _CURRENCY_PREFIX_RE.sub("", token, count=1).strip()
    if not stripped:
        return ParseError(ParseErrorCode.NO_AMOUNT, "No amount found.")

    # "100." is a truncated number, not a missing one — say so precisely rather
    # than letting the anchored regex report it as "no amount here".
    if stripped.endswith("."):
        return ParseError(
            ParseErrorCode.BAD_AMOUNT,
            f"{token!r} is not a valid amount — it ends in a decimal point.",
        )

    match = _AMOUNT_RE.match(stripped)
    if match is None:
        return ParseError(
            ParseErrorCode.NO_AMOUNT,
            f"{token!r} is not an amount. Start the message with a number.",
        )

    integer_part = match.group("int").replace(",", "")
    frac_part = match.group("frac")

    # ".50" is fine; "" with no fraction is not a number at all.
    if not integer_part and frac_part is None:
        return ParseError(ParseErrorCode.NO_AMOUNT, "No amount found.")
    if not integer_part and not stripped.startswith("."):
        return ParseError(
            ParseErrorCode.BAD_AMOUNT, f"{token!r} is not a valid amount."
        )

    if frac_part is not None and len(frac_part) > 2:
        return ParseError(
            ParseErrorCode.SUB_CENTAVO,
            f"{token!r} is finer than one centavo. Round it yourself — "
            "I will not guess.",
        )

    centavos = frac_part.ljust(2, "0") if frac_part else "00"
    try:
        amount = int(integer_part or "0") * 100 + int(centavos)
    except ValueError:
        return ParseError(
            ParseErrorCode.BAD_AMOUNT, f"{token!r} is not a valid amount."
        )

    if amount == 0:
        return ParseError(ParseErrorCode.ZERO_AMOUNT, "Amount must be more than zero.")
    if amount > MAX_AMOUNT_MINOR:
        return ParseError(ParseErrorCode.AMOUNT_OVERFLOW, "That amount is too large.")
    return amount


def _extract_dates(text: str, today: date) -> tuple[str, date | None] | ParseError:
    """Pull the single optional @date out of `text`."""
    matches = _DATE_TOKEN_RE.findall(text)
    if not matches:
        return text, None
    if len(matches) > 1:
        return ParseError(
            ParseErrorCode.MULTIPLE_DATES,
            "More than one @date — I will not choose between them.",
        )

    token = matches[0].lower()
    if token in _RELATIVE_DATES:
        occurred = today + timedelta(days=_RELATIVE_DATES[token])
    elif _ISO_DATE_RE.match(token):
        try:
            occurred = date.fromisoformat(token)
        except ValueError:
            return ParseError(
                ParseErrorCode.INVALID_DATE, f"@{token} is not a real date."
            )
    else:
        return ParseError(
            ParseErrorCode.AMBIGUOUS_DATE,
            f"@{token} is ambiguous. Use @today, @yesterday, @tomorrow, "
            "or @YYYY-MM-DD.",
        )

    return _DATE_TOKEN_RE.sub("", text), occurred


def _extract_tags(text: str) -> tuple[str, tuple[str, ...]] | ParseError:
    """Pull #tags out of `text`, lowercased and deduped with order preserved."""
    tags: list[str] = []
    for token in _TAG_TOKEN_RE.findall(text):
        if not token:
            return ParseError(ParseErrorCode.EMPTY_TAG, "A '#' with no tag after it.")
        if len(token) > MAX_TAG_LENGTH:
            return ParseError(
                ParseErrorCode.TAG_TOO_LONG,
                f"Tag #{token} is longer than {MAX_TAG_LENGTH} characters.",
            )
        lowered = token.lower()
        if lowered not in tags:
            tags.append(lowered)
    return _TAG_TOKEN_RE.sub("", text), tuple(tags)


def parse(raw: str, *, today: date) -> ParsedEntry | ParseError:
    """Parse `raw` into a ParsedEntry, or explain why it cannot be one.

    `today` must already be the current date in Asia/Manila. The parser has no
    clock of its own, so relative dates like @yesterday are fully determined by
    the caller.
    """
    text = _normalize_whitespace(raw)
    if not text:
        return ParseError(ParseErrorCode.EMPTY, "Nothing to parse.")

    kind: EntryKind = "expense"
    tokens = text.split(" ")

    command = _COMMAND_RE.match(tokens[0])
    if command is not None:
        name = command.group("name").lower()
        if name in _COMMAND_KINDS:
            kind = _COMMAND_KINDS[name]
            tokens = tokens[1:]
        # An unrecognised command falls through: the amount check below will
        # reject it with NO_AMOUNT, which is the honest reason.

    if kind == "transfer":
        return ParseError(
            ParseErrorCode.TRANSFER_NOT_PARSEABLE,
            "Transfers need a source and a destination account, which text "
            "alone cannot supply. Use the transfer keyboard.",
        )

    # "PHP 100 coffee" — a standalone currency word before the amount.
    if tokens and tokens[0].lower() in _CURRENCY_WORDS and len(tokens) > 1:
        tokens = tokens[1:]

    if not tokens:
        return ParseError(ParseErrorCode.NO_AMOUNT, "No amount found.")

    remainder = " ".join(tokens)
    if _has_non_ascii_digits(remainder):
        return ParseError(
            ParseErrorCode.NON_ASCII_DIGITS,
            "Use plain ASCII digits (0-9) for the amount.",
        )

    amount = _parse_amount(tokens[0])
    if isinstance(amount, ParseError):
        return amount

    rest = " ".join(tokens[1:])

    dated = _extract_dates(rest, today)
    if isinstance(dated, ParseError):
        return dated
    rest, occurred_on = dated

    tagged = _extract_tags(rest)
    if isinstance(tagged, ParseError):
        return tagged
    rest, tags = tagged

    note = _normalize_whitespace(rest)
    if len(note) > MAX_NOTE_LENGTH:
        return ParseError(
            ParseErrorCode.NOTE_TOO_LONG,
            f"Note is longer than {MAX_NOTE_LENGTH} characters.",
        )

    return ParsedEntry(
        kind=kind,
        amount_minor=amount,
        note=note,
        occurred_on=occurred_on,
        tags=tags,
        raw=raw,
    )
