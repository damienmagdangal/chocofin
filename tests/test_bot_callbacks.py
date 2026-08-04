"""callback_data is a wire format with a hard 64-byte ceiling. No database."""

from __future__ import annotations

import pytest

from bot import callbacks
from bot.callbacks import (
    MAX_CALLBACK_DATA_BYTES,
    Action,
    CallbackTooLongError,
    decode,
    encode,
)
from bot.keyboards import void_keyboard

# Widest a BIGINT gets. Nothing in this household will ever reach it, but the
# budget has to hold at the type's limit or it is not a budget.
BIGINT_MAX = 9_223_372_036_854_775_807


def test_round_trip():
    for action in Action:
        data = encode(action, 1, 2) if action not in (Action.DISMISS,) else encode(action)
        parsed = decode(data)
        assert parsed is not None
        assert parsed.action is action


def test_builders_produce_the_documented_shapes():
    assert callbacks.pick_account(7, 12) == "a:7:12"
    assert callbacks.pick_source(7, 12) == "t:7:12"
    assert callbacks.pick_destination(7, 12) == "d:7:12"
    assert callbacks.show_all(7) == "o:7"
    assert callbacks.cancel_pending(7) == "x:7"
    assert callbacks.confirm_void(412) == "v:412"
    assert callbacks.dismiss() == "n"


def test_decode_reads_the_ids_back():
    parsed = decode("a:7:12")
    assert parsed is not None
    assert parsed.action is Action.PICK_ACCOUNT
    assert parsed.ids == (7, 12)
    assert parsed.first == 7
    assert parsed.second == 12


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "zzz",  # unknown verb
        "a:7:notanumber",
        "a:7:-3",  # ids are positive
        "a:7:",  # trailing separator, empty id
        "a:7:12:",
        "🎉",  # someone else's button entirely
    ],
)
def test_decode_never_raises_and_rejects_junk(payload):
    """A malformed payload is a tampered or obsolete button, not a crash.

    Raising here would surface as a traceback in the update loop and a client
    left spinning on the button forever.
    """
    assert decode(payload) is None


def test_every_payload_fits_at_maximum_id_width():
    """The 64-byte limit, checked where it actually binds.

    Telegram rejects the whole message if any button is over, with an error
    that names no button, so this has to be proven up front rather than
    discovered in production.
    """
    widest = max(
        callbacks.pick_account(BIGINT_MAX, BIGINT_MAX),
        callbacks.pick_source(BIGINT_MAX, BIGINT_MAX),
        callbacks.pick_destination(BIGINT_MAX, BIGINT_MAX),
        callbacks.show_all(BIGINT_MAX),
        callbacks.cancel_pending(BIGINT_MAX),
        callbacks.confirm_void(BIGINT_MAX),
        key=lambda data: len(data.encode("utf-8")),
    )
    assert len(widest.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES
    # Two BIGINTs and a verb is 41 bytes. Keeping the transfer's source on the
    # pending row instead of in the button is what bought that headroom.
    assert len(widest.encode("utf-8")) == 41


def test_encoding_refuses_to_emit_an_oversized_payload():
    """Fail loudly here rather than at Telegram, which names no button."""
    with pytest.raises(CallbackTooLongError):
        encode(Action.PICK_ACCOUNT, *([BIGINT_MAX] * 4))


def test_no_payload_ever_carries_a_name():
    """Category and account names hold emoji at four bytes each.

    A scheme that put one in `callback_data` would work for "Cash" and fail for
    "🏦 BPI Savings", which is the kind of bug that ships.
    """
    payloads = [
        callbacks.pick_account(1, 2),
        callbacks.pick_source(1, 2),
        callbacks.pick_destination(1, 2),
        callbacks.show_all(1),
        callbacks.cancel_pending(1),
        callbacks.confirm_void(1),
        callbacks.dismiss(),
    ]
    for payload in payloads:
        assert payload.isascii()
        assert payload.replace(":", "").replace("-", "").isalnum()


def test_void_keyboard_offers_confirm_and_cancel():
    (row,) = void_keyboard(412)
    assert [button.data for button in row] == ["v:412", "n"]
