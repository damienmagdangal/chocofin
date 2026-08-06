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

from core.errors import PeriodError
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
    """A stored UTC instant as Manila wall-clock time.

    Rejects a naive datetime, exactly as `core.periods.manila_today` does, and
    for a sharper reason: `astimezone` on a naive value does not fail, it
    assumes the SYSTEM's local time. On a box that is not set to UTC every
    displayed date would then be silently wrong, and it would be wrong in the
    same direction on every entry, so nothing on screen would look odd. Every
    timestamp in `entries` is TIMESTAMPTZ and comes back aware; one that
    arrives naive is a bug upstream of here, not a value to render anyway.
    """
    if moment.tzinfo is None:
        raise PeriodError("naive datetime — every instant must carry a timezone")
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


# Keyed by `entries.kind` — what the ledger wrote. A settlement appears here as
# "Transfer", because that is what it is.
KIND_LABELS = {
    "expense": "Expense",
    "income": "Income",
    "transfer": "Transfer",
}

# Keyed by `pending_entries.intent` — what the user asked for, before anything
# is written. A superset of KIND_LABELS: a settlement is still called a
# settlement while it is being asked about, which is the whole reason the two
# vocabularies are not one.
INTENT_LABELS = {
    **KIND_LABELS,
    "settlement": "Settlement",
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
