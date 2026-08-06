"""Flow-layer tests: real database, real pending rows, no telegram objects.

`bot.flows` functions take `(session, actor, ...)` and return a `Reply`, so the
whole behaviour of a command — the parse, the pending row, the claim, the ledger
write — is reachable from a test with no bot token and no fake `Update`. That is
what the adapter split is for.

Two kinds of test live here, in that order:

* Regressions. Each one was written after a bug and asserts it is gone — the
  `/pay` [Other…] fix, the transfer-source race, the hand-made-payload guards,
  member scoping, the `created_at` orderings, the `/balances` reconciliation.
  They name the bug in their docstring because a test whose reason is forgotten
  is a test that gets deleted.
* A happy path and a rejection for every command. Proof that each flow works at
  all, which the regressions above assume and never state.

The happy paths assert the user-visible result: the `Reply` the user reads, the
entry that was written, the legs it moved and the balance that follows. "No
exception was raised" is not a passing bot.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import callbacks, flows, keyboards
from bot.auth import Actor
from bot.callbacks import decode
from bot.formatting import account_label
from bot.keyboards import Keyboard
from core import accounts as core_accounts
from core import balances, ledger, pending
from core.models import Account, Entry, EntryLeg, EntryTag, PendingEntry
from core.periods import manila_today, to_utc
from tests.factories import JAN_15, MAR_20, World, build_world

pytestmark = pytest.mark.asyncio


def _actor(world: World) -> Actor:
    return Actor(
        member_id=world.member_id,
        household_id=world.household_id,
        display_name="Tester",
    )


def _housemate(world: World) -> Actor:
    """A second active member of the SAME household.

    Same `household_id` on purpose: it is what makes the member-scoping tests
    below prove something. Every household check in the codebase passes for this
    actor, so anything that refuses them is refusing on the member alone.
    """
    return Actor(
        member_id=world.other_member_id,
        household_id=world.household_id,
        display_name="Housemate",
    )


def _account_buttons(keyboard: Keyboard | None):
    """Every button that names an account, in keyboard order.

    [Other…] and [Cancel] carry one id, an account button carries two, which is
    how they are told apart without matching on a label.
    """
    assert keyboard is not None
    buttons = []
    for row in keyboard:
        for button in row:
            callback = decode(button.data)
            assert callback is not None, f"undecodable payload {button.data!r}"
            if len(callback.ids) == 2:
                buttons.append((button, callback))
    return buttons


def _pending_id(keyboard: Keyboard | None) -> int:
    (_, callback), *_ = _account_buttons(keyboard)
    return callback.first


def _account_ids(keyboard: Keyboard | None) -> set[int]:
    return {callback.second for _, callback in _account_buttons(keyboard)}


def _tap(keyboard: Keyboard | None, account_id: int) -> tuple[int, int]:
    """The ids under the button for `account_id`, as a real tap would send them.

    Both ids come off the keyboard rather than out of the `World`, so a happy
    path that commits to the right account has also shown that the button
    offering it exists and carries the right payload. Passing `world.cash_id`
    straight to a commit proves the write and nothing about the screen.
    """
    for _button, callback in _account_buttons(keyboard):
        if callback.second == account_id:
            return callback.first, callback.second
    raise AssertionError(f"no button for account {account_id} on this keyboard")


async def _only_entry(session: AsyncSession, world: World) -> Entry:
    entry = await session.scalar(
        select(Entry).where(Entry.household_id == world.household_id)
    )
    assert entry is not None
    return entry


async def _log(
    session: AsyncSession, actor: Actor, raw: str, *, now, account_id: int
) -> Entry:
    """Type a message, tap an account, commit — and hand back what landed.

    The whole path, not a `ledger.create_expense` shortcut: what these tests are
    about is which entry the bot picks out afterwards, and picking the wrong one
    is only reachable if the entries were logged the way a user logs them.
    """
    started = await flows.start_entry(session, actor, raw=raw, now=now)
    pending_id = _pending_id(started.keyboard)
    await flows.commit_account_choice(
        session, actor, pending_id=pending_id, account_id=account_id, now=now
    )
    await session.commit()

    entry = await session.scalar(
        select(Entry)
        .where(Entry.household_id == actor.household_id)
        .order_by(Entry.id.desc())
        .limit(1)
    )
    assert entry is not None
    return entry


async def _move(
    session: AsyncSession,
    actor: Actor,
    *,
    raw: str,
    now,
    source_id: int,
    destination_id: int,
) -> Entry:
    """The whole two-tap transfer, for tests that need one but are not about it."""
    started = await flows.start_transfer(session, actor, raw=raw, now=now)
    pending_id, source = _tap(started.keyboard, source_id)
    second = await flows.pick_transfer_source(
        session, actor, pending_id=pending_id, account_id=source, now=now
    )
    _, destination = _tap(second.keyboard, destination_id)
    await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=destination, now=now
    )
    await session.commit()

    entry = await session.scalar(
        select(Entry)
        .where(Entry.household_id == actor.household_id)
        .order_by(Entry.id.desc())
        .limit(1)
    )
    assert entry is not None
    return entry


async def _legs(
    session: AsyncSession, world: World, entry: Entry
) -> dict[str, EntryLeg]:
    legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=entry.id
    )
    return {leg.leg_role: leg for leg in legs}


async def _balance(session: AsyncSession, world: World, account_id: int) -> int:
    row = await balances.account_balance(
        session, household_id=world.household_id, account_id=account_id
    )
    return row.balance_minor


async def _nothing_written(session: AsyncSession, world: World) -> None:
    """No entry, and nothing parked waiting for a tap.

    The second half matters as much as the first. A rejection that still writes
    a `pending_entries` row leaves a keyboard-less orphan in the table and, worse,
    an id a hand-made callback could still commit against.
    """
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None
    assert (
        await session.scalar(
            select(PendingEntry).where(PendingEntry.household_id == world.household_id)
        )
    ) is None


async def test_other_on_a_pay_keyboard_stays_a_settlement(session: AsyncSession):
    """[Other…] asks the SAME question. It used to ask a different one.

    `/pay` writes `parsed_kind='transfer'` with no source, which is also exactly
    what `/transfer` writes before its first tap. `show_all_accounts` told them
    apart by those two columns and therefore could not: it re-rendered the
    settlement as "From which account?" with a source-picking builder and no
    credit-card filter, and the next two taps wrote a plain transfer into any
    account at all. `intent` is what makes the two rows different.

    The load-bearing assertion is the last one. Nothing in this flow ever names
    Savings; if the source leg is Savings, the write went through `settle_card`
    and resolved the payer from the card's billing account, which a plain
    transfer would have had no way to do.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_settlement(session, actor, raw="/pay 3000", now=JAN_15)
    assert "Which card?" in started.text
    pending_id = _pending_id(started.keyboard)

    expanded = await flows.show_all_accounts(
        session, actor, pending_id=pending_id, now=JAN_15
    )

    assert "Which card?" in expanded.text
    assert "Settlement" in expanded.text
    assert "From which account?" not in expanded.text

    buttons = _account_buttons(expanded.keyboard)
    # Destination buttons, never source ones: 'd:' commits, 't:' would have
    # turned this into the first half of a transfer.
    assert all(button.data.startswith("d:") for button, _ in buttons)
    # And still only cards. Cash, Savings and the excluded account are not
    # things a card can be settled INTO.
    assert {callback.second for _, callback in buttons} == {
        world.card_id,
        world.orphan_card_id,
    }

    committed = await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=world.card_id, now=JAN_15
    )
    await session.commit()
    assert committed.toast == "Recorded"

    entry = await _only_entry(session, world)
    assert entry.kind == "transfer"
    assert entry.amount_minor == 300_000
    legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=entry.id
    )
    by_role = {leg.leg_role: leg for leg in legs}
    assert by_role["source"].account_id == world.savings_id
    assert by_role["destination"].account_id == world.card_id


