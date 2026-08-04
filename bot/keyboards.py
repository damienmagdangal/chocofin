"""Inline keyboards, described without importing `telegram`.

A keyboard here is plain data: rows of `(label, callback_data)`. `handlers.py`
turns it into `InlineKeyboardMarkup` at the last moment. That split is what
lets the whole flow layer be tested against a real database with no bot token,
no network, and no PTB objects to fake.

Labels carry emoji and account names; `callback_data` carries ids. The two are
built in the same place precisely so it stays obvious which is which.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from bot import callbacks
from bot.formatting import account_label
from core.models import Account

# How many MRU accounts get a shortcut before the rest go behind [Other…].
MRU_LIMIT = 3

OTHER_LABEL = "Other…"
CANCEL_LABEL = "Cancel"


@dataclass(frozen=True, slots=True)
class Button:
    label: str
    data: str


Keyboard = tuple[tuple[Button, ...], ...]

DataBuilder = Callable[[int, int], str]
"""(pending_id, account_id) -> callback_data."""


def _chunk(buttons: Sequence[Button], columns: int) -> list[tuple[Button, ...]]:
    return [tuple(buttons[i : i + columns]) for i in range(0, len(buttons), columns)]


def account_keyboard(
    accounts: Sequence[Account],
    *,
    pending_id: int,
    build_data: DataBuilder,
    show_other: bool = False,
    columns: int = 2,
) -> Keyboard:
    """Buttons for a list of accounts, plus [Other…] and [Cancel].

    `show_other` is set when this is the shortened MRU keyboard and there are
    more accounts behind it. The full list omits it — there is nothing further
    to expand into, and a button that reopens the list you are looking at reads
    as a bug.
    """
    rows = _chunk(
        [
            Button(
                label=account_label(account.name, account.type),
                data=build_data(pending_id, account.id),
            )
            for account in accounts
        ],
        columns,
    )

    tail: list[Button] = []
    if show_other:
        tail.append(Button(OTHER_LABEL, callbacks.show_all(pending_id)))
    tail.append(Button(CANCEL_LABEL, callbacks.cancel_pending(pending_id)))
    rows.append(tuple(tail))

    return tuple(rows)


def mru_keyboard(
    accounts: Sequence[Account],
    *,
    pending_id: int,
    build_data: DataBuilder,
) -> Keyboard:
    """The first keyboard: top `MRU_LIMIT` accounts, then [Other…].

    `accounts` is the FULL ordered list, not a pre-trimmed one, so this can
    tell whether anything is actually hidden. Offering [Other…] when the three
    buttons already are every account sends the user to a second, identical
    keyboard.
    """
    shortlist = accounts[:MRU_LIMIT]
    return account_keyboard(
        shortlist,
        pending_id=pending_id,
        build_data=build_data,
        show_other=len(accounts) > len(shortlist),
        columns=3 if len(shortlist) == MRU_LIMIT else 2,
    )


def void_keyboard(entry_id: int) -> Keyboard:
    """Confirm or walk away. Voiding is not undoable by another tap.

    A void inserts nothing and deletes nothing — the entry stays readable
    forever — but it does change every total the entry was in, so it asks.
    """
    return (
        (
            Button("Void it", callbacks.confirm_void(entry_id)),
            Button(CANCEL_LABEL, callbacks.dismiss()),
        ),
    )
