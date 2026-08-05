"""SQLAlchemy 2.x models for the eight in-scope tables.

Schema notes that are easy to get wrong and expensive to get wrong:

* Money is BIGINT centavos. `entries.amount_minor` is UNSIGNED display only;
  the signed amounts live in `entry_legs`.
* Every enumerated column is TEXT + CHECK, never a Postgres ENUM type.
* Every child table carries `household_id` AND a composite foreign key back to
  `(id, household_id)` of its parent. Without the composite key the denormalised
  household_id could drift from its parent's, which is precisely the column
  every tenant-scoped query depends on.
* The cross-row rules — leg shape, leg sum-to-zero for transfers, the
  amount cross-check, and category depth — cannot be expressed as CHECK
  constraints. They live in DEFERRABLE constraint triggers created by the
  baseline migration.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- vocabulary -------------------------------------------------------------

ENTRY_KINDS = ("income", "expense", "transfer")
# What the user ASKED FOR, which is not always what the ledger writes. A card
# settlement commits as kind='transfer' and always will; "settlement" records
# that the question on screen was "which card?", not "which account?". The two
# are different facts and a row that stores only the first cannot answer the
# second.
PENDING_INTENTS = ("expense", "income", "transfer", "settlement")
CATEGORY_KINDS = ("income", "expense")
LEG_ROLES = ("source", "destination")
MEMBER_ROLES = ("owner", "member")
ENTRY_SOURCES = ("telegram", "web")
TAG_ORIGINS = ("rule", "manual", "ai")
ACCOUNT_TYPES = ("cash", "bank", "ewallet", "credit_card", "savings", "loan")

CURRENCY = "PHP"
TIMEZONE = "Asia/Manila"


def _in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base.

    `AsyncAttrs` gives `await obj.awaitable_attrs.x` for relationships; under
    asyncio a plain lazy load raises rather than silently blocking.
    """

    type_annotation_map = {
        int: BigInteger,
        str: Text,
        dt.datetime: TIMESTAMP(timezone=True),
    }


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


# --- households -------------------------------------------------------------


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # PHP-only and Manila-only are hard invariants. The columns exist because
    # the schema specifies them; the CHECKs make the real constraint visible
    # here instead of only in prose, and make any future multi-currency or
    # multi-timezone attempt fail at the database.
    base_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=CURRENCY
    )
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=TIMEZONE)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(f"base_currency = '{CURRENCY}'", name="ck_households_currency"),
        CheckConstraint(f"timezone = '{TIMEZONE}'", name="ck_households_timezone"),
    )


# --- members ----------------------------------------------------------------


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = _pk()
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="RESTRICT"), nullable=False
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(_in("role", MEMBER_ROLES), name="ck_members_role"),
        # Target for composite FKs from entries.
        UniqueConstraint("id", "household_id", name="uq_members_id_household"),
        Index("ix_members_household", "household_id"),
    )


# --- categories -------------------------------------------------------------


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = _pk()
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    __table_args__ = (
        CheckConstraint(_in("kind", CATEGORY_KINDS), name="ck_categories_kind"),
        # A category cannot be its own parent. Deeper cycles and the max-depth-2
        # rule need the trigger; this catches the trivial case cheaply.
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name="ck_categories_not_self_parent"
        ),
        # Name uniqueness is case-insensitive and lives in a functional index
        # below, which a table-level UniqueConstraint cannot express.
        UniqueConstraint("id", "household_id", name="uq_categories_id_household"),
        # Composite: a subcategory must live in its parent's household.
        ForeignKeyConstraint(
            ["parent_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_categories_parent_household",
            ondelete="RESTRICT",
        ),
        Index("ix_categories_household", "household_id"),
    )


# Outside the class body: a functional index needs a real column expression,
# which does not exist while the class is still being constructed.
#
# Case-insensitive on `name`, but NOT on `kind` — `kind` is a fixed vocabulary
# guarded by ck_categories_kind, not user text. 'Coffee' and 'coffee' are one
# category; an expense 'Refunds' and an income 'Refunds' are two, correctly.
Index(
    "uq_categories_household_name_kind_lower",
    Category.__table__.c.household_id,
    func.lower(Category.__table__.c.name),
    Category.__table__.c.kind,
    unique=True,
)