async def test_transfer_source_stops_when_the_pending_row_has_gone(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """A vanished row must end the flow, not draw a second keyboard.

    `pick_transfer_source` reads the row with `get`, which takes no lock, then
    records the source with an UPDATE. Between those two there is a real window:
    a competing tap can claim the row, or a /cancel can remove it. The UPDATE
    then matches nothing and returns False — which the caller used to discard,
    so it went on to render "To which account?" over a pending row that no
    longer existed. Every button on that keyboard was already dead.

    The window is only reachable by landing in it, so the account lookup that
    sits inside it is where the competing tap is injected.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_transfer(
        session, actor, raw="/transfer 500", now=JAN_15
    )
    pending_id = _pending_id(started.keyboard)

    real_get_account = core_accounts.get_account

    async def claim_it_first(*args, **kwargs):
        await pending.claim(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=pending_id,
        )
        return await real_get_account(*args, **kwargs)

    monkeypatch.setattr(core_accounts, "get_account", claim_it_first)

    reply = await flows.pick_transfer_source(
        session, actor, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    # The load-bearing assertion: no keyboard. Before the fix this was a full
    # destination picker.
    assert reply.keyboard is None
    assert reply.text == flows.ALREADY_DONE
    assert reply.toast == flows.ALREADY_DONE

    # And nothing was written or left behind on either side of the ledger.
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None
    assert (
        await session.scalar(select(PendingEntry).where(PendingEntry.id == pending_id))
    ) is None


async def test_transfer_source_renders_a_vanished_account_rather_than_raising(
    session: AsyncSession,
):
    """`get_account` returning None is a message, not a fault.

    core/accounts.py says so in as many words: the account can go between the
    keyboard being drawn and the button being tapped, and None is how that is
    reported. Turning it back into an `AccountNotFoundError` sent the user the
    generic "Something went wrong" and logged a traceback for something that is
    not a bug.

    The account id here belongs to another household, which is the same thing
    from `get_account`'s point of view — household scoping is what makes it
    invisible — and it needs no row deleted out from under the fixtures.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_transfer(
        session, actor, raw="/transfer 500", now=JAN_15
    )
    pending_id = _pending_id(started.keyboard)

    reply = await flows.pick_transfer_source(
        session,
        actor,
        pending_id=pending_id,
        account_id=world.outsider_account_id,
        now=JAN_15,
    )
    await session.commit()

    assert reply.text == flows.ACCOUNT_GONE
    assert reply.toast == flows.ACCOUNT_GONE
    # The dead keyboard comes off screen rather than being redrawn.
    assert reply.keyboard is None
    assert reply.edit is True

    # Refused before the write: the row is untouched, so the keyboard the user
    # still has in their chat is answerable.
    row = await session.get(PendingEntry, pending_id)
    assert row is not None
    assert row.source_account_id is None
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None


async def test_source_button_refuses_an_expense_row(session: AsyncSession):
    """A `t:` payload against an expense row must not start a transfer.

    Our keyboards never send one: `start_entry` builds `pick_account`, so a
    source button on this row can only be hand-made. Following it would store a
    source on an expense and let the next tap commit it as `kind='transfer'`,
    which every spending summary filters out — the ledger stays well-formed and
    the money quietly stops being spending.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_entry(session, actor, raw="120 coffee", now=JAN_15)
    pending_id = _pending_id(started.keyboard)

    reply = await flows.pick_transfer_source(
        session, actor, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    assert reply.text == flows.GONE
    assert reply.toast == flows.GONE
    assert reply.keyboard is None

    # Refused before the write, so the row is untouched and the real keyboard
    # sent with it still works.
    row = await session.get(PendingEntry, pending_id)
    assert row is not None
    assert row.intent == "expense"
    assert row.source_account_id is None


async def test_destination_button_refuses_an_expense_row(session: AsyncSession):
    """The second half of the same payload, guarded on its own.

    The source is written here with a raw UPDATE rather than through
    `pending.set_source_account`, which now refuses it: this reconstructs the
    state the attack produced and proves `commit_destination` does not rely on
    either of the other two guards having run.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_entry(session, actor, raw="120 coffee", now=JAN_15)
    pending_id = _pending_id(started.keyboard)
    await session.execute(
        update(PendingEntry)
        .where(PendingEntry.id == pending_id)
        .values(source_account_id=world.cash_id)
    )

    reply = await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=world.savings_id, now=JAN_15
    )
    await session.commit()

    assert reply.text == flows.GONE
    assert reply.toast == flows.GONE

    # The load-bearing assertion. The bug wrote a perfectly well-formed
    # transfer, so the only proof is that no entry exists at all.
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None


