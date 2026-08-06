"""Tests for the two things `bot.handlers` decides.

Almost nothing in that module is worth a test — every command handler is three
lines that read an update and call one flow, and the flows are covered against a
real database in `tests/test_bot_flows.py`. Two exceptions:

* `_argument`, the one place a COMMAND makes a decision. Getting it wrong picked
  a different entry to void than the one the user named.
* `on_callback`'s dispatch table, the one place a BUTTON can be wired to the
  wrong flow. Nothing else in the suite reads it: every flow test calls the
  flows directly, so `PICK_SOURCE` could dispatch to `commit_destination` — a
  transfer committed on the first tap, with no source ever chosen — and the
  whole suite would stay green.

No database, no `bot.auth`, no `telegram`. The handler under test is unwrapped
(`functools.wraps` puts it on `__wrapped__`) and every flow is a recorder, so
the session it is handed is never touched and the stubs below carry only the
attributes the handler actually reads.
"""

from __future__ import annotations

import datetime as dt

import pytest

from bot import callbacks, flows, handlers
from bot.auth import Actor, Reply
from bot.callbacks import Action, decode
from bot.handlers import STALE_BUTTON, _argument, on_callback


class FakeMessage:
    def __init__(self, text: str | None) -> None:
        self.text = text


class FakeUpdate:
    """The two attributes `_text` reads, and no more.

    `effective_message` can be None on an update PTB still routes — an edited
    message reaction, a poll answer — and `text` can be None on a message that
    carries only a photo, so both are representable here.
    """

    def __init__(self, text: str | None, *, message: bool = True) -> None:
        self.effective_message = FakeMessage(text) if message else None


# --- the newline bug --------------------------------------------------------
#
# `partition(" ")` splits on a literal space, and a message can be separated by
# any whitespace. "/void" + newline + "412" is a shape mobile keyboards produce
# routinely: autocomplete inserts the command, the user hits return, then types
# the id. There was no space in that message, so `partition` returned no
# argument at all — and `_argument` returning None does not mean "no argument",
# it means "no id given", which is bare `/void`.
#
# Bare `/void` targets the most recently LOGGED entry. So a user who asked to
# void #412 was shown some other entry to confirm, with a confirm button
# carrying that other entry's id. The wrong money comes off the books on the
# next tap, and every total that entry was part of moves.


@pytest.mark.parametrize("separator", [" ", "\n", "\r\n", "\n\n", "\t", " \n  "])
def test_the_argument_survives_any_whitespace_after_the_command(separator: str):
    assert _argument(FakeUpdate(f"/void{separator}412")) == "412"


def test_an_argument_of_several_words_is_kept_whole():
    """`maxsplit=1`, not a full split: only the command comes off the front.

    Nothing today passes a multi-word argument, but `_argument` is the generic
    helper every command reaches for, and one that silently dropped everything
    after the first word would be found by whichever command needs it next.
    """
    assert _argument(FakeUpdate("/void 412 wrong account")) == "412 wrong account"
    assert _argument(FakeUpdate("/void\n412 wrong account")) == "412 wrong account"


# --- no argument ------------------------------------------------------------
#
# All of these must be None. None is what makes `/void` mean "the entry I just
# logged", so anything returning "" or "/void" here would be routed down the
# explicit-id path and rejected as a malformed id instead.


@pytest.mark.parametrize(
    "text",
    [
        "/void",
        "/void ",
        "/void\n",
        "/void   \n\t ",
        "",
        "   ",
        "\n",
    ],
)
def test_a_command_with_nothing_after_it_yields_no_argument(text: str):
    assert _argument(FakeUpdate(text)) is None


def test_an_update_carrying_no_text_yields_no_argument():
    """A message with only a photo, and an update with no message at all.

    Both reach a `CommandHandler` in principle, and neither is an argument.
    """
    assert _argument(FakeUpdate(None)) is None
    assert _argument(FakeUpdate(None, message=False)) is None


# --- the callback dispatch table --------------------------------------------
#
# Every inline button in this bot arrives at `on_callback`, which is the single
# place a verb is turned into a flow. A wrong arm there is invisible to every
# other test in the suite and catastrophic on screen: `t:` (choose the source of
# a transfer) routed to `commit_destination` would write the transfer on the
# first tap, from a source that was never chosen, against whichever account the
# user thought they were picking as the payer.
#
# So the assertion is not "some flow ran". It is: exactly one flow ran, it is
# the one the verb names, and the ids came out of the payload in the right
# order — a swapped `pending_id`/`account_id` commits someone's coffee to the
# account id of their pending row.


NOW = dt.datetime(2026, 1, 15, 4, 0, tzinfo=dt.UTC)

# Sentinels. The flows are stubbed, so neither is ever used as anything; what
# matters is that the objects the decorator handed the handler are the objects
# the flow is called with, rather than something the handler made up.
SESSION = object()
CONTEXT = object()
ACTOR = Actor(member_id=1, household_id=1, display_name="Tester")

# Every flow `on_callback` can reach. Patched together rather than one at a
# time, so "no other flow ran" is a thing this file can assert.
FLOW_NAMES = (
    "commit_account_choice",
    "pick_transfer_source",
    "commit_destination",
    "show_all_accounts",
    "cancel_pending",
    "confirm_void",
    "dismiss",
)

