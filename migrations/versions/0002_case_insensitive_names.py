"""Account and category names are unique per household, case-insensitively.

Both constraints were plain UNIQUEs over TEXT columns with the default
collation, so 'GoTyme' and 'gotyme' were two different accounts, and 'Coffee'
and 'coffee' two different categories, in the same household.

  * accounts:   (household_id, name)       -> (household_id, lower(name))
  * categories: (household_id, name, kind) -> (household_id, lower(name), kind)

`kind` stays in the category key and stays case-sensitive: it is a controlled
vocabulary fixed by ck_categories_kind, not user text, and an expense 'Refunds'
bucket alongside an income 'Refunds' bucket is legitimate.

Why this matters differently for each: a duplicate account splits real money —
each row accumulates its own derived balance and neither is wrong, so nothing
looks broken while half the household's cash sits in an account it thinks it
already has. A duplicate category splits a report instead, which is milder but
just as invisible, and it defeats the parent/child rollup in `summarise`.

On a populated database this FAILS if case-duplicates already exist. That is
deliberate. Merging two accounts means moving legs between them, and `entries`
is append-only: the correction is a void plus a replacement, a judgement call
about real money that a migration may not make on its own. Reconcile by hand,
then run this.

Revision ID: 0002_case_insensitive_names
Revises: 0001_baseline
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_case_insensitive_names"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_accounts_household_name", "accounts", type_="unique")
    op.create_index(
        "uq_accounts_household_name_lower",
        "accounts",
        ["household_id", sa.text("lower(name)")],
        unique=True,
    )

    op.drop_constraint(
        "uq_categories_household_name_kind", "categories", type_="unique"
    )
    op.create_index(
        "uq_categories_household_name_kind_lower",
        "categories",
        ["household_id", sa.text("lower(name)"), "kind"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_categories_household_name_kind_lower", table_name="categories")
    op.create_unique_constraint(
        "uq_categories_household_name_kind",
        "categories",
        ["household_id", "name", "kind"],
    )

    op.drop_index("uq_accounts_household_name_lower", table_name="accounts")
    op.create_unique_constraint(
        "uq_accounts_household_name", "accounts", ["household_id", "name"]
    )