async def test_pay_tags_survive_the_commit(session: AsyncSession):
    """A tag typed on `/pay` reaches the entry.

    It was parsed and stored in `parsed_tags` all along; the settlement branch
    of the commit simply never passed it on, because `settle_card` had nowhere
    to put it. A settlement carries no category, so the tag was the only label
    that entry could ever have had.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_settlement(
        session, actor, raw="/pay 3000 #visa", now=JAN_15
    )
    pending_id = _pending_id(started.keyboard)

    await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=world.card_id, now=JAN_15
    )
    await session.commit()

    entry = await _only_entry(session, world)
    tags = list(
        await session.scalars(select(EntryTag.tag).where(EntryTag.entry_id == entry.id))
    )
    assert tags == ["visa"]


async def test_a_housemate_cannot_complete_your_entry(session: AsyncSession):
    """The tapper is not the author, and in a shared chat they are often not.

    A household is one Telegram group, so every member can see and tap every
    keyboard in it. Pending rows used to be scoped to the household alone, which
    made the button answerable by anyone: whoever tapped got the entry filed
    under their name, and their per-member recent-accounts shortlist learned
    from money they never spent. The household check could not catch it — the
    two members really are in the same household.

    The last assertion is the load-bearing one. It is not enough that the
    housemate is refused; the entry that eventually lands has to carry the
    typist, not whoever was holding the phone.
    """
    world = await build_world(session)
    typist = _actor(world)
    housemate = _housemate(world)

    started = await flows.start_entry(session, typist, raw="120 coffee", now=JAN_15)
    pending_id = _pending_id(started.keyboard)

    refused = await flows.commit_account_choice(
        session, housemate, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    # The same answer a dead button gets. A housemate learns nothing about what
    # anyone else has in flight.
    assert refused.text == flows.ALREADY_DONE
    assert refused.toast == flows.ALREADY_DONE
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None
    # And the typist's keyboard is still live — refusing everyone would satisfy
    # the assertions above and break the bot.
    assert (await session.get(PendingEntry, pending_id)) is not None

    recorded = await flows.commit_account_choice(
        session, typist, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    assert recorded.toast == "Recorded"
    entry = await _only_entry(session, world)
    assert entry.member_id == world.member_id


async def test_a_housemate_cannot_answer_your_transfer(session: AsyncSession):
    """The same rule across the gap between a transfer's two taps.

    `pick_transfer_source` reads with `get` and writes with an UPDATE, so both
    statements have to be member-scoped. If only the claim were, a housemate
    could still choose which account someone else's money leaves from and hand
    the destination keyboard back to them.
    """
    world = await build_world(session)
    typist = _actor(world)
    housemate = _housemate(world)

    started = await flows.start_transfer(
        session, typist, raw="/transfer 500", now=JAN_15
    )
    pending_id = _pending_id(started.keyboard)

    refused = await flows.pick_transfer_source(
        session, housemate, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    assert refused.text == flows.GONE
    assert refused.toast == flows.GONE
    # No second keyboard: the housemate is not handed a live-looking picker for
    # a flow that is not theirs.
    assert refused.keyboard is None

    row = await session.get(PendingEntry, pending_id)
    assert row is not None
    assert row.source_account_id is None


# --- reading back what you just logged --------------------------------------
#
# Both commands below used to order by `occurred_at`, when the money moved.
# They are questions about `created_at`, when the entry was LOGGED — the same
# distinction `core.accounts` draws for the MRU keyboard, for the same reason.


async def test_last_shows_a_backdated_entry_you_just_logged(session: AsyncSession):
    """The entry you most need to see is the one ledger order buries.

    Log today's rent, then backfill a January receipt. Under `occurred_at` the
    January entry sinks below every newer entry and falls off a short list
    entirely — and `/last` is the only place its id is ever shown, so a wrong
    amount on a backdated entry became uncorrectable.
    """
    world = await build_world(session)
    actor = _actor(world)

    today = await _log(
        session, actor, "1000 rent", now=MAR_20, account_id=world.cash_id
    )
    backdated = await _log(
        session, actor, "500 receipt @2026-01-15", now=MAR_20, account_id=world.cash_id
    )

    # The premise: the two clocks really do disagree about these two entries.
    assert backdated.occurred_at < today.occurred_at

    reply = await flows.show_last(session, actor, limit=1)
    assert f"#{backdated.id}" in reply.text
    assert f"#{today.id}" not in reply.text

    # Both are there at full length, most recently logged first.
    full = await flows.show_last(session, actor, limit=10)
    assert full.text.index(f"#{backdated.id}") < full.text.index(f"#{today.id}")


async def test_void_offers_the_entry_you_last_logged(session: AsyncSession):
    """Bare `/void` follows a typo you just noticed.

    The entry you just typed is the one you mean, even when you dated it into
    last month. Ledger order offered the newest-dated entry instead — a
    different, correct entry — and the confirm button carried its id.
    """
    world = await build_world(session)
    actor = _actor(world)

    today = await _log(
        session, actor, "1000 rent", now=MAR_20, account_id=world.cash_id
    )
    backdated = await _log(
        session, actor, "500 receipt @2026-01-15", now=MAR_20, account_id=world.cash_id
    )

    reply = await flows.start_void(session, actor)
    assert f"#{backdated.id}" in reply.text
    assert f"#{today.id}" not in reply.text

    # The load-bearing assertion: the button votes the same way as the text. A
    # correct message over a payload naming the other entry would void the
    # wrong money on the next tap.
    assert reply.keyboard is not None
    (confirm, _cancel), *_ = reply.keyboard
    decoded = decode(confirm.data)
    assert decoded is not None
    assert decoded.first == backdated.id


async def test_void_with_an_id_ignores_the_ordering(session: AsyncSession):
    """An explicit id is obeyed, and a non-id is refused rather than guessed."""
    world = await build_world(session)
    actor = _actor(world)

    today = await _log(
        session, actor, "1000 rent", now=MAR_20, account_id=world.cash_id
    )
    backdated = await _log(
        session, actor, "500 receipt @2026-01-15", now=MAR_20, account_id=world.cash_id
    )

    # `today` is not what bare /void would offer, which is what makes this
    # prove the argument is doing the choosing.
    reply = await flows.start_void(session, actor, argument=str(today.id))
    assert f"#{today.id}" in reply.text
    assert f"#{backdated.id}" not in reply.text

    refused = await flows.start_void(session, actor, argument="last")
    assert refused.text == flows.BAD_ENTRY_ID
    assert refused.keyboard is None


async def test_today_is_now_not_this_mornings_midnight(session: AsyncSession):
    """`@today` means now. It used to mean 00:00 Manila.

    An entry typed at 22:00 was filed at 00:00 that morning, and the header
    said nothing, because the date line is suppressed exactly when the two
    Manila dates match — which they did. `@today` and no date at all are the
    same instant now.
    """
    world = await build_world(session)
    actor = _actor(world)

    now = MAR_20  # 12:00 Manila, hours after local midnight
    entry = await _log(
        session, actor, "120 coffee @today", now=now, account_id=world.cash_id
    )

    assert entry.occurred_at == now
    assert entry.occurred_at != to_utc(manila_today(now))

    # And no date line, which is now the truth rather than a coincidence.
    started = await flows.start_entry(session, actor, raw="90 pandesal", now=now)
    dated = await flows.start_entry(session, actor, raw="90 pandesal @today", now=now)
    assert started.text == dated.text


async def test_a_backdated_entry_still_lands_on_manila_midnight(session: AsyncSession):
    """The other half of the rule, unchanged: a named DAY is not a second."""
    world = await build_world(session)
    actor = _actor(world)

    entry = await _log(
        session, actor, "500 receipt @2026-01-15", now=MAR_20, account_id=world.cash_id
    )

    assert entry.occurred_at == to_utc(dt.date(2026, 1, 15))
    # 16:00Z the day before — the Manila boundary, not the UTC one.
    assert entry.occurred_at == dt.datetime(2026, 1, 14, 16, 0, tzinfo=dt.UTC)


# --- /balances --------------------------------------------------------------
#
# The screen has to add up. `net_worth_minor` counts deactivated accounts on
# purpose — a closed card still owing money still owes it — so an account-list
# that stops at the active ones prints a column of numbers under a total they
# do not reach, with nothing on screen accounting for the difference.


_AMOUNT = re.compile(r"<b>(?:Net worth )?(-?₱[\d,]+\.\d{2})</b>")


def _to_minor(rendered: str) -> int:
    """Read `format_minor` back, so the assertions are about money.

    Parsing the rendered message rather than re-querying is the point: what is
    being checked is the arithmetic of what the user actually sees.
    """
    sign = -1 if rendered.startswith("-") else 1
    pesos, centavos = rendered.lstrip("-₱").replace(",", "").split(".")
    return sign * (int(pesos) * 100 + int(centavos))


def _screen(text: str) -> tuple[list[int], list[int], int]:
    """Split a /balances message into (counted, uncounted, printed net worth).

    A line is uncounted when the message itself says so — that is the whole
    contract being tested. Anything the screen does not disclaim has to be in
    the total.
    """
    counted: list[int] = []
    uncounted: list[int] = []
    net: int | None = None
    for line in text.split("\n"):
        if "not in net worth" in line and counted:
            uncounted.append(counted.pop())
            continue
        found = _AMOUNT.search(line)
        if not found:
            continue
        if "Net worth" in line:
            net = _to_minor(found.group(1))
        else:
            counted.append(_to_minor(found.group(1)))
    assert net is not None, f"no net worth line in {text!r}"
    return counted, uncounted, net


async def _close(session: AsyncSession, account_id: int, **changes) -> Account:
    account = await session.get(Account, account_id)
    assert account is not None
    account.is_active = False
    for field, value in changes.items():
        setattr(account, field, value)
    await session.commit()
    return account


async def test_the_balance_lines_add_up_to_the_net_worth_beneath_them(
    session: AsyncSession,
):
    """The bug, stated as arithmetic.

    A card is used, then closed. Its debt stays in `net_worth_minor`, which is
    correct — closing an account moves no money. Listing only active accounts
    left that debt off the screen, so the lines summed to PHP 3,000.00 more
    than the total printed under them and nothing said why.

    Both disclaimed and undisclaimed lines are checked: the excluded account is
    on screen too, and it must NOT be in the total.
    """
    world = await build_world(session)
    actor = _actor(world)

    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.card_id,
        amount_minor=300_000,  # PHP 3,000.00 owed on the card
        occurred_at=JAN_15,
    )
    # Money in an account that is deliberately outside net worth, so the two
    # reasons a line can differ from the total are both on screen at once.
    await ledger.create_income(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.excluded_id,
        amount_minor=150_000,
        occurred_at=JAN_15,
    )
    await session.commit()
    await _close(session, world.card_id)

    reply = await flows.show_balances(session, actor)
    counted, uncounted, net = _screen(reply.text)

    # The load-bearing assertion: the numbers on the screen make the number at
    # the bottom of the screen.
    assert sum(counted) == net
    assert net == await balances.net_worth_minor(
        session, household_id=world.household_id
    )
    # And the closed card is one of the lines that made it, rather than a gap.
    assert -300_000 in counted
    assert uncounted == [150_000]

    assert flows.CLOSED_SECTION in reply.text
    closed_section = reply.text.split(flows.CLOSED_SECTION)[1]
    assert account_label("Card", "credit_card") in closed_section
    assert "closed" in closed_section
    # A shut card's remaining credit is not a number to act on — though the
    # still-open card above it keeps showing its own.
    assert "available" not in closed_section
    assert "available" in reply.text.split(flows.CLOSED_SECTION)[0]


async def test_a_closed_account_at_zero_is_not_listed(session: AsyncSession):
    """Only the closed accounts that are actually holding the total apart.

    The factory's closed account sits at zero, so it explains nothing and would
    be noise on a screen whose whole job is to reconcile.
    """
    world = await build_world(session)
    actor = _actor(world)

    reply = await flows.show_balances(session, actor)
    counted, _uncounted, net = _screen(reply.text)

    assert sum(counted) == net
    assert flows.CLOSED_SECTION not in reply.text
    assert account_label("Closed", "bank") not in reply.text


async def test_a_closed_and_excluded_account_is_not_listed(session: AsyncSession):
    """`exclude_from_totals` already keeps it out of the total.

    A closed account that is also excluded contributes nothing to the net worth
    printed below, so there is no gap for it to explain. It is left off rather
    than listed under a heading that says "still counted" about money that is
    not.
    """
    world = await build_world(session)
    actor = _actor(world)

    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.card_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
    )
    await session.commit()
    await _close(session, world.card_id, exclude_from_totals=True)

    reply = await flows.show_balances(session, actor)
    counted, uncounted, net = _screen(reply.text)

    assert sum(counted) == net
    # The card is nowhere: not in the total, and not listed under either
    # heading. (The zero from the factory's active excluded account is.)
    assert -300_000 not in counted
    assert -300_000 not in uncounted
    assert flows.CLOSED_SECTION not in reply.text
    assert account_label("Card", "credit_card") not in reply.text


async def test_no_accounts_when_nothing_is_active_or_owed(session: AsyncSession):
    """Everything closed and everything at zero: there is nothing to reconcile.

    The message still has to be the "no active accounts" one rather than a bare
    net worth of zero over an empty list.
    """
    world = await build_world(session)
    actor = _actor(world)

    await session.execute(
        update(Account)
        .where(Account.household_id == world.household_id)
        .values(is_active=False)
    )
    await session.commit()

    reply = await flows.show_balances(session, actor)
    assert reply.text == flows.NO_ACCOUNTS
    assert reply.keyboard is None


# --- nothing is parked for a question that cannot be asked -------------------
#
# Every `start_*` command wrote its pending row and only THEN looked for an
# account to offer. With none to offer the user was told there was nowhere to
# put the money and the row stayed behind, committed, with no keyboard pointing
# at it — an orphan id a hand-made `callback_data` could still commit against
# for the next 24 hours. `_nothing_written` is the assertion that was missing on
# exactly these paths.


async def _deactivate_every_account(session: AsyncSession, world: World) -> None:
    """A household with accounts on the books but none in use.

    Not a household with no rows at all: `_require_account` does not filter on
    `is_active`, so this leaves every id still resolvable while
    `recent_accounts` — which does filter — returns nothing. That is the state
    the empty-keyboard branch is actually reached in.
    """
    await session.execute(
        update(Account)
        .where(Account.household_id == world.household_id)
        .values(is_active=False)
    )


async def test_an_expense_with_no_accounts_parks_nothing(session: AsyncSession):
    world = await build_world(session)
    await _deactivate_every_account(session, world)

    reply = await flows.start_entry(
        session, _actor(world), raw="120 coffee", now=JAN_15
    )

    assert reply.text == flows.NO_ACCOUNTS
    assert reply.keyboard is None
    await _nothing_written(session, world)


async def test_a_transfer_with_no_accounts_parks_nothing(session: AsyncSession):
    world = await build_world(session)
    await _deactivate_every_account(session, world)

    reply = await flows.start_transfer(
        session, _actor(world), raw="/transfer 500", now=JAN_15
    )

    assert reply.text == flows.NO_ACCOUNTS
    assert reply.keyboard is None
    await _nothing_written(session, world)


async def test_a_settlement_with_no_cards_parks_nothing(session: AsyncSession):
    """Only the cards go. The household still has somewhere to spend from.

    So this also pins that the pre-check filters on the SAME `types` as the
    keyboard it is guarding: a check that asked for every account would find the
    cash account, sail past, and park a row for a card question with no cards.
    """
    world = await build_world(session)
    await session.execute(
        update(Account)
        .where(
            Account.household_id == world.household_id,
            Account.type == "credit_card",
        )
        .values(is_active=False)
    )

    reply = await flows.start_settlement(
        session, _actor(world), raw="/pay 3000", now=JAN_15
    )

    assert reply.text == flows.NO_CARDS
    assert reply.keyboard is None
    await _nothing_written(session, world)


async def test_reopening_for_a_payer_with_no_accounts_parks_nothing(
    session: AsyncSession,
):
    """The second question, asked of a household that cannot answer it.

    The narrow path: the card names no billing account, so `settle_card` raises
    and `_reopen_for_payer` would normally create a replacement row. The claim
    has already removed the first one, so a replacement that nobody can answer
    would be the only row left in the table — an orphan created by the recovery
    from a failure, which is the worst place to leave one.

    Reachable because `_require_account` does not filter `is_active`: the orphan
    card is still settleable after `recent_accounts` has stopped offering it.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_settlement(session, actor, raw="/pay 3000", now=JAN_15)
    pending_id, orphan_id = _tap(started.keyboard, world.orphan_card_id)

    # Only now, so the settlement gets as far as needing a payer.
    await _deactivate_every_account(session, world)

    reply = await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=orphan_id, now=JAN_15
    )

    assert reply.text == flows.NO_ACCOUNTS
    assert reply.keyboard is None
    assert reply.edit is True
    await _nothing_written(session, world)


