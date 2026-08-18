"""Tests for `scripts.seed_household` — the one INSERT written from outside.

This script is how the first `members` row exists at all. Until `/link` lands,
nothing inside the bot can write it, so if this is wrong the household is
either unreachable (no member) or duplicated (two households, two ledgers, and
the money split between them with nothing to show for it).

It is also the only chance to get an opening balance right. Balances are
derived — `opening_balance_minor + SUM(legs)` — and `entries` is append-only,
so an account seeded at zero that was never at zero cannot be corrected later
except by an adjusting entry for money that never moved. Hence the tests below
that follow a seeded peso amount all the way to `core.balances`.

Two things are deliberately NOT tested here:

* `_main`'s happy path. It calls `make_engine()`, which reads `DATABASE_URL` —
  the live household. Tests use `TEST_DATABASE_URL` and nothing else, so the
  DB-backed tests below drive `seed()` against the `session` fixture instead.
  What `_main` uniquely owns is argument parsing and the confirmation guard,
  and both are reachable without a database: argparse rejects a bad `--account`
  before the engine is built, and the guard decides whether the engine is built
  at all. The guard tests below stub `make_engine` to prove exactly that.
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

from core import balances, ledger
from core.config import Settings
from core.errors import CardHasNoBillingAccountError
from core.models import Account, Entry, Household, Member
from scripts import seed_household
from scripts.seed_household import (
    AccountSpec,
    SeedError,
    _main,
    _parse_account,
    _parse_pesos,
    _redact,
    seed,
)
from tests.factories import JAN_15

# One household, one owner, and a card that settles from the bank. The card is
# listed BEFORE the account that bills it on purpose: an operator types the
# accounts in whatever order they think of them, and the card cannot be linked
# on the pass that creates it because "BPI" does not exist yet.
#
# Every amount is in pesos, as typed on a command line. The centavo values they
# must become are spelled out beneath so a conversion bug shows up as a wrong
# constant rather than as two expressions agreeing with each other.
SPECS = [
    "Wallet:cash:opening=1500",
    "Visa:credit_card:opening=-3000:limit=50000:billing=BPI",
    "BPI:bank:opening=42350.75",
]

WALLET_OPENING = 150_000  # PHP 1,500.00
VISA_OPENING = -300_000  # PHP 3,000.00 owed — a liability is a negative balance
VISA_LIMIT = 5_000_000  # PHP 50,000.00
BPI_OPENING = 4_235_075  # PHP 42,350.75

HOUSEHOLD = "Seeded Home"
TELEGRAM_ID = 20_000_001
DISPLAY_NAME = "Operator"


def _specs() -> list[AccountSpec]:
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


# --- pesos to centavos ------------------------------------------------------
#
# The single conversion in the script. It happens once, here, so that nothing
# downstream ever sees a peso — and it is integer arithmetic, because
# `1234.56 * 100` is 123455.99999999999 and money does not survive that.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("1500", 150_000),
        # The case that makes float wrong. 42350.75 * 100 is not 4235075.0 in
        # every rounding mode, and this is a real bank balance shape.
        ("42350.75", 4_235_075),
        ("1,234.50", 123_450),
        # A card's debt. Negatives exist here and nowhere else in the money
        # path: liabilities are negative balances.
        ("-3000", -300_000),
        ("-0.01", -1),
        (".50", 50),
        ("0.05", 5),
        # One decimal place means tenths of a peso, not centavos.
        ("1.5", 150),
        (" 1500 ", 150_000),
    ],
)
def test_pesos_become_centavos_exactly(text: str, expected: int):
    assert _parse_pesos(text, field="opening") == expected


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        # Finer than a centavo. Rounding it would invent a value the operator
        # did not type, in the one column that cannot be corrected later.
        ("1.234", "finer than one centavo"),
        ("1.", "truncated"),
        ("abc", "not an amount in pesos"),
        ("1 000", "not an amount in pesos"),
        ("--5", "not an amount in pesos"),
        ("50%", "not an amount in pesos"),
        # int("١٠٠") is 100 and str.isdigit() agrees. Accepting it would put a
        # number nobody typed into a balance.
        ("١٠٠", "not an amount in pesos"),
        ("", "no amount"),
        ("-", "no digits"),
    ],
)
def test_a_bad_peso_amount_is_rejected_rather_than_guessed(text: str, fragment: str):
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        _parse_pesos(text, field="opening")
    assert fragment in str(excinfo.value)
    # The message names the field, so a spec with several amounts says which.
    assert "opening" in str(excinfo.value)


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


async def test_the_pesos_on_the_command_line_land_as_centavos_in_the_columns(
    session: AsyncSession,
):
    """The whole point of `opening=` and `limit=`.

    Asserted against the constants at the top of this file rather than against
    an expression, so a conversion that is wrong by a factor of a hundred
    cannot agree with the test that checks it.
    """
    await _seed(session)

    wallet = await _account(session, "Wallet")
    bank = await _account(session, "BPI")
    card = await _account(session, "Visa")

    assert wallet.opening_balance_minor == WALLET_OPENING
    assert bank.opening_balance_minor == BPI_OPENING
    assert card.opening_balance_minor == VISA_OPENING
    assert card.credit_limit_minor == VISA_LIMIT

    # A limit belongs to a card and to nothing else: `available_credit_minor`
    # returns a number for any account that has one.
    assert wallet.credit_limit_minor is None
    assert bank.credit_limit_minor is None


async def test_a_seeded_account_reports_the_balance_it_was_seeded_with(
    session: AsyncSession,
):
    """Through `core.balances`, which is where the operator will see it.

    A balance is `opening_balance_minor + SUM(legs)` and there are no legs yet,
    so this is the seed and only the seed. Seeding zero here would not fail
    anywhere — it would just quietly show the wrong number forever.
    """
    await _seed(session)

    bank = await _account(session, "BPI")
    card = await _account(session, "Visa")

    bank_balance = await balances.account_balance(
        session, household_id=bank.household_id, account_id=bank.id
    )
    assert bank_balance.balance_minor == BPI_OPENING

    card_balance = await balances.account_balance(
        session, household_id=card.household_id, account_id=card.id
    )
    # Money owed, so negative. A card seeded positive would add its debt to net
    # worth instead of subtracting it.
    assert card_balance.balance_minor == VISA_OPENING

    # Limit plus balance: PHP 50,000.00 - PHP 3,000.00. Without `limit=` this
    # is None for the life of the card, which is why the spec insists on it.
    available = await balances.available_credit(
        session, household_id=card.household_id, account_id=card.id
    )
    assert available == VISA_LIMIT + VISA_OPENING == 4_700_000

    # Net worth adds them up, cash included, with the debt pulling it down.
    assert await balances.net_worth_minor(session, household_id=card.household_id) == (
        WALLET_OPENING + BPI_OPENING + VISA_OPENING
    )


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
    # Identical means identical: no drift to report.
    assert not any("left alone" in line for line in log)


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
    # No `opening=` was typed, so there is no disagreement about money to
    # report — only a stated amount that differs is worth a line.
    assert not any("left alone" in line for line in log)
    assert (await _account(session, "BPI")).opening_balance_minor == BPI_OPENING


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


async def test_a_rerun_reports_a_different_opening_balance_instead_of_writing_it(
    session: AsyncSession,
):
    """The rule the billing link already follows, on the two money columns.

    An opening balance is history: every balance the household has ever seen
    was derived from it, and `entries` is append-only, so silently rewriting it
    moves all of them at once with nothing in the ledger to explain the jump. A
    credit limit is the same answer for a smaller reason — the operator is told
    what is there and decides.
    """
    await _seed(session)

    log = await _seed(
        session,
        account_specs=[
            _parse_account("Wallet:cash:opening=9999"),
            _parse_account("Visa:credit_card:opening=-4500:limit=80000:billing=BPI"),
        ],
    )

    assert (await _account(session, "Wallet")).opening_balance_minor == WALLET_OPENING
    card = await _account(session, "Visa")
    assert card.opening_balance_minor == VISA_OPENING
    assert card.credit_limit_minor == VISA_LIMIT

    # In pesos, because that is what the operator typed and can act on.
    assert any(
        line == "'Wallet' opening balance is ₱1,500.00, not ₱9,999.00 — left alone"
        for line in log
    )
    assert any(
        line == "'Visa' opening balance is -₱3,000.00, not -₱4,500.00 — left alone"
        for line in log
    )
    assert any(
        line == "'Visa' credit limit is ₱50,000.00, not ₱80,000.00 — left alone"
        for line in log
    )


# --- rejected before anything is written ------------------------------------


async def test_a_billing_account_that_names_nothing_aborts_the_whole_run(
    session: AsyncSession,
):
    """A typo in `billing=` must not leave a card behind.

    Creating the card anyway and reporting the failed link in a log line is
    exactly the half-usable account this script must not produce: `/pay` raises
    `CardHasNoBillingAccountError` on it, and by then nobody remembers a line
    from the seed. It is one mistyped argument, so it costs the run.
    """
    with pytest.raises(SeedError) as excinfo:
        await seed(
            session,
            household_name=HOUSEHOLD,
            telegram_user_id=TELEGRAM_ID,
            display_name=DISPLAY_NAME,
            account_specs=[
                _parse_account("BPI:bank:opening=1000"),
                _parse_account("Visa:credit_card:limit=50000:billing=BDO"),
            ],
        )
    assert "'BDO'" in str(excinfo.value)
    await session.rollback()

    # Not just the card: the household and the member go too. `session_scope`
    # rolls the transaction back in production for the same reason.
    assert await _count(session, Account) == 0
    assert await _count(session, Household) == 0
    assert await _count(session, Member) == 0


async def test_a_billing_account_already_in_the_household_is_enough(
    session: AsyncSession,
):
    """The other half: `billing=` may name a row from an earlier run.

    Adding a card months later is the normal way this script gets used a second
    time, and the bank it settles from is not on that command line.
    """
    await _seed(session, account_specs=[_parse_account("BPI:bank:opening=1000")])

    await _seed(
        session,
        account_specs=[_parse_account("Amex:credit_card:limit=20000:billing=BPI")],
    )

    card = await _account(session, "Amex")
    assert card.billing_account_id == (await _account(session, "BPI")).id


async def test_two_specs_claiming_one_name_abort_before_the_first_insert(
    session: AsyncSession,
):
    """`uq_accounts_household_name_lower` would catch this on the second
    INSERT — after the household, the member and one account were written, with
    a Postgres error naming an index rather than the two arguments that
    disagree. Names are case-insensitive, so 'wallet' and 'Wallet' are one."""
    with pytest.raises(SeedError) as excinfo:
        await seed(
            session,
            household_name=HOUSEHOLD,
            telegram_user_id=TELEGRAM_ID,
            display_name=DISPLAY_NAME,
            account_specs=[
                _parse_account("Wallet:cash:opening=100"),
                _parse_account("wallet:ewallet:opening=200"),
            ],
        )
    assert "case-insensitive" in str(excinfo.value)
    await session.rollback()

    assert await _count(session, Household) == 0
    assert await _count(session, Account) == 0


# --- malformed specs --------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        # Not enough colons: the whole shape is wrong, so say the shape.
        ("Wallet", "NAME:TYPE"),
        ("", "NAME:TYPE"),
        # An empty name would create an account nobody can pick off a keyboard.
        (":cash", "empty account name"),
        ("  :cash", "empty account name"),
        # A type outside ACCOUNT_TYPES would fail at `ck_accounts_type` with a
        # Postgres error; reject it here where the message can list the options.
        ("Wallet:crypto", "'crypto' is not one of"),
        ("Wallet:crypto", "cash, bank, ewallet, credit_card, savings, loan"),
        # The old spec was NAME:TYPE:BILLING. Anyone with that command in their
        # shell history lands here, so the message has to name the new form.
        ("Visa:credit_card:BPI", "did you mean billing=BPI?"),
        ("Visa:credit_card:limit=1:billing=BPI:extra", "is not key=value"),
        ("Wallet:cash:opening", "did you mean billing=opening?"),
        # A typo in a key must not be read as "no opinion about that field".
        ("Wallet:cash:openning=100", "'openning' in"),
        ("Wallet:cash:openning=100", "one of opening, limit, billing"),
        ("Wallet:cash:opening=1:opening=2", "gives opening= twice"),
        # A card with no limit can never report available credit, and a card
        # with no billing account fails every /pay. Both are half an account.
        ("Visa:credit_card:opening=-3000", "needs limit=<pesos>"),
        ("Visa:credit_card:limit=50000", "needs billing=<account name>"),
        ("Visa:credit_card:limit=50000", "CardHasNoBillingAccountError"),
        # Only a card has either. Silently ignoring them would leave the
        # operator sure they had set something.
        ("BPI:bank:billing=Wallet", "cannot have a billing account"),
        ("BPI:bank:limit=50000", "cannot have a credit limit"),
        # ck_accounts_credit_limit_non_negative, said where it can be read.
        ("Visa:credit_card:limit=-5:billing=BPI", "negative credit limit"),
        # ck_accounts_not_self_billing. A card settling from itself would make
        # /pay a transfer between one account and itself.
        ("Visa:credit_card:limit=5:billing=visa", "cannot settle from itself"),
        # The amount errors reach the top through the same path.
        ("Wallet:cash:opening=1.234", "finer than one centavo"),
    ],
)
def test_a_malformed_account_spec_is_rejected_with_a_usable_error(
    spec: str, fragment: str
):
    with pytest.raises(argparse.ArgumentTypeError) as excinfo:
        _parse_account(spec)
    assert fragment in str(excinfo.value)


def test_a_well_formed_spec_survives_whitespace_and_an_absent_amount():
    """An omitted `opening=` is None, not zero.

    The distinction is what lets a re-run tell "the operator says this account
    holds nothing" apart from "the operator did not mention it", so an omission
    never produces a line claiming a disagreement about money.
    """
    assert _parse_account(" Wallet : cash ") == AccountSpec(
        name="Wallet",
        type="cash",
        opening_balance_minor=None,
        credit_limit_minor=None,
        billing_account_name=None,
    )
    assert _parse_account("Wallet:cash:opening=0") == AccountSpec(
        name="Wallet", type="cash", opening_balance_minor=0
    )
    assert _parse_account(
        "Visa : credit_card : opening=-3000 : limit=50,000 : billing=BPI "
    ) == AccountSpec(
        name="Visa",
        type="credit_card",
        opening_balance_minor=-300_000,
        credit_limit_minor=5_000_000,
        billing_account_name="BPI",
    )


async def test_the_cli_rejects_a_malformed_account_spec_before_touching_the_db(
    guarded, capsys: pytest.CaptureFixture[str]
):
    """End of the same path, as the operator meets it: argparse turns the
    `ArgumentTypeError` into exit code 2 and a message on stderr.

    Takes `guarded` (defined with the confirmation tests below) even though the
    rejection happens in `parse_args`, before an engine could be built. That
    ordering is the CLAIM under test, not a licence to run unprotected: without
    the fixture this test calls `_main` with the real `get_settings` and the real
    `make_engine`, so the only thing standing between it and the ambient
    `DATABASE_URL` — the live household — is argparse failing first. If the
    `--account` validation ever moves after the engine is built, the guard turns
    that into a `_Reached` and a red test instead of a connection to production.
    """
    with pytest.raises(SystemExit) as excinfo:
        await _main(_argv("--account", "Wallet:crypto"))
    assert excinfo.value.code == 2

    stderr = capsys.readouterr().err
    assert "--account" in stderr
    assert "'crypto' is not one of" in stderr


async def test_the_cli_rejects_a_card_with_no_credit_limit_before_touching_the_db(
    guarded, capsys: pytest.CaptureFixture[str]
):
    """The new half of the same guarantee.

    A card missing `limit=` is not a malformed string — it parses fine and
    would have inserted cleanly. It is refused because the account it would
    create can never report available credit, and refused early enough that no
    engine is built to insert it with.
    """
    with pytest.raises(SystemExit) as excinfo:
        await _main(_argv("--account", "Visa:credit_card:billing=BPI"))
    assert excinfo.value.code == 2

    stderr = capsys.readouterr().err
    assert "needs limit=<pesos>" in stderr


# --- the billing link -------------------------------------------------------


async def test_a_seeded_card_settles_from_its_billing_account(session: AsyncSession):
    """The point of the whole `billing=` field.

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

    # The settlement moved real money, so both derived balances move with it —
    # off the bank, and against the debt on the card.
    assert (
        await balances.account_balance(
            session, household_id=card.household_id, account_id=bank.id
        )
    ).balance_minor == BPI_OPENING - 250_000
    assert (
        await balances.account_balance(
            session, household_id=card.household_id, account_id=card.id
        )
    ).balance_minor == VISA_OPENING + 250_000


