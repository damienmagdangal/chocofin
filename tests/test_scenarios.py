"""End-to-end scenarios.

These are the ones that catch a whole class of bug at once: if transfers leak
into spending, if a card settlement is miscounted as an expense, if a fee is
netted inside a transfer, or if subcategories fail to roll up, one of these
fails.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import balances, ledger
from core.models import Account, Entry
from core.periods import resolve
from tests.factories import FEB_10, JAN_15, build_world

pytestmark = pytest.mark.asyncio

JANUARY = resolve("month", anchor=dt.date(2026, 1, 15))
FEBRUARY = resolve("month", anchor=dt.date(2026, 2, 15))


async def test_card_purchase_then_settlement(session: AsyncSession):
    """The scenario that defines the whole design.

    PHP 3,000.00 card purchase in January, settled from savings in February.

    Afterwards: the card is back to zero, savings is down 3,000.00, and exactly
    3,000.00 of expense is counted in January and none in February. The
    settlement is a transfer, so it must not appear in ANY spending total —
    otherwise the household would appear to have spent 6,000.00 on one coffee
    machine.
    """
    world = await build_world(session)

    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.card_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
        category_id=world.groceries_id,
        note="coffee machine",
    )
    await session.commit()

    # Settlement: billing account -> card account. Never an expense.
    await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.savings_id,
        destination_account_id=world.card_id,
        amount_minor=300_000,
        occurred_at=FEB_10,
        note="card settlement",
    )
    await session.commit()

    card = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.card_id
    )
    savings = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.savings_id
    )
    assert card.balance_minor == 0
    assert savings.balance_minor == -300_000

    january = await ledger.summarise(
        session,
        household_id=world.household_id,
        start_utc=JANUARY[0],
        end_utc=JANUARY[1],
    )
    february = await ledger.summarise(
        session,
        household_id=world.household_id,
        start_utc=FEBRUARY[0],
        end_utc=FEBRUARY[1],
    )
    assert january.expense_minor == 300_000
    assert february.expense_minor == 0
    assert february.income_minor == 0


async def test_card_available_credit_recovers_after_settlement(
    session: AsyncSession,
):
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

    mid = await balances.available_credit(
        session, household_id=world.household_id, account_id=world.card_id
    )
    assert mid == 4_700_000

    await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.savings_id,
        destination_account_id=world.card_id,
        amount_minor=300_000,
        occurred_at=FEB_10,
    )
    await session.commit()

    after = await balances.available_credit(
        session, household_id=world.household_id, account_id=world.card_id
    )
    assert after == 5_000_000


async def test_transfer_fee_is_a_separate_expense_not_a_leg(
    session: AsyncSession,
):
    """A PHP 50.00 fee on a transfer.

    The fee is real money leaving the household, so it must land in the expense
    total. As a third leg it would both break sum-to-zero and vanish from every
    spending report.
    """
    world = await build_world(session)

    transfer = await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.savings_id,
        destination_account_id=world.cash_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
        note="move to cash",
        fee_minor=5_000,
        fee_account_id=world.savings_id,
    )
    await session.commit()

    # The transfer itself is still exactly two legs summing to zero. Asserted
    # here rather than left to the trigger: the trigger refuses a THIRD leg, but
    # a fee netted into the source leg — 305,000 out, 300,000 in — would break
    # sum-to-zero, and a fee entry written with the wrong sign or against the
    # wrong account would satisfy every constraint in the database.
    transfer_legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=transfer.id
    )
    assert len(transfer_legs) == 2
    by_role = {leg.leg_role: leg for leg in transfer_legs}
    assert set(by_role) == {"source", "destination"}
    assert by_role["source"].account_id == world.savings_id
    assert by_role["source"].amount_minor == -300_000
    assert by_role["destination"].account_id == world.cash_id
    assert by_role["destination"].amount_minor == 300_000
    assert sum(leg.amount_minor for leg in transfer_legs) == 0
    # And the fee is nowhere among them, in either direction.
    assert 5_000 not in {abs(leg.amount_minor) for leg in transfer_legs}

    # It is its own entry instead, pointing back at the transfer.
    related = list(
        await session.scalars(
            select(Entry).where(Entry.related_entry_id == transfer.id)
        )
    )
    assert len(related) == 1
    fee = related[0]
    assert fee.kind == "expense"
    assert fee.amount_minor == 5_000
    assert fee.related_entry_id == transfer.id

    # One leg, negative, on the account that paid the fee — the shape of an
    # expense, which is what a fee is.
    fee_legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=fee.id
    )
    (fee_leg,) = fee_legs
    assert fee_leg.leg_role == "source"
    assert fee_leg.account_id == world.savings_id
    assert fee_leg.amount_minor == -5_000

    summary = await ledger.summarise(
        session,
        household_id=world.household_id,
        start_utc=JANUARY[0],
        end_utc=JANUARY[1],
    )
    # The 3,000.00 transfer is invisible; the 50.00 fee is not.
    assert summary.expense_minor == 5_000

    savings = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.savings_id
    )
    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    assert savings.balance_minor == -305_000
    assert cash.balance_minor == 300_000


async def test_subcategory_totals_roll_up_to_the_parent(session: AsyncSession):
    """Coffee and Groceries both sit under Food & Dining."""
    world = await build_world(session)

    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=15_000,
        occurred_at=JAN_15,
        category_id=world.coffee_id,
    )
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=250_000,
        occurred_at=JAN_15,
        category_id=world.groceries_id,
    )
    await session.commit()

    rolled = await ledger.summarise(
        session,
        household_id=world.household_id,
        start_utc=JANUARY[0],
        end_utc=JANUARY[1],
        group_by="parent",
    )
    assert dict((c.category_id, c.total_minor) for c in rolled.by_category) == {
        world.food_id: 265_000
    }

    leaf = await ledger.summarise(
        session,
        household_id=world.household_id,
        start_utc=JANUARY[0],
        end_utc=JANUARY[1],
        group_by="leaf",
    )
    assert dict((c.category_id, c.total_minor) for c in leaf.by_category) == {
        world.coffee_id: 15_000,
        world.groceries_id: 250_000,
    }

    # Both groupings must still add up to the same period total.
    assert (
        sum(c.total_minor for c in rolled.by_category)
        == sum(c.total_minor for c in leaf.by_category)
        == rolled.expense_minor
    )


async def test_a_full_month_of_activity_reconciles(session: AsyncSession):
    """Income, spending, a transfer and a correction, all in one month.

    Balances and the summary must agree on what happened.

    The household does not start from nothing, and every figure below is a
    MOVEMENT measured against where the accounts under test began. Asserting
    absolute balances would only work while every account happens to open at
    zero — true of the factory today, enforced nowhere, and the January the
    ledger has to survive is never the first one.
    """
    world = await build_world(session)
    common = {
        "household_id": world.household_id,
        "member_id": world.member_id,
        "occurred_at": JAN_15,
    }

    cash_account = await session.scalar(
        select(Account).where(Account.id == world.cash_id)
    )
    savings_account = await session.scalar(
        select(Account).where(Account.id == world.savings_id)
    )
    cash_account.opening_balance_minor = 200_000
    savings_account.opening_balance_minor = 1_500_000
    await session.commit()

    opening_net = await balances.net_worth_minor(
        session, household_id=world.household_id
    )
    opening_cash = (
        await balances.account_balance(
            session, household_id=world.household_id, account_id=world.cash_id
        )
    ).balance_minor
    opening_savings = (
        await balances.account_balance(
            session, household_id=world.household_id, account_id=world.savings_id
        )
    ).balance_minor

    await ledger.create_income(
        session, account_id=world.cash_id, amount_minor=4_500_000, **common
    )
    await ledger.create_expense(
        session, account_id=world.cash_id, amount_minor=125_050, **common
    )
    mistake = await ledger.create_expense(
        session, account_id=world.cash_id, amount_minor=32_000, **common
    )
    await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.cash_id,
        destination_account_id=world.savings_id,
        amount_minor=1_000_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    # The 320.00 lunch was actually paid from savings.
    await ledger.reassign_account(
        session,
        household_id=world.household_id,
        entry_id=mistake.id,
        account_id=world.savings_id,
    )
    await session.commit()

    summary = await ledger.summarise(
        session,
        household_id=world.household_id,
        start_utc=JANUARY[0],
        end_utc=JANUARY[1],
    )
    assert summary.income_minor == 4_500_000
    assert summary.expense_minor == 125_050 + 32_000  # counted once, not twice

    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    savings = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.savings_id
    )
    # cash: +4,500,000 income  -125,050 groceries  -1,000,000 transfer out
    assert cash.balance_minor - opening_cash == 4_500_000 - 125_050 - 1_000_000
    # savings: +1,000,000 transfer in  -32,000 the reassigned lunch
    assert savings.balance_minor - opening_savings == 1_000_000 - 32_000

    net = await balances.net_worth_minor(session, household_id=world.household_id)
    # The transfer nets to zero across the two accounts, so the only thing that
    # moved net worth is the month's income less its spending.
    assert net - opening_net == summary.income_minor - summary.expense_minor
    assert net - opening_net == (cash.balance_minor - opening_cash) + (
        savings.balance_minor - opening_savings
    )