# ============================================================================
# Happy path and rejection, one command at a time.
#
# Everything above this line was written after a bug. Everything below it is
# the thing those tests assume and never say: that the command works.
# ============================================================================


# --- /expense ---------------------------------------------------------------


async def test_an_expense_lands_on_the_account_that_was_tapped(session: AsyncSession):
    """The whole command, from typed text to money in an account.

    The account id is read back off the keyboard rather than taken from the
    `World`, so this covers the button as well as the write: a commit that lands
    correctly on an account the user was never offered is not a working bot.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_entry(
        session, actor, raw="/expense 120.50 coffee #cafe", now=JAN_15
    )

    # What was understood, on screen, before anything is written.
    assert "Expense ₱120.50" in started.text
    assert "coffee" in started.text
    assert "Which account?" in started.text
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None

    pending_id, account_id = _tap(started.keyboard, world.cash_id)
    recorded = await flows.commit_account_choice(
        session, actor, pending_id=pending_id, account_id=account_id, now=JAN_15
    )
    await session.commit()

    entry = await _only_entry(session, world)
    assert recorded.toast == "Recorded"
    assert "Expense ₱120.50" in recorded.text
    assert "coffee" in recorded.text
    assert "Cash" in recorded.text
    assert f"#{entry.id}" in recorded.text

    assert entry.kind == "expense"
    assert entry.amount_minor == 12050
    assert entry.note == "coffee"
    assert entry.occurred_at == JAN_15
    assert entry.member_id == world.member_id
    assert entry.voided_at is None

    tags = list(
        await session.scalars(select(EntryTag.tag).where(EntryTag.entry_id == entry.id))
    )
    assert tags == ["cafe"]

    # One leg, negative, on the account under the button. An expense has no
    # counterparty to balance against, so one leg is the whole shape.
    legs = await _legs(session, world, entry)
    assert set(legs) == {"source"}
    assert legs["source"].account_id == world.cash_id
    assert legs["source"].amount_minor == -12050
    assert await _balance(session, world, world.cash_id) == -12050


async def test_an_expense_of_zero_is_refused_before_anything_is_parked(
    session: AsyncSession,
):
    """A parse failure writes nothing at all — not even a pending row.

    The rejection text is the parser's own. `flows` does not restate it, so
    there is one place amounts are judged and one wording to keep right.
    """
    world = await build_world(session)
    actor = _actor(world)

    reply = await flows.start_entry(session, actor, raw="/expense 0 coffee", now=JAN_15)
    await session.commit()

    assert reply.text == "Amount must be more than zero."
    assert reply.keyboard is None
    await _nothing_written(session, world)


# --- /income ----------------------------------------------------------------


async def test_income_lands_as_a_single_positive_leg(session: AsyncSession):
    """Income is the mirror of an expense, and the sign is the whole difference.

    The prompt differs too: "Into which account?" rather than "Which account?".
    They are two branches of one function, and a test that only ever sends an
    expense would pass with the income branch deleted.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_entry(
        session, actor, raw="/income 25000 salary", now=JAN_15
    )
    assert "Income ₱25,000.00" in started.text
    assert "Into which account?" in started.text
    assert "Which account?" not in started.text

    pending_id, account_id = _tap(started.keyboard, world.savings_id)
    recorded = await flows.commit_account_choice(
        session, actor, pending_id=pending_id, account_id=account_id, now=JAN_15
    )
    await session.commit()

    entry = await _only_entry(session, world)
    assert recorded.toast == "Recorded"
    assert "Income ₱25,000.00" in recorded.text
    assert "Savings" in recorded.text

    assert entry.kind == "income"
    assert entry.amount_minor == 2_500_000
    assert entry.note == "salary"

    legs = await _legs(session, world, entry)
    assert set(legs) == {"destination"}
    assert legs["destination"].account_id == world.savings_id
    assert legs["destination"].amount_minor == 2_500_000
    assert await _balance(session, world, world.savings_id) == 2_500_000


async def test_income_with_no_amount_is_refused(session: AsyncSession):
    """A bare `/income` is a question, not an entry."""
    world = await build_world(session)
    actor = _actor(world)

    reply = await flows.start_entry(session, actor, raw="/income", now=JAN_15)
    await session.commit()

    assert reply.text == "No amount found."
    assert reply.keyboard is None
    await _nothing_written(session, world)


# --- /transfer --------------------------------------------------------------


