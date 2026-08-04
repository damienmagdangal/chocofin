"""Derived balances. There is no stored balance column and never will be.

    balance = opening_balance_minor + SUM(legs for that account)

Transfers ARE included here — the money genuinely moved. That is the opposite
of `core.ledger.summarise`, which excludes them. Two code paths, never merged.

Sign convention: assets positive, liabilities negative. A credit card you owe
₱3,000 on has a balance of -300000, so available credit is
`credit_limit_minor + balance_minor`.

`exclude_from_totals` is honoured HERE and only here — an excluded account
still reports its own balance, it is just left out of net worth. It is also the
ONLY flag that does so: `is_active` controls visibility, never money, and never
reaches `net_worth_minor`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import AccountNotFoundError
from core.models import Account, Entry, EntryLeg


@dataclass(frozen=True, slots=True)
class AccountBalance:
    account_id: int
    name: str
    type: str
    balance_minor: int
    credit_limit_minor: int | None
    exclude_from_totals: bool

    @property
    def available_credit_minor(self) -> int | None:
        """Limit plus balance. None for accounts that are not credit cards.

        The balance is negative when money is owed, so a ₱50,000 limit with
        ₱3,000 owed gives 5_000_000 + (-300_000) = 4_700_000.
        """
        if self.credit_limit_minor is None:
            return None
        return self.credit_limit_minor + self.balance_minor


def _legs_sum_subquery(household_id: int):
    """Signed leg totals per account, ignoring voided entries.

    Voided entries never moved money, so their legs must not count toward a
    balance any more than they count toward a summary.
    """
    return (
        select(
            EntryLeg.account_id.label("account_id"),
            func.sum(EntryLeg.amount_minor).label("legs_minor"),
        )
        .join(Entry, Entry.id == EntryLeg.entry_id)
        .where(
            EntryLeg.household_id == household_id,
            Entry.household_id == household_id,
            Entry.voided_at.is_(None),
        )
        .group_by(EntryLeg.account_id)
        .subquery()
    )


async def account_balances(
    session: AsyncSession,
    *,
    household_id: int,
    include_inactive: bool = False,
) -> list[AccountBalance]:
    """Every account's derived balance."""
    legs = _legs_sum_subquery(household_id)
    stmt = (
        select(
            Account,
            (Account.opening_balance_minor + func.coalesce(legs.c.legs_minor, 0)).label(
                "balance_minor"
            ),
        )
        .outerjoin(legs, legs.c.account_id == Account.id)
        .where(Account.household_id == household_id)
        .order_by(Account.sort_order, Account.id)
    )
    if not include_inactive:
        stmt = stmt.where(Account.is_active.is_(True))

    return [
        AccountBalance(
            account_id=account.id,
            name=account.name,
            type=account.type,
            balance_minor=int(balance_minor),
            credit_limit_minor=account.credit_limit_minor,
            exclude_from_totals=account.exclude_from_totals,
        )
        for account, balance_minor in (await session.execute(stmt)).all()
    ]


async def account_balance(
    session: AsyncSession, *, household_id: int, account_id: int
) -> AccountBalance:
    """One account's derived balance, active or not."""
    for balance in await account_balances(
        session, household_id=household_id, include_inactive=True
    ):
        if balance.account_id == account_id:
            return balance
    raise AccountNotFoundError(
        f"account {account_id} is not in household {household_id}"
    )


async def available_credit(
    session: AsyncSession, *, household_id: int, account_id: int
) -> int | None:
    """Remaining credit on a card, or None if the account has no limit."""
    balance = await account_balance(
        session, household_id=household_id, account_id=account_id
    )
    return balance.available_credit_minor


async def net_worth_minor(session: AsyncSession, *, household_id: int) -> int:
    """Sum of balances, skipping accounts flagged `exclude_from_totals`.

    That flag is the ONLY thing that removes an account from this total, and
    this is the only place it applies. It must never reach a spending summary:
    money spent from an excluded account is still spending.

    `is_active` deliberately has no say here, which is why this function takes
    no `include_inactive` switch to get wrong. Deactivating an account hides it
    from pickers and listings; it does not settle its balance. A closed card
    still owing PHP 3,000.00 is PHP 3,000.00 the household still owes, and
    dropping it would make net worth jump by exactly the amount of a debt that
    never went anywhere.
    """
    return sum(
        b.balance_minor
        for b in await account_balances(
            session, household_id=household_id, include_inactive=True
        )
        if not b.exclude_from_totals
    )