async def test_a_rerun_never_repoints_a_card_that_already_has_a_billing_account(
    session: AsyncSession,
):
    """The same "leave what is there alone" rule the accounts and the member
    follow, on the one column that decides where money comes from.

    A card linked to BPI, re-run as `billing=Wallet`, used to be repointed in
    silence and logged with the line that means "linked" — so the next `/pay`
    would have moved real money out of Wallet, and the log the operator read
    would have looked like the first run's. It keeps BPI, and the difference is
    reported.
    """
    await _seed(session)
    bank = await _account(session, "BPI")

    log = await _seed(
        session,
        account_specs=[_parse_account("Visa:credit_card:limit=50000:billing=Wallet")],
    )

    card = await _account(session, "Visa")
    assert card.billing_account_id == bank.id
    assert any("left alone" in line and "'BPI'" in line for line in log)
    # Never the line that claims a link was made.
    assert not any(line == "'Visa' settles from 'Wallet'" for line in log)


async def test_a_card_with_no_billing_account_refuses_to_invent_a_payer(
    session: AsyncSession,
):
    """Why the spec insists on `billing=`, demonstrated rather than asserted.

    `--account Visa:credit_card:limit=50000` cannot produce this row any more —
    it exits 2 — so the spec is built by hand to show what the rejection is
    protecting the operator from: a card that exists, looks fine in every
    listing, and fails every single `/pay`.
    """
    await _seed(
        session,
        account_specs=[
            _parse_account("BPI:bank:opening=1000"),
            AccountSpec(name="Visa", type="credit_card", credit_limit_minor=VISA_LIMIT),
        ],
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


# --- the confirmation guard -------------------------------------------------
#
# `DATABASE_URL` is ambient, and this script is the only thing that can bind a
# Telegram account to a household. `telegram_user_id` is globally UNIQUE and
# `seed` short-circuits on a known one, so aiming this at the wrong database
# grants access that a later run cannot take back. None of these tests need a
# database: that is the property under test.

# Invented, like everything else in the fixtures. The password is here so the
# tests can prove it never reaches stdout.
FAKE_PASSWORD = "not-a-real-password"
FAKE_URL = f"postgresql+asyncpg://chocofin:{FAKE_PASSWORD}@db.invalid:5432/chocofin"


class _Reached(Exception):
    """Raised in place of `make_engine`: the guard let this run through."""


def _argv(*extra: str) -> list[str]:
    return [
        "--household",
        HOUSEHOLD,
        "--telegram-user-id",
        str(TELEGRAM_ID),
        "--display-name",
        DISPLAY_NAME,
        *extra,
    ]


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch):
    """A fixed target, and an engine that announces itself instead of connecting.

    Reaching `make_engine` is exactly what the confirmation is meant to gate,
    so the fixture makes that reachable event loud rather than letting it try
    to open a socket.
    """
    monkeypatch.setattr(
        seed_household, "get_settings", lambda: Settings(database_url=FAKE_URL)
    )

    def reached(*args, **kwargs):
        raise _Reached

    monkeypatch.setattr(seed_household, "make_engine", reached)


