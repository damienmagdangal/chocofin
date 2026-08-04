"""Encoding and decoding `callback_data`.

Telegram allows 1-64 BYTES of `callback_data` per button and rejects the
message outright if any button exceeds it. So this module carries ids and a
one-character verb, and nothing else — never a note, never raw input, and above
all never a category or account NAME. Names here contain emoji at four bytes
apiece, and a keyboard that works for "Cash" would fail for "🏦 BPI Savings"
with an error that names no button.

PTB's `arbitrary_callback_data` would side-step the limit by keeping objects in
a process-local cache keyed by a uuid. It is not used, and not because of the
limit: that cache does not survive a restart. Every button sent before a
redeploy would come back as `InvalidCallbackData`, which is exactly the
stranded keyboard this phase exists to prevent. Ids in the button and state in
`pending_entries` survive anything.

Decoding NEVER raises. A malformed payload is a tampered or long-obsolete
button, and the answer to one is a polite message, not a traceback in the
update loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Telegram Bot API: callback_data is 1-64 bytes.
MAX_CALLBACK_DATA_BYTES = 64

SEP = ":"


class Action(StrEnum):
    """One character each, because the budget is bytes.

    The values are a wire format. Changing one silently breaks every button
    already sitting in someone's chat history.
    """

    PICK_ACCOUNT = "a"  # commit an expense or income to this account
    PICK_SOURCE = "t"  # transfer: this is where the money leaves from
    PICK_DESTINATION = "d"  # transfer: this is where it lands; commit
    SHOW_ALL = "o"  # expand [Other…] into the full account list
    CANCEL_PENDING = "x"  # discard the pending row
    CONFIRM_VOID = "v"  # void this entry
    DISMISS = "n"  # close a prompt that never created anything


@dataclass(frozen=True, slots=True)
class Callback:
    action: Action
    ids: tuple[int, ...]

    @property
    def first(self) -> int:
        return self.ids[0]

    @property
    def second(self) -> int:
        return self.ids[1]


class CallbackTooLongError(ValueError):
    """An encoded payload would not fit in 64 bytes.

    Raised at send time, never at receive time. If this fires, ids have grown
    past what the scheme budgeted for and the scheme needs changing — better a
    hard failure here than a Telegram error that names no button.
    """


def encode(action: Action, *ids: int) -> str:
    """Build a payload, refusing to emit one Telegram would reject."""
    data = SEP.join((action.value, *(str(i) for i in ids)))
    size = len(data.encode("utf-8"))
    if size > MAX_CALLBACK_DATA_BYTES:
        raise CallbackTooLongError(
            f"callback_data {data!r} is {size} bytes, over the "
            f"{MAX_CALLBACK_DATA_BYTES}-byte limit"
        )
    return data


def decode(data: str | None) -> Callback | None:
    """Parse a payload, or None if it is not one of ours.

    Every rejection path returns None: unknown verb, non-numeric id, negative
    id, empty string, or the `None` that PTB hands over for a button with no
    data at all.
    """
    if not data:
        return None

    verb, _, rest = data.partition(SEP)
    try:
        action = Action(verb)
    except ValueError:
        return None

    if not rest:
        return Callback(action=action, ids=())

    ids: list[int] = []
    for part in rest.split(SEP):
        if not part.isdigit():  # also rejects "-1" and "" — ids are positive
            return None
        ids.append(int(part))

    return Callback(action=action, ids=tuple(ids))


# --- convenience builders, so no handler assembles a payload by hand --------


def pick_account(pending_id: int, account_id: int) -> str:
    return encode(Action.PICK_ACCOUNT, pending_id, account_id)


def pick_source(pending_id: int, account_id: int) -> str:
    return encode(Action.PICK_SOURCE, pending_id, account_id)


def pick_destination(pending_id: int, account_id: int) -> str:
    return encode(Action.PICK_DESTINATION, pending_id, account_id)


def show_all(pending_id: int) -> str:
    return encode(Action.SHOW_ALL, pending_id)


def cancel_pending(pending_id: int) -> str:
    return encode(Action.CANCEL_PENDING, pending_id)


def confirm_void(entry_id: int) -> str:
    return encode(Action.CONFIRM_VOID, entry_id)


def dismiss() -> str:
    return encode(Action.DISMISS)
