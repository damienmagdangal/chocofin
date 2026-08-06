"""Ledger operations. The only place entries are written.

Rules encoded here, each of which has a test:

* `household_id` is in every WHERE clause.
* Entries are append-only. A correction voids the original and inserts a
  replacement carrying `replaces_entry_id`, in ONE transaction.
* A replacement copies `occurred_at` from the entry it replaces. Never `now()`:
  correcting a January entry in March must leave the money in January. It also
  carries each tag's `origin` and `confidence` across unchanged — a correction
  does not re-decide who tagged the entry.
* Summaries exclude transfers AND voided entries. Balance math includes
  transfers. Two code paths, never merged — see `core.balances`.
* Summaries never consult `exclude_from_totals`. Money spent from an excluded
  account is still spending; that flag is balance-only.
* No account is ever defaulted. Callers pass `account_id` explicitly.
* Every instant this module stores — `occurred_at`, and an explicitly supplied
  `voided_at` — must be timezone-aware. The write boundaries call
  `core.periods.require_aware` before touching anything, via `_require_when`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.errors import (
    AccountNotFoundError,
    CardHasNoBillingAccountError,
    EntryAlreadyVoidedError,
    EntryNotFoundError,
    InvalidAmountError,
    NotACreditCardError,
    SameAccountTransferError,
)
from core.models import Account, Category, Entry, EntryLeg, EntryTag
from core.periods import require_aware

EntrySource = Literal["telegram", "web"]
GroupBy = Literal["parent", "leaf"]

# Which clock a listing is sorted by. Two different questions: `occurred_at` is
# when the money moved, `created_at` is when the entry was LOGGED. See
# `list_entries`.
OrderBy = Literal["occurred_at", "created_at"]


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category_id: int | None
    total_minor: int


@dataclass(frozen=True, slots=True)
class TagSpec:
    """A tag together with its provenance.

    `origin` says who decided this tag — a rule, a human, or the tagger — and
    `confidence` how sure they were. Both must survive a correction unchanged:
    re-stamping an AI guess as 'manual' claims a human confirmed a tag no human
    ever saw, which quietly poisons any later measurement of how good the
    tagger is and any rule-learning that keys off `origin='manual'`.

    `confidence` is a Numeric(4,3) score, NOT money. It comes back from the
    database as a Decimal and is carried straight through rather than being
    routed via float, so a round trip cannot perturb it — which is why the
    default is a Decimal too. A float default would be the one value in the
    column that this class itself put through binary floating point, in a
    docstring that says it never does. A caller may still pass a float and is
    accepted as given; that is the caller's number, not this class's.
    """

    tag: str
    origin: str = "manual"
    confidence: Decimal | float | None = Decimal("1.000")


@dataclass(frozen=True, slots=True)
class Summary:
    """Totals for a period, with the two sides kept apart.

    `by_category` breaks down EXPENSE only; `by_income_category` breaks down
    INCOME only. Both hold unsigned totals. They are deliberately not merged
    into one signed list: in a shared bucket, income posted against a category
    would cancel spending in that same category, and a month with a ₱4,500.00
    salary and ₱4,500.00 of groceries would report as an empty one.
    """

    income_minor: int
    expense_minor: int
    by_category: tuple[CategoryTotal, ...]
    by_income_category: tuple[CategoryTotal, ...]

    @property
    def net_minor(self) -> int:
        return self.income_minor - self.expense_minor


def _require_positive(amount_minor: int) -> None:
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        raise InvalidAmountError("amount_minor must be an int of centavos")
    if amount_minor <= 0:
        raise InvalidAmountError(f"amount_minor must be positive, got {amount_minor}")


def _require_when(moment: dt.datetime) -> None:
    """Refuse a naive instant before anything is written.

    Called on `occurred_at` at every entry point, and on an explicitly supplied
    `voided_at`. The first is the one that moves money into the wrong period;
    the second only misdates the correction, but both are stored, and there is
    no reason for this module to hold two standards for what a datetime is.

    This is the write boundary. `core.periods` rejects a naive instant on the
    way IN to a period calculation, and `bot.formatting` on the way OUT to a
    screen, but a caller that builds an `occurred_at` some other way reached the
    ledger unchecked — and this is the column every period, every monthly total
    and every budget window is cut on. `datetime.now()` without a tz, or a value
    parsed from a config file, lands the entry up to eight hours early: an
    expense logged just after Manila midnight is filed on the previous DAY, and
    at a month boundary in the previous MONTH, where no total will ever show it
    to the person looking for it.

    A shared helper rather than a check per function, so that adding a fifth way
    to write an entry cannot quietly skip it — `PeriodError` and its wording come
    from `core.periods`, the one place that owns what "naive" means here.
    """
    require_aware(moment)


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
    session: AsyncSession, *, entry: Entry, tags: Sequence[str | TagSpec]
) -> None:
    """Attach tags, keeping each one's provenance.

    A bare string is a tag someone typed, so it defaults to manual at full
    confidence. A `TagSpec` carries its own origin and confidence and is
    written exactly as given — that is what lets a correction preserve the
    history of a tag it did not author.
    """
    seen: set[str] = set()
    for item in tags:
        spec = TagSpec(item) if isinstance(item, str) else item
        lowered = spec.tag.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        session.add(
            EntryTag(
                entry_id=entry.id,
                household_id=entry.household_id,
                tag=lowered,
                origin=spec.origin,
                confidence=spec.confidence,
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
    tags: Sequence[str | TagSpec],
    related_entry_id: int | None,
    replaces_entry_id: int | None = None,
) -> Entry:
    _require_positive(amount_minor)
    _require_when(occurred_at)
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
    tags: Sequence[str | TagSpec] = (),
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
    tags: Sequence[str | TagSpec] = (),
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
    tags: Sequence[str | TagSpec] = (),
    fee_minor: int | None = None,
    fee_account_id: int | None = None,
    fee_category_id: int | None = None,
) -> Entry:
    """Two legs summing to zero. Never an expense, in any period or view.

    A card settlement is exactly this: a transfer from the billing account to
    the card account.

    `tags` attach to the transfer exactly as they do for an expense or an
    income — same de-duplication, same lowercasing, same provenance. A transfer
    carries no category (`ck_entries_transfer_has_no_category`), so a tag is
    the ONLY label it can hold; dropping tags here would make "gcash top-up"
    unsearchable while the identical words on an expense stayed findable.

    They do NOT propagate to the fee entry below. The fee is its own expense,
    and copying the transfer's tags onto it would double every tag total that
    counts entries.

    `fee_minor`, if given, becomes a SEPARATE one-leg expense entry pointing
    back at this transfer via `related_entry_id`. It is never a third leg — a
    fee leg would break sum-to-zero and would hide real spending from totals.
    """
    _require_positive(amount_minor)
    _require_when(occurred_at)
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
    await _add_tags(session, entry=entry, tags=tags)
    await session.flush()

    if fee_minor:
        await create_expense(
            session,
            household_id=household_id,
            member_id=member_id,
            # `is not None`, never `or` — the same rule `settle_card` spells out
            # below. `or` reads an id of 0 as "no fee account given" and books
            # the fee to the transfer's source instead of failing on an account
            # that does not exist.
            account_id=(
                fee_account_id if fee_account_id is not None else source_account_id
            ),
            amount_minor=fee_minor,
            occurred_at=occurred_at,
            category_id=fee_category_id,
            note=f"Fee: {note}" if note else "Transfer fee",
            source=source,
            related_entry_id=entry.id,
        )

    return entry


async def settle_card(
    session: AsyncSession,
    *,
    household_id: int,
    member_id: int,
    card_id: int,
    amount_minor: int,
    occurred_at: dt.datetime,
    source_account_id: int | None = None,
    note: str | None = None,
    description: str | None = None,
    source: EntrySource = "telegram",
    raw_input: str | None = None,
    tags: Sequence[str | TagSpec] = (),
) -> Entry:
    """Pay down a credit card. A TRANSFER, never an expense.

    The purchases were the spending; they were expensed when they happened.
    Booking the settlement as an expense too would count the same money twice
    and turn every month you pay a card into a month you overspent. So this
    moves money from the paying account into the card, and `summarise` never
    sees it.

    Which account pays is resolved here, not by the caller: "a settlement comes
    from the card's billing account" is a domain rule, and `bot/` and `api/`
    are adapters that must not know it. An explicit `source_account_id` wins,
    for the month you settle from somewhere else; otherwise the card's
    `billing_account_id` is used; if there is neither, this raises rather than
    picking an account, because inventing where the money came from is a lie
    about real money.

    `tags` are forwarded to the transfer for the reason given on
    `create_transfer`: a transfer carries no category, so a tag is the only
    label it can hold. Dropping them here would mean `/pay 3000 #visa` parses a
    tag, stores it, and loses it at the last step — the tag would exist for
    every account except the one the user actually names when settling.

    The card's statement cycle — closing dates, minimum due, what is even in
    this month's bill — is deliberately not modelled here.
    """
    card = await _require_account(
        session, household_id=household_id, account_id=card_id
    )
    if card.type != "credit_card":
        raise NotACreditCardError(
            f"account {card_id} is a {card.type!r}, not a credit card"
        )

    # `is not None`, never a truth test: account ids are integers, and `or`
    # would treat an id of 0 as "no source given" and silently bill the card's
    # billing account instead of the account the caller named. Falling through
    # to `_require_account` and being told account 0 does not exist is the
    # right failure; quietly moving real money out of a different account is not.
    paying_account_id = (
        source_account_id if source_account_id is not None else card.billing_account_id
    )
    if paying_account_id is None:
        raise CardHasNoBillingAccountError(
            f"card {card_id} has no billing account and no source was given"
        )

    return await create_transfer(
        session,
        household_id=household_id,
        member_id=member_id,
        source_account_id=paying_account_id,
        destination_account_id=card_id,
        amount_minor=amount_minor,
        occurred_at=occurred_at,
        note=note,
        description=description,
        source=source,
        raw_input=raw_input,
        tags=tags,
    )


async def get_entry(
    session: AsyncSession, *, household_id: int, entry_id: int
) -> Entry:
    entry = await session.scalar(
        select(Entry).where(Entry.id == entry_id, Entry.household_id == household_id)
    )
    if entry is None:
        raise EntryNotFoundError(f"entry {entry_id} is not in household {household_id}")
    return entry


async def list_legs(
    session: AsyncSession, *, household_id: int, entry_id: int
) -> Sequence[EntryLeg]:
    """The signed movements an entry actually wrote.

    For reading back what happened rather than re-deriving it. A caller that
    wants to name a transfer's two accounts should ask the legs, not re-apply
    the rule that chose them — otherwise the display and the ledger are two
    implementations of the same decision, free to disagree.

    Source first, destination second — money order, the direction a transfer is
    read and rendered in ("Savings → Visa"). Ordering by `leg_role` itself
    sorted the words rather than the movement, so 'destination' came back first
    and a caller that indexed `legs[0]` for the payer got the payee. Today's
    callers all key by role and could not see it; the ordering is stated here
    so the next one cannot be caught by it.
    """
    return list(
        await session.scalars(
            select(EntryLeg)
            .where(
                EntryLeg.entry_id == entry_id,
                EntryLeg.household_id == household_id,
            )
            .order_by(
                case({"source": 0, "destination": 1}, value=EntryLeg.leg_role, else_=2),
                EntryLeg.id,
            )
        )
    )


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

    An explicit `voided_at` must be aware, like every other instant this module
    stores. It moves no money and cuts no period — which is why it is the last
    datetime here to get the guard — but it is what says WHEN a correction was
    made, and an eight-hour drift is enough to reorder a void against the
    replacement that followed it. The default is `now(UTC)` and is aware
    already; only a caller supplying its own can get this wrong, and a caller
    supplying its own is a caller reconstructing history.
    """
    if voided_at is not None:
        _require_when(voided_at)
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

    # Carry origin and confidence across, not just the tag text. A correction
    # moves an entry between accounts; it does not re-decide who tagged it or
    # how sure they were.
    tags = [
        TagSpec(tag=tag, origin=origin, confidence=confidence)
        for tag, origin, confidence in (
            await session.execute(
                select(EntryTag.tag, EntryTag.origin, EntryTag.confidence).where(
                    EntryTag.entry_id == original.id,
                    EntryTag.household_id == household_id,
                )
            )
        ).all()
    ]

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
    order_by: OrderBy = "occurred_at",
    newest_first: bool = False,
    limit: int | None = None,
    offset: int | None = None,
) -> Sequence[Entry]:
    """Live entries by default. Voided rows are readable on request.

    `[start_utc, end_utc)` is half-open — the caller gets these from
    `core.periods.resolve`, which already resolved them in Manila.

    `newest_first` reverses the ordering, which is the only way to ask for the
    most recent N: with the default ascending order, `limit=5` returns the five
    OLDEST entries in range, and "show me what I just logged" cannot be
    expressed at all. `id` breaks ties in the same direction as the sort
    column, so two entries sharing a timestamp — everything dated `@yesterday`
    lands on Manila midnight — come back in the order they were written rather
    than arbitrarily.

    `order_by` chooses WHICH clock is sorted on, and the two answer different
    questions:

    * `occurred_at`, the default, is ledger order — when the money moved. Every
      summary, statement and period view wants this, so it does not change.
    * `created_at` is when the entry was LOGGED. This is the ordering
      `core.accounts` documents for the MRU keyboard, for the same reason:
      backfilling last month's receipt must not reorder what you did most
      recently. It is also the ONLY ordering under which a backdated entry is
      reachable — "undo the thing I just typed" and "the last ten things I
      typed" are questions about the typing, and under `occurred_at` a
      backdated entry sinks below newer money and can fall off a short list
      entirely, leaving no way to find its id and so no way to correct it.

    `id` matters more than usual under `created_at`: it defaults to `now()`,
    which in Postgres is transaction start time, so every entry written in one
    transaction shares a `created_at` to the microsecond. The tiebreak is what
    makes "the one I logged last" mean the row actually written last.

    `start_utc`/`end_utc` filter on `occurred_at` whichever ordering is asked
    for. A period is a question about when the money moved, never about when it
    was typed; a January expense logged in March belongs to January in every
    total, and sorting it by the typing must not move it.
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

    column = Entry.created_at if order_by == "created_at" else Entry.occurred_at
    if newest_first:
        stmt = stmt.order_by(column.desc(), Entry.id.desc())
    else:
        stmt = stmt.order_by(column, Entry.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset is not None:
        stmt = stmt.offset(offset)
    return list(await session.scalars(stmt))


async def _category_totals(
    session: AsyncSession,
    *,
    household_id: int,
    kind: Literal["income", "expense"],
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    group_by: GroupBy,
) -> tuple[CategoryTotal, ...]:
    """Per-category totals for ONE kind over a half-open period.

    Run once for expense and once for income, never together — see `Summary`
    for why the two breakdowns stay separate. Transfers cannot reach here:
    `kind` is only ever 'income' or 'expense', and a transfer carries no
    category at all (`ck_entries_transfer_has_no_category`).
    """
    if group_by == "parent":
        # COALESCE(parent_id, id) folds a child into its parent and leaves a
        # top-level category as itself.
        bucket = func.coalesce(Category.parent_id, Category.id)
    else:
        bucket = Category.id

    stmt = (
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
            Entry.kind == kind,
            Entry.occurred_at >= start_utc,
            Entry.occurred_at < end_utc,
        )
        .group_by("bucket")
        .order_by("bucket")
    )
    return tuple(
        CategoryTotal(
            category_id=int(bucket) if bucket is not None else None,
            total_minor=int(total),
        )
        for bucket, total in (await session.execute(stmt)).all()
    )


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
    reported under `category_id=None` rather than dropped, so each breakdown
    always sums to its own side of the period total: `by_category` to
    `expense_minor`, `by_income_category` to `income_minor`.
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

    return Summary(
        income_minor=totals.get("income", 0),
        expense_minor=totals.get("expense", 0),
        by_category=await _category_totals(
            session,
            household_id=household_id,
            kind="expense",
            start_utc=start_utc,
            end_utc=end_utc,
            group_by=group_by,
        ),
        by_income_category=await _category_totals(
            session,
            household_id=household_id,
            kind="income",
            start_utc=start_utc,
            end_utc=end_utc,
            group_by=group_by,
        ),
    )