# --- accounts ---------------------------------------------------------------


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = _pk()
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    opening_balance_minor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    credit_limit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billing_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    statement_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    payment_day: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Balance and net-worth math only. NEVER consulted by a spending summary:
    # money spent from an excluded account is still money spent.
    exclude_from_totals: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    sort_order: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )

    __table_args__ = (
        CheckConstraint(_in("type", ACCOUNT_TYPES), name="ck_accounts_type"),
        CheckConstraint(
            "credit_limit_minor IS NULL OR credit_limit_minor >= 0",
            name="ck_accounts_credit_limit_non_negative",
        ),
        CheckConstraint(
            "statement_day IS NULL OR statement_day BETWEEN 1 AND 31",
            name="ck_accounts_statement_day",
        ),
        CheckConstraint(
            "payment_day IS NULL OR payment_day BETWEEN 1 AND 31",
            name="ck_accounts_payment_day",
        ),
        CheckConstraint(
            "billing_account_id IS NULL OR billing_account_id <> id",
            name="ck_accounts_not_self_billing",
        ),
        # Name uniqueness is case-insensitive and lives in a functional index
        # below, which a table-level UniqueConstraint cannot express.
        UniqueConstraint("id", "household_id", name="uq_accounts_id_household"),
        # Composite: a card's billing account must be in the same household.
        ForeignKeyConstraint(
            ["billing_account_id", "household_id"],
            ["accounts.id", "accounts.household_id"],
            name="fk_accounts_billing_household",
            ondelete="RESTRICT",
        ),
        Index("ix_accounts_household", "household_id"),
    )


# Defined outside the class body because a functional index needs a real column
# expression, which does not exist while the class is still being constructed.
#
# Case-insensitive on purpose: 'GoTyme' and 'gotyme' are one account. Two rows
# differing only in case would each accumulate their own derived balance, and
# neither would be wrong — the household's money would simply be split in half
# with nothing to show for it.
Index(
    "uq_accounts_household_name_lower",
    Account.__table__.c.household_id,
    func.lower(Account.__table__.c.name),
    unique=True,
)


# --- entries ----------------------------------------------------------------


