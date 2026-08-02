"""Ledger operations. The only place entries are written.

Rules encoded here, each of which has a test:

* `household_id` is in every WHERE clause.
* Entries are append-only. A correction voids the original and inserts a
  replacement carrying `replaces_entry_id`, in ONE transaction.
* A replacement copies `occurred_at` from the entry it replaces. Never `now()`:
  correcting a January entry in March must leave the money in January.
* Summaries exclude transfers AND voided entries. Balance math includes
  transfers. Two code paths, never merged — see `core.balances`.
* Summaries never consult `exclude_from_totals`. Money spent from an excluded
  account is still spending; that flag is balance-only.
* No account is ever defaulted. Callers pass `account_id` explicitly.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    AccountNotFoundError,
    EntryAlreadyVoidedError,
    EntryNotFoundError,
    InvalidAmountError,
    SameAccountTransferError,
)
from core.models import Account, Category, Entry, EntryLeg, EntryTag

EntrySource = Literal["telegram", "web"]
GroupBy = Literal["parent", "leaf"]


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category_id: int | None
    total_minor: int


@dataclass(frozen=True, slots=True)
class Summary:
    income_minor: int
    expense_minor: int
    by_category: tuple[CategoryTotal, ...]

    @property
    def net_minor(self) -> int:
        return self.income_minor - self.expense_minor


def _require_positive(amount_minor: int) -> None:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise InvalidAmountError("amount_minor must be an int of centavos")
    if amount_minor <= 0:
        raise InvalidAmountError(f"amount_minor must be positive, got {amount_minor}")


async def _require_account(
    session: AsyncSession, *, household_id: int, account_id: int
) -> Account:
    account = await session.scalar(
        select(Account).where(
            Account.id == account_id, Account.household_id == household_id
        )
    )
    if account is None:
        raise AccountNotFoundError(
            f"account {account_id} is not in household {household_id}"
        )
    return account


async def _add_tags(
    session: AsyncSession,
    *,
    entry: Entry,
    tags: Sequence[str],
    origin: str = "manual",
    confidence: float | None = 1.0,
) -> None:
    seen: set[str] = set()
    for tag in tags:
        lowered = tag.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        session.add(
            EntryTag(
                entry_id=entry.id,
                household_id=entry.household_id,
                tag=lowered,
                origin=origin,
                confidence=confidence,
            )
        )


async def _create_single_leg_entry(
    session: AsyncSession,
    *,
    kind: Literal["income", "expense"],
    household_id: int,
    member_id: int,
    account_id: int,
    amount_minor: int,
    occurred_at: dt.datetime,
    category_id: int | None,
    note: str | None,
    description: str | None,
    source: EntrySource,
    raw_input: str | None,
    tags: Sequence[str],
    related_entry_id: int | None,
    replaces_entry_id: int | None = None,
) -> Entry:
    _require_positive(amount_minor)
    await _require_account(session, household_id=household_id, account_id=account_id)

    entry = Entry(
        household_id=household_id,
        member_id=member_id,
        kind=kind,
        amount_minor=amount_minor,
        category_id=category_id,
        note=note,
        description=description,
        occurred_at=occurred_at,
        source=source,
        raw_input=raw_input,
        related_entry_id=related_entry_id,
        # Set at INSERT, never by a follow-up UPDATE: `entries` is append-only.
        replaces_entry_id=replaces_entry_id,
    )
    session.add(entry)
    await session.flush()

    # Expense drains an account (negative source leg); income fills one.
    signed = -amount_minor if kind == "expense" else amount_minor
    session.add(
        EntryLeg(
            entry_id=entry.id,
            household_id=household_id,
            account_id=account_id,
            amount_minor=signed,
            leg_role="source" if kind == "expense" else "destination",
        )
    )
    await _add_tags(session, entry=entry, tags=tags)
    await session.flush()
    return entry


async def create_expense(
    session: AsyncSession,
    *,
    household_id: int,
    member_id: int,
    account_id: int,
    amount_minor: int,
    occurred_at: dt.datetime,
    category_id: int | None = None,
    note: str | None = None,
    description: str | None = None,
    source: EntrySource = "telegram",
    raw_input: str | None = None,
    tags: Sequence[str] = (),
    related_entry_id: int | None = None,
) -> Entry:
    """One negative `source` leg against `account_id`."""
    return await _create_single_leg_entry(
        session,
        kind="expense",
        household_id=household_id,
        member_id=member_id,
        account_id=account_id,
        amount_minor=amount_minor,
        occurred_at=occurred_at,
        category_id=category_id,
        note=note,
        description=description,
        source=source,
        raw_input=raw_input,
        tags=tags,
        related_entry_id=related_entry_id,
    )


async def create_income(
    session: AsyncSession,
    *,
    household_id: int,
    member_id: int,
    account_id: int,
    amount_minor: int,
    occurred_at: dt.datetime,
    category_id: int | None = None,
    note: str | None = None,
    description: str | None = None,
    source: EntrySource = "telegram",
    raw_input: str | None = None,
    tags: Sequence[str] = (),
) -> Entry:
    """One positive `destination` leg into `account_id`."""
    return await _create_single_leg_entry(
        session,
        kind="income",
        household_id=household_id,
        member_id=member_id,
        account_id=account_id,
        amount_minor=amount_minor,
        occurred_at=occurred_at,
        category_id=category_id,
        note=note,
        description=description,
        source=source,
        raw_input=raw_input,
        tags=tags,
        related_entry_id=None,
    )


async def create_transfer(
    session: AsyncSession,
    *,
    household_id: int,
    member_id: int,
    source_account_id: int,
    destination_account_id: int,
    amount_minor: int,
    occurred_at: dt.datetime,
    note: str | None = None,
    description: str | None = None,
    source: EntrySource = "telegram",
    raw_input: str | None = None,
    fee_minor: int | None = None,
    fee_account_id: int | None = None,
    fee_category_id: int | None = None,
) -> Entry:
    """Two legs summing to zero. Never an expense, in any period or view.

    A card settlement is exactly this: a transfer from the billing account to
    the card account.

    `fee_minor`, if given, becomes a SEPARATE one-leg expense entry pointing
    back at this transfer via `related_entry_id`. It is never a third leg — a
    fee leg would break sum-to-zero and would hide real spending from totals.
    """
    _require_positive(amount_minor)
    if source_account_id == destination_account_id:
        raise SameAccountTransferError(
            f"transfer source and destination are both account {source_account_id}"
        )
    await _require_account(
        session, household_id=household_id, account_id=source_account_id
    )
    await _require_account(
        session, household_id=household_id, account_id=destination_account_id
    )

    entry = Entry(
        household_id=household_id,
        member_id=member_id,
        kind="transfer",
        amount_minor=amount_minor,
        category_id=None,
        note=note,
        description=description,
        occurred_at=occurred_at,
        source=source,
        raw_input=raw_input,
    )
    session.add(entry)
    await session.flush()

    session.add_all(
        [
            EntryLeg(
                entry_id=entry.id,
                household_id=household_id,
                account_id=source_account_id,
                amount_minor=-amount_minor,
                leg_role="source",
            ),
            EntryLeg(
                entry_id=entry.id,
                household_id=household_id,
                account_id=destination_account_id,
                amount_minor=amount_minor,
                leg_role="destination",
            ),
        ]
    )
    await session.flush()

    if fee_minor:
        await create_expense(
            session,
            household_id=household_id,
            member_id=member_id,
            account_id=fee_account_id or source_account_id,
            amount_minor=fee_minor,
            occurred_at=occurred_at,
            category_id=fee_category_id,
            note=f"Fee: {note}" if note else "Transfer fee",
            source=source,
            related_entry_id=entry.id,
        )

    return entry


async def get_entry(
    session: AsyncSession, *, household_id: int, entry_id: int
) -> Entry:
    entry = await session.scalar(
        select(Entry).where(Entry.id == entry_id, Entry.household_id == household_id)
    )
    if entry is None:
        raise EntryNotFoundError(f"entry {entry_id} is not in household {household_id}")
    return entry


async def void_entry(
    session: AsyncSession,
    *,
    household_id: int,
    entry_id: int,
    voided_by: int | None = None,
    voided_at: dt.datetime | None = None,
) -> Entry:
    """Mark an entry void. The row stays readable forever.

    Stamping `voided_at` is the ONLY mutation permitted on `entries`.
    """
    entry = await get_entry(session, household_id=household_id, entry_id=entry_id)
    if entry.voided_at is not None:
        raise EntryAlreadyVoidedError(f"entry {entry_id} is already voided")
    entry.voided_at = voided_at or dt.datetime.now(dt.UTC)
    entry.voided_by = voided_by
    await session.flush()
    return entry


async def reassign_account(
    session: AsyncSession,
    *,
    household_id: int,
    entry_id: int,
    account_id: int,
    voided_by: int | None = None,
    voided_at: dt.datetime | None = None,
) -> Entry:
    """Move an entry to a different account: void it, insert a replacement.

    ONE transaction, no UPDATE of the original beyond `voided_at`.

    The replacement copies `occurred_at` from the original. Using `now()` would
    move a corrected January expense into March — and would still pass a naive
    "exactly one live entry" test while doing it.
    """
    original = await get_entry(session, household_id=household_id, entry_id=entry_id)
    if original.voided_at is not None:
        raise EntryAlreadyVoidedError(f"entry {entry_id} is already voided")
    if original.kind == "transfer":
        raise InvalidAmountError(
            "reassign_account handles single-leg entries; a transfer has two "
            "accounts and needs an explicit source/destination correction"
        )
    await _require_account(session, household_id=household_id, account_id=account_id)

    tags = list(
        await session.scalars(
            select(EntryTag.tag).where(
                EntryTag.entry_id == original.id,
                EntryTag.household_id == household_id,
            )
        )
    )

    await void_entry(
        session,
        household_id=household_id,
        entry_id=entry_id,
        voided_by=voided_by,
        voided_at=voided_at,
    )

    replacement = await _create_single_leg_entry(
        session,
        kind=original.kind,  # type: ignore[arg-type]
        household_id=household_id,
        member_id=original.member_id,
        account_id=account_id,
        amount_minor=original.amount_minor,
        occurred_at=original.occurred_at,  # NEVER now()
        category_id=original.category_id,
        note=original.note,
        description=original.description,
        source=original.source,  # type: ignore[arg-type]
        raw_input=original.raw_input,
        tags=tags,
        related_entry_id=original.related_entry_id,
        replaces_entry_id=original.id,
    )
    return replacement


def _live(stmt: Select, include_voided: bool) -> Select:
    return stmt if include_voided else stmt.where(Entry.voided_at.is_(None))


async def list_entries(
    session: AsyncSession,
    *,
    household_id: int,
    start_utc: dt.datetime | None = None,
    end_utc: dt.datetime | None = None,
    kinds: Sequence[str] | None = None,
    account_id: int | None = None,
    include_voided: bool = False,
    limit: int | None = None,
    offset: int | None = None,
) -> Sequence[Entry]:
    """Live entries by default. Voided rows are readable on request.

    `[start_utc, end_utc)` is half-open — the caller gets these from
    `core.periods.resolve`, which already resolved them in Manila.
    """
    stmt = select(Entry).where(Entry.household_id == household_id)
    stmt = _live(stmt, include_voided)

    if start_utc is not None:
        stmt = stmt.where(Entry.occurred_at >= start_utc)
    if end_utc is not None:
        stmt = stmt.where(Entry.occurred_at < end_utc)
    if kinds:
        stmt = stmt.where(Entry.kind.in_(kinds))
    if account_id is not None:
        stmt = stmt.where(
            Entry.id.in_(
                select(EntryLeg.entry_id).where(
                    EntryLeg.account_id == account_id,
                    EntryLeg.household_id == household_id,
                )
            )
        )

    stmt = stmt.order_by(Entry.occurred_at, Entry.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return list(await session.scalars(stmt))


async def summarise(
    session: AsyncSession,
    *,
    household_id: int,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    group_by: GroupBy = "parent",
) -> Summary:
    """Income and expense totals for a half-open period.

    Transfers are excluded — always, in every period and every view. So are
    voided entries. `exclude_from_totals` is NOT consulted: money spent from an
    excluded account is still spending.

    `group_by="parent"` rolls subcategory totals into their parent;
    `"leaf"` reports each subcategory separately. Entries with no category are
    reported under `category_id=None` rather than dropped, so the breakdown
    always sums to the period total.
    """
    base = (
        select(Entry.kind, func.sum(Entry.amount_minor))
        .where(
            Entry.household_id == household_id,
            Entry.voided_at.is_(None),
            Entry.kind != "transfer",  # never in an income/expense total
            Entry.occurred_at >= start_utc,
            Entry.occurred_at < end_utc,
        )
        .group_by(Entry.kind)
    )
    totals = {kind: int(total) for kind, total in (await session.execute(base)).all()}

    if group_by == "parent":
        # COALESCE(parent_id, id) folds a child into its parent and leaves a
        # top-level category as itself.
        bucket = func.coalesce(Category.parent_id, Category.id)
    else:
        bucket = Category.id

    by_category_stmt = (
        select(bucket.label("bucket"), func.sum(Entry.amount_minor))
        .select_from(Entry)
        .outerjoin(
            Category,
            (Category.id == Entry.category_id)
            & (Category.household_id == Entry.household_id),
        )
        .where(
            Entry.household_id == household_id,
            Entry.voided_at.is_(None),
            Entry.kind == "expense",
            Entry.occurred_at >= start_utc,
            Entry.occurred_at < end_utc,
        )
        .group_by("bucket")
        .order_by("bucket")
    )
    by_category = tuple(
        CategoryTotal(
            category_id=int(bucket) if bucket is not None else None,
            total_minor=int(total),
        )
        for bucket, total in (await session.execute(by_category_stmt)).all()
    )

    return Summary(
        income_minor=totals.get("income", 0),
        expense_minor=totals.get("expense", 0),
        by_category=by_category,
    )
