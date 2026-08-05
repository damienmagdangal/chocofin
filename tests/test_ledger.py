"""Ledger operation tests."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import balances, ledger
from core.errors import (
    AccountNotFoundError,
    CardHasNoBillingAccountError,
    EntryAlreadyVoidedError,
    EntryNotFoundError,
    InvalidAmountError,
    NotACreditCardError,
    SameAccountTransferError,
)
from core.models import Entry, EntryLeg, EntryTag
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


async def test_reassign_preserves_tag_provenance(session: AsyncSession):
    """A correction moves money between accounts. It does not re-author tags.

    The tagger guessed '#lunch' at 0.62 confidence. If the replacement carries
    that tag as manual/1.0, the ledger now claims a human confirmed a tag no
    human ever saw — which poisons any later measure of how good the tagger is,
    and any rule-learning that keys off origin='manual'.
    """
    world = await build_world(session)
    original = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=32_000,
        occurred_at=JAN_15,
        note="lunch",
        tags=[ledger.TagSpec("lunch", origin="ai", confidence=0.62)],
    )
    await session.commit()

    replacement = await ledger.reassign_account(
        session,
        household_id=world.household_id,
        entry_id=original.id,
        account_id=world.savings_id,
    )
    await session.commit()

    carried = (
        await session.execute(
            select(EntryTag.tag, EntryTag.origin, EntryTag.confidence).where(
                EntryTag.entry_id == replacement.id,
                EntryTag.household_id == world.household_id,
            )
        )
    ).all()
    assert len(carried) == 1
    tag, origin, confidence = carried[0]
    assert tag == "lunch"
    assert origin == "ai"
    assert float(confidence) == 0.62


async def test_plain_string_tags_are_manual(session: AsyncSession):
    """The other half of the rule: a tag someone typed IS manual, at full
    confidence. Only a carried tag keeps someone else's provenance."""
    world = await build_world(session)
    entry = await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=1_000,
        occurred_at=JAN_15,
        tags=["Bonus"],
    )
    await session.commit()

    rows = (
        await session.execute(
            select(EntryTag.tag, EntryTag.origin, EntryTag.confidence).where(
                EntryTag.entry_id == entry.id
            )
        )
    ).all()
    assert len(rows) == 1
    tag, origin, confidence = rows[0]
    assert tag == "bonus"  # lowercased on the way in
    assert origin == "manual"
    assert float(confidence) == 1.0


async def test_transfer_tags_survive_a_commit(session: AsyncSession):
    """A transfer holds tags on exactly the same terms as an expense.

    A transfer carries no category at all — `ck_entries_transfer_has_no_category`
    forbids one — so a tag is the only label it can ever have. The parser reads
    tags off `/transfer 500 gcash top-up`, the pending row stores them, and the
    commit has to write them: otherwise the one kind of entry that cannot be
    categorised is also the one kind that cannot be labelled.
    """
    world = await build_world(session)
    entry = await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.cash_id,
        destination_account_id=world.savings_id,
        amount_minor=500_000,
        occurred_at=JAN_15,
        tags=["Savings", "savings", "Top-Up"],
    )
    await session.commit()

    rows = (
        await session.execute(
            select(EntryTag.tag, EntryTag.origin, EntryTag.confidence)
            .where(EntryTag.entry_id == entry.id)
            .order_by(EntryTag.tag)
        )
    ).all()
    # Lowercased and de-duplicated, identically to create_expense.
    assert [tag for tag, _, _ in rows] == ["savings", "top-up"]
    assert all(origin == "manual" for _, origin, _ in rows)
    assert all(float(confidence) == 1.0 for _, _, confidence in rows)

    # Tagging moved no money: still two legs, still summing to zero.
    legs = list(
        await session.scalars(select(EntryLeg).where(EntryLeg.entry_id == entry.id))
    )
    assert len(legs) == 2
    assert sum(leg.amount_minor for leg in legs) == 0


