"""The one authorisation decorator, exercised as the update loop calls it.

There is no bot token in this file and no network. `bot.auth.authorised` touches
exactly four things on a PTB update — `effective_user`, `effective_message`,
`callback_query`, and `bot_data` — so the stubs below supply those four and
nothing else. Building real `telegram` objects would drag in a `Bot`, and a
`Bot` wants a token; the decorator does not, and a test that needed one would be
testing the wrong thing.

What is real here is the database. Authorisation is a membership lookup, so a
test that faked the member would prove only that the fake was consulted. The
household boundary in particular can only be shown with two real households and
a real pending row, which is what the last group does.

Three properties are under test, and they are the three that cannot be checked
by reading a handler:

* Who gets in. An unknown id and an inactive member are turned away BEFORE the
  handler runs, and the `household_id` the handler works with comes from the
  members table rather than from anything the caller sent.
* That `answer_callback_query` happens exactly once on every path — success,
  rejection, and a handler that raises. Zero leaves the client spinning on the
  button forever; twice is an API error. The `finally` block is what makes it
  one, so the exception path is the case worth having.
* That nothing reaches the chat until the transaction has committed. The leg
  trigger is deferred to COMMIT, so a handler that rendered its own receipt
  would be announcing an entry the commit could still refuse. The last two
  tests pin the order from both sides: a receipt that must never be sent, and
  one that must not be sent early.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot import flows
from bot.auth import (
    INACTIVE_MEMBER,
    INTERNAL_ERROR,
    SESSIONMAKER_KEY,
    UNKNOWN_USER,
    Actor,
    Reply,
    authorised,
)
from core.db import make_sessionmaker
from core.models import Entry, EntryLeg, Member, PendingEntry
from tests.factories import JAN_15, build_world

pytestmark = pytest.mark.asyncio

# Not a member of anything. Chosen far from the factory's 10_000_00x block so it
# cannot collide with a real row as the fixtures grow.
STRANGER_TELEGRAM_ID = 99_999_999


# --- the four attributes the decorator actually reads ------------------------


class FakeUser:
    def __init__(self, telegram_id: int) -> None:
        self.id = telegram_id


class FakeMessage:
    """Records what the user was told, instead of sending it."""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeCallbackQuery:
    """Records every `answer()` and every edit.

    `message` is a bare sentinel: the decorator only edits a query that has a
    message to edit, and nothing is ever read off it.
    """

    def __init__(self) -> None:
        self.answers: list[str | None] = []
        self.edits: list[str] = []
        self.message = object()

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)


class FakeUpdate:
    def __init__(self, *, telegram_id: int, callback: bool = False) -> None:
        self.effective_user = FakeUser(telegram_id)
        self.effective_message = FakeMessage()
        self.callback_query = FakeCallbackQuery() if callback else None

    @property
    def query(self) -> FakeCallbackQuery:
        assert self.callback_query is not None
        return self.callback_query

    @property
    def shown(self) -> list[str]:
        """Everything that reached the chat, by either route.

        The decorator chooses between a new message and an edit. What these
        tests care about is what a human ends up reading, not which API call
        put it there.
        """
        edits = self.callback_query.edits if self.callback_query is not None else []
        return [*self.effective_message.replies, *edits]


class FakeContext:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.bot_data = {SESSIONMAKER_KEY: factory}


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A real sessionmaker, because the decorator opens its own session.

    It cannot be handed the test's session: `session_scope` commits, and the
    point of several of these tests is what is in the database after it does.
    """
    return make_sessionmaker(engine)


class Recorder:
    """A handler that remembers it was called, and with which Actor."""

    def __init__(self, toast: str | None = "Done") -> None:
        self.calls: list[Actor] = []
        self.reply = Reply(text="Done", toast=toast)

    async def __call__(
        self, update, context, actor: Actor, session: AsyncSession
    ) -> Reply:
        self.calls.append(actor)
        return self.reply

    @property
    def called(self) -> bool:
        return bool(self.calls)


