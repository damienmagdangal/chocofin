"""Two columns `pending_entries` needs before the bot can use it.

A pending entry is the half-finished thought between "100 coffee" arriving and
an account being tapped. The baseline table can hold the amount, the note and
the date, but not two things the Telegram flow produces:

  * `parsed_tags` — the parser already extracts #tags, and without somewhere to
    put them the only way to recover them at commit time is to parse the raw
    message a second time. Parsing twice means two chances to disagree about
    what the user wrote, over a value that was already known once.

  * `source_account_id` — a transfer needs two accounts and the keyboard can
    only ask for one at a time. Between the two taps the first choice has to
    live somewhere, and CLAUDE.md rules out process memory: a restart between
    the taps would strand the keyboard. It also keeps the second button's
    `callback_data` down to `d:<pending>:<destination>`, well inside the
    64-byte limit, instead of carrying a third id.

Both columns are nullable and neither is written by anything that exists yet,
so this is additive on a populated table: no rewrite, no backfill, no lock
beyond the catalogue update.

`source_account_id` gets a COMPOSITE foreign key to `(id, household_id)`, the
same shape every other child table uses. A plain FK to `accounts.id` would let
a pending row point at an account in someone else's household, and the
denormalised `household_id` this schema relies on everywhere would be a lie for
exactly the rows that are about to become real money.

Revision ID: 0003_pending_entry_columns
Revises: 0002_case_insensitive_names
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_pending_entry_columns"
down_revision: str | None = "0002_case_insensitive_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pending_entries",
        sa.Column("parsed_tags", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "pending_entries",
        sa.Column("source_account_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pending_entries_source_account_household",
        "pending_entries",
        "accounts",
        ["source_account_id", "household_id"],
        ["id", "household_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # Dropping the columns would take the constraint with them, but naming it
    # here keeps the reversal explicit rather than incidental.
    op.drop_constraint(
        "fk_pending_entries_source_account_household",
        "pending_entries",
        type_="foreignkey",
    )
    op.drop_column("pending_entries", "source_account_id")
    op.drop_column("pending_entries", "parsed_tags")