async def test_transfer_tags_do_not_leak_onto_the_fee(session: AsyncSession):
    """The fee is its own expense entry, and it does not inherit the tags.

    Copying them across would double every tag total that counts entries, and
    would claim the user labelled a fee they never saw.
    """
    world = await build_world(session)
    transfer = await ledger.create_transfer(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        source_account_id=world.cash_id,
        destination_account_id=world.savings_id,
        amount_minor=500_000,
        occurred_at=JAN_15,
        tags=["topup"],
        fee_minor=1_500,
    )
    await session.commit()

    fee = await session.scalar(
        select(Entry).where(Entry.related_entry_id == transfer.id)
    )
    assert fee is not None
    assert fee.kind == "expense"
    fee_tags = list(
        await session.scalars(select(EntryTag.tag).where(EntryTag.entry_id == fee.id))
    )
    assert fee_tags == []
    # ...and the transfer kept its own.
    kept = list(
        await session.scalars(
            select(EntryTag.tag).where(EntryTag.entry_id == transfer.id)
        )
    )
    assert kept == ["topup"]


async def test_settle_card_keeps_its_tags(session: AsyncSession):
    """`/pay 3000 #visa` has to arrive at the ledger with the tag still on it.

    A settlement is a transfer, so it carries no category and a tag is the only
    label it can hold — the same argument as `test_transfer_tags_survive_a_commit`,
    and the reason `settle_card` cannot be the one write that quietly drops them.
    The parser found the tag and the pending row stored it; a settlement that
    lost it here would make the tag findable on every account except the card.
    """
    world = await build_world(session)
    entry = await ledger.settle_card(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        card_id=world.card_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
        tags=["Visa", "visa", "Statement"],
    )
    await session.commit()

    rows = (
        await session.execute(
            select(EntryTag.tag, EntryTag.origin, EntryTag.confidence)
            .where(EntryTag.entry_id == entry.id)
            .order_by(EntryTag.tag)
        )
    ).all()
    # Lowercased and de-duplicated by the same `_add_tags` every other write uses.
    assert [tag for tag, _, _ in rows] == ["statement", "visa"]
    assert all(origin == "manual" for _, origin, _ in rows)

    # Still a transfer, and still the settlement it was: money left Savings —
    # the card's billing account — and landed on the card.
    assert entry.kind == "transfer"
    legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=entry.id
    )
    by_role = {leg.leg_role: leg for leg in legs}
    assert by_role["source"].account_id == world.savings_id
    assert by_role["destination"].account_id == world.card_id
    assert sum(leg.amount_minor for leg in legs) == 0


# --- settle_card ------------------------------------------------------------


async def test_settle_card_moves_money_from_the_billing_account(
    session: AsyncSession,
):
    """The happy path: no source given, so the card's `billing_account_id` pays.

    Which account pays is resolved in `core`, not by the caller — an adapter
    that had to know "a settlement comes from the billing account" would be a
    second copy of the rule, free to drift from this one.
    """
    world = await build_world(session)
    entry = await ledger.settle_card(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        card_id=world.card_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
    )
    await session.commit()

    assert entry.kind == "transfer"
    assert entry.amount_minor == 300_000
    assert entry.category_id is None  # a transfer never carries one

    legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=entry.id
    )
    by_role = {leg.leg_role: leg for leg in legs}
    assert by_role["source"].account_id == world.savings_id
    assert by_role["source"].amount_minor == -300_000
    assert by_role["destination"].account_id == world.card_id
    assert by_role["destination"].amount_minor == 300_000

    savings = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.savings_id
    )
    card = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.card_id
    )
    assert savings.balance_minor == -300_000
    assert card.balance_minor == 300_000


async def test_settle_card_rejects_a_non_card_account(session: AsyncSession):
    """Settling Cash is meaningless: there is no debt to pay down.

    Letting it through would write a transfer from Savings into Cash and call it
    a settlement, which is a plain transfer wearing the wrong name.
    """
    world = await build_world(session)
    with pytest.raises(NotACreditCardError):
        await ledger.settle_card(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            card_id=world.cash_id,
            amount_minor=100_000,
            occurred_at=JAN_15,
        )