async def _pending_id(session: AsyncSession, household_id: int) -> int:
    row = await session.scalar(
        select(PendingEntry.id).where(PendingEntry.household_id == household_id)
    )
    assert row is not None
    return row


# --- who gets in -------------------------------------------------------------


async def test_an_unknown_telegram_id_is_rejected(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """No row in `members`, no household, no handler.

    The rejection has to happen before the handler, not inside it: a handler
    that ran with no Actor would have nothing to filter on, and the first thing
    it did would be to invent a `household_id`.
    """
    await build_world(session)
    handler = Recorder()
    update = FakeUpdate(telegram_id=STRANGER_TELEGRAM_ID)

    await authorised(handler)(update, FakeContext(factory))

    assert not handler.called
    assert update.effective_message.replies == [UNKNOWN_USER]


async def test_an_inactive_member_is_rejected(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """A known id is not the same question as a permitted one.

    `get_member_by_telegram_id` returns inactive members on purpose — a name
    still has to be resolvable after someone leaves — so `is_active` is a
    decision the decorator makes, and it is the only place that makes it.
    """
    world = await build_world(session)
    former = Member(
        household_id=world.household_id,
        telegram_user_id=10_000_009,
        display_name="Former Housemate",
        role="member",
        is_active=False,
    )
    session.add(former)
    await session.commit()

    handler = Recorder()
    update = FakeUpdate(telegram_id=former.telegram_user_id)

    await authorised(handler)(update, FakeContext(factory))

    assert not handler.called
    assert update.effective_message.replies == [INACTIVE_MEMBER]


async def test_the_actor_household_comes_from_the_members_table(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """The Actor is the bot's only source of `household_id`.

    Stated as a test so that the day a handler grows a household argument, this
    is what fails.
    """
    world = await build_world(session)
    handler = Recorder()

    await authorised(handler)(
        FakeUpdate(telegram_id=world.member_telegram_id), FakeContext(factory)
    )

    (actor,) = handler.calls
    assert actor.household_id == world.household_id
    assert actor.member_id == world.member_id
    assert actor.display_name == "Tester"


# --- the household boundary --------------------------------------------------


async def test_a_member_cannot_claim_another_households_pending_row(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """A real button id from household A, tapped by a member of household B.

    `pending_id` travels in `callback_data`, so it is the one number a caller
    can choose freely — and there is nothing in it that says whose it is. The
    only thing standing between that and reading another household's ledger is
    that the `household_id` used to claim comes from the Actor, which came from
    the members table.

    The outsider is a genuine authorised member, so this gets all the way past
    the decorator and fails at the WHERE clause, which is where it must fail.
    Nothing is written and household A's row is left untouched, still tappable
    by the person it belongs to.
    """
    world = await build_world(session)
    owner = Actor(
        member_id=world.member_id,
        household_id=world.household_id,
        display_name="Tester",
    )
    await flows.start_entry(session, owner, raw="120 coffee", now=JAN_15)
    await session.commit()
    pending_id = await _pending_id(session, world.household_id)

    async def commit_it(update, context, actor: Actor, session: AsyncSession):
        assert actor.household_id == world.outsider_household_id
        return await flows.commit_account_choice(
            session,
            actor,
            pending_id=pending_id,
            account_id=world.outsider_account_id,
            now=JAN_15,
        )

    update = FakeUpdate(telegram_id=world.outsider_telegram_id, callback=True)
    await authorised(commit_it)(update, FakeContext(factory))

    # The claim matched nothing, so the flow reports the same thing it reports
    # for a double tap. It cannot report anything more specific without telling
    # an outsider that the row exists.
    assert update.query.answers == [flows.ALREADY_DONE]

    await session.rollback()  # drop this session's snapshot; read what committed
    assert (await session.scalar(select(Entry.id))) is None
    assert (
        await session.scalar(
            select(PendingEntry.id).where(PendingEntry.id == pending_id)
        )
    ) == pending_id


# --- exactly one answer_callback_query, on every path ------------------------


async def test_a_successful_callback_is_answered_exactly_once(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """Success, through a real claim and a real ledger write.

    The toast the handler returned is the toast that reaches Telegram, which is
    the arrangement that lets handlers stay out of `answer()` entirely.
    """
    world = await build_world(session)
    owner = Actor(
        member_id=world.member_id,
        household_id=world.household_id,
        display_name="Tester",
    )
    await flows.start_entry(session, owner, raw="120 coffee", now=JAN_15)
    await session.commit()
    pending_id = await _pending_id(session, world.household_id)

    async def commit_it(update, context, actor: Actor, session: AsyncSession):
        return await flows.commit_account_choice(
            session,
            actor,
            pending_id=pending_id,
            account_id=world.cash_id,
            now=JAN_15,
        )

    update = FakeUpdate(telegram_id=world.member_telegram_id, callback=True)
    await authorised(commit_it)(update, FakeContext(factory))

    assert update.query.answers == ["Recorded"]

    # And the transaction the decorator opened really committed, so the answer
    # was not a toast over a rolled-back write.
    await session.rollback()
    assert (await session.scalar(select(Entry.amount_minor))) == 12_000


async def test_a_rejected_callback_is_answered_exactly_once(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """A stranger tapping a button still gets the spinner cleared.

    Rejection returns early from inside a `try`, which is exactly the shape
    that skips a cleanup written anywhere but a `finally`.
    """
    await build_world(session)
    handler = Recorder()
    update = FakeUpdate(telegram_id=STRANGER_TELEGRAM_ID, callback=True)

    await authorised(handler)(update, FakeContext(factory))

    assert not handler.called
    assert update.query.answers == [UNKNOWN_USER]
    # A callback query is answered with a toast, and nothing is put in the chat
    # by either route: the bot does not announce itself to someone who may not
    # use it, in front of everyone else in the room.
    assert update.shown == []


async def test_a_raising_handler_still_answers_exactly_once(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """The case the `finally` exists for.

    The exception is re-raised — the update loop should log it as a real
    failure — and the button is released on the way out regardless. If the
    answer moved into the success path, this is the test that goes red: the
    user would be left holding a spinning button on the one path where
    something actually went wrong.

    The toast says nothing was recorded, and that is true: `session_scope`
    rolled back before the `finally` ran. The same words go in the chat as
    well, because a toast is gone in a second and a failed expense is worth a
    line the user can still see afterwards.
    """
    world = await build_world(session)

    class Boom(Exception):
        pass

    async def explode(update, context, actor: Actor, session: AsyncSession):
        # A write first, so the rollback has something to undo and the toast is
        # a claim about the database rather than about an empty transaction.
        await flows.start_entry(session, actor, raw="120 coffee", now=JAN_15)
        raise Boom("handler blew up after writing")

    update = FakeUpdate(telegram_id=world.member_telegram_id, callback=True)

    with pytest.raises(Boom):
        await authorised(explode)(update, FakeContext(factory))

    assert update.query.answers == [INTERNAL_ERROR]
    assert update.shown == [INTERNAL_ERROR]

    await session.rollback()
    assert (await session.scalar(select(PendingEntry.id))) is None


# --- nothing is said until the transaction has committed ---------------------


async def test_a_commit_that_fails_sends_no_confirmation(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
):
    """A receipt for an entry that COMMIT then refused.

    The leg trigger is DEFERRABLE INITIALLY DEFERRED, so it runs at COMMIT —
    after the handler has returned, and after everything a handler could
    possibly check for itself. This one writes a perfectly good expense, gets
    back a `Reply` naming the new entry id, and then adds a second `source` leg
    so the commit is refused. Nothing complains when that leg is inserted; that
    is what "deferred" means, and it is the entire reason the ordering matters.

    So the receipt must not reach the chat. It names an entry that does not
    exist, and unlike the toast that would contradict it, a message stays in the
    scrollback for good.
    """
    world = await build_world(session)
    owner = Actor(
        member_id=world.member_id,
        household_id=world.household_id,
        display_name="Tester",
    )
    await flows.start_entry(session, owner, raw="120 coffee", now=JAN_15)
    await session.commit()
    pending_id = await _pending_id(session, world.household_id)

    receipts: list[str] = []
    entry_ids: list[int] = []

    async def commit_then_break_the_legs(
        update, context, actor: Actor, session: AsyncSession
    ):
        reply = await flows.commit_account_choice(
            session,
            actor,
            pending_id=pending_id,
            account_id=world.cash_id,
            now=JAN_15,
        )
        receipts.append(reply.text)

        entry_id = await session.scalar(select(Entry.id))
        assert entry_id is not None
        entry_ids.append(entry_id)

        # A duplicate of the leg the ledger just wrote. An expense may have
        # exactly one source leg, so this makes COMMIT fail — and it is accepted
        # without complaint here, which is the point.
        session.add(
            EntryLeg(
                entry_id=entry_id,
                household_id=actor.household_id,
                account_id=world.savings_id,
                amount_minor=-12_000,
                leg_role="source",
            )
        )
        return reply

    update = FakeUpdate(telegram_id=world.member_telegram_id, callback=True)

    with pytest.raises(DBAPIError):
        await authorised(commit_then_break_the_legs)(update, FakeContext(factory))

    # The handler really did produce a receipt naming the entry ...
    (receipt,) = receipts
    assert f"#{entry_ids[0]}" in receipt

    # ... and not one character of it was put on screen.
    assert update.shown == [INTERNAL_ERROR]
    assert update.query.answers == [INTERNAL_ERROR]

    await session.rollback()
    assert (await session.scalar(select(Entry.id))) is None
    # The claim rolled back with everything else, so the keyboard still on
    # screen has a row to act on. That is why the error reply does not edit it
    # away: the user can simply tap again.
    assert (
        await session.scalar(
            select(PendingEntry.id).where(PendingEntry.id == pending_id)
        )
    ) == pending_id


async def test_the_receipt_is_sent_after_the_commit(
    session: AsyncSession,
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
):
    """The same ordering, stated on the path that works.

    A receipt is only true if the money is in the database before it is sent, so
    this reads the ledger from a SEPARATE connection at the moment the message
    goes out. Another connection can see committed rows and nothing else, which
    makes this green exactly when the send happens after COMMIT — and red, not
    flaky, if it ever moves back inside the transaction.
    """
    world = await build_world(session)
    owner = Actor(
        member_id=world.member_id,
        household_id=world.household_id,
        display_name="Tester",
    )
    await flows.start_entry(session, owner, raw="120 coffee", now=JAN_15)
    await session.commit()
    pending_id = await _pending_id(session, world.household_id)

    visible: list[int | None] = []

    class ProbingQuery(FakeCallbackQuery):
        async def edit_message_text(self, text: str, **kwargs) -> None:
            async with AsyncSession(engine) as probe:
                visible.append(await probe.scalar(select(Entry.amount_minor)))
            await super().edit_message_text(text, **kwargs)

    async def commit_it(update, context, actor: Actor, session: AsyncSession):
        return await flows.commit_account_choice(
            session,
            actor,
            pending_id=pending_id,
            account_id=world.cash_id,
            now=JAN_15,
        )

    update = FakeUpdate(telegram_id=world.member_telegram_id, callback=True)
    update.callback_query = ProbingQuery()

    await authorised(commit_it)(update, FakeContext(factory))

    assert visible == [12_000]
    assert update.query.answers == ["Recorded"]
