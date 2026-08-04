"""The half-finished entry between a parsed message and a chosen account.

No entry is written until an account is tapped, and the waiting state is a
row rather than a dict in the process, so a redeploy mid-flow leaves every
keyboard still live. That is the whole reason this table exists.

`claim` is the load-bearing function. It is a single

    DELETE ... WHERE id = ... RETURNING ...

which is atomic: two taps racing on the same button both run it, exactly one
gets a row back, and the other gets `None`. Idempotency therefore does not
depend on the bot checking anything first — there is no window between "is it
still there?" and "take it", because those are one statement. The caller runs
`claim` and the ledger write in ONE transaction, so an entry cannot be written
without its pending row disappearing, and the pending row cannot disappear
without an entry being written.

`claim` returns a frozen snapshot rather than the ORM object. The row is gone
by the time the caller sees it; handing back a live instance mapped to a
deleted row is a trap that eventually loads or flushes something that is not
there.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import PendingEntry

# How long a keyboard stays live. Long enough to survive a night's sleep and a
# redeploy, short enough that a button found in yesterday's scrollback does not
# quietly book money at today's balance. A constant, not config: it is a
# product decision, and it is not a secret.
PENDING_TTL = dt.timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ClaimedPending:
    """A pending row that has just been deleted, and its contents."""

    id: int
    household_id: int
    member_id: int
    raw_input: str
    parsed_kind: str | None
    parsed_amount_minor: int | None
    parsed_category_id: int | None
    parsed_note: str | None
    parsed_tags: tuple[str, ...]
    source_account_id: int | None
    occurred_at: dt.datetime | None
    expires_at: dt.datetime

    def is_expired(self, now: dt.datetime) -> bool:
        return now >= self.expires_at


# The column list is written once and reused by the DELETE ... RETURNING, so
# the snapshot and the statement cannot drift apart.
_RETURNED = (
    PendingEntry.id,
    PendingEntry.household_id,
    PendingEntry.member_id,
    PendingEntry.raw_input,
    PendingEntry.parsed_kind,
    PendingEntry.parsed_amount_minor,
    PendingEntry.parsed_category_id,
    PendingEntry.parsed_note,
    PendingEntry.parsed_tags,
    PendingEntry.source_account_id,
    PendingEntry.occurred_at,
    PendingEntry.expires_at,
)


async def create(
    session: AsyncSession,
    *,
    household_id: int,
    member_id: int,
    raw_input: str,
    parsed_kind: str,
    parsed_amount_minor: int,
    occurred_at: dt.datetime,
    parsed_note: str | None = None,
    parsed_tags: Sequence[str] = (),
    parsed_category_id: int | None = None,
    now: dt.datetime | None = None,
) -> PendingEntry:
    """Park a parsed message until an account is chosen.

    `now` is injectable so a test can age a row without waiting a day.

    Creating a pending entry also clears this member's OWN expired ones. There
    is no scheduler in this service — long polling only, no job queue — so
    without an opportunistic sweep the table would accumulate dead rows
    forever. It only ever removes rows already past their TTL, whose buttons
    are dead in any case, and only this member's, so it can never disturb
    someone else's open keyboard.
    """
    moment = now or dt.datetime.now(dt.UTC)

    await session.execute(
        delete(PendingEntry).where(
            PendingEntry.household_id == household_id,
            PendingEntry.member_id == member_id,
            PendingEntry.expires_at <= moment,
        )
    )

    pending = PendingEntry(
        household_id=household_id,
        member_id=member_id,
        raw_input=raw_input,
        parsed_kind=parsed_kind,
        parsed_amount_minor=parsed_amount_minor,
        parsed_category_id=parsed_category_id,
        parsed_note=parsed_note,
        parsed_tags=list(parsed_tags),
        occurred_at=occurred_at,
        expires_at=moment + PENDING_TTL,
    )
    session.add(pending)
    await session.flush()
    return pending


async def get(
    session: AsyncSession, *, household_id: int, pending_id: int
) -> PendingEntry | None:
    """Read a pending row without taking it.

    For rendering the next keyboard only. Never read-then-write: that is the
    race `claim` exists to close.
    """
    return await session.scalar(
        select(PendingEntry).where(
            PendingEntry.id == pending_id,
            PendingEntry.household_id == household_id,
        )
    )


async def set_source_account(
    session: AsyncSession, *, household_id: int, pending_id: int, account_id: int
) -> bool:
    """Record a transfer's source account between the two keyboard taps.

    Returns False if the row is gone — cancelled, expired, or already claimed
    by a competing tap. The caller reports that rather than carrying on with a
    transfer whose first half no longer exists.
    """
    result = await session.execute(
        update(PendingEntry)
        .where(
            PendingEntry.id == pending_id,
            PendingEntry.household_id == household_id,
        )
        .values(source_account_id=account_id)
    )
    return result.rowcount == 1


async def claim(
    session: AsyncSession, *, household_id: int, pending_id: int
) -> ClaimedPending | None:
    """Atomically take a pending row, or return None if someone already did.

    None means: double tap, a button from a flow that was cancelled, or a
    message from before a `/void`-and-retry. All three are the same answer —
    do nothing and say so — which is why they are not distinguished here.

    An EXPIRED row is still returned, and still deleted. The caller checks
    `is_expired` and refuses. Filtering expiry into the WHERE clause instead
    would leave dead rows in the table and tell the user "already recorded",
    which is false: nothing was recorded and nothing now can be.
    """
    result = await session.execute(
        delete(PendingEntry)
        .where(
            PendingEntry.id == pending_id,
            PendingEntry.household_id == household_id,
        )
        .returning(*_RETURNED)
        .execution_options(synchronize_session=False)
    )
    row = result.first()
    if row is None:
        return None

    return ClaimedPending(
        id=row.id,
        household_id=row.household_id,
        member_id=row.member_id,
        raw_input=row.raw_input,
        parsed_kind=row.parsed_kind,
        parsed_amount_minor=row.parsed_amount_minor,
        parsed_category_id=row.parsed_category_id,
        parsed_note=row.parsed_note,
        parsed_tags=tuple(row.parsed_tags or ()),
        source_account_id=row.source_account_id,
        occurred_at=row.occurred_at,
        expires_at=row.expires_at,
    )


async def cancel(session: AsyncSession, *, household_id: int, pending_id: int) -> bool:
    """Discard a pending row. True if this call is the one that removed it."""
    return (
        await claim(session, household_id=household_id, pending_id=pending_id)
    ) is not None
