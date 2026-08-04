"""Rendering money and time for a human. The only place `bot/` touches a number.

Two conversions live here and nowhere else:

* BIGINT centavos -> "PHP 1,234.50". Done with `divmod` and string assembly:
  integer operations, never float, never Decimal. `120050 / 100` is 1200.5,
  which is exactly representable, but `1234.56 * 100` is 123455.99999999999 and
  the habit of routing money through float is how that gets in. Nothing here
  changes a value; it only chooses characters to print.

* UTC -> Asia/Manila. Every timestamp in the database is UTC. A user in Manila
  who logs a coffee at 00:30 and sees it dated yesterday will not trust the
  ledger again, so display always converts.

This is the "convert to display units only in formatters" clause of CLAUDE.md.
Everywhere else in `bot/`, an amount is an opaque integer to be handed to
`core.ledger` unmodified.
"""

from __future__ import annotations

import datetime as dt
import html

from core.periods import MANILA

PESO = "₱"

# The bot sends HTML rather than Markdown: note text is arbitrary user input,
# and Telegram's Markdown parsers reject an unmatched "*" or "_" outright,
# which would turn a stray asterisk in a merchant name into a failed reply.
# HTML needs only three characters escaped and they are escaped by `esc`.
PARSE_MODE = "HTML"


def esc(text: str) -> str:
    """Escape user text for Telegram's HTML parse mode."""
    return html.escape(text, quote=False)


def format_minor(amount_minor: int) -> str:
    """Centavos to a peso string: 120050 -> 'PHP 1,200.50'.

    Handles negatives, which balances produce: a card that owes money has a
    negative balance and must read as -PHP 3,000.00, not PHP -3,000.00.
    """
    sign = "-" if amount_minor < 0 else ""
    pesos, centavos = divmod(abs(amount_minor), 100)
    return f"{sign}{PESO}{pesos:,}.{centavos:02d}"


def format_signed_minor(amount_minor: int) -> str:
    """Like `format_minor`, but always shows the sign.

    For lists where income and expense sit side by side and the direction is
    the point.
    """
    if amount_minor >= 0:
        return f"+{format_minor(amount_minor)}"
    return format_minor(amount_minor)


def to_manila(moment: dt.datetime) -> dt.datetime:
    """A stored UTC instant as Manila wall-clock time."""
    return moment.astimezone(MANILA)


def format_datetime(moment: dt.datetime) -> str:
    """'4 Aug 2026, 2:15 PM' in Manila."""
    local = to_manila(moment)
    # %-d and %-I are not portable to Windows; strip the zeros by hand.
    day = local.day
    hour12 = local.strftime("%I").lstrip("0") or "12"
    return f"{day} {local:%b %Y}, {hour12}:{local:%M %p}"


def format_date(moment: dt.datetime) -> str:
    """'4 Aug 2026' in Manila."""
    local = to_manila(moment)
    return f"{local.day} {local:%b %Y}"


KIND_LABELS = {
    "expense": "Expense",
    "income": "Income",
    "transfer": "Transfer",
}

# Rendered next to an account name so a keyboard scans at a glance. Keys are
# the `accounts.type` vocabulary from core.models.ACCOUNT_TYPES.
TYPE_ICONS = {
    "cash": "\U0001f4b5",
    "bank": "\U0001f3e6",
    "ewallet": "\U0001f4f1",
    "credit_card": "\U0001f4b3",
    "savings": "\U0001f416",
    "loan": "\U0001f4dd",
}


def account_label(name: str, account_type: str) -> str:
    """A button label. Never used as `callback_data` — these carry emoji."""
    icon = TYPE_ICONS.get(account_type, "")
    return f"{icon} {name}".strip()