async def test_a_transfer_moves_money_between_two_accounts_and_no_totals(
    session: AsyncSession,
):
    """Two taps, two legs summing to zero, and a net worth that does not move.

    The last part is the invariant: a transfer is real to every balance and
    invisible to every total. Money the household already had, in a different
    pocket.
    """
    world = await build_world(session)
    actor = _actor(world)

    # Money on the books first, so "net worth did not change" is a statement
    # about a real number rather than about zero staying zero.
    await _log(
        session, actor, "/income 25000 salary", now=JAN_15, account_id=world.savings_id
    )
    before = await balances.net_worth_minor(session, household_id=world.household_id)
    assert before == 2_500_000

    started = await flows.start_transfer(
        session, actor, raw="/transfer 500 top-up", now=JAN_15
    )
    assert "Transfer ₱500.00" in started.text
    assert "top-up" in started.text
    assert "From which account?" in started.text
    # Source buttons. A `d:` here would commit on the FIRST tap, with no source.
    assert all(
        button.data.startswith("t:") for button, _ in _account_buttons(started.keyboard)
    )

    pending_id, source_id = _tap(started.keyboard, world.cash_id)
    second = await flows.pick_transfer_source(
        session, actor, pending_id=pending_id, account_id=source_id, now=JAN_15
    )

    # The choice is on the row, not in this process — that is what survives a
    # redeploy between the two taps.
    row = await session.get(PendingEntry, pending_id)
    assert row is not None
    assert row.source_account_id == world.cash_id

    assert "To which account?" in second.text
    assert "Cash" in second.text
    assert all(
        button.data.startswith("d:") for button, _ in _account_buttons(second.keyboard)
    )
    # The source is not offered back: an account cannot pay itself.
    assert world.cash_id not in _account_ids(second.keyboard)

    _, destination_id = _tap(second.keyboard, world.savings_id)
    recorded = await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=destination_id, now=JAN_15
    )
    await session.commit()

    entry = await session.scalar(
        select(Entry).where(
            Entry.household_id == world.household_id, Entry.kind == "transfer"
        )
    )
    assert entry is not None
    assert recorded.toast == "Recorded"
    assert "Transfer ₱500.00" in recorded.text
    assert "Cash → Savings" in recorded.text
    assert f"#{entry.id}" in recorded.text
    assert entry.amount_minor == 50_000
    assert entry.note == "top-up"

    legs = await _legs(session, world, entry)
    assert set(legs) == {"source", "destination"}
    assert legs["source"].account_id == world.cash_id
    assert legs["source"].amount_minor == -50_000
    assert legs["destination"].account_id == world.savings_id
    assert legs["destination"].amount_minor == 50_000
    assert legs["source"].amount_minor + legs["destination"].amount_minor == 0

    assert await _balance(session, world, world.cash_id) == -50_000
    assert await _balance(session, world, world.savings_id) == 2_550_000
    # The load-bearing assertion: both balances moved and the household is no
    # richer or poorer.
    assert (
        await balances.net_worth_minor(session, household_id=world.household_id)
        == before
    )


async def test_a_transfer_to_its_own_source_is_refused(session: AsyncSession):
    """An account cannot pay itself, and core is what says so.

    Unreachable from our keyboards — the destination picker excludes the source
    — so this is a hand-made `d:` payload. `create_transfer` refuses it before
    touching the database, and the refusal reaches the user as the message
    rather than as "Something went wrong".
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_transfer(
        session, actor, raw="/transfer 500", now=JAN_15
    )
    pending_id, source_id = _tap(started.keyboard, world.cash_id)
    await flows.pick_transfer_source(
        session, actor, pending_id=pending_id, account_id=source_id, now=JAN_15
    )

    refused = await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    assert "source and destination are both account" in refused.text
    assert refused.toast == "Rejected."
    assert refused.edit is True

    # Nothing written. The pending row is gone too: `claim` took it before the
    # ledger refused, so the user retypes rather than tapping again — which is
    # the honest state, not a bug this test is papering over.
    await _nothing_written(session, world)


# --- /pay -------------------------------------------------------------------


async def test_paying_a_card_moves_money_from_the_billing_account(
    session: AsyncSession,
):
    """One tap. The payer is never asked for, because the card already knows.

    Nothing in this flow names Savings — the user typed an amount and tapped a
    card. Savings appears on the source leg because `settle_card` read the
    card's `billing_account_id`, which is the rule this path exists to apply.

    And the settlement is a `transfer`, never an expense: the purchases were
    expensed when they happened, so booking the payment as spending too would
    count the same money twice.
    """
    world = await build_world(session)
    actor = _actor(world)

    # A real card balance to settle against, so the numbers below are a card
    # being paid off rather than an abstract movement.
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.card_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
    )
    await session.commit()
    assert await _balance(session, world, world.card_id) == -300_000
    before = await balances.net_worth_minor(session, household_id=world.household_id)

    started = await flows.start_settlement(session, actor, raw="/pay 3000", now=JAN_15)
    assert "Settlement ₱3,000.00" in started.text
    assert "Which card?" in started.text
    # Only cards are settleable. Cash and Savings are not things that can be owed.
    assert _account_ids(started.keyboard) == {world.card_id, world.orphan_card_id}

    pending_id, card_id = _tap(started.keyboard, world.card_id)
    recorded = await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=card_id, now=JAN_15
    )
    await session.commit()

    entry = await session.scalar(
        select(Entry).where(
            Entry.household_id == world.household_id, Entry.kind == "transfer"
        )
    )
    assert entry is not None
    assert recorded.toast == "Recorded"
    assert "Transfer ₱3,000.00" in recorded.text
    assert "Savings → Card" in recorded.text

    assert entry.amount_minor == 300_000
    legs = await _legs(session, world, entry)
    assert legs["source"].account_id == world.savings_id
    assert legs["source"].amount_minor == -300_000
    assert legs["destination"].account_id == world.card_id
    assert legs["destination"].amount_minor == 300_000

    # The card is clear, Savings paid for it, and no total moved: settling a
    # card is not spending.
    assert await _balance(session, world, world.card_id) == 0
    assert await _balance(session, world, world.savings_id) == -300_000
    assert (
        await balances.net_worth_minor(session, household_id=world.household_id)
        == before
    )

    # Two entries, and the settlement is not one of the spending ones. The card
    # purchase was expensed when it happened; expensing the payment too would
    # count the same PHP 3,000.00 twice.
    kinds = sorted(
        await session.scalars(
            select(Entry.kind).where(Entry.household_id == world.household_id)
        )
    )
    assert kinds == ["expense", "transfer"]


async def test_a_card_with_no_billing_account_asks_who_is_paying(
    session: AsyncSession,
):
    """The whole orphan-card settlement, tap by tap.

    A card that names no billing account is the one settlement that cannot be
    finished in a single tap: `settle_card` has nowhere to read the payer from
    and raises. `_reopen_for_payer` answers that by CLAIMING the pending row and
    creating a fresh one, which is the only place in the bot where an in-flight
    entry is carried from one row to another — so every field has to make the
    hop, and until now nothing said so.

    Backdated on purpose. `now` is March and the entry is January, so a
    replacement row that stamped `now()` instead of copying `occurred_at` would
    file the settlement two months late — invisible in a test where the two are
    the same day.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_settlement(
        session, actor, raw="/pay 3000 visa bill #card @2026-01-15", now=MAR_20
    )
    first_pending_id, orphan_id = _tap(started.keyboard, world.orphan_card_id)

    reopened = await flows.commit_destination(
        session, actor, pending_id=first_pending_id, account_id=orphan_id, now=MAR_20
    )

    assert "That card has no billing account set." in reopened.text
    assert "Settlement ₱3,000.00" in reopened.text
    assert reopened.edit is True

    # Nothing was written, and the row that was claimed is really gone: this is
    # a second question, not a half-finished entry left lying about.
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None
    assert (await session.get(PendingEntry, first_pending_id)) is None

    (row,) = list(
        await session.scalars(
            select(PendingEntry).where(PendingEntry.household_id == world.household_id)
        )
    )
    assert row.id != first_pending_id
    assert _pending_id(reopened.keyboard) == row.id

    # The load-bearing assertions of the hop: everything the user already typed
    # is on the replacement row, so the second question is the ONLY question.
    assert row.intent == "settlement"
    assert row.parsed_kind == "transfer"
    assert row.parsed_amount_minor == 300_000
    assert row.parsed_note == "visa bill"
    assert list(row.parsed_tags) == ["card"]
    assert row.occurred_at == to_utc(dt.date(2026, 1, 15))
    assert row.raw_input == "/pay 3000 visa bill #card @2026-01-15"
    assert row.member_id == world.member_id
    assert row.source_account_id is None

    # The FULL account list, and therefore no [Other…]: a settlement with no
    # source is the one state `intent` cannot disambiguate, so the button that
    # would have to guess which question is being re-asked is not sent at all.
    buttons = _account_buttons(reopened.keyboard)
    assert len(buttons) > keyboards.MRU_LIMIT
    assert all(button.data.startswith("t:") for button, _ in buttons)
    assert world.cash_id in _account_ids(reopened.keyboard)
    assert not [
        button
        for keyboard_row in reopened.keyboard
        for button in keyboard_row
        if button.data.startswith("o:")
    ]

    # Answer it: Cash is paying.
    second_pending_id, payer_id = _tap(reopened.keyboard, world.cash_id)
    assert second_pending_id == row.id
    second = await flows.pick_transfer_source(
        session, actor, pending_id=second_pending_id, account_id=payer_id, now=MAR_20
    )

    # And the question after THAT is still the card question. `intent` is what
    # keeps it from widening into a plain transfer's "To which account?".
    assert "Which card?" in second.text
    assert "To which account?" not in second.text
    assert all(
        button.data.startswith("d:") for button, _ in _account_buttons(second.keyboard)
    )
    assert _account_ids(second.keyboard) == {world.card_id, world.orphan_card_id}

    stored = await session.get(PendingEntry, second_pending_id)
    assert stored is not None
    assert stored.source_account_id == world.cash_id

    _, target_id = _tap(second.keyboard, world.orphan_card_id)
    recorded = await flows.commit_destination(
        session, actor, pending_id=second_pending_id, account_id=target_id, now=MAR_20
    )
    await session.commit()

    assert recorded.toast == "Recorded"
    assert "Transfer ₱3,000.00" in recorded.text
    assert "Cash → Orphan Card" in recorded.text

    entry = await _only_entry(session, world)
    # Still a settlement all the way to the ledger: a transfer, never an expense,
    # even though its payer was chosen by hand.
    assert entry.kind == "transfer"
    assert entry.amount_minor == 300_000
    assert entry.note == "visa bill"
    assert entry.occurred_at == to_utc(dt.date(2026, 1, 15))
    assert entry.member_id == world.member_id
    assert entry.raw_input == "/pay 3000 visa bill #card @2026-01-15"

    tags = list(
        await session.scalars(select(EntryTag.tag).where(EntryTag.entry_id == entry.id))
    )
    assert tags == ["card"]

    legs = await _legs(session, world, entry)
    assert set(legs) == {"source", "destination"}
    # The hand-chosen payer really is the source leg. That is the whole reason
    # the second question was asked.
    assert legs["source"].account_id == world.cash_id
    assert legs["source"].amount_minor == -300_000
    assert legs["destination"].account_id == world.orphan_card_id
    assert legs["destination"].amount_minor == 300_000
    assert legs["source"].amount_minor + legs["destination"].amount_minor == 0

    assert await _balance(session, world, world.cash_id) == -300_000
    assert await _balance(session, world, world.orphan_card_id) == 300_000
    # Nothing parked afterwards: the replacement row was claimed by the commit.
    assert (
        await session.scalar(
            select(PendingEntry).where(PendingEntry.household_id == world.household_id)
        )
    ) is None


