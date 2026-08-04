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


# --- one clause at a time ---------------------------------------------------
#
# The tests above reject bad entries, but most of them are caught by the FIRST
# clause the trigger reaches — usually the leg count — which means deleting a
# later clause leaves them all green. These three are built to slip past every
# clause except the one under test, so each rule is proven on its own.


async def test_expense_source_leg_must_be_negative(connection: AsyncConnection):
    """Isolates the expense sign clause.

    One `source` leg, so the count clause is satisfied. The cross-check
    compares `abs(leg)`, so a +100000 leg against a declared 100000 satisfies
    that too. Nothing is left but the sign clause, which is what makes this a
    real probe: delete it and this test goes green.

    `test_expense_with_a_positive_leg_fails` does NOT cover this — it uses
    leg_role='destination', so the count clause rejects it first.
    """
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "expense", 100_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], 100_000, "source")

    with pytest.raises(FAILS_AT_COMMIT):
        await connection.commit()


async def test_income_destination_leg_must_be_positive(connection: AsyncConnection):
    """Pins the income sign clause by the message it raises, not by rejection.

    Rejection alone cannot detect this clause going missing. A negative
    destination leg is ALSO caught by the cross-check, because
    `entries.amount_minor` is positive by CHECK and can therefore never equal
    it. Isolating the sign clause would need `amount_minor` to equal a negative
    leg, which `ck_entries_amount_positive` forbids.

    What stays observable is which rule speaks first, so that is what this
    asserts. The message is part of the contract here.
    """
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "income", 100_000)
    await insert_leg(connection, ids, entry_id, ids["cash_id"], -100_000, "destination")

    with pytest.raises(FAILS_AT_COMMIT) as caught:
        await connection.commit()
    assert "destination leg must be positive" in str(caught.value)


async def test_transfer_legs_must_sum_to_zero(connection: AsyncConnection):
    """Isolates sum-to-zero.

    Two legs, one of each role, both signed correctly, and `amount_minor`
    matches the destination leg so the cross-check passes. Only sum-to-zero can
    reject this: PHP 3,000.00 leaves savings and PHP 2,000.00 arrives in cash,
    with PHP 1,000.00 unaccounted for.

    `test_b_unbalanced_transfer_fails_at_commit` does NOT cover this — a
    one-leg transfer is caught by the leg-count clause long before the sum is
    ever examined.
    """
    ids = await seed(connection)
    entry_id = await insert_entry(connection, ids, "transfer", 200_000)
    await insert_leg(connection, ids, entry_id, ids["savings_id"], -300_000, "source")
    await insert_leg(connection, ids, entry_id, ids["cash_id"], 200_000, "destination")

    with pytest.raises(FAILS_AT_COMMIT):
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


# --- name uniqueness --------------------------------------------------------
#
# `seed` already created accounts named 'Cash' and 'Savings' in the household.


async def insert_account(
    conn: AsyncConnection, household_id: int, name: str, type_: str = "cash"
) -> int:
    return await conn.scalar(
        text(
            "INSERT INTO accounts (household_id, name, type) "
            "VALUES (:h, :n, :t) RETURNING id"
        ),
        {"h": household_id, "n": name, "t": type_},
    )


async def test_duplicate_account_name_is_rejected(connection: AsyncConnection):
    ids = await seed(connection)
    with pytest.raises(FAILS_AT_COMMIT):
        await insert_account(connection, ids["household_id"], "Cash")
        await connection.commit()


async def test_account_name_uniqueness_is_case_insensitive(
    connection: AsyncConnection,
):
    """'GoTyme' and 'gotyme' are one account, not two.

    Two rows differing only in case would each accumulate their own derived
    balance and neither would be wrong, so the split is invisible: the money is
    simply in two places the household thinks are one.
    """
    ids = await seed(connection)
    with pytest.raises(FAILS_AT_COMMIT):
        await insert_account(connection, ids["household_id"], "cASH")
        await connection.commit()


async def test_the_same_account_name_in_another_household_is_fine(
    connection: AsyncConnection,
):
    """Uniqueness is per household, never global. Two households both having a
    'Cash' account is the normal case, not a collision."""
    ids = await seed(connection)
    other_household = await connection.scalar(
        text("INSERT INTO households (name) VALUES ('Other') RETURNING id")
    )
    account_id = await insert_account(connection, other_household, "Cash")
    await connection.commit()

    assert account_id is not None
    assert account_id != ids["cash_id"]


async def test_duplicate_category_name_and_kind_is_rejected(
    connection: AsyncConnection,
):
    ids = await seed(connection)
    await make_category(connection, ids["household_id"], "Food", "expense")
    await connection.commit()

    with pytest.raises(FAILS_AT_COMMIT):
        await make_category(connection, ids["household_id"], "Food", "expense")
        await connection.commit()


async def test_category_name_uniqueness_is_case_insensitive(
    connection: AsyncConnection,
):
    """'Coffee' and 'coffee' are one category, not two.

    A split category is quieter than a split account — no balance is wrong —
    but it silently halves every report the category appears in, and only one
    of the two can be the child of 'Food & Dining', so the rollup in
    `summarise` drops the other.
    """
    ids = await seed(connection)
    await make_category(connection, ids["household_id"], "Coffee", "expense")
    await connection.commit()

    with pytest.raises(FAILS_AT_COMMIT):
        await make_category(connection, ids["household_id"], "cOFFEE", "expense")
        await connection.commit()


async def test_the_same_category_name_under_a_different_kind_is_fine(
    connection: AsyncConnection,
):
    """`kind` is part of the key on purpose: an expense 'Refunds' bucket and an
    income 'Refunds' bucket are different things and both are legitimate."""
    ids = await seed(connection)
    expense_side = await make_category(
        connection, ids["household_id"], "Refunds", "expense"
    )
    income_side = await make_category(
        connection, ids["household_id"], "Refunds", "income"
    )
    await connection.commit()

    assert expense_side != income_side
