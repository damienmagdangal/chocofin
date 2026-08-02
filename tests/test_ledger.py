"""Ledger operation tests."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import balances, ledger
from core.errors import (
    AccountNotFoundError,
    EntryAlreadyVoidedError,
    EntryNotFoundError,
    InvalidAmountError,
    SameAccountTransferError,
)
from core.models import Entry, EntryLeg
from core.periods import resolve
from tests.factories import FEB_10, JAN_15, MAR_20, build_world

pytestmark = pytest.mark.asyncio


def january():
    return resolve("month", anchor=dt.date(2026, 1, 15))


def february():
    return resolve("month", anchor=dt.date(2026, 2, 15))


def march():
    return resolve("month", anchor=dt.date(2026, 3, 15))


# --- money survives the round trip -----------------------------------------


@pytest.mark.parametrize(
    "amount_minor",
    [
        1,  # one centavo
        50,
        10_000,
        125_050,  # 1,250.50
        4_500_000,
        999_999_999_999,  # far beyond any real household, still exact
    ],
)
async def test_money_round_trips_without_precision_loss(
    session: AsyncSession, amount_minor: int
):
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=amount_minor,
        occurred_at=JAN_15,
    )
    await session.commit()

    stored = await session.scalar(select(Entry).where(Entry.id == entry.id))
    leg = await session.scalar(select(EntryLeg).where(EntryLeg.entry_id == entry.id))
    assert stored.amount_minor == amount_minor
    assert isinstance(stored.amount_minor, int)
    assert leg.amount_minor == -amount_minor


async def test_amounts_must_be_positive_integers(session: AsyncSession):
    world = await build_world(session)
    for bad in (0, -1, -100):
        with pytest.raises(InvalidAmountError):
            await ledger.create_expense(
                session,
                household_id=world.household_id,
                member_id=world.member_id,
                account_id=world.cash_id,
                amount_minor=bad,
                occurred_at=JAN_15,
            )


# --- leg shape --------------------------------------------------------------


async def test_expense_has_one_negative_source_leg(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    legs = list(
        await session.scalars(select(EntryLeg).where(EntryLeg.entry_id == entry.id))
    )
    assert len(legs) == 1
    assert legs[0].leg_role == "source"
    assert legs[0].amount_minor == -10_000


async def test_income_has_one_positive_destination_leg(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_income(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=4_500_000,
        occurred_at=JAN_15,
        category_id=world.salary_id,
    )
    await session.commit()

    legs = list(
        await session.scalars(select(EntryLeg).where(EntryLeg.entry_id == entry.id))
    )
    assert len(legs) == 1
    assert legs[0].leg_role == "destination"
    assert legs[0].amount_minor == 4_500_000


async def test_transfer_legs_sum_to_zero(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.savings_id,
        destination_account_id=world.cash_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    total = await session.scalar(
        select(func.sum(EntryLeg.amount_minor)).where(EntryLeg.entry_id == entry.id)
    )
    assert total == 0


async def test_every_entry_has_at_least_one_leg(session: AsyncSession):
    """The universal part of the leg rule: sum-to-zero is transfer-only, but
    'no entry is legless' holds for every kind."""
    world = await build_world(session)
    common = {
        "household_id": world.household_id,
        "member_id": world.member_id,
        "occurred_at": JAN_15,
    }
    await ledger.create_expense(
        session, account_id=world.cash_id, amount_minor=1_000, **common
    )
    await ledger.create_income(
        session, account_id=world.cash_id, amount_minor=2_000, **common
    )
    await ledger.create_transfer(
        session,
        source_account_id=world.cash_id,
        destination_account_id=world.savings_id,
        amount_minor=500,
        **common,
    )
    await session.commit()

    rows = (
        await session.execute(
            select(Entry.id, func.count(EntryLeg.id))
            .outerjoin(EntryLeg, EntryLeg.entry_id == Entry.id)
            .group_by(Entry.id)
        )
    ).all()
    assert rows
    assert all(count >= 1 for _, count in rows)


async def test_transfer_rejects_same_account(session: AsyncSession):
    world = await build_world(session)
    with pytest.raises(SameAccountTransferError):
        await ledger.create_transfer(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            source_account_id=world.cash_id,
            destination_account_id=world.cash_id,
            amount_minor=1_000,
            occurred_at=JAN_15,
        )


# --- no account is ever defaulted -------------------------------------------


async def test_account_must_belong_to_the_household(session: AsyncSession):
    world = await build_world(session)
    with pytest.raises(AccountNotFoundError):
        await ledger.create_expense(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            account_id=999_999,
            amount_minor=1_000,
            occurred_at=JAN_15,
        )


# --- voiding ----------------------------------------------------------------


async def test_void_leaves_the_original_readable(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
        note="coffee",
    )
    await session.commit()

    await ledger.void_entry(session, household_id=world.household_id, entry_id=entry.id)
    await session.commit()

    still_there = await session.scalar(select(Entry).where(Entry.id == entry.id))
    assert still_there is not None
    assert still_there.voided_at is not None
    assert still_there.note == "coffee"
    assert still_there.amount_minor == 10_000


async def test_void_is_not_idempotent_and_says_so(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=JAN_15,
    )
    await session.commit()
    await ledger.void_entry(session, household_id=world.household_id, entry_id=entry.id)
    await session.commit()

    with pytest.raises(EntryAlreadyVoidedError):
        await ledger.void_entry(
            session, household_id=world.household_id, entry_id=entry.id
        )


async def test_void_is_scoped_to_the_household(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    with pytest.raises(EntryNotFoundError):
        await ledger.void_entry(
            session, household_id=world.household_id + 1_000, entry_id=entry.id
        )


async def test_voided_entry_leaves_the_summary(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    start, end = january()
    before = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )
    assert before.expense_minor == 10_000

    await ledger.void_entry(session, household_id=world.household_id, entry_id=entry.id)
    await session.commit()

    after = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )
    assert after.expense_minor == 0


# --- reassign_account -------------------------------------------------------


async def test_reassign_leaves_exactly_one_live_entry(session: AsyncSession):
    world = await build_world(session)
    original = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    replacement = await ledger.reassign_account(
        session,
        household_id=world.household_id,
        entry_id=original.id,
        account_id=world.savings_id,
    )
    await session.commit()

    live = list(await session.scalars(select(Entry).where(Entry.voided_at.is_(None))))
    assert len(live) == 1
    assert live[0].id == replacement.id
    assert replacement.replaces_entry_id == original.id

    # And the original is still on file, unmodified apart from the void stamp.
    old = await session.scalar(select(Entry).where(Entry.id == original.id))
    assert old.voided_at is not None
    assert old.amount_minor == 10_000


async def test_reassign_preserves_occurred_at(session: AsyncSession):
    """Correcting a January entry in March must leave the money in January.

    A replacement stamped with now() would move PHP 100.00 of January spending
    into March — and would still pass the 'exactly one live entry' test above
    while doing it.
    """
    world = await build_world(session)
    original = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    # The correction happens in March.
    replacement = await ledger.reassign_account(
        session,
        household_id=world.household_id,
        entry_id=original.id,
        account_id=world.savings_id,
        voided_at=MAR_20,
    )
    await session.commit()

    assert replacement.occurred_at == JAN_15

    jan_start, jan_end = january()
    mar_start, mar_end = march()
    jan = await ledger.summarise(
        session, household_id=world.household_id, start_utc=jan_start, end_utc=jan_end
    )
    mar = await ledger.summarise(
        session, household_id=world.household_id, start_utc=mar_start, end_utc=mar_end
    )
    assert jan.expense_minor == 10_000
    assert mar.expense_minor == 0


async def test_reassign_does_not_double_count_in_any_period(session: AsyncSession):
    world = await build_world(session)
    original = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    await ledger.reassign_account(
        session,
        household_id=world.household_id,
        entry_id=original.id,
        account_id=world.savings_id,
    )
    await session.commit()

    for start, end in (january(), february(), march()):
        summary = await ledger.summarise(
            session, household_id=world.household_id, start_utc=start, end_utc=end
        )
        assert summary.expense_minor in (0, 10_000)

    year_start, year_end = resolve("year", anchor=dt.date(2026, 6, 1))
    year = await ledger.summarise(
        session, household_id=world.household_id, start_utc=year_start, end_utc=year_end
    )
    assert year.expense_minor == 10_000


async def test_reassign_moves_the_money_to_the_new_account(session: AsyncSession):
    world = await build_world(session)
    original = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    await ledger.reassign_account(
        session,
        household_id=world.household_id,
        entry_id=original.id,
        account_id=world.savings_id,
    )
    await session.commit()

    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    savings = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.savings_id
    )
    assert cash.balance_minor == 0
    assert savings.balance_minor == -10_000


async def test_reassign_refuses_transfers(session: AsyncSession):
    world = await build_world(session)
    transfer = await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.savings_id,
        destination_account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    with pytest.raises(InvalidAmountError):
        await ledger.reassign_account(
            session,
            household_id=world.household_id,
            entry_id=transfer.id,
            account_id=world.cash_id,
        )


# --- list_entries -----------------------------------------------------------


async def test_list_entries_hides_voided_by_default(session: AsyncSession):
    world = await build_world(session)
    kept = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=JAN_15,
    )
    dropped = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=2_000,
        occurred_at=JAN_15,
    )
    await session.commit()
    await ledger.void_entry(
        session, household_id=world.household_id, entry_id=dropped.id
    )
    await session.commit()

    live = await ledger.list_entries(session, household_id=world.household_id)
    assert [e.id for e in live] == [kept.id]

    everything = await ledger.list_entries(
        session, household_id=world.household_id, include_voided=True
    )
    assert {e.id for e in everything} == {kept.id, dropped.id}


async def test_list_entries_is_scoped_to_the_household(session: AsyncSession):
    world = await build_world(session)
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    assert (
        await ledger.list_entries(session, household_id=world.household_id + 1_000)
        == []
    )


async def test_list_entries_period_is_half_open(session: AsyncSession):
    """An entry at the exact boundary belongs to the later period only."""
    world = await build_world(session)
    jan_start, jan_end = january()

    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=jan_end,  # first instant of February, Manila
    )
    await session.commit()

    in_january = await ledger.list_entries(
        session,
        household_id=world.household_id,
        start_utc=jan_start,
        end_utc=jan_end,
    )
    feb_start, feb_end = february()
    in_february = await ledger.list_entries(
        session,
        household_id=world.household_id,
        start_utc=feb_start,
        end_utc=feb_end,
    )
    assert in_january == []
    assert len(in_february) == 1


# --- summarise --------------------------------------------------------------


async def test_summarise_excludes_transfers(session: AsyncSession):
    world = await build_world(session)
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.savings_id,
        destination_account_id=world.cash_id,
        amount_minor=500_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    start, end = january()
    summary = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )
    assert summary.expense_minor == 10_000  # not 510_000
    assert summary.income_minor == 0


async def test_summarise_ignores_exclude_from_totals(session: AsyncSession):
    """Money spent from an excluded account is still spending. That flag is
    balance-only; a summary must never consult it."""
    world = await build_world(session)
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.excluded_id,
        amount_minor=25_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    start, end = january()
    summary = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )
    assert summary.expense_minor == 25_000

    # ...but it IS left out of net worth.
    net = await balances.net_worth_minor(session, household_id=world.household_id)
    assert net == 0


async def test_summarise_reports_income_and_expense_separately(
    session: AsyncSession,
):
    world = await build_world(session)
    await ledger.create_income(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=4_500_000,
        occurred_at=JAN_15,
        category_id=world.salary_id,
    )
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=125_050,
        occurred_at=JAN_15,
        category_id=world.groceries_id,
    )
    await session.commit()

    start, end = january()
    summary = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )
    assert summary.income_minor == 4_500_000
    assert summary.expense_minor == 125_050
    assert summary.net_minor == 4_374_950


async def test_summarise_keeps_uncategorised_spending_visible(
    session: AsyncSession,
):
    """The category breakdown must always sum to the period total."""
    world = await build_world(session)
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=7_000,
        occurred_at=JAN_15,
        category_id=None,
    )
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=3_000,
        occurred_at=JAN_15,
        category_id=world.coffee_id,
    )
    await session.commit()

    start, end = january()
    summary = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )
    assert sum(c.total_minor for c in summary.by_category) == summary.expense_minor
    assert any(c.category_id is None for c in summary.by_category)


async def test_summarise_is_scoped_to_the_household(session: AsyncSession):
    world = await build_world(session)
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    start, end = january()
    other = await ledger.summarise(
        session,
        household_id=world.household_id + 1_000,
        start_utc=start,
        end_utc=end,
    )
    assert other.expense_minor == 0
    assert other.income_minor == 0


async def test_summarise_respects_the_half_open_boundary(session: AsyncSession):
    world = await build_world(session)
    jan_start, jan_end = january()

    # Last instant of January, Manila.
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=jan_end - dt.timedelta(microseconds=1),
    )
    # First instant of February, Manila.
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=2_000,
        occurred_at=jan_end,
    )
    await session.commit()

    jan = await ledger.summarise(
        session, household_id=world.household_id, start_utc=jan_start, end_utc=jan_end
    )
    feb_start, feb_end = february()
    feb = await ledger.summarise(
        session, household_id=world.household_id, start_utc=feb_start, end_utc=feb_end
    )
    assert jan.expense_minor == 1_000
    assert feb.expense_minor == 2_000


# --- balances ---------------------------------------------------------------


async def test_balance_is_derived_from_legs(session: AsyncSession):
    world = await build_world(session)
    await ledger.create_income(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=100_000,
        occurred_at=JAN_15,
    )
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=30_000,
        occurred_at=FEB_10,
    )
    await session.commit()

    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    assert cash.balance_minor == 70_000


async def test_balance_includes_transfers(session: AsyncSession):
    """The mirror image of summarise: transfers count here, always."""
    world = await build_world(session)
    await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.savings_id,
        destination_account_id=world.cash_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    savings = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.savings_id
    )
    assert cash.balance_minor == 300_000
    assert savings.balance_minor == -300_000


async def test_voided_entries_do_not_move_balances(session: AsyncSession):
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=10_000,
        occurred_at=JAN_15,
    )
    await session.commit()
    await ledger.void_entry(session, household_id=world.household_id, entry_id=entry.id)
    await session.commit()

    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    assert cash.balance_minor == 0


async def test_available_credit_is_limit_plus_balance(session: AsyncSession):
    world = await build_world(session)
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.card_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    credit = await balances.available_credit(
        session, household_id=world.household_id, account_id=world.card_id
    )
    # 50,000.00 limit less 3,000.00 spent.
    assert credit == 5_000_000 - 300_000


async def test_available_credit_is_none_for_non_cards(session: AsyncSession):
    world = await build_world(session)
    credit = await balances.available_credit(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    assert credit is None


async def test_opening_balance_is_part_of_the_derived_balance(
    session: AsyncSession,
):
    world = await build_world(session)
    from core.models import Account

    account = await session.scalar(select(Account).where(Account.id == world.cash_id))
    account.opening_balance_minor = 50_000
    await session.commit()

    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    assert cash.balance_minor == 50_000
