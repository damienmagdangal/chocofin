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
        self.cash_id: int
        self.savings_id: int
        self.card_id: int
        self.excluded_id: int
        self.food_id: int
        self.coffee_id: int
        self.groceries_id: int
        self.salary_id: int


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
    session.add(member)
    await session.flush()
    world.member_id = member.id

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
    session.add_all([cash, savings, card, excluded])
    await session.flush()
    world.cash_id = cash.id
    world.savings_id = savings.id
    world.card_id = card.id
    world.excluded_id = excluded.id

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

    await session.commit()
    return world
