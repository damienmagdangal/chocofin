"""Flow-layer tests: real database, real pending rows, no telegram objects.

`bot.flows` functions take `(session, actor, ...)` and return a `Reply`, so the
whole behaviour of a command — the parse, the pending row, the claim, the ledger
write — is reachable from a test with no bot token and no fake `Update`. That is
what the adapter split is for.

This file covers the two things phase 2b changed. The rest of the flow layer is
2c.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import flows
from bot.auth import Actor
from bot.callbacks import decode
from bot.keyboards import Keyboard
from core import accounts as core_accounts
from core import ledger, pending
from core.models import Entry, EntryTag, PendingEntry
from tests.factories import JAN_15, World, build_world

pytestmark = pytest.mark.asyncio


def _actor(world: World) -> Actor:
    return Actor(
        member_id=world.member_id,
        household_id=world.household_id,
        display_name="Tester",
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


async def _only_entry(session: AsyncSession, world: World) -> Entry:
    entry = await session.scalar(
        select(Entry).where(Entry.household_id == world.household_id)
    )
    assert entry is not None
    return entry


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
            session, household_id=world.household_id, pending_id=pending_id
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
