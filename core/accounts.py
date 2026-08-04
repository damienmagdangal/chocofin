"""Choosing an account, as opposed to computing what is in one.

The inline keyboard can only show three buttons before it stops being faster
than typing, so it has to guess well. The guess is most-recently-used, per
member: the account you last spent from is overwhelmingly the account you are
about to spend from again, and two people in one household have different
answers — one lives out of a wallet, the other out of a card.

Recency is measured by `entries.created_at`, when the entry was LOGGED, not
`occurred_at`, when the money moved. Backfilling last month's receipts should
not reorder the buttons for tomorrow, and someone who logs a forgotten January
expense today has not changed which account they habitually use.

This is deliberately not in `core/balances.py`. That module is scoped to
derived balances and says so; which account to offer someone is not balance
math, and the two would only ever be confused for each other.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from sqlalchemy import func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Account, Entry, EntryLeg


async def get_account(
    session: AsyncSession, *, household_id: int, account_id: int
) -> Account | None:
    """One account, scoped to its household.

    Adapters need this to put a name on a confirmation message. It returns None
    rather than raising because "the account vanished between the keyboard and
    the tap" is a message to render, not an exception to handle.
    """
    return await session.scalar(
        select(Account).where(
            Account.id == account_id, Account.household_id == household_id
        )
    )


def _mru_subquery(*, household_id: int, member_id: int):
    """Each account's last use by this member, as a timestamp.

    Voided entries are excluded. A void says the entry should never have
    existed, and an account you touched only by mistake has no claim on a
    button.
    """
    return (
        select(
            EntryLeg.account_id.label("account_id"),
            func.max(Entry.created_at).label("last_used"),
        )
        .join(Entry, Entry.id == EntryLeg.entry_id)
        .where(
            EntryLeg.household_id == household_id,
            Entry.household_id == household_id,
            Entry.member_id == member_id,
            Entry.voided_at.is_(None),
        )
        .group_by(EntryLeg.account_id)
        .subquery()
    )


async def recent_accounts(
    session: AsyncSession,
    *,
    household_id: int,
    member_id: int,
    limit: int | None = None,
    types: Collection[str] | None = None,
    exclude: Collection[int] = (),
) -> Sequence[Account]:
    """Active accounts, most-recently-used by `member_id` first.

    Accounts the member has never touched still appear, after the used ones, in
    `sort_order`. That is what makes this safe to use as the ONLY account
    listing: a brand-new household with no entries at all gets a sensible
    keyboard rather than an empty one, and `limit=3` plus a full list behind
    `[Other…]` are the same query asked twice.

    `types` narrows to particular `accounts.type` values — `/pay` uses it to
    offer credit cards only. `exclude` drops specific ids, which is how a
    transfer's destination keyboard avoids offering the source account back.

    `exclude_from_totals` is NOT consulted. That flag is balance-and-net-worth
    only; an account left out of net worth is still an account you can spend
    from, and hiding it here would make money spent from it unloggable.
    """
    mru = _mru_subquery(household_id=household_id, member_id=member_id)

    stmt = (
        select(Account)
        .outerjoin(mru, mru.c.account_id == Account.id)
        .where(Account.household_id == household_id, Account.is_active.is_(True))
        # DESC puts NULLs first in Postgres, which would float every
        # never-used account above the ones actually in use.
        .order_by(nulls_last(mru.c.last_used.desc()), Account.sort_order, Account.id)
    )
    if types:
        stmt = stmt.where(Account.type.in_(tuple(types)))
    if exclude:
        stmt = stmt.where(Account.id.notin_(tuple(exclude)))
    if limit is not None:
        stmt = stmt.limit(limit)

    return list(await session.scalars(stmt))