async def test_settle_card_refuses_a_card_with_no_billing_account(
    session: AsyncSession,
):
    """No billing account and no explicit source: raise, never guess.

    Picking any account here would invent where real money came from, and the
    invented account's balance would be wrong from that moment on.
    """
    world = await build_world(session)
    with pytest.raises(CardHasNoBillingAccountError):
        await ledger.settle_card(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            card_id=world.orphan_card_id,
            amount_minor=100_000,
            occurred_at=JAN_15,
        )

    # Nothing was written on the way to the refusal.
    assert await ledger.list_entries(session, household_id=world.household_id) == []


async def test_explicit_source_beats_the_billing_account(session: AsyncSession):
    """`billing_account_id` is the default, not a lock.

    The card bills to Savings, but this month it was paid from Cash. The money
    has to leave the account it actually left, or both balances are wrong.
    """
    world = await build_world(session)
    entry = await ledger.settle_card(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        card_id=world.card_id,
        amount_minor=250_000,
        occurred_at=JAN_15,
        source_account_id=world.cash_id,
    )
    await session.commit()

    legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=entry.id
    )
    by_role = {leg.leg_role: leg for leg in legs}
    assert by_role["source"].account_id == world.cash_id
    assert by_role["destination"].account_id == world.card_id

    cash = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.cash_id
    )
    savings = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.savings_id
    )
    assert cash.balance_minor == -250_000
    assert savings.balance_minor == 0  # the billing account was not touched


async def test_settlement_never_reaches_a_summary(session: AsyncSession):
    """The purchase is the spending. The settlement is not a second one.

    Counting both would double the money and turn every month you pay a card
    into a month you overspent — here, PHP 3,000.00 of groceries would report
    as PHP 6,000.00.
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
    )
    await ledger.settle_card(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        card_id=world.card_id,
        amount_minor=300_000,
        occurred_at=FEB_10,
    )
    await session.commit()

    jan_start, jan_end = january()
    jan = await ledger.summarise(
        session, household_id=world.household_id, start_utc=jan_start, end_utc=jan_end
    )
    assert jan.expense_minor == 300_000

    # February is the month the card was paid, and it is an empty month.
    feb_start, feb_end = february()
    feb = await ledger.summarise(
        session, household_id=world.household_id, start_utc=feb_start, end_utc=feb_end
    )
    assert feb.expense_minor == 0
    assert feb.income_minor == 0
    assert feb.by_category == ()

    # ...and the settlement did move money: the card is back to zero.
    card = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.card_id
    )
    assert card.balance_minor == 0


# --- list_legs --------------------------------------------------------------


async def test_list_legs_returns_both_sides_of_a_transfer(session: AsyncSession):
    """Read back what was written instead of re-deriving it.

    A caller that re-applies the rule which chose the accounts has two
    implementations of one decision, and they are free to disagree.
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
    )
    await session.commit()

    legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=transfer.id
    )
    assert len(legs) == 2
    # Ordered by leg_role, so 'destination' comes before 'source'.
    assert [leg.leg_role for leg in legs] == ["destination", "source"]
    assert [leg.account_id for leg in legs] == [world.cash_id, world.savings_id]
    assert [leg.amount_minor for leg in legs] == [300_000, -300_000]
    assert sum(leg.amount_minor for leg in legs) == 0


async def test_list_legs_returns_one_leg_for_an_expense(session: AsyncSession):
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

    legs = await ledger.list_legs(
        session, household_id=world.household_id, entry_id=entry.id
    )
    assert len(legs) == 1
    assert legs[0].leg_role == "source"
    assert legs[0].account_id == world.cash_id
    assert legs[0].amount_minor == -10_000


