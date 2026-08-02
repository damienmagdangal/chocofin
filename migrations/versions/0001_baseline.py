"""Baseline schema.

Creates all nine tables and the two DEFERRABLE constraint triggers.

The triggers exist because Postgres CHECK constraints cannot span rows. Three
rules need to see an entry's legs together, at COMMIT, not row by row:

  * leg shape and count per entry kind
  * sum-to-zero, for transfers only
  * `entries.amount_minor` matching the money the legs actually move

DEFERRABLE INITIALLY DEFERRED is load-bearing, not decoration: a transfer is
built one leg at a time, so it is legitimately unbalanced mid-transaction. An
immediate trigger would reject the first leg of every transfer ever written.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CURRENCY = "PHP"
TIMEZONE = "Asia/Manila"


VALIDATE_ENTRY_LEGS = """
CREATE FUNCTION chocofin_validate_entry_legs() RETURNS TRIGGER AS $$
DECLARE
    v_entry_id      BIGINT;
    v_kind          TEXT;
    v_amount        BIGINT;
    v_leg_count     INT;
    v_sum           BIGINT;
    v_source_count  INT;
    v_dest_count    INT;
    v_source_amount BIGINT;
    v_dest_amount   BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'entries' THEN
        v_entry_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        v_entry_id := OLD.entry_id;
    ELSE
        v_entry_id := NEW.entry_id;
    END IF;

    SELECT kind, amount_minor INTO v_kind, v_amount
      FROM entries WHERE id = v_entry_id;

    -- The entry itself is gone (legs cascade). Nothing left to validate.
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT count(*),
           COALESCE(sum(amount_minor), 0),
           count(*) FILTER (WHERE leg_role = 'source'),
           count(*) FILTER (WHERE leg_role = 'destination'),
           COALESCE(sum(amount_minor) FILTER (WHERE leg_role = 'source'), 0),
           COALESCE(sum(amount_minor) FILTER (WHERE leg_role = 'destination'), 0)
      INTO v_leg_count, v_sum, v_source_count, v_dest_count,
           v_source_amount, v_dest_amount
      FROM entry_legs WHERE entry_id = v_entry_id;

    IF v_leg_count = 0 THEN
        RAISE EXCEPTION 'entry % (%) has no legs', v_entry_id, v_kind;
    END IF;

    IF v_kind = 'expense' THEN
        IF v_leg_count <> 1 OR v_source_count <> 1 THEN
            RAISE EXCEPTION
                'expense entry % needs exactly 1 source leg (has % legs, % source)',
                v_entry_id, v_leg_count, v_source_count;
        END IF;
        IF v_source_amount >= 0 THEN
            RAISE EXCEPTION
                'expense entry % source leg must be negative, is %',
                v_entry_id, v_source_amount;
        END IF;
        IF v_amount <> abs(v_source_amount) THEN
            RAISE EXCEPTION
                'expense entry % declares amount_minor % but its leg moves %',
                v_entry_id, v_amount, abs(v_source_amount);
        END IF;

    ELSIF v_kind = 'income' THEN
        IF v_leg_count <> 1 OR v_dest_count <> 1 THEN
            RAISE EXCEPTION
                'income entry % needs exactly 1 destination leg (has % legs, % dest)',
                v_entry_id, v_leg_count, v_dest_count;
        END IF;
        IF v_dest_amount <= 0 THEN
            RAISE EXCEPTION
                'income entry % destination leg must be positive, is %',
                v_entry_id, v_dest_amount;
        END IF;
        IF v_amount <> v_dest_amount THEN
            RAISE EXCEPTION
                'income entry % declares amount_minor % but its leg moves %',
                v_entry_id, v_amount, v_dest_amount;
        END IF;

    ELSIF v_kind = 'transfer' THEN
        IF v_leg_count <> 2 OR v_source_count <> 1 OR v_dest_count <> 1 THEN
            RAISE EXCEPTION
                'transfer entry % needs exactly 1 source + 1 destination leg '
                '(has % legs, % source, % dest)',
                v_entry_id, v_leg_count, v_source_count, v_dest_count;
        END IF;
        -- Roles must match signs. Without this a transfer of source +3000 and
        -- destination -3000 would sum to zero and slip through.
        IF v_source_amount >= 0 OR v_dest_amount <= 0 THEN
            RAISE EXCEPTION
                'transfer entry % has inverted legs: source %, destination %',
                v_entry_id, v_source_amount, v_dest_amount;
        END IF;
        IF v_sum <> 0 THEN
            RAISE EXCEPTION
                'transfer entry % legs sum to %, not zero', v_entry_id, v_sum;
        END IF;
        IF v_amount <> v_dest_amount THEN
            RAISE EXCEPTION
                'transfer entry % declares amount_minor % but its legs move %',
                v_entry_id, v_amount, v_dest_amount;
        END IF;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


