"""Tests for `core.accounts` — choosing an account, not computing what is in one.

Three buttons is all the keyboard gets, so the ordering is the feature. Note
that every entry below is committed on its own: `entries.created_at` defaults to
`now()`, which in Postgres is the TRANSACTION start time, so two entries written
in one transaction share a timestamp exactly and the MRU order would quietly
fall through to the `sort_order, id` tie-break — proving nothing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core import accounts, ledger
from tests.factories import JAN_15, MAR_20, World, build_world

pytestmark = pytest.mark.asyncio


async def _log(
    session: AsyncSession,
    world: World,
    account_id: int,
    *,
    member_id: int | None = None,
    occurred_at: dt.datetime = JAN_15,
):
    """One expense, committed on its own so its `created_at` is distinct."""
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=member_id or world.member_id,
        account_id=account_id,
        amount_minor=10_000,
        occurred_at=occurred_at,
    )
    await session.commit()
    return entry


async def _offered(
    session: AsyncSession, world: World, *, member_id: int | None = None, **kwargs
) -> list[int]:
    return [
        a.id
        for a in await accounts.recent_accounts(
            session,
            household_id=world.household_id,
            member_id=member_id or world.member_id,
            **kwargs,
        )
    ]


# --- get_account ------------------------------------------------------------


async def test_get_account_returns_the_account(session: AsyncSession):
    world = await build_world(session)
    account = await accounts.get_account(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    assert account is not None
    assert account.name == "Cash"


async def test_get_account_is_scoped_to_the_household(session: AsyncSession):
    """None rather than an exception: "the account vanished between the keyboard
    and the tap" is a message to render, not a crash."""
    world = await build_world(session)
    assert (
        await accounts.get_account(
            session,
            household_id=world.household_id,
            account_id=world.outsider_account_id,
        )
        is None
    )


# --- most-recently-used ordering --------------------------------------------


async def test_the_last_account_used_comes_first(session: AsyncSession):
    world = await build_world(session)
    await _log(session, world, world.card_id)
    await _log(session, world, world.savings_id)
    await _log(session, world, world.cash_id)

    offered = await _offered(session, world)
    assert offered[:3] == [world.cash_id, world.savings_id, world.card_id]


async def test_never_used_accounts_sort_after_used_ones(session: AsyncSession):
    """A brand-new household still gets a keyboard, and a used account always
    outranks an unused one however low its id."""
    world = await build_world(session)
    untouched = [world.cash_id, world.savings_id, world.card_id, world.excluded_id]

    # With nothing logged at all, the fallback ordering is sort_order then id.
    assert await _offered(session, world) == [*untouched, world.orphan_card_id]

    # Orphan Card has the HIGHEST id of the active accounts, so if it leads it
    # can only be because it was used.
    await _log(session, world, world.orphan_card_id)
    assert await _offered(session, world) == [world.orphan_card_id, *untouched]


async def test_two_members_get_different_keyboards(session: AsyncSession):
    """One lives out of a wallet, the other out of a card. The whole reason the
    ordering is per member and not per household."""
    world = await build_world(session)
    await _log(session, world, world.cash_id)
    await _log(session, world, world.card_id, member_id=world.other_member_id)

    mine = await _offered(session, world)
    theirs = await _offered(session, world, member_id=world.other_member_id)
    assert mine[0] == world.cash_id
    assert theirs[0] == world.card_id


async def test_recency_is_when_it_was_logged_not_when_the_money_moved(
    session: AsyncSession,
):
    """Backfilling last month's receipt must not reorder tomorrow's buttons.

    Savings holds the LATER `occurred_at`; Cash was logged later. Cash wins —
    someone who remembers a forgotten January expense today has not changed
    which account they habitually reach for.
    """
    world = await build_world(session)
    await _log(session, world, world.savings_id, occurred_at=MAR_20)
    await _log(session, world, world.cash_id, occurred_at=JAN_15)

    offered = await _offered(session, world)
    assert offered[:2] == [world.cash_id, world.savings_id]


async def test_a_voided_entry_does_not_earn_a_button(session: AsyncSession):
    """A void says the entry should never have existed. An account you touched
    only by mistake has no claim on one of three buttons."""
    world = await build_world(session)
    mistake = await _log(session, world, world.orphan_card_id)

    assert (await _offered(session, world))[0] == world.orphan_card_id

    await ledger.void_entry(
        session, household_id=world.household_id, entry_id=mistake.id
    )
    await session.commit()

    # Back to the fallback order, with Orphan Card last on id.
    assert await _offered(session, world) == [
        world.cash_id,
        world.savings_id,
        world.card_id,
        world.excluded_id,
        world.orphan_card_id,
    ]


# --- filters ----------------------------------------------------------------


async def test_types_narrows_to_credit_cards(session: AsyncSession):
    """`/pay` offers cards only."""
    world = await build_world(session)
    await _log(session, world, world.orphan_card_id)

    offered = await _offered(session, world, types=("credit_card",))
    assert offered == [world.orphan_card_id, world.card_id]


async def test_exclude_drops_specific_accounts(session: AsyncSession):
    """A transfer's destination keyboard must not offer the source back."""
    world = await build_world(session)
    offered = await _offered(session, world, exclude=(world.cash_id, world.card_id))
    assert world.cash_id not in offered
    assert world.card_id not in offered
    assert world.savings_id in offered


async def test_limit_takes_the_head_of_the_same_order(session: AsyncSession):
    """`limit=3` and the full list behind [Other…] are one query asked twice."""
    world = await build_world(session)
    await _log(session, world, world.card_id)
    await _log(session, world, world.savings_id)

    full = await _offered(session, world)
    assert await _offered(session, world, limit=3) == full[:3]
    assert full[:2] == [world.savings_id, world.card_id]


async def test_inactive_accounts_are_never_offered(session: AsyncSession):
    world = await build_world(session)
    assert world.inactive_id not in await _offered(session, world)
    # 'Closed' is the household's only bank account, so asking for banks asks
    # for it by name and still gets nothing.
    assert await _offered(session, world, types=("bank",)) == []


async def test_excluded_from_totals_accounts_are_still_offered(session: AsyncSession):
    """That flag is balance and net-worth math, not a picker filter.

    Money spent from an excluded account is still money spent; hiding it here
    would make that spending unloggable, which is worse than leaving it out of
    net worth was ever meant to be.
    """
    world = await build_world(session)
    assert world.excluded_id in await _offered(session, world)

    await _log(session, world, world.excluded_id)
    assert (await _offered(session, world))[0] == world.excluded_id


# --- scoping ----------------------------------------------------------------


async def test_accounts_are_scoped_to_the_household(session: AsyncSession):
    world = await build_world(session)
    theirs = await accounts.recent_accounts(
        session,
        household_id=world.outsider_household_id,
        member_id=world.outsider_member_id,
    )
    assert [a.id for a in theirs] == [world.outsider_account_id]
