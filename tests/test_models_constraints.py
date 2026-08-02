"""Database constraint tests.

These drive transactions by hand rather than through a session fixture. The
whole point is WHEN a rule fires: a DEFERRABLE constraint trigger must stay
quiet mid-transaction and speak at COMMIT. A test that never reaches a real
COMMIT cannot tell a deferred trigger from an immediate one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

pytestmark = pytest.mark.asyncio

FAILS_AT_COMMIT = (IntegrityError, DBAPIError)


async def seed(conn: AsyncConnection) -> dict[str, int]:
    """Household, member and two accounts, committed."""
    household_id = await conn.scalar(
        text("INSERT INTO households (name) VALUES ('H') RETURNING id")
    )
    member_id = await conn.scalar(
        text(
            "INSERT INTO members (household_id, telegram_user_id, display_name, role)"
            " VALUES (:h, 20000001, 'M', 'owner') RETURNING id"
        ),
        {"h": household_id},
    )
    cash_id = await conn.scalar(
        text(
            "INSERT INTO accounts (household_id, name, type) "
            "VALUES (:h, 'Cash', 'cash') RETURNING id"
        ),
        {"h": household_id},
    )
    savings_id = await conn.scalar(
        text(
            "INSERT INTO accounts (household_id, name, type) "
            "VALUES (:h, 'Savings', 'savings') RETURNING id"
        ),
        {"h": household_id},
    )
    await conn.commit()
    return {
        "household_id": household_id,
        "member_id": member_id,
        "cash_id": cash_id,
        "savings_id": savings_id,
    }


async def insert_entry(
    conn: AsyncConnection, ids: dict[str, int], kind: str, amount_minor: int
) -> int:
    return await conn.scalar(
        text(
            "INSERT INTO entries "
            "(household_id, member_id, kind, amount_minor, occurred_at, source) "
            "VALUES (:h, :m, :k, :a, now(), 'telegram') RETURNING id"
        ),
        {"h": ids["household_id"], "m": ids["member_id"], "k": kind, "a": amount_minor},
    )


async def insert_leg(
    conn: AsyncConnection,
    ids: dict[str, int],
    entry_id: int,
    account_id: int,
    amount_minor: int,
    leg_role: str,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO entry_legs "
            "(entry_id, household_id, account_id, amount_minor, leg_role) "
            "VALUES (:e, :h, :ac, :am, :r)"
        ),
        {
            "e": entry_id,
            "h": ids["household_id"],
            "ac": account_id,
            "am": amount_minor,
            "r": leg_role,
        },
    )


# --- the four required leg-trigger tests -----------------------------------


async def test_a_trigger_is_deferred_not_immediate(connection: AsyncConnection):
    """A transfer is built one leg at a time and is legitimately unbalanced in
    between. The first leg must NOT raise — that is what DEFERRED buys, and an
    immediate trigger would reject every transfer ever written."""
    ids = await seed(connection)

    entry_id = await insert_entry(connection, ids, "transfer", 300_000)
    # One leg only. Unbalanced right now, and that must be tolerated.
    await insert_leg(connection, ids, entry_id, ids["savings_id"], -300_000, "source")

    # The proof: we got here without an exception.
    await insert_leg(connection, ids, entry_id, ids["cash_id"], 300_000, "destination")
    await connection.commit()

    legs = await connection.scalar(
        text("SELECT count(*) FROM entry_legs WHERE entry_id = :e"), {"e": entry_id}
    )
    assert legs == 2


async def test_b_unbalanced_transfer_fails_at_commit(connection: AsyncConnection):
    ids = await seed(connection)

    entry_id = await insert_entry(connection, ids, "transfer", 300_000)
    await insert_leg(connection, ids, entry_id, ids["savings_id"], -300_000, "source")

    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


async def test_c_inverted_transfer_legs_fail_even_though_they_sum_to_zero(
    connection: AsyncConnection,
):
    """source +3000 and destination -3000 sums to zero. A sum-only check would
    wave this through; the money would move the wrong way."""
    ids = await seed(connection)

    entry_id = await insert_entry(connection, ids, "transfer", 300_000)
    await insert_leg(connection, ids, entry_id, ids["savings_id"], 300_000, "source")
    await insert_leg(connection, ids, entry_id, ids["cash_id"], -300_000, "destination")

    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


async def test_d_entry_amount_must_match_its_legs(connection: AsyncConnection):
    """An expense claiming PHP 50.00 while its leg moves PHP 30.00. The legs are
    internally valid and the sign shape is right — only the cross-check catches
    the drift between the display amount and the money."""
    ids = await seed(connection)

    entry_id = await insert_entry(connection, ids, "expense", 500_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], -300_000, "source")

    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


# --- the rest of the leg shape ---------------------------------------------


async def test_entry_with_no_legs_fails_at_commit(connection: AsyncConnection):
    ids = await seed(connection)
    await insert_entry(connection, ids, "expense", 100_000)
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


async def test_expense_with_two_legs_fails(connection: AsyncConnection):
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "expense", 100_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], -50_000, "source")
    await insert_leg(connection, ids, entry_id, ids["savings_id"], -50_000, "source")
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


async def test_expense_with_a_positive_leg_fails(connection: AsyncConnection):
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "expense", 100_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], 100_000, "destination")
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


async def test_income_with_a_negative_leg_fails(connection: AsyncConnection):
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "income", 100_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], -100_000, "source")
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


async def test_valid_expense_commits(connection: AsyncConnection):
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "expense", 100_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], -100_000, "source")
    await connection.commit()
    assert entry_id is not None


async def test_valid_income_commits(connection: AsyncConnection):
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "income", 100_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], 100_000, "destination")
    await connection.commit()
    assert entry_id is not None


async def test_zero_amount_leg_rejected(connection: AsyncConnection):
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "expense", 100_000)
    with pytest.raises(FAILS_AT_COMMIT):
        await insert_leg(connection, ids, entry_id, ids["cash_id"], 0, "source")
        await connection.commit()


# --- single-row CHECKs ------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1, -100_000])
async def test_entry_amount_must_be_positive(connection: AsyncConnection, amount: int):
    """`entries.amount_minor` is the unsigned display amount."""
    ids = await seed(connection)
    with pytest.raises(FAILS_AT_COMMIT):
        await insert_entry(connection, ids, "expense", amount)
        await connection.commit()


async def test_currency_check_rejects_non_php(connection: AsyncConnection):
    ids = await seed(connection)
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.execute(
            text(
                "INSERT INTO entries (household_id, member_id, kind, amount_minor, "
                "currency, occurred_at, source) "
                "VALUES (:h, :m, 'expense', 1000, 'USD', now(), 'telegram')"
            ),
            {"h": ids["household_id"], "m": ids["member_id"]},
        )
        await connection.commit()


async def test_kind_check_rejects_unknown_kind(connection: AsyncConnection):
    ids = await seed(connection)
    with pytest.raises(FAILS_AT_COMMIT):
        await insert_entry(connection, ids, "refund", 1000)
        await connection.commit()


async def test_household_currency_check_rejects_non_php(
    connection: AsyncConnection,
):
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.execute(
            text("INSERT INTO households (name, base_currency) VALUES ('X', 'USD')")
        )
        await connection.commit()


async def test_household_timezone_check_rejects_other_zones(
    connection: AsyncConnection,
):
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.execute(
            text("INSERT INTO households (name, timezone) VALUES ('X', 'UTC')")
        )
        await connection.commit()


async def test_transfer_cannot_carry_a_category(connection: AsyncConnection):
    ids = await seed(connection)
    category_id = await connection.scalar(
        text(
            "INSERT INTO categories (household_id, name, kind) "
            "VALUES (:h, 'Food', 'expense') RETURNING id"
        ),
        {"h": ids["household_id"]},
    )
    with pytest.raises(FAILS_AT_COMMIT):
        await connection.execute(
            text(
                "INSERT INTO entries (household_id, member_id, kind, amount_minor, "
                "category_id, occurred_at, source) "
                "VALUES (:h, :m, 'transfer', 1000, :c, now(), 'telegram')"
            ),
            {"h": ids["household_id"], "m": ids["member_id"], "c": category_id},
        )
        await connection.commit()


# --- tenant integrity: the composite foreign keys ---------------------------


async def test_leg_household_cannot_drift_from_its_entry(
    connection: AsyncConnection,
):
    """The reason `entry_legs.household_id` is safe to denormalise at all."""
    ids = await seed(connection)
    other_household = await connection.scalar(
        text("INSERT INTO households (name) VALUES ('Other') RETURNING id")
    )
    other_account = await connection.scalar(
        text(
            "INSERT INTO accounts (household_id, name, type) "
            "VALUES (:h, 'Cash', 'cash') RETURNING id"
        ),
        {"h": other_household},
    )
    entry_id = await insert_entry(connection, ids, "expense", 100_000)

    with pytest.raises(FAILS_AT_COMMIT):
        await connection.execute(
            text(
                "INSERT INTO entry_legs "
                "(entry_id, household_id, account_id, amount_minor, leg_role) "
                "VALUES (:e, :h, :a, -100000, 'source')"
            ),
            {"e": entry_id, "h": other_household, "a": other_account},
        )
        await connection.commit()


async def test_entry_cannot_reference_another_households_account(
    connection: AsyncConnection,
):
    ids = await seed(connection)
    other_household = await connection.scalar(
        text("INSERT INTO households (name) VALUES ('Other') RETURNING id")
    )
    other_account = await connection.scalar(
        text(
            "INSERT INTO accounts (household_id, name, type) "
            "VALUES (:h, 'Cash', 'cash') RETURNING id"
        ),
        {"h": other_household},
    )
    entry_id = await insert_entry(connection, ids, "expense", 100_000)

    with pytest.raises(FAILS_AT_COMMIT):
        await insert_leg(connection, ids, entry_id, other_account, -100_000, "source")
        await connection.commit()


# --- category rules ---------------------------------------------------------


async def make_category(
    conn: AsyncConnection,
    household_id: int,
    name: str,
    kind: str,
    parent_id: int | None = None,
) -> int:
    return await conn.scalar(
        text(
            "INSERT INTO categories (household_id, name, kind, parent_id) "
            "VALUES (:h, :n, :k, :p) RETURNING id"
        ),
        {"h": household_id, "n": name, "k": kind, "p": parent_id},
    )


async def test_two_level_categories_are_allowed(connection: AsyncConnection):
    ids = await seed(connection)
    h = ids["household_id"]
    food = await make_category(connection, h, "Food", "expense")
    coffee = await make_category(connection, h, "Coffee", "expense", food)
    await connection.commit()
    assert coffee is not None


async def test_three_level_category_is_rejected(connection: AsyncConnection):
    ids = await seed(connection)
    h = ids["household_id"]
    food = await make_category(connection, h, "Food", "expense")
    coffee = await make_category(connection, h, "Coffee", "expense", food)
    await connection.commit()

    with pytest.raises(FAILS_AT_COMMIT):
        await make_category(connection, h, "Espresso", "expense", coffee)
        await connection.commit()


async def test_child_category_kind_must_match_its_parent(
    connection: AsyncConnection,
):
    """An income subcategory under an expense parent would land its entries in
    the wrong half of every summary."""
    ids = await seed(connection)
    h = ids["household_id"]
    food = await make_category(connection, h, "Food", "expense")
    await connection.commit()

    with pytest.raises(FAILS_AT_COMMIT):
        await make_category(connection, h, "Refunds", "income", food)
        await connection.commit()


async def test_category_cannot_be_its_own_parent(connection: AsyncConnection):
    ids = await seed(connection)
    h = ids["household_id"]
    food = await make_category(connection, h, "Food", "expense")
    await connection.commit()

    with pytest.raises(FAILS_AT_COMMIT):
        await connection.execute(
            text("UPDATE categories SET parent_id = id WHERE id = :c"), {"c": food}
        )
        await connection.commit()


async def test_subcategory_cannot_cross_households(connection: AsyncConnection):
    ids = await seed(connection)
    other_household = await connection.scalar(
        text("INSERT INTO households (name) VALUES ('Other') RETURNING id")
    )
    foreign_parent = await make_category(connection, other_household, "Food", "expense")
    await connection.commit()

    with pytest.raises(FAILS_AT_COMMIT):
        await make_category(
            connection, ids["household_id"], "Coffee", "expense", foreign_parent
        )
        await connection.commit()