VALIDATE_CATEGORY_DEPTH = """
CREATE FUNCTION chocofin_validate_category_depth() RETURNS TRIGGER AS $$
DECLARE
    v_parent_parent BIGINT;
    v_parent_kind   TEXT;
    v_child_count   INT;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT parent_id, kind INTO v_parent_parent, v_parent_kind
      FROM categories WHERE id = NEW.parent_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'category % references missing parent %',
            NEW.id, NEW.parent_id;
    END IF;

    IF v_parent_parent IS NOT NULL THEN
        RAISE EXCEPTION
            'category % would sit three levels deep; max depth is 2', NEW.id;
    END IF;

    IF v_parent_kind <> NEW.kind THEN
        RAISE EXCEPTION
            'category % is %, but its parent % is %',
            NEW.id, NEW.kind, NEW.parent_id, v_parent_kind;
    END IF;

    -- A category with children cannot itself become a child.
    SELECT count(*) INTO v_child_count
      FROM categories WHERE parent_id = NEW.id;
    IF v_child_count > 0 THEN
        RAISE EXCEPTION
            'category % has % children and cannot also have a parent',
            NEW.id, v_child_count;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.create_table(
        "households",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "base_currency", sa.String(3), nullable=False, server_default=CURRENCY
        ),
        sa.Column("timezone", sa.Text(), nullable=False, server_default=TIMEZONE),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"base_currency = '{CURRENCY}'", name="ck_households_currency"
        ),
        sa.CheckConstraint(f"timezone = '{TIMEZONE}'", name="ck_households_timezone"),
    )

    op.create_table(
        "members",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_members_role"),
        sa.UniqueConstraint("id", "household_id", name="uq_members_id_household"),
    )
    op.create_index("ix_members_household", "members", ["household_id"])

    op.create_table(
        "categories",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("kind IN ('income', 'expense')", name="ck_categories_kind"),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name="ck_categories_not_self_parent",
        ),
        sa.UniqueConstraint(
            "household_id", "name", "kind", name="uq_categories_household_name_kind"
        ),
        sa.UniqueConstraint("id", "household_id", name="uq_categories_id_household"),
        sa.ForeignKeyConstraint(
            ["parent_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_categories_parent_household",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_categories_household", "categories", ["household_id"])

    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "opening_balance_minor",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("credit_limit_minor", sa.BigInteger(), nullable=True),
        sa.Column("billing_account_id", sa.BigInteger(), nullable=True),
        sa.Column("statement_day", sa.SmallInteger(), nullable=True),
        sa.Column("payment_day", sa.SmallInteger(), nullable=True),
        sa.Column(
            "exclude_from_totals",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "type IN ('cash', 'bank', 'ewallet', 'credit_card', 'savings', 'loan')",
            name="ck_accounts_type",
        ),
        sa.CheckConstraint(
            "credit_limit_minor IS NULL OR credit_limit_minor >= 0",
            name="ck_accounts_credit_limit_non_negative",
        ),
        sa.CheckConstraint(
            "statement_day IS NULL OR statement_day BETWEEN 1 AND 31",
            name="ck_accounts_statement_day",
        ),
        sa.CheckConstraint(
            "payment_day IS NULL OR payment_day BETWEEN 1 AND 31",
            name="ck_accounts_payment_day",
        ),
        sa.CheckConstraint(
            "billing_account_id IS NULL OR billing_account_id <> id",
            name="ck_accounts_not_self_billing",
        ),
        sa.UniqueConstraint("household_id", "name", name="uq_accounts_household_name"),
        sa.UniqueConstraint("id", "household_id", name="uq_accounts_id_household"),
        sa.ForeignKeyConstraint(
            ["billing_account_id", "household_id"],
            ["accounts.id", "accounts.household_id"],
            name="fk_accounts_billing_household",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_accounts_household", "accounts", ["household_id"])

    op.create_table(
        "entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default=CURRENCY),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("raw_input", sa.Text(), nullable=True),
        sa.Column("voided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("voided_by", sa.BigInteger(), nullable=True),
        sa.Column("replaces_entry_id", sa.BigInteger(), nullable=True),
        sa.Column("related_entry_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "kind IN ('income', 'expense', 'transfer')", name="ck_entries_kind"
        ),
        sa.CheckConstraint("source IN ('telegram', 'web')", name="ck_entries_source"),
        sa.CheckConstraint(f"currency = '{CURRENCY}'", name="ck_entries_currency"),
        sa.CheckConstraint("amount_minor > 0", name="ck_entries_amount_positive"),
        sa.CheckConstraint(
            "voided_at IS NOT NULL OR voided_by IS NULL",
            name="ck_entries_voided_by_needs_voided_at",
        ),
        sa.CheckConstraint(
            "replaces_entry_id IS NULL OR replaces_entry_id <> id",
            name="ck_entries_not_self_replacing",
        ),
        sa.CheckConstraint(
            "kind <> 'transfer' OR category_id IS NULL",
            name="ck_entries_transfer_has_no_category",
        ),
        sa.UniqueConstraint("id", "household_id", name="uq_entries_id_household"),
        sa.ForeignKeyConstraint(
            ["member_id", "household_id"],
            ["members.id", "members.household_id"],
            name="fk_entries_member_household",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_entries_category_household",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replaces_entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entries_replaces_household",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["related_entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entries_related_household",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_entries_household_occurred_live",
        "entries",
        ["household_id", "occurred_at"],
        postgresql_where=sa.text("voided_at IS NULL"),
    )
    op.create_index(
        "ix_entries_household_kind_occurred",
        "entries",
        ["household_id", "kind", "occurred_at"],
    )

    op.create_table(
        "entry_legs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("leg_role", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "leg_role IN ('source', 'destination')", name="ck_entry_legs_role"
        ),
        sa.CheckConstraint("amount_minor <> 0", name="ck_entry_legs_amount_nonzero"),
        sa.ForeignKeyConstraint(
            ["entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entry_legs_entry_household",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "household_id"],
            ["accounts.id", "accounts.household_id"],
            name="fk_entry_legs_account_household",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_entry_legs_entry", "entry_legs", ["entry_id"])
    op.create_index(
        "ix_entry_legs_account_household", "entry_legs", ["account_id", "household_id"]
    )

    op.create_table(
        "entry_tags",
        sa.Column("entry_id", sa.BigInteger(), primary_key=True),
        sa.Column("tag", sa.Text(), primary_key=True),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "origin IN ('rule', 'manual', 'ai')", name="ck_entry_tags_origin"
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_entry_tags_confidence_range",
        ),
        sa.CheckConstraint(
            "length(tag) BETWEEN 1 AND 32", name="ck_entry_tags_tag_length"
        ),
        sa.ForeignKeyConstraint(
            ["entry_id", "household_id"],
            ["entries.id", "entries.household_id"],
            name="fk_entry_tags_entry_household",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_entry_tags_household_tag", "entry_tags", ["household_id", "tag"]
    )

    # --- not mapped in core/models.py this phase, but part of the baseline ---

    op.create_table(
        "pending_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_input", sa.Text(), nullable=False),
        sa.Column("parsed_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("parsed_kind", sa.Text(), nullable=True),
        sa.Column("parsed_category_id", sa.BigInteger(), nullable=True),
        sa.Column("parsed_note", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "parsed_kind IS NULL OR parsed_kind IN ('income', 'expense', 'transfer')",
            name="ck_pending_entries_kind",
        ),
        sa.CheckConstraint(
            "parsed_amount_minor IS NULL OR parsed_amount_minor > 0",
            name="ck_pending_entries_amount_positive",
        ),
        sa.ForeignKeyConstraint(
            ["member_id", "household_id"],
            ["members.id", "members.household_id"],
            name="fk_pending_entries_member_household",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parsed_category_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_pending_entries_category_household",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_pending_entries_expires", "pending_entries", ["expires_at"])

    op.create_table(
        "merchant_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("pattern", sa.Text(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("subcategory_id", sa.BigInteger(), nullable=True),
        sa.Column("hit_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "origin IN ('seed', 'learned')", name="ck_merchant_rules_origin"
        ),
        sa.CheckConstraint("hit_count >= 0", name="ck_merchant_rules_hit_count"),
        sa.UniqueConstraint(
            "household_id", "pattern", name="uq_merchant_rules_household_pattern"
        ),
        sa.ForeignKeyConstraint(
            ["category_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_merchant_rules_category_household",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subcategory_id", "household_id"],
            ["categories.id", "categories.household_id"],
            name="fk_merchant_rules_subcategory_household",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_merchant_rules_household", "merchant_rules", ["household_id"])

    # --- cross-row rules: deferred constraint triggers -----------------------

    op.execute(VALIDATE_ENTRY_LEGS)
    op.execute(VALIDATE_CATEGORY_DEPTH)

    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_entry_legs_validate
        AFTER INSERT OR UPDATE OR DELETE ON entry_legs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION chocofin_validate_entry_legs();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_entries_validate_legs
        AFTER INSERT OR UPDATE ON entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION chocofin_validate_entry_legs();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_categories_validate_depth
        AFTER INSERT OR UPDATE ON categories
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION chocofin_validate_category_depth();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_categories_validate_depth ON categories")
    op.execute("DROP TRIGGER IF EXISTS trg_entries_validate_legs ON entries")
    op.execute("DROP TRIGGER IF EXISTS trg_entry_legs_validate ON entry_legs")
    op.execute("DROP FUNCTION IF EXISTS chocofin_validate_category_depth()")
    op.execute("DROP FUNCTION IF EXISTS chocofin_validate_entry_legs()")

    op.drop_table("merchant_rules")
    op.drop_table("pending_entries")
    op.drop_table("entry_tags")
    op.drop_table("entry_legs")
    op.drop_table("entries")
    op.drop_table("accounts")
    op.drop_table("categories")
    op.drop_table("members")
    op.drop_table("households")
