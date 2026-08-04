"""Fixture data builders.

No real amounts, no real names, no secrets — everything here is invented.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Account, Category, Household, Member

# Manila midnight, expressed in UTC. Every timestamp in the tests is built from
# these so a boundary bug shows up as a wrong period, not a wrong hour.
JAN_15 = dt.datetime(2026, 1, 15, 4, 0, tzinfo=dt.UTC)  # 12:00 Manila
FEB_10 = dt.datetime(2026, 2, 10, 4, 0, tzinfo=dt.UTC)
MAR_20 = dt.datetime(2026, 3, 20, 4, 0, tzinfo=dt.UTC)


class World:
    """A household with the accounts and categories the tests need."""

    def __init__(self) -> None:
        self.household_id: int
        self.member_id: int
        # A second member of the SAME household. The account keyboard is
        # most-recently-used per member, so proving that needs two people
        # with different habits.
        self.other_member_id: int
        self.other_member_telegram_id: int
        self.member_telegram_id: int
        self.cash_id: int
        self.savings_id: int
        self.card_id: int
        # A card with no billing account, so the settlement path that has to
        # ask who is paying is reachable at all.
        self.orphan_card_id: int
        self.excluded_id: int
        self.inactive_id: int
        self.food_id: int
        self.coffee_id: int
        self.groceries_id: int
        self.salary_id: int
        # A DIFFERENT household entirely. Every core call filters on
        # household_id; the only way to prove that is to have a second one
        # whose rows must never appear.
        self.outsider_household_id: int
        self.outsider_member_id: int
        self.outsider_telegram_id: int
        self.outsider_account_id: int


async def build_world(session: AsyncSession) -> World:
    world = World()

    household = Household(name="Test Household")
    session.add(household)
    await session.flush()
    world.household_id = household.id

    member = Member(
        household_id=household.id,
        telegram_user_id=10_000_001,
        display_name="Tester",
        role="owner",
    )
    other_member = Member(
        household_id=household.id,
        telegram_user_id=10_000_002,
        display_name="Housemate",
        role="member",
    )
    session.add_all([member, other_member])
    await session.flush()
    world.member_id = member.id
    world.member_telegram_id = member.telegram_user_id
    world.other_member_id = other_member.id
    world.other_member_telegram_id = other_member.telegram_user_id

    cash = Account(
        household_id=household.id,
        name="Cash",
        type="cash",
        opening_balance_minor=0,
    )
    savings = Account(
        household_id=household.id,
        name="Savings",
        type="savings",
        opening_balance_minor=0,
    )
    card = Account(
        household_id=household.id,
        name="Card",
        type="credit_card",
        opening_balance_minor=0,
        credit_limit_minor=5_000_000,  # PHP 50,000.00
    )
    excluded = Account(
        household_id=household.id,
        name="Excluded",
        type="cash",
        opening_balance_minor=0,
        exclude_from_totals=True,
    )
    orphan_card = Account(
        household_id=household.id,
        name="Orphan Card",
        type="credit_card",
        opening_balance_minor=0,
        credit_limit_minor=1_000_000,
        # No billing_account_id on purpose.
    )
    inactive = Account(
        household_id=household.id,
        name="Closed",
        type="bank",
        opening_balance_minor=0,
        is_active=False,
    )
    session.add_all([cash, savings, card, excluded, orphan_card, inactive])
    await session.flush()
    world.cash_id = cash.id
    world.savings_id = savings.id
    world.card_id = card.id
    world.excluded_id = excluded.id
    world.orphan_card_id = orphan_card.id
    world.inactive_id = inactive.id

    # The card settles from Savings. Everything about a settlement — which
    # account pays, and that it is a transfer rather than an expense — hangs
    # off this one column.
    card.billing_account_id = savings.id
    await session.flush()

    food = Category(household_id=household.id, name="Food & Dining", kind="expense")
    salary = Category(household_id=household.id, name="Salary", kind="income")
    session.add_all([food, salary])
    await session.flush()
    world.food_id = food.id
    world.salary_id = salary.id

    coffee = Category(
        household_id=household.id, name="Coffee", kind="expense", parent_id=food.id
    )
    groceries = Category(
        household_id=household.id, name="Groceries", kind="expense", parent_id=food.id
    )
    session.add_all([coffee, groceries])
    await session.flush()
    world.coffee_id = coffee.id
    world.groceries_id = groceries.id

    # A second household. Nothing in it should ever be reachable from the
    # first, and a test that never builds one can only prove that by accident.
    outsider_household = Household(name="Someone Else")
    session.add(outsider_household)
    await session.flush()
    world.outsider_household_id = outsider_household.id

    outsider_member = Member(
        household_id=outsider_household.id,
        telegram_user_id=10_000_003,
        display_name="Outsider",
        role="owner",
    )
    outsider_account = Account(
        household_id=outsider_household.id,
        name="Their Wallet",
        type="cash",
        opening_balance_minor=0,
    )
    session.add_all([outsider_member, outsider_account])
    await session.flush()
    world.outsider_member_id = outsider_member.id
    world.outsider_telegram_id = outsider_member.telegram_user_id
    world.outsider_account_id = outsider_account.id

    await session.commit()
    return world