# Payloads come from the builders in `bot.callbacks`, not from string literals:
# these are the exact bytes `bot.keyboards` puts under a button, so the table
# below is the real route from a tap to a flow.
DISPATCH = (
    (
        callbacks.pick_account(7, 12),
        "commit_account_choice",
        {"pending_id": 7, "account_id": 12, "now": NOW},
    ),
    (
        callbacks.pick_source(7, 12),
        "pick_transfer_source",
        {"pending_id": 7, "account_id": 12, "now": NOW},
    ),
    (
        callbacks.pick_destination(7, 12),
        "commit_destination",
        {"pending_id": 7, "account_id": 12, "now": NOW},
    ),
    (callbacks.show_all(7), "show_all_accounts", {"pending_id": 7, "now": NOW}),
    (callbacks.cancel_pending(7), "cancel_pending", {"pending_id": 7}),
    (callbacks.confirm_void(412), "confirm_void", {"entry_id": 412}),
    (callbacks.dismiss(), "dismiss", {}),
)


class FakeCallbackQuery:
    """`data` is the only thing `on_callback` reads off a query.

    `answer()` and the edit belong to `bot.auth`, which is not under test here
    and is covered in `tests/test_bot_auth.py`.
    """

    def __init__(self, data: str | None) -> None:
        self.data = data


class FakeCallbackUpdate:
    def __init__(self, data: str | None, *, query: bool = True) -> None:
        self.callback_query = FakeCallbackQuery(data) if query else None


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple, dict]]:
    """Replace every flow with a recorder, and freeze the clock.

    `handlers.py` calls `flows.X(...)`, so the lookup happens at call time and
    patching the module attribute is enough. `dismiss` is patched with a SYNC
    stub because the dispatch table does not await it — it builds a `Reply` and
    touches no database.
    """
    calls: list[tuple[str, tuple, dict]] = []

    def recorder(name: str):
        async def flow(*args, **kwargs) -> Reply:
            calls.append((name, args, kwargs))
            return Reply(text=name)

        return flow

    def sync_recorder(name: str):
        def flow(*args, **kwargs) -> Reply:
            calls.append((name, args, kwargs))
            return Reply(text=name)

        return flow

    for name in FLOW_NAMES:
        stub = sync_recorder(name) if name == "dismiss" else recorder(name)
        monkeypatch.setattr(flows, name, stub)

    monkeypatch.setattr(handlers, "_now", lambda: NOW)
    return calls


async def _dispatch(data: str | None, *, query: bool = True) -> Reply:
    """One tap, straight into the dispatch table.

    `on_callback.__wrapped__` is the handler without `@authorised`: the Actor
    and the session are what the decorator would have injected, and this file
    is about which flow the verb reaches, not about who was allowed to send it.
    """
    return await on_callback.__wrapped__(
        FakeCallbackUpdate(data, query=query), CONTEXT, ACTOR, SESSION
    )


@pytest.mark.parametrize(
    ("payload", "flow_name", "expected"),
    DISPATCH,
    ids=[payload for payload, _, _ in DISPATCH],
)
async def test_every_callback_verb_routes_to_the_flow_it_names(
    routed: list[tuple[str, tuple, dict]],
    payload: str,
    flow_name: str,
    expected: dict,
):
    reply = await _dispatch(payload)

    # `dismiss` alone is handed neither: it reads nothing and writes nothing.
    # Every other flow gets the decorator's own session and Actor, which is what
    # keeps `household_id` coming from the members table rather than from a
    # button.
    positional = () if flow_name == "dismiss" else (SESSION, ACTOR)
    assert routed == [(flow_name, positional, expected)]
    # And the flow's answer is what the user gets back, unaltered.
    assert reply.text == flow_name


def test_the_dispatch_table_covers_every_verb():
    """A verb added to `bot.callbacks` without a route fails HERE.

    Otherwise a new button would ship reading as a stale one — `on_callback`
    answers an unrouted verb politely, so nothing else would ever go red.
    """
    covered = set()
    for payload, _flow_name, _expected in DISPATCH:
        parsed = decode(payload)
        assert parsed is not None
        covered.add(parsed.action)
    assert covered == set(Action)
    # Every routed flow is a real function on `bot.flows`, so the patching above
    # cannot be silently creating attributes that production never calls.
    assert all(callable(getattr(flows, name)) for name in FLOW_NAMES)


@pytest.mark.parametrize(
    ("data", "query"),
    [
        ("zzz", True),  # unknown verb
        ("a:7:notanumber", True),
        ("", True),
        (None, True),  # a button with no data at all
        (None, False),  # an update with no callback query
    ],
)
async def test_a_payload_that_does_not_decode_reaches_no_flow(
    routed: list[tuple[str, tuple, dict]], data: str | None, query: bool
):
    """A tampered or long-obsolete button is answered, never dispatched.

    Politely: a silent no-op leaves the client spinning, and a raise would only
    reach the error handler. Both look like a broken bot to someone holding a
    phone.
    """
    reply = await _dispatch(data, query=query)

    assert routed == []
    assert reply.text == STALE_BUTTON
    assert reply.toast == STALE_BUTTON


@pytest.mark.parametrize(
    "payload",
    [
        "a:7",  # one id where the account is missing
        "a:7:12:13",
        "t:7",
        "d:7",
        "o:7:12",  # one id too many
        "x:7:12",
        "v:1:2",
    ],
)
async def test_a_known_verb_with_the_wrong_number_of_ids_reaches_no_flow(
    routed: list[tuple[str, tuple, dict]], payload: str
):
    """Right verb, wrong shape. Not a button this version ever sent.

    The `if len(parsed.ids) == N` guard on each arm is what stands between a
    hand-made payload and an `IndexError` inside `parsed.second` — or worse, a
    flow called with an id it was never given.
    """
    reply = await _dispatch(payload)

    assert routed == []
    assert reply.text == STALE_BUTTON
    assert reply.toast == STALE_BUTTON