async def test_list_legs_is_scoped_to_the_household(session: AsyncSession):
    """An entry id from another household reads back as nothing, not as legs."""
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

    assert (
        await ledger.list_legs(
            session,
            household_id=world.outsider_household_id,
            entry_id=entry.id,
        )
        == []
    )
    assert (
        await ledger.list_legs(
            session, household_id=world.household_id, entry_id=999_999
        )
        == []
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


async def test_newest_first_reverses_the_ordering(session: AsyncSession):
    """The only way to ask for the most recent N.

    Under the default ascending order, `limit=2` returns the two OLDEST entries
    in range, so "show me what I just logged" cannot be expressed at all.
    """
    world = await build_world(session)
    common = {
        "household_id": world.household_id,
        "member_id": world.member_id,
        "account_id": world.cash_id,
    }
    oldest = await ledger.create_expense(
        session, amount_minor=1_000, occurred_at=JAN_15, **common
    )
    middle = await ledger.create_expense(
        session, amount_minor=2_000, occurred_at=FEB_10, **common
    )
    newest = await ledger.create_expense(
        session, amount_minor=3_000, occurred_at=MAR_20, **common
    )
    await session.commit()

    ascending = await ledger.list_entries(session, household_id=world.household_id)
    assert [e.id for e in ascending] == [oldest.id, middle.id, newest.id]

    descending = await ledger.list_entries(
        session, household_id=world.household_id, newest_first=True
    )
    assert [e.id for e in descending] == [newest.id, middle.id, oldest.id]

    # ...and `limit` now takes from the recent end, which is the whole point.
    assert [
        e.id
        for e in await ledger.list_entries(
            session, household_id=world.household_id, newest_first=True, limit=2
        )
    ] == [newest.id, middle.id]
    assert [
        e.id
        for e in await ledger.list_entries(
            session, household_id=world.household_id, limit=2
        )
    ] == [oldest.id, middle.id]


async def test_newest_first_breaks_ties_by_write_order(session: AsyncSession):
    """Entries sharing a timestamp come back reversed too, not arbitrarily.

    Everything dated `@yesterday` lands on the same Manila midnight, so a tie is
    the normal case rather than a curiosity. `id` has to fall in the same
    direction as `occurred_at` or the newest of three same-day entries is
    whichever one the planner happened to emit first.
    """
    world = await build_world(session)
    common = {
        "household_id": world.household_id,
        "member_id": world.member_id,
        "account_id": world.cash_id,
        "occurred_at": JAN_15,  # identical for all three
    }
    first = await ledger.create_expense(session, amount_minor=1_000, **common)
    second = await ledger.create_expense(session, amount_minor=2_000, **common)
    third = await ledger.create_expense(session, amount_minor=3_000, **common)
    await session.commit()

    descending = await ledger.list_entries(
        session, household_id=world.household_id, newest_first=True
    )
    assert [e.id for e in descending] == [third.id, second.id, first.id]

    ascending = await ledger.list_entries(session, household_id=world.household_id)
    assert [e.id for e in ascending] == [first.id, second.id, third.id]

    # The most recent single entry is the last one written, not any of the ties.
    latest = await ledger.list_entries(
        session, household_id=world.household_id, newest_first=True, limit=1
    )
    assert [e.id for e in latest] == [third.id]


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
    balance-only; a summary must never consult it.

    A second, NON-excluded account moves too, and net worth is read as a
    movement from where the household started. A bare `net == 0` proved nothing:
    zero is what you get when the flag is honoured, and equally what you get
    when net worth counts nothing at all — and it only held while every factory
    account happened to open at zero, which nothing enforces.
    """
    world = await build_world(session)
    opening_net = await balances.net_worth_minor(
        session, household_id=world.household_id
    )
    opening_excluded = (
        await balances.account_balance(
            session, household_id=world.household_id, account_id=world.excluded_id
        )
    ).balance_minor

    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.excluded_id,
        amount_minor=25_000,
        occurred_at=JAN_15,
    )
    await ledger.create_income(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.cash_id,
        amount_minor=40_000,
        occurred_at=JAN_15,
        category_id=world.salary_id,
    )
    await session.commit()

    start, end = january()
    summary = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )
    assert summary.expense_minor == 25_000
    assert summary.income_minor == 40_000

    # The spending really did leave the excluded account — it is not missing
    # from net worth because it was never recorded.
    excluded = await balances.account_balance(
        session, household_id=world.household_id, account_id=world.excluded_id
    )
    assert excluded.balance_minor - opening_excluded == -25_000

    # ...and net worth keeps only the 40,000 that landed in Cash. 15,000 would
    # mean the excluded spending leaked in; 0 would mean nothing counted.
    net = await balances.net_worth_minor(session, household_id=world.household_id)
    assert net - opening_net == 40_000


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


async def test_summarise_breaks_down_income_by_category(session: AsyncSession):
    """Income gets its own breakdown, and it stays out of the expense one.

    The two are separate fields on purpose. Merged into one signed list, the
    salary below would cancel most of the groceries and the month would report
    as almost nothing happening.
    """
    world = await build_world(session)
    common = {
        "household_id": world.household_id,
        "member_id": world.member_id,
        "occurred_at": JAN_15,
    }
    await ledger.create_income(
        session,
        account_id=world.cash_id,
        amount_minor=4_500_000,
        category_id=world.salary_id,
        **common,
    )
    await ledger.create_income(
        session,
        account_id=world.cash_id,
        amount_minor=120_000,
        category_id=None,
        **common,
    )
    await ledger.create_expense(
        session,
        account_id=world.cash_id,
        amount_minor=15_000,
        category_id=world.coffee_id,
        **common,
    )
    await session.commit()

    start, end = january()
    summary = await ledger.summarise(
        session, household_id=world.household_id, start_utc=start, end_utc=end
    )

    income = {c.category_id: c.total_minor for c in summary.by_income_category}
    assert income == {world.salary_id: 4_500_000, None: 120_000}
    assert sum(income.values()) == summary.income_minor

    # And the expense breakdown is untouched: Coffee still rolls up to Food,
    # with no income bucket anywhere in it.
    assert {c.category_id: c.total_minor for c in summary.by_category} == {
        world.food_id: 15_000
    }
    assert sum(c.total_minor for c in summary.by_category) == summary.expense_minor


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


async def test_net_worth_includes_deactivated_accounts(session: AsyncSession):
    """`is_active` hides an account from pickers. It does not settle its debt.

    A closed card still owing PHP 3,000.00 is PHP 3,000.00 the household still
    owes. Dropping it from net worth would make the household appear PHP
    3,000.00 richer at the exact moment it stopped using the card, with the
    debt sitting untouched in the ledger the whole time.

    Every figure is measured against the accounts under test — the card's own
    balance and the total before the change — rather than an absolute. The
    absolutes only worked because the factory opens every account at zero, which
    is a fixture detail and not a rule.
    """
    from core.models import Account

    world = await build_world(session)
    opening_net = await balances.net_worth_minor(
        session, household_id=world.household_id
    )
    await ledger.create_expense(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        account_id=world.card_id,
        amount_minor=300_000,
        occurred_at=JAN_15,
    )
    await session.commit()
    owing = await balances.net_worth_minor(session, household_id=world.household_id)
    assert owing - opening_net == -300_000

    card = await session.scalar(select(Account).where(Account.id == world.card_id))
    card.is_active = False
    await session.commit()

    # Not "still -300,000" — still the SAME total. Closing an account moves no
    # money at all, so nothing about this number may change.
    assert (
        await balances.net_worth_minor(session, household_id=world.household_id)
    ) == owing

    # ...and `exclude_from_totals` is still the one flag that does remove it:
    # the total drops by exactly the card's own balance, debt and opening alike.
    card_balance = (
        await balances.account_balance(
            session, household_id=world.household_id, account_id=world.card_id
        )
    ).balance_minor
    card.exclude_from_totals = True
    await session.commit()
    assert (
        await balances.net_worth_minor(session, household_id=world.household_id)
    ) == owing - card_balance


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
