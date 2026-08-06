"""Tests for `core.pending` — the half-finished entry between a parsed
message and a chosen account.

The load-bearing test here is `test_two_concurrent_claims_leave_one_winner`.
`claim` is a single `DELETE ... RETURNING`, and the whole double-tap guarantee
rests on that being atomic: two taps on one button must produce exactly one
entry. Proving it needs two real connections racing on one row, so that test
opens its own sessions rather than using the shared fixture.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from core import pending
from core.models import PendingEntry
from tests.factories import JAN_15, World, build_world

pytestmark = pytest.mark.asyncio

NOW = dt.datetime(2026, 1, 15, 6, 30, tzinfo=dt.UTC)


async def _create(
    session: AsyncSession,
    world: World,
    *,
    member_id: int | None = None,
    household_id: int | None = None,
    intent: str = "expense",
    parsed_kind: str = "expense",
    now: dt.datetime = NOW,
    **kwargs,
) -> PendingEntry:
    return await pending.create(
        session,
        household_id=household_id or world.household_id,
        member_id=member_id or world.member_id,
        raw_input="100 coffee",
        intent=intent,
        parsed_kind=parsed_kind,
        parsed_amount_minor=10_000,
        occurred_at=JAN_15,
        now=now,
        **kwargs,
    )


async def _count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(PendingEntry))


# --- create -----------------------------------------------------------------


async def test_create_stores_the_whole_parsed_message(session: AsyncSession):
    """The raw message is parsed exactly once, so everything it yielded has to
    survive in the row — otherwise a second parse gets to disagree with the
    first about what the user typed."""
    world = await build_world(session)
    row = await _create(
        session,
        world,
        parsed_category_id=world.coffee_id,
        parsed_note="Starbucks",
        parsed_tags=["coffee", "work"],
    )
    await session.commit()

    assert row.household_id == world.household_id
    assert row.member_id == world.member_id
    assert row.raw_input == "100 coffee"
    assert row.parsed_kind == "expense"
    assert row.parsed_amount_minor == 10_000
    assert row.parsed_category_id == world.coffee_id
    assert row.parsed_note == "Starbucks"
    assert row.parsed_tags == ["coffee", "work"]
    assert row.occurred_at == JAN_15
    assert row.source_account_id is None  # nothing is chosen yet
    assert row.expires_at == NOW + pending.PENDING_TTL


async def test_intent_is_recorded_not_inferred(session: AsyncSession):
    """A settlement commits as a transfer, and the row has to remember both.

    `/pay` and `/transfer` produce identical `parsed_*` columns. Without
    `intent`, the only thing separating them is buttons that have already been
    sent, so the next step would have to guess from which columns are NULL.
    """
    world = await build_world(session)
    row = await _create(session, world, intent="settlement", parsed_kind="transfer")
    await session.commit()

    assert row.intent == "settlement"
    assert row.parsed_kind == "transfer"


# --- get --------------------------------------------------------------------


async def test_get_reads_the_row_without_taking_it(session: AsyncSession):
    world = await build_world(session)
    row = await _create(session, world)
    await session.commit()

    got = await pending.get(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=row.id,
    )
    assert got is not None
    assert got.id == row.id
    # Still there: `get` renders the next keyboard, it does not consume.
    assert await _count(session) == 1


async def test_get_is_scoped_to_the_household(session: AsyncSession):
    world = await build_world(session)
    row = await _create(session, world)
    await session.commit()

    assert (
        await pending.get(
            session,
            household_id=world.outsider_household_id,
            member_id=world.outsider_member_id,
            pending_id=row.id,
        )
        is None
    )


async def test_get_is_scoped_to_the_member(session: AsyncSession):
    """A housemate cannot read the state of a flow they did not start.

    Same household, so `household_id` is not what makes this pass — this is the
    member predicate on its own. A household is a shared Telegram chat: everyone
    in it can see everyone else's keyboards, which is exactly why the row has to
    be scoped tighter than the household that owns the money.
    """
    world = await build_world(session)
    row = await _create(session, world)
    await session.commit()

    assert (
        await pending.get(
            session,
            household_id=world.household_id,
            member_id=world.other_member_id,
            pending_id=row.id,
        )
        is None
    )
    # ...and it is still there for the member whose row it is.
    assert (
        await pending.get(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=row.id,
        )
        is not None
    )


# --- set_source_account -----------------------------------------------------


async def test_set_source_account_holds_the_first_tap(session: AsyncSession):
    """A transfer needs two accounts and the keyboard can only ask for one."""
    world = await build_world(session)
    row = await _create(session, world, intent="transfer", parsed_kind="transfer")
    await session.commit()
    # Read the id out to a plain int NOW. `expire_all` below blanks every
    # attribute on `row`, and reading one back then is a lazy refresh, which
    # raises MissingGreenlet under asyncio rather than loading.
    pending_id = row.id

    assert (
        await pending.set_source_account(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=pending_id,
            account_id=world.cash_id,
        )
        is True
    )
    await session.commit()
    # Expire so the read below is a real SELECT. Otherwise the ORM's own
    # synchronisation of the UPDATE would answer it, and a `set_source_account`
    # that never reached the database would still look like it worked.
    session.expire_all()

    got = await pending.get(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=pending_id,
    )
    assert got is not None
    assert got.source_account_id == world.cash_id


async def test_set_source_account_reports_a_row_that_is_gone(session: AsyncSession):
    """Cancelled, expired or already claimed are all the same answer: False.

    The caller says so rather than carrying on with a transfer whose first half
    no longer exists.
    """
    world = await build_world(session)
    row = await _create(session, world, intent="transfer", parsed_kind="transfer")
    await session.commit()
    await pending.claim(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=row.id,
    )
    await session.commit()

    assert (
        await pending.set_source_account(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=row.id,
            account_id=world.cash_id,
        )
        is False
    )


async def test_set_source_account_cannot_reach_another_household(
    session: AsyncSession,
):
    world = await build_world(session)
    row = await _create(session, world, intent="transfer", parsed_kind="transfer")
    await session.commit()

    assert (
        await pending.set_source_account(
            session,
            household_id=world.outsider_household_id,
            member_id=world.outsider_member_id,
            pending_id=row.id,
            account_id=world.outsider_account_id,
        )
        is False
    )
    await session.commit()

    # ...and the row itself is untouched.
    stored = await session.scalar(
        select(PendingEntry.source_account_id).where(PendingEntry.id == row.id)
    )
    assert stored is None


async def test_set_source_account_cannot_reach_another_members_row(
    session: AsyncSession,
):
    """A housemate cannot steer the first half of someone else's transfer.

    Same household — the money is jointly theirs, so nothing above this level
    would object. A transfer is two taps with a gap in between, and without the
    member predicate whoever taps first decides where the other person's money
    leaves from.
    """
    world = await build_world(session)
    row = await _create(session, world, intent="transfer", parsed_kind="transfer")
    await session.commit()

    assert (
        await pending.set_source_account(
            session,
            household_id=world.household_id,
            member_id=world.other_member_id,
            pending_id=row.id,
            account_id=world.cash_id,
        )
        is False
    )
    await session.commit()

    # The load-bearing assertion, as in the single-leg case above: nothing
    # reached the database. A False with the UPDATE still applied would be the
    # same bug wearing a different answer.
    stored = await session.scalar(
        select(PendingEntry.source_account_id).where(PendingEntry.id == row.id)
    )
    assert stored is None


async def test_set_source_account_refuses_a_single_leg_row(session: AsyncSession):
    """An expense has no source to remember, and lending it one is the whole bug.

    A source on an expense row is indistinguishable from a half-finished
    transfer, so the destination tap after it commits as `kind='transfer'` —
    money the user typed as spending, gone from every spending total. The bot
    refuses that payload one layer up; core refusing it too is what stops a
    future caller from reopening the hole.
    """
    world = await build_world(session)
    row = await _create(session, world, intent="expense", parsed_kind="expense")
    await session.commit()

    assert (
        await pending.set_source_account(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=row.id,
            account_id=world.cash_id,
        )
        is False
    )
    await session.commit()

    # The load-bearing assertion: nothing reached the database. A returned
    # False with the UPDATE still applied would be the same bug wearing a
    # different answer.
    stored = await session.scalar(
        select(PendingEntry.source_account_id).where(PendingEntry.id == row.id)
    )
    assert stored is None


# --- claim ------------------------------------------------------------------


async def test_claim_returns_a_snapshot_and_deletes_the_row(session: AsyncSession):
    """The row is gone by the time the caller sees the result, which is why the
    result is a frozen snapshot and not a live ORM object mapped to nothing."""
    world = await build_world(session)
    row = await _create(
        session,
        world,
        parsed_category_id=world.coffee_id,
        parsed_note="Starbucks",
        parsed_tags=["coffee"],
    )
    await session.commit()

    claimed = await pending.claim(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=row.id,
    )
    await session.commit()

    assert claimed is not None
    assert claimed.id == row.id
    assert claimed.household_id == world.household_id
    assert claimed.member_id == world.member_id
    assert claimed.raw_input == "100 coffee"
    assert claimed.intent == "expense"
    assert claimed.parsed_kind == "expense"
    assert claimed.parsed_amount_minor == 10_000
    assert claimed.parsed_category_id == world.coffee_id
    assert claimed.parsed_note == "Starbucks"
    assert claimed.parsed_tags == ("coffee",)  # a tuple: the snapshot is frozen
    assert claimed.occurred_at == JAN_15
    assert await _count(session) == 0


async def test_a_second_claim_finds_nothing(session: AsyncSession):
    """The double-tap answer, without any concurrency involved."""
    world = await build_world(session)
    row = await _create(session, world)
    await session.commit()

    first = await pending.claim(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=row.id,
    )
    await session.commit()
    second = await pending.claim(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=row.id,
    )
    await session.commit()

    assert first is not None
    assert second is None


async def test_two_concurrent_claims_leave_one_winner(
    engine: AsyncEngine, session: AsyncSession
):
    """Two taps racing on one button write one entry, not two.

    Both connections issue the same `DELETE ... RETURNING`. Postgres serialises
    them on the row lock, so one gets the row and the other gets nothing — with
    no window in between for the bot to check anything, because the check and
    the take are one statement.
    """
    world = await build_world(session)
    row = await _create(session, world)
    await session.commit()

    async def tap() -> pending.ClaimedPending | None:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            claimed = await pending.claim(
                s,
                household_id=world.household_id,
                member_id=world.member_id,
                pending_id=row.id,
            )
            await s.commit()
            return claimed

    results = await asyncio.gather(tap(), tap())

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, "both taps claimed the same pending row"
    assert winners[0].id == row.id
    assert await _count(session) == 0


async def test_an_expired_row_is_still_claimed_and_deleted(session: AsyncSession):
    """Expiry is the caller's decision, not a WHERE clause.

    Filtering expiry into the DELETE would leave the dead row in the table and
    tell the user "already recorded" — which is false twice over: nothing was
    recorded, and nothing now can be.
    """
    world = await build_world(session)
    stale = NOW - pending.PENDING_TTL - dt.timedelta(hours=1)
    row = await _create(session, world, now=stale)
    await session.commit()

    claimed = await pending.claim(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=row.id,
    )
    await session.commit()

    assert claimed is not None
    assert claimed.is_expired(NOW) is True
    assert claimed.is_expired(stale) is False  # it was live when it was written
    assert await _count(session) == 0


async def test_a_member_cannot_claim_another_households_row(session: AsyncSession):
    """`household_id` is in the WHERE clause of the take itself, so a leaked or
    guessed pending id buys nothing."""
    world = await build_world(session)
    theirs = await _create(
        session,
        world,
        household_id=world.outsider_household_id,
        member_id=world.outsider_member_id,
    )
    await session.commit()

    assert (
        await pending.claim(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=theirs.id,
        )
        is None
    )
    assert (
        await pending.cancel(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=theirs.id,
        )
        is False
    )
    await session.commit()

    # Their row is untouched, and still theirs.
    assert (
        await pending.get(
            session,
            household_id=world.outsider_household_id,
            member_id=world.outsider_member_id,
            pending_id=theirs.id,
        )
        is not None
    )


async def test_a_housemate_cannot_claim_your_pending_row(session: AsyncSession):
    """The bug this scoping exists for, at the level it has to be fixed.

    Member A types the message; member B taps the button in the same shared
    chat. `household_id` matches — they really are in the same household — so it
    is the member predicate or nothing. Without it the entry is written against
    B, who never typed it, and B's recent-accounts shortlist learns from money
    they did not spend.

    `cancel` is checked alongside because it is `claim` underneath: if it took a
    different route to the row, B could still make A's keyboard vanish.
    """
    world = await build_world(session)
    row = await _create(session, world)
    await session.commit()
    pending_id = row.id

    assert (
        await pending.claim(
            session,
            household_id=world.household_id,
            member_id=world.other_member_id,
            pending_id=pending_id,
        )
        is None
    )
    assert (
        await pending.cancel(
            session,
            household_id=world.household_id,
            member_id=world.other_member_id,
            pending_id=pending_id,
        )
        is False
    )
    await session.commit()

    # Still there, and still answerable by the member who typed it. A guard that
    # refused everyone would pass the two assertions above and break the bot.
    claimed = await pending.claim(
        session,
        household_id=world.household_id,
        member_id=world.member_id,
        pending_id=pending_id,
    )
    await session.commit()

    assert claimed is not None
    assert claimed.member_id == world.member_id
    assert await _count(session) == 0


# --- cancel -----------------------------------------------------------------


async def test_cancel_removes_the_row_once(session: AsyncSession):
    world = await build_world(session)
    row = await _create(session, world)
    await session.commit()

    assert (
        await pending.cancel(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=row.id,
        )
        is True
    )
    await session.commit()
    assert (
        await pending.cancel(
            session,
            household_id=world.household_id,
            member_id=world.member_id,
            pending_id=row.id,
        )
        is False
    )
    assert await _count(session) == 0


# --- the TTL sweep ----------------------------------------------------------


async def test_the_sweep_only_clears_this_members_expired_rows(session: AsyncSession):
    """There is no scheduler in this service, so `create` sweeps opportunistically.

    It must only ever remove rows already past their TTL, and only this
    member's: someone else's open keyboard is live and must not be disturbed by
    an unrelated person logging an expense.
    """
    world = await build_world(session)
    stale = NOW - pending.PENDING_TTL - dt.timedelta(hours=1)

    # Every `create` sweeps, for whichever member it is creating FOR. So each
    # member's live row is written before their dead one: a create at `stale`
    # collects nothing, whereas a create at `NOW` would collect the dead row
    # this test is about to look for. Setup must not do the acting call's job.
    mine_live = await _create(session, world, now=NOW)
    mine_dead = await _create(session, world, now=stale)
    theirs_live = await _create(
        session, world, member_id=world.other_member_id, now=NOW
    )
    theirs_dead = await _create(
        session, world, member_id=world.other_member_id, now=stale
    )
    outsider_dead = await _create(
        session,
        world,
        household_id=world.outsider_household_id,
        member_id=world.outsider_member_id,
        now=stale,
    )
    await session.commit()

    # The call under test: this member logging something, nothing more.
    await _create(session, world, now=NOW)
    await session.commit()

    # `get` is member-scoped, so each row has to be looked for as its own owner.
    # Asking as the sweeping member would return None for everyone else's rows
    # and this test would report them all collected.
    async def alive(pending_id: int, household_id: int, member_id: int) -> bool:
        return (
            await pending.get(
                session,
                household_id=household_id,
                member_id=member_id,
                pending_id=pending_id,
            )
        ) is not None

    assert await alive(mine_dead.id, world.household_id, world.member_id) is False
    assert await alive(mine_live.id, world.household_id, world.member_id) is True
    # Another member's dead row is still dead, but it is not this call's to
    # collect — and their live keyboard is certainly not.
    assert (
        await alive(theirs_dead.id, world.household_id, world.other_member_id) is True
    )
    assert (
        await alive(theirs_live.id, world.household_id, world.other_member_id) is True
    )
    assert (
        await alive(
            outsider_dead.id, world.outsider_household_id, world.outsider_member_id
        )
        is True
    )