def test_redact_prints_the_target_without_the_password():
    redacted = _redact(FAKE_URL)

    assert FAKE_PASSWORD not in redacted
    # Still identifies the target, which is the entire point of printing it.
    assert redacted == "postgresql+asyncpg://chocofin@db.invalid:5432/chocofin"


def test_redact_survives_a_password_full_of_url_punctuation():
    """Cutting the password out with a string edit would leak half of this one."""
    redacted = _redact("postgresql+asyncpg://user:p@ss:w0rd@host:5432/db")

    assert "p@ss:w0rd" not in redacted
    assert redacted == "postgresql+asyncpg://user@host:5432/db"


async def test_a_declined_confirmation_writes_nothing(
    guarded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The whole point: a wrong answer stops before an engine exists."""
    monkeypatch.setattr(seed_household, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "chocofin_test")

    assert await _main(_argv()) == 1

    out = capsys.readouterr().out
    assert "Aborted. Nothing was written." in out
    assert "created" not in out


async def test_the_target_and_what_it_will_write_are_printed_before_the_prompt(
    guarded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The operator has to be able to see they are aimed at the wrong database.

    The Telegram id is the line that matters most — it is the value `seed`
    short-circuits on, and the one that is awkward to reverse. The amounts are
    next: a limit typed into `opening=` is not something anyone spots in
    centavos, and it is a wrong balance for the life of the household.
    """
    monkeypatch.setattr(seed_household, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    assert (
        await _main(
            _argv(
                "--account",
                "Wallet:cash:opening=1500",
                "--account",
                "Visa:credit_card:opening=-3000:limit=50000:billing=BPI",
            )
        )
        == 1
    )

    out = capsys.readouterr().out
    assert "postgresql+asyncpg://chocofin@db.invalid:5432/chocofin" in out
    assert "database: chocofin" in out
    assert str(TELEGRAM_ID) in out
    assert HOUSEHOLD in out
    # In pesos, the way they were typed — not the centavos they become.
    assert "'Wallet' (cash), opening ₱1,500.00" in out
    assert (
        "'Visa' (credit_card), opening -₱3,000.00, limit ₱50,000.00, "
        "settles from 'BPI'" in out
    )
    # No secrets on stdout, ever.
    assert FAKE_PASSWORD not in out


async def test_without_a_terminal_it_refuses_rather_than_blocking(
    guarded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A cron job or a piped stdin has nobody to answer, so it must not write.

    Reading from a closed stdin would either hang or take EOF for an answer.
    """
    monkeypatch.setattr(seed_household, "_interactive", lambda: False)

    def never(*args, **kwargs):
        raise AssertionError("prompted with no terminal to prompt")

    monkeypatch.setattr("builtins.input", never)

    assert await _main(_argv()) == 1
    assert "Aborted. Nothing was written." in capsys.readouterr().out


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
async def test_an_unanswered_prompt_is_a_no_rather_than_a_traceback(
    guarded,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
):
    """`isatty` is not a promise that anyone will answer.

    A stdin redirected from /dev/null reports a terminal under some shells and
    then reads EOF, which crashed the script with a traceback where it should
    have said it was aborting. Ctrl-C at the prompt is the same situation and
    the same answer.
    """
    monkeypatch.setattr(seed_household, "_interactive", lambda: True)

    def interrupted(_prompt):
        raise interruption

    monkeypatch.setattr("builtins.input", interrupted)

    assert await _main(_argv()) == 1
    assert "Aborted. Nothing was written." in capsys.readouterr().out


async def test_typing_the_database_name_lets_the_run_through(
    guarded, monkeypatch: pytest.MonkeyPatch
):
    """The other half: a confirmed run does reach the engine."""
    monkeypatch.setattr(seed_household, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "  chocofin  ")

    with pytest.raises(_Reached):
        await _main(_argv())


async def test_yes_skips_the_question_but_not_the_echo(
    guarded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """`--yes` is for scripts. It waives the answer, never the disclosure."""

    def never(*args, **kwargs):
        raise AssertionError("prompted despite --yes")

    monkeypatch.setattr("builtins.input", never)

    with pytest.raises(_Reached):
        await _main(_argv("--yes"))

    out = capsys.readouterr().out
    assert "postgresql+asyncpg://chocofin@db.invalid:5432/chocofin" in out
    assert "database: chocofin" in out
    assert FAKE_PASSWORD not in out
