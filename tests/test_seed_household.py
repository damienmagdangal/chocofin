"""Tests for `scripts.seed_household` — the one INSERT written from outside.

This script is how the first `members` row exists at all. Until `/link` lands,
nothing inside the bot can write it, so if this is wrong the household is
either unreachable (no member) or duplicated (two households, two ledgers, and
the money split between them with nothing to show for it).

Two things are deliberately NOT tested here:

* `_main`'s happy path. It calls `make_engine()`, which reads `DATABASE_URL` —
  the live household. Tests use `TEST_DATABASE_URL` and nothing else, so the
  DB-backed tests below drive `seed()` against the `session` fixture instead.
  What `_main` uniquely owns is argument parsing, and that is reachable without
  a database because argparse rejects a bad `--account` before the engine is
  built.
* Anything about `_parse_account` being reached from the command line with a
  *good* spec, for the same reason.

No module-level `pytest.mark.asyncio`: unlike the other DB test modules this one
is half pure (argument parsing needs no database), and `asyncio_mode = "auto"`
already collects the coroutines.
"""

from __future__ import annotations

import argparse

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import ledger
from core.errors import CardHasNoBillingAccountError
from core.models import Account, Entry, Household, Member
from scripts.seed_household import _main, _parse_account, seed
from tests.factories import JAN_15

# One household, one owner, and a card that settles from the bank. The card is
# listed BEFORE the account that bills it on purpose: an operator types the
# accounts in whatever order they think of them, and the card cannot be linked
# on the pass that creates it because "BPI" does not exist yet.
SPECS = ["Wallet:cash", "Visa:credit_card:BPI", "BPI:bank"]

HOUSEHOLD = "Seeded Home"
TELEGRAM_ID = 20_000_001
DISPLAY_NAME = "Operator"


def _specs() -> list[tuple[str, str, str | None]]:
    """Parse the CLI strings, so the tests below seed what `--account` means."""
    return [_parse_account(spec) for spec in SPECS]


async def _seed(session: AsyncSession, **overrides) -> list[str]:
    kwargs = {
        "household_name": HOUSEHOLD,
        "telegram_user_id": TELEGRAM_ID,
        "display_name": DISPLAY_NAME,
        "account_specs": _specs(),
    }
    kwargs.update(overrides)
    log = await seed(session, **kwargs)
    await session.commit()
    return log


async def _count(session: AsyncSession, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def _account(session: AsyncSession, name: str) -> Account:
    account = await session.scalar(
        select(Account).where(func.lower(Account.name) == name.lower())
    )
    assert account is not None, f"no account named {name!r}"
    return account


# --- first run --------------------------------------------------------------


async def test_the_first_run_creates_the_household_member_and_accounts(
    session: AsyncSession,
):
    log = await _seed(session)

    household = await session.scalar(select(Household))
    assert household is not None
    assert household.name == HOUSEHOLD
    # Not asserted for their own sake — they are the PHP-only and Manila-only
    # invariants, and a seed that wrote anything else could not be corrected
    # later without touching money.
    assert household.base_currency == "PHP"
    assert household.timezone == "Asia/Manila"

    member = await session.scalar(select(Member))
    assert member is not None
    assert member.household_id == household.id
    assert member.telegram_user_id == TELEGRAM_ID
    assert member.display_name == DISPLAY_NAME
    # The first member is the owner; there is nobody to be owned by.
    assert member.role == "owner"
    assert member.is_active is True

    accounts = list(await session.scalars(select(Account).order_by(Account.name)))
    assert [(a.name, a.type) for a in accounts] == [
        ("BPI", "bank"),
        ("Visa", "credit_card"),
        ("Wallet", "cash"),
    ]
    # `household_id` is on every table. A seeded account in no household, or in
    # a second one, is invisible to every core query.
    assert {a.household_id for a in accounts} == {household.id}

    # The log is the only thing the operator sees, so it has to say what happened.
    assert any("created household" in line for line in log)
    assert any("created member" in line and "owner" in line for line in log)
    assert sum("created account" in line for line in log) == 3


# --- re-runs ----------------------------------------------------------------


async def test_a_second_identical_run_is_a_no_op_not_a_duplicate(
    session: AsyncSession,
):
    """Re-running is the normal case: the operator adds one account and runs the
    same command again. Everything already there must be left alone."""
    await _seed(session)
    visa_id = (await _account(session, "Visa")).id

    log = await _seed(session)

    assert await _count(session, Household) == 1
    assert await _count(session, Member) == 1
    assert await _count(session, Account) == 3
    # Same row, not a replacement — anything already pointing at this account
    # keeps pointing at it.
    assert (await _account(session, "Visa")).id == visa_id

    assert any("already exists" in line and DISPLAY_NAME in line for line in log)
    assert sum("already exists" in line for line in log) == 4  # member + 3 accounts
    assert not any("created" in line for line in log)


async def test_a_rerun_in_different_case_is_still_the_same_account(
    session: AsyncSession,
):
    """`uq_accounts_household_name_lower` is case-insensitive, so `seed` has to
    match it. Looking up 'bpi' case-sensitively would find nothing, try to
    insert a second BPI, and hit the unique index instead of doing nothing."""
    await _seed(session)

    log = await _seed(session, account_specs=[_parse_account("bpi:bank")])

    assert await _count(session, Account) == 3
    assert any("already exists" in line for line in log)


async def test_a_rerun_never_moves_a_known_telegram_id_to_another_household(
    session: AsyncSession,
):
    """`telegram_user_id` is globally UNIQUE. Given a different `--household`,
    the existing membership wins: re-pointing it would strand every entry the
    member has already written."""
    await _seed(session)
    original = await session.scalar(select(Member))

    log = await _seed(session, household_name="Somewhere Else")

    assert await _count(session, Household) == 1
    assert await _count(session, Member) == 1
    member = await session.scalar(select(Member))
    assert member.household_id == original.household_id
    assert any("already exists" in line for line in log)


# --- malformed specs --------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        # Not enough colons: the whole shape is wrong, so say the shape.
        ("Wallet", "NAME:TYPE"),
        ("", "NAME:TYPE"),
        # Too many: a name containing a colon is a typo, not a fourth field.
        ("Visa:credit_card:BPI:extra", "NAME:TYPE"),
        # An empty name would create an account nobody can pick off a keyboard.
        (":cash", "empty account name"),
        ("  :cash", "empty account name"),
        # A type outside ACCOUNT_TYPES would fail at `ck_accounts_type` with a
        # Postgres error; reject it here where the message can list the options.
        ("Wallet:crypto", "'crypto' is not one of"),
        ("Wallet:crypto", "cash, bank, ewallet, credit_card, savings, loan"),
        # Only a card has a billing account. Silently ignoring the third field
        # would leave the operator sure they had linked something.
        ("BPI:bank:Wallet", "cannot have a billing account"),
    ],
)
def test_a_malformed_account_spec_is_rejected_with_a_usable_error(
    spec: str, fragment: str
):
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        _parse_account(spec)
    assert fragment in str(excinfo.value)