class Entry(Base):
    """Append-only. Corrections set `voided_at` and insert a replacement.

    Never UPDATE or DELETE a row here except to stamp `voided_at`/`voided_by`.
    """

    __tablename__ = "entries"

    id: Mapped[int] = _pk()
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="RESTRICT"), nullable=False
    )
    member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Unsigned display amount. The signed truth is in entry_legs; a trigger
    # cross-checks the two so this can never drift from the money moved.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=CURRENCY
    )
    category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # `note` is the merchant text and feeds the tagger. `description` is free
    # text the user adds later; the parser never produces one.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[dt.datetime] = _created_at()
    source: Mapped[str] = mapped_column(Text, nullable=False)
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    voided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replaces_entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # A transfer fee is its own expense entry pointing back at the transfer.
    # It is never a leg on the transfer — that would break sum-to-zero and
    # hide real spending.
    related_entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(_in("kind", ENTRY_KINDS), name="ck_entries_kind"),
        CheckConstraint(_in("source", ENTRY_SOURCES), name="ck_entries_source"),
        CheckConstraint(f"currency = '{CURRENCY}'", name="ck_entries_currency"),
        CheckConstraint("amount_minor > 0", name="ck_entries_amount_positive"),
        CheckConstraint(
            "voided_at IS NOT NULL OR voided_by IS NULL",
            name="ck_entries_voided_by_needs_voided_at",
        ),
        CheckConstraint(
            "replaces_entry_id IS NULL OR replaces_entry_id <> id",
            name="ck_entries_not_self_replacing",
        ),
        # Transfers have no category; income/expense may have one.
        CheckConstraint(
            "kind <> 'transfer' OR category_id IS NULL",
            name="ck_entries_transfer_has_no_category",
        ),
        # Target for composite FKs from entry_legs and entry_tags.
        UniqueConstraint("id", "household_id", name="uq_entries_id_household"),
        ForeignKeyConstraint(
            ["member_id", "household_id"],
            ["members.id", "members.household_id"],
            name="fk_entries_member_household",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["category_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_entries_category_household",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["replaces_entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entries_replaces_household",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["related_entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entries_related_household",
            ondelete="RESTRICT",
        ),
    )


# Defined outside the class body because the partial index needs a real column
# expression for `postgresql_where`, which is not available while the class is
# still being constructed.
#
# Period queries are always (household_id, occurred_at) and almost always
# live-only, so the partial index matches the hot path exactly.
Index(
    "ix_entries_household_occurred_live",
    Entry.__table__.c.household_id,
    Entry.__table__.c.occurred_at,
    postgresql_where=Entry.__table__.c.voided_at.is_(None),
)
Index(
    "ix_entries_household_kind_occurred",
    Entry.__table__.c.household_id,
    Entry.__table__.c.kind,
    Entry.__table__.c.occurred_at,
)


# --- entry_legs -------------------------------------------------------------


class EntryLeg(Base):
    """Signed movements. Shape is enforced by a deferred constraint trigger:

    expense  -> exactly one 'source' leg, amount < 0
    income   -> exactly one 'destination' leg, amount > 0
    transfer -> exactly one 'source' (< 0) and one 'destination' (> 0),
                summing to zero
    """

    __tablename__ = "entry_legs"

    id: Mapped[int] = _pk()
    entry_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Denormalised from the parent entry so every leg query is tenant-scoped
    # without a join. The composite FK below is what keeps it honest.
    household_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    leg_role: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(_in("leg_role", LEG_ROLES), name="ck_entry_legs_role"),
        # A zero-amount leg moves nothing and would satisfy sum-to-zero on its
        # own; reject it outright.
        CheckConstraint("amount_minor <> 0", name="ck_entry_legs_amount_nonzero"),
        # NOTE: the role/sign rule (source < 0, destination > 0) is deliberately
        # NOT a CHECK here. A CHECK fires at INSERT, but the rule must surface at
        # COMMIT alongside the rest of the leg shape — a half-built entry is
        # legal mid-transaction. It lives in the deferred trigger instead.
        ForeignKeyConstraint(
            ["entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entry_legs_entry_household",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["account_id", "household_id"],
            ["accounts.id", "accounts.household_id"],
            name="fk_entry_legs_account_household",
            ondelete="RESTRICT",
        ),
        Index("ix_entry_legs_entry", "entry_id"),
        Index("ix_entry_legs_account_household", "account_id", "household_id"),
    )


# --- entry_tags -------------------------------------------------------------


class EntryTag(Base):
    __tablename__ = "entry_tags"

    entry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    household_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(_in("origin", TAG_ORIGINS), name="ck_entry_tags_origin"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_entry_tags_confidence_range",
        ),
        CheckConstraint(
            "length(tag) BETWEEN 1 AND 32", name="ck_entry_tags_tag_length"
        ),
        ForeignKeyConstraint(
            ["entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entry_tags_entry_household",
            ondelete="CASCADE",
        ),
        Index("ix_entry_tags_household_tag", "household_id", "tag"),
    )


# --- pending_entries --------------------------------------------------------


class PendingEntry(Base):
    """A parsed message waiting for an account to be chosen.

    This is the one table in the schema that is genuinely mutable. It is NOT
    part of the ledger: nothing here has moved any money, and the row is
    deleted the instant it becomes an `Entry`. The append-only rule protects
    `entries`, where the money lives, and does not reach here.

    It exists because CLAUDE.md forbids keeping a half-finished entry in
    process memory. A restart between "100 coffee" and the account tap would
    otherwise strand the keyboard: the buttons would still be on screen and
    every one of them dead. Because the state is a row, the same tap works an
    hour and a redeploy later.

    Every `parsed_*` column is nullable. The bot only ever writes rows it has
    fully parsed, but the schema does not insist, so a future flow can ask for
    a missing piece without a migration.
    """

    __tablename__ = "pending_entries"

    id: Mapped[int] = _pk()
    household_id: Mapped[int] = mapped_column(
        ForeignKey("households.id", ondelete="RESTRICT"), nullable=False
    )
    member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_input: Mapped[str] = mapped_column(Text, nullable=False)
    # WHICH FLOW this row belongs to — the question the keyboard is asking.
    # NOT NULL alone among the columns here, because every other one describes
    # what was parsed and this one describes what was asked; a row that does not
    # know which flow it is cannot render its own next keyboard. `/pay` and
    # `/transfer` produce identical `parsed_*` columns, so without this the only
    # difference between a settlement and a plain transfer lives in buttons that
    # have already been sent.
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The LEDGER KIND this commits as. A settlement is 'transfer' here and
    # 'settlement' in `intent`; never conflate the two.
    parsed_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parsed_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The parser already found these. Storing them means the raw message is
    # parsed exactly once, so there is no second parse to disagree with the
    # first about what the user actually typed.
    parsed_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    # A transfer needs two accounts and the keyboard can only ask for one at a
    # time. This holds the first answer across the gap between the two taps.
    source_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    occurred_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = _created_at()
    expires_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            _in("intent", PENDING_INTENTS),
            name="ck_pending_entries_intent",
        ),
        CheckConstraint(
            f"parsed_kind IS NULL OR {_in('parsed_kind', ENTRY_KINDS)}",
            name="ck_pending_entries_kind",
        ),
        CheckConstraint(
            "parsed_amount_minor IS NULL OR parsed_amount_minor > 0",
            name="ck_pending_entries_amount_positive",
        ),
        ForeignKeyConstraint(
            ["member_id", "household_id"],
            ["members.id", "members.household_id"],
            name="fk_pending_entries_member_household",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["parsed_category_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_pending_entries_category_household",
            ondelete="RESTRICT",
        ),
        # Composite, like every other cross-table reference here: a pending row
        # must not be able to point at an account in another household.
        ForeignKeyConstraint(
            ["source_account_id", "household_id"],
            ["accounts.id", "accounts.household_id"],
            name="fk_pending_entries_source_account_household",
            ondelete="RESTRICT",
        ),
        Index("ix_pending_entries_expires", "expires_at"),
    )