async def test_pay_with_no_amount_asks_for_one(session: AsyncSession):
    """`/pay` alone cannot be guessed at. The amount is the one thing no
    keyboard can supply."""
    world = await build_world(session)
    actor = _actor(world)

    reply = await flows.start_settlement(session, actor, raw="/pay", now=JAN_15)
    await session.commit()

    assert reply.text == flows.PAY_USAGE
    assert reply.keyboard is None
    await _nothing_written(session, world)


async def test_a_settlement_aimed_at_a_non_card_is_refused(session: AsyncSession):
    """Settling something that cannot be owed is a different operation.

    The keyboard only ever offers credit cards, so this is a hand-made `d:`
    payload — and it is exactly the check that used to be skipped on this path,
    which is why it is asserted rather than assumed. `settle_card` is what
    refuses, so the rule stays in core.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_settlement(session, actor, raw="/pay 3000", now=JAN_15)
    assert world.cash_id not in _account_ids(started.keyboard)
    pending_id = _pending_id(started.keyboard)

    refused = await flows.commit_destination(
        session, actor, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    assert "not a credit card" in refused.text
    assert refused.toast == "Rejected."
    assert refused.edit is True
    await _nothing_written(session, world)


# --- /balances --------------------------------------------------------------


async def test_balances_prints_every_account_and_a_total_that_matches(
    session: AsyncSession,
):
    """A spend, an income and a transfer — and the screen still reconciles.

    The transfer is the point. It changes two of the lines above the total and
    must not change the total itself, which is the same invariant from the
    reader's side: transfers are in balance math and out of every total.
    """
    world = await build_world(session)
    actor = _actor(world)

    await _log(session, actor, "120.50 coffee", now=JAN_15, account_id=world.cash_id)
    await _log(
        session, actor, "/income 25000 salary", now=JAN_15, account_id=world.savings_id
    )

    before = await flows.show_balances(session, actor)
    _, _, net_before = _screen(before.text)

    await _move(
        session,
        actor,
        raw="/transfer 500",
        now=JAN_15,
        source_id=world.cash_id,
        destination_id=world.savings_id,
    )

    reply = await flows.show_balances(session, actor)
    counted, uncounted, net = _screen(reply.text)

    # Every number on screen is the number in the database, account by account.
    rows = await balances.account_balances(session, household_id=world.household_id)
    assert counted == [row.balance_minor for row in rows if not row.exclude_from_totals]
    assert uncounted == [row.balance_minor for row in rows if row.exclude_from_totals]

    assert sum(counted) == net
    assert net == await balances.net_worth_minor(
        session, household_id=world.household_id
    )
    # The transfer moved two lines and left the total alone.
    assert net == net_before
    assert await _balance(session, world, world.cash_id) == -62_050
    assert await _balance(session, world, world.savings_id) == 2_550_000

    for name, account_type in (
        ("Cash", "cash"),
        ("Savings", "savings"),
        ("Card", "credit_card"),
    ):
        assert account_label(name, account_type) in reply.text


async def test_balances_never_shows_another_households_money(session: AsyncSession):
    """`household_id` is in every WHERE clause, and this is what that buys.

    The outsider household is rich; ours has nothing. Both facts have to survive
    the same screen.
    """
    world = await build_world(session)
    actor = _actor(world)

    await ledger.create_income(
        session,
        household_id=world.outsider_household_id,
        member_id=world.outsider_member_id,
        account_id=world.outsider_account_id,
        amount_minor=9_999_900,
        occurred_at=JAN_15,
    )
    await session.commit()

    reply = await flows.show_balances(session, actor)
    counted, _uncounted, net = _screen(reply.text)

    assert "Their Wallet" not in reply.text
    assert "₱99,999.00" not in reply.text
    assert net == 0
    assert sum(counted) == net
    # And the money really is there, in the household that owns it — otherwise
    # this passes by having written nothing at all.
    assert (
        await balances.net_worth_minor(
            session, household_id=world.outsider_household_id
        )
        == 9_999_900
    )


# --- /last ------------------------------------------------------------------


async def test_last_lists_what_was_logged_newest_first(session: AsyncSession):
    """Three kinds of entry, in the order they were typed.

    Each line carries its own id — the only place an id is ever shown, and so
    the only route to `/void <id>` — its kind, its amount and its own date.
    """
    world = await build_world(session)
    actor = _actor(world)

    expense = await _log(
        session, actor, "120.50 coffee", now=JAN_15, account_id=world.cash_id
    )
    income = await _log(
        session, actor, "/income 25000 salary", now=JAN_15, account_id=world.savings_id
    )
    transfer = await _move(
        session,
        actor,
        raw="/transfer 500",
        now=JAN_15,
        source_id=world.cash_id,
        destination_id=world.savings_id,
    )

    reply = await flows.show_last(session, actor, limit=10)

    for entry in (expense, income, transfer):
        assert f"#{entry.id}" in reply.text
    assert (
        reply.text.index(f"#{transfer.id}")
        < reply.text.index(f"#{income.id}")
        < reply.text.index(f"#{expense.id}")
    )

    assert "Expense" in reply.text
    assert "Income" in reply.text
    assert "Transfer" in reply.text
    assert "₱120.50" in reply.text
    assert "₱25,000.00" in reply.text
    assert "₱500.00" in reply.text
    assert "coffee" in reply.text
    # Every line dates itself, so a backdated entry says so rather than looking
    # mis-sorted.
    assert reply.text.count("15 Jan 2026") == 3

    shortest = await flows.show_last(session, actor, limit=1)
    assert f"#{transfer.id}" in shortest.text
    assert f"#{income.id}" not in shortest.text
    assert f"#{expense.id}" not in shortest.text


async def test_last_shows_nothing_when_only_another_household_has_entries(
    session: AsyncSession,
):
    """An empty ledger reads as empty, not as someone else's."""
    world = await build_world(session)
    actor = _actor(world)

    theirs = await ledger.create_expense(
        session,
        household_id=world.outsider_household_id,
        member_id=world.outsider_member_id,
        account_id=world.outsider_account_id,
        amount_minor=45_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    reply = await flows.show_last(session, actor)

    assert reply.text == "Nothing recorded yet."
    assert reply.keyboard is None
    assert f"#{theirs.id}" not in reply.text


# --- /void ------------------------------------------------------------------


async def test_voiding_an_entry_undoes_its_money_without_deleting_it(
    session: AsyncSession,
):
    """Confirm, void, and watch the balance come back.

    The entry itself stays: voiding is append-only bookkeeping, not a delete.
    What changes is every total it was part of, which is why it asks first.

    The last assertion is the ordering one. Once the newest entry is voided,
    bare `/void` has to offer the one below it — not the entry it just voided,
    and not nothing.
    """
    world = await build_world(session)
    actor = _actor(world)

    first = await _log(
        session, actor, "1000 rent", now=JAN_15, account_id=world.cash_id
    )
    second = await _log(
        session, actor, "120.50 coffee", now=JAN_15, account_id=world.cash_id
    )
    assert await _balance(session, world, world.cash_id) == -112_050

    asked = await flows.start_void(session, actor)
    assert f"#{second.id}" in asked.text
    assert "₱120.50" in asked.text
    assert "Void this?" in asked.text
    (confirm, cancel), *_ = asked.keyboard
    assert decode(confirm.data).first == second.id
    assert cancel.data == "n"

    voided = await flows.confirm_void(session, actor, entry_id=second.id)
    await session.commit()

    assert voided.toast == "Voided"
    assert "<s>Expense ₱120.50</s>" in voided.text
    assert "Voided." in voided.text

    # Still readable. Append-only means the row never goes.
    row = await session.get(Entry, second.id)
    assert row is not None
    assert row.voided_at is not None
    assert row.voided_by == world.member_id
    assert row.amount_minor == 120_50

    # And the money it moved is no longer in the balance.
    assert await _balance(session, world, world.cash_id) == -100_000

    # The next bare /void offers the entry below the one just voided.
    again = await flows.start_void(session, actor)
    assert f"#{first.id}" in again.text
    assert f"#{second.id}" not in again.text


async def test_voiding_the_same_entry_twice_is_refused_on_both_surfaces(
    session: AsyncSession,
):
    """Append-only history forbids voiding twice, and both routes to it say so.

    `/void <id>` refuses before drawing a confirm button, and `confirm_void`
    refuses again for the tap that was already on screen when the first one
    landed — a double-tap, which is the common case rather than the exotic one.
    """
    world = await build_world(session)
    actor = _actor(world)

    entry = await _log(
        session, actor, "1000 rent", now=JAN_15, account_id=world.cash_id
    )
    await flows.confirm_void(session, actor, entry_id=entry.id)
    await session.commit()
    first_voided_at = (await session.get(Entry, entry.id)).voided_at

    asked = await flows.start_void(session, actor, argument=str(entry.id))
    assert asked.text == f"Entry #{entry.id} is already voided."
    # No confirm button: there is nothing left to confirm.
    assert asked.keyboard is None

    tapped = await flows.confirm_void(session, actor, entry_id=entry.id)
    await session.commit()
    assert tapped.text == f"Entry #{entry.id} was already voided."
    assert tapped.toast == flows.ALREADY_DONE
    assert tapped.edit is True

    # The load-bearing assertion: the second void did not re-stamp the first.
    # A moved `voided_at` would silently rewrite when the correction happened.
    assert (await session.get(Entry, entry.id)).voided_at == first_voided_at


# --- [Cancel] and the prompt that has nothing behind it ----------------------
#
# Two buttons that look identical on screen and are not the same thing.
# `cancel_pending` DELETES a pending row; `dismiss` closes a prompt that never
# created one — the [Cancel] beside [Void it]. `tests/test_bot_callbacks.py`
# pins their payloads ("x:7" and "n"), which says nothing at all about what
# happens when either is tapped.


async def test_cancel_drops_the_pending_row_and_writes_nothing(session: AsyncSession):
    """[Cancel] on an account keyboard: the entry never existed.

    The button is taken off the real keyboard rather than assumed, so this
    covers the wiring as well as the flow — a [Cancel] carrying the wrong id
    would cancel a different message's keyboard and leave this one live.

    The second tap is the case that matters as much as the first. Both buttons
    stay on screen until the message is edited, and a double tap is the normal
    way to use a phone.
    """
    world = await build_world(session)
    actor = _actor(world)

    started = await flows.start_entry(session, actor, raw="120 coffee", now=JAN_15)
    pending_id = _pending_id(started.keyboard)
    cancel_button = started.keyboard[-1][-1]
    assert cancel_button.label == keyboards.CANCEL_LABEL
    assert cancel_button.data == callbacks.cancel_pending(pending_id)

    cancelled = await flows.cancel_pending(session, actor, pending_id=pending_id)
    await session.commit()

    assert cancelled.text == flows.CANCELLED
    assert cancelled.toast == flows.CANCELLED
    assert cancelled.edit is True
    # The dead keyboard comes off screen rather than being redrawn.
    assert cancelled.keyboard is None
    await _nothing_written(session, world)

    again = await flows.cancel_pending(session, actor, pending_id=pending_id)
    await session.commit()

    assert again.text == flows.ALREADY_DONE
    assert again.toast == flows.ALREADY_DONE
    assert again.edit is True
    await _nothing_written(session, world)


async def test_a_housemate_cannot_cancel_your_pending_entry(session: AsyncSession):
    """The member scope reaches [Cancel] too, and it has to.

    `pending.cancel` is `claim` underneath, which is what carries the member
    predicate along. Without it a housemate tapping the [Cancel] under someone
    else's message — every keyboard in a shared chat is visible to everyone —
    would make their entry vanish mid-flow with no explanation and nothing to
    tap.
    """
    world = await build_world(session)
    typist = _actor(world)
    housemate = _housemate(world)

    started = await flows.start_entry(session, typist, raw="120 coffee", now=JAN_15)
    pending_id = _pending_id(started.keyboard)

    refused = await flows.cancel_pending(session, housemate, pending_id=pending_id)
    await session.commit()

    # The same answer a dead button gets: a housemate learns nothing about what
    # anyone else has in flight.
    assert refused.text == flows.ALREADY_DONE
    assert refused.toast == flows.ALREADY_DONE
    assert (await session.get(PendingEntry, pending_id)) is not None

    # And the typist's keyboard is still live — refusing everyone would satisfy
    # the assertions above and break the bot.
    recorded = await flows.commit_account_choice(
        session, typist, pending_id=pending_id, account_id=world.cash_id, now=JAN_15
    )
    await session.commit()

    assert recorded.toast == "Recorded"
    entry = await _only_entry(session, world)
    assert entry.member_id == world.member_id


async def test_dismiss_closes_a_void_prompt_without_touching_the_entry(
    session: AsyncSession,
):
    """[Cancel] beside [Void it] — the one button with no pending row behind it.

    Walking away from a void confirmation must leave the money exactly where it
    was. `dismiss` therefore takes no session and no id at all, and this is what
    that buys: the entry is still there, still not voided, and the balance has
    not moved.
    """
    world = await build_world(session)
    actor = _actor(world)

    entry = await _log(
        session, actor, "1000 rent", now=JAN_15, account_id=world.cash_id
    )
    asked = await flows.start_void(session, actor)
    (confirm, cancel), *_ = asked.keyboard
    assert decode(confirm.data).first == entry.id
    # No id on this one. There is no row to name.
    assert cancel.data == callbacks.dismiss()

    dismissed = flows.dismiss()

    assert dismissed.text == flows.CANCELLED
    assert dismissed.toast == flows.CANCELLED
    assert dismissed.edit is True
    assert dismissed.keyboard is None

    # The load-bearing assertions: the entry survived being asked about.
    row = await session.get(Entry, entry.id)
    assert row is not None
    assert row.voided_at is None
    assert await _balance(session, world, world.cash_id) == -100_000
    # And asking the question parked nothing that a later tap could commit.
    assert (
        await session.scalar(
            select(PendingEntry).where(PendingEntry.household_id == world.household_id)
        )
    ) is None


# --- /start and /help -------------------------------------------------------


async def test_start_greets_by_name_and_both_commands_carry_the_same_help(
    session: AsyncSession,
):
    """The only two commands that write nothing, and the only copy of the manual.

    `/start` is `/help` with a greeting on top. If the two ever diverge, one of
    them is the stale one and there is no way to tell which.
    """
    world = await build_world(session)
    actor = _actor(world)

    helped = flows.help_reply()
    greeted = flows.start_reply(actor)

    assert helped.text == flows.HELP
    assert helped.keyboard is None
    assert greeted.text.startswith("Hello Tester.")
    assert flows.HELP in greeted.text
    assert greeted.keyboard is None

    # Every command a user can send is on the one screen that tells them so.
    for command in (
        "/expense",
        "/income",
        "/transfer",
        "/pay",
        "/balances",
        "/last",
        "/void",
    ):
        assert command in helped.text

    await _nothing_written(session, world)


async def test_a_display_name_carrying_markup_is_escaped_not_rendered(
    session: AsyncSession,
):
    """A Telegram display name is arbitrary text, and this bot sends HTML.

    `PARSE_MODE` is HTML, so an unescaped "<b>" in a name does not merely look
    wrong — Telegram rejects the whole message, and the greeting never arrives.
    The name is the only user-controlled text in this reply, so it is the only
    thing that can do it.
    """
    world = await build_world(session)
    hostile = Actor(
        member_id=world.member_id,
        household_id=world.household_id,
        display_name="<b>Bobby</b> & co",
    )

    greeted = flows.start_reply(hostile)

    assert "&lt;b&gt;Bobby&lt;/b&gt; &amp; co" in greeted.text
    assert "<b>Bobby</b>" not in greeted.text
    # The bot's OWN markup is untouched — escaping the name, not the message.
    assert "<b>ChocoFin</b>" in greeted.text
    assert greeted.keyboard is None

    await _nothing_written(session, world)


# --- the bare-text route ----------------------------------------------------


async def test_bare_text_is_the_same_command_as_slash_expense(session: AsyncSession):
    """ "120 coffee" and "/expense 120 coffee" are one path, not two.

    That equivalence is the whole design of `start_entry`: the parser reads the
    leading command itself and defaults to expense when there is none. Asserting
    the two replies are identical is the only way to keep a second parsing rule
    from growing on the bare-text side, where most messages actually arrive.
    """
    world = await build_world(session)
    actor = _actor(world)

    typed = await flows.start_entry(session, actor, raw="120 coffee", now=JAN_15)
    commanded = await flows.start_entry(
        session, actor, raw="/expense 120 coffee", now=JAN_15
    )

    assert typed.text == commanded.text
    assert "Expense ₱120.00" in typed.text
    assert _account_ids(typed.keyboard) == _account_ids(commanded.keyboard)

    pending_id, account_id = _tap(typed.keyboard, world.cash_id)
    recorded = await flows.commit_account_choice(
        session, actor, pending_id=pending_id, account_id=account_id, now=JAN_15
    )
    await session.commit()

    assert recorded.toast == "Recorded"
    entry = await session.scalar(
        select(Entry)
        .where(Entry.household_id == world.household_id)
        .order_by(Entry.id.desc())
        .limit(1)
    )
    assert entry.kind == "expense"
    assert entry.amount_minor == 12_000
    assert entry.note == "coffee"
    assert entry.raw_input == "120 coffee"


async def test_text_with_no_amount_is_refused_and_so_is_an_unknown_command(
    session: AsyncSession,
):
    """Never guess an amount. A message without one is a rejection.

    The second half is why the bare-text handler is registered with `~COMMAND`:
    a mistyped `/expence 120` must not be read as ₱120 of something. The parser
    lets an unrecognised command fall through to the amount check, which then
    fails on the command word itself — the honest reason.
    """
    world = await build_world(session)
    actor = _actor(world)

    reply = await flows.start_entry(session, actor, raw="coffee", now=JAN_15)
    assert "is not an amount" in reply.text
    assert reply.keyboard is None

    mistyped = await flows.start_entry(session, actor, raw="/expence 120", now=JAN_15)
    assert "is not an amount" in mistyped.text
    assert "₱120.00" not in mistyped.text
    assert mistyped.keyboard is None

    await session.commit()
    await _nothing_written(session, world)


# --- a newline where a space was expected -----------------------------------
#
# `_strip_command` split on a literal " ". A mobile keyboard sends "/transfer"
# + newline + "500" routinely — autocomplete puts the command in, the user hits
# return, then types the amount — and `partition(" ")` found no space in that
# message at all: the whole thing stayed as the head, nothing was stripped, and
# the parser was handed the word "transfer", which it refuses outright. The user
# saw an amount rejection for a message that named a perfectly good amount.
#
# The other half of the same bug lives in `bot.handlers._argument`, where the
# consequence was worse than a rejection — see `tests/test_bot_handlers.py`.


@pytest.mark.parametrize("separator", [" ", "\n", "\n\n", " \n ", "\t"])
async def test_a_command_and_its_amount_survive_any_whitespace(
    session: AsyncSession, separator: str
):
    """Whatever separates the command from its argument, the amount gets through.

    Asserted against the plain-space reply rather than against a fixed string:
    what has to hold is that the separator makes NO difference, which is a
    stronger claim than any one rendering of it.
    """
    world = await build_world(session)
    actor = _actor(world)

    expected = await flows.start_transfer(
        session, actor, raw="/transfer 500 top-up", now=JAN_15
    )
    reply = await flows.start_transfer(
        session, actor, raw=f"/transfer{separator}500 top-up", now=JAN_15
    )

    assert reply.text == expected.text
    assert "Transfer ₱500.00" in reply.text
    assert "top-up" in reply.text
    # A keyboard at all: the rejection path returns none, and that is exactly
    # what the user used to get here.
    assert _account_ids(reply.keyboard) == _account_ids(expected.keyboard)


async def test_pay_reads_its_amount_across_a_newline(session: AsyncSession):
    """`/pay` shares `_strip_command`, so it shared the bug."""
    world = await build_world(session)
    actor = _actor(world)

    reply = await flows.start_settlement(session, actor, raw="/pay\n3000", now=JAN_15)

    assert "Settlement ₱3,000.00" in reply.text
    assert "Which card?" in reply.text
    assert reply.keyboard is not None


async def test_a_command_with_nothing_after_it_still_asks_for_an_amount(
    session: AsyncSession,
):
    """The empty case, which the new split has to keep answering the same way.

    A bare `/transfer` and a `/transfer` followed by nothing but whitespace are
    the same message: no amount was given, so the usage line is the reply. A
    split that returned the command itself as the remainder would send the word
    "transfer" to the parser and report it as a bad amount instead.
    """
    world = await build_world(session)
    actor = _actor(world)

    for raw in ("/transfer", "/transfer ", "/transfer\n", "/transfer \n "):
        reply = await flows.start_transfer(session, actor, raw=raw, now=JAN_15)
        assert reply.text == flows.TRANSFER_USAGE, raw
        assert reply.keyboard is None, raw

    settlement = await flows.start_settlement(session, actor, raw="/pay\n", now=JAN_15)
    assert settlement.text == flows.PAY_USAGE

    await session.commit()
    await _nothing_written(session, world)


# --- expiry -----------------------------------------------------------------


async def test_a_tap_on_an_expired_row_records_nothing(session: AsyncSession):
    """A keyboard found in yesterday's scrollback must not book today's money.

    Both expiry checks are exercised, because they are different code and behave
    differently on purpose:

    * `commit_account_choice` goes through `claim`, which returns the expired row
      AND deletes it. Filtering expiry into the DELETE instead would leave dead
      rows in the table and answer "Already recorded", which is false — nothing
      was recorded and nothing now can be.
    * `pick_transfer_source` reads with `get` and only reports; it takes nothing,
      because it commits nothing.
    """
    world = await build_world(session)
    actor = _actor(world)

    entry_started = await flows.start_entry(
        session, actor, raw="120 coffee", now=JAN_15
    )
    entry_pending = _pending_id(entry_started.keyboard)
    transfer_started = await flows.start_transfer(
        session, actor, raw="/transfer 500", now=JAN_15
    )
    transfer_pending = _pending_id(transfer_started.keyboard)

    later = JAN_15 + pending.PENDING_TTL + dt.timedelta(hours=1)

    expired = await flows.commit_account_choice(
        session, actor, pending_id=entry_pending, account_id=world.cash_id, now=later
    )
    assert expired.text == flows.EXPIRED
    assert expired.toast == flows.EXPIRED
    assert expired.edit is True
    # Claimed and gone: the button is not merely refused, it is spent.
    assert (await session.get(PendingEntry, entry_pending)) is None

    refused = await flows.pick_transfer_source(
        session, actor, pending_id=transfer_pending, account_id=world.cash_id, now=later
    )
    assert refused.text == flows.EXPIRED
    assert refused.toast == flows.EXPIRED
    assert refused.keyboard is None
    # Read, not taken — and no source recorded on the way past.
    row = await session.get(PendingEntry, transfer_pending)
    assert row is not None
    assert row.source_account_id is None

    await session.commit()
    assert (
        await session.scalar(
            select(Entry).where(Entry.household_id == world.household_id)
        )
    ) is None