def test_a_well_formed_spec_survives_whitespace_and_an_absent_billing_account():
    assert _parse_account(" Wallet : cash ") == ("Wallet", "cash", None)
    assert _parse_account("Visa:credit_card:BPI") == ("Visa", "credit_card", "BPI")


async def test_the_cli_rejects_a_malformed_account_spec_before_touching_the_db(
    capsys: pytest.CaptureFixture[str],
):
    """End of the same path, as the operator meets it: argparse turns the
    `ArgumentTypeError` into exit code 2 and a message on stderr. It happens in
    `parse_args`, so no engine is ever built and no database is reached — which
    is also why this test needs no fixture."""
    with pytest.raises(SystemExit) as excinfo:
        await _main(
            [
                "--household",
                HOUSEHOLD,
                "--telegram-user-id",
                str(TELEGRAM_ID),
                "--display-name",
                DISPLAY_NAME,
                "--account",
                "Wallet:crypto",
            ]
        )
    assert excinfo.value.code == 2

    stderr = capsys.readouterr().err
    assert "--account" in stderr
    assert "'crypto' is not one of" in stderr


# --- the billing link -------------------------------------------------------


async def test_a_seeded_card_settles_from_its_billing_account(session: AsyncSession):
    """The point of the whole `--account NAME:TYPE:BILLING` third field.

    `settle_card` resolves the paying account from `billing_account_id` and
    raises rather than guessing, so a seed that wrote the column as NULL — or
    linked it to the wrong row — turns `/pay` into an error the operator cannot
    explain. The card is seeded BEFORE the account that bills it, which is the
    case the second pass exists for.
    """
    await _seed(session)

    card = await _account(session, "Visa")
    bank = await _account(session, "BPI")
    member = await session.scalar(select(Member))

    assert card.billing_account_id == bank.id

    entry = await ledger.settle_card(
        session,
        household_id=card.household_id,
        member_id=member.id,
        card_id=card.id,
        amount_minor=250_000,  # PHP 2,500.00
        occurred_at=JAN_15,
        # No `source_account_id`: the seeded link is what has to answer this.
        note="Visa bill",
    )
    # A real COMMIT, so the deferred leg-shape trigger actually fires.
    await session.commit()

    # A settlement is a transfer. Never an expense — the purchases were the
    # spending and were expensed when they happened.
    assert entry.kind == "transfer"
    assert entry.amount_minor == 250_000  # unsigned display amount
    assert entry.category_id is None

    legs = {
        leg.leg_role: leg
        for leg in await ledger.list_legs(
            session, household_id=card.household_id, entry_id=entry.id
        )
    }
    assert legs["source"].account_id == bank.id
    assert legs["source"].amount_minor == -250_000
    assert legs["destination"].account_id == card.id
    assert legs["destination"].amount_minor == 250_000
    assert sum(leg.amount_minor for leg in legs.values()) == 0

    # Nothing anywhere in the ledger counts this as money spent.
    assert (
        await session.scalar(
            select(func.count()).select_from(Entry).where(Entry.kind != "transfer")
        )
        == 0
    )


async def test_an_unlinked_seeded_card_refuses_to_invent_a_payer(
    session: AsyncSession,
):
    """The rejection half. A two-field spec leaves `billing_account_id` NULL,
    and `settle_card` raises instead of picking an account — inventing where
    the money came from is a lie about real money."""
    await _seed(
        session,
        account_specs=[_parse_account("BPI:bank"), _parse_account("Visa:credit_card")],
    )
    card = await _account(session, "Visa")
    member = await session.scalar(select(Member))

    assert card.billing_account_id is None

    with pytest.raises(CardHasNoBillingAccountError):
        await ledger.settle_card(
            session,
            household_id=card.household_id,
            member_id=member.id,
            card_id=card.id,
            amount_minor=250_000,
            occurred_at=JAN_15,
        )
