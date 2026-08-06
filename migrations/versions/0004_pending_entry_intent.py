"""The one column that says which question the keyboard is asking.

`pending_entries` records what was PARSED. It has never recorded what was
ASKED, and those are not the same thing. `/transfer 500` and `/pay 3000` write
byte-identical rows — `parsed_kind='transfer'`, `source_account_id` NULL — so
the only thing distinguishing a card settlement from an ordinary transfer was
the one-character verb in the `callback_data` of buttons already on screen, plus
a `types=('credit_card',)` argument that lived for the duration of one function
call. Neither survives into the row, and the row is the only thing that survives
a redeploy.

The cost of that was a real bug: tapping [Other…] on a `/pay` keyboard made the
bot re-read the row, see a transfer with no source, and ask "From which
account?" — turning a settlement into a plain transfer that could put money into
any account at all, with no card check anywhere on the path.

So `intent` is added, and it is deliberately NOT the same column as
`parsed_kind`:

  * `parsed_kind` is the LEDGER KIND the entry will commit as. A settlement
    commits as `transfer`, because a settlement IS a transfer from the billing
    account to the card, and CLAUDE.md says so in the invariants.
  * `intent` is WHAT THE USER ASKED FOR. A settlement is `settlement` here even
    though it is `transfer` there.

Conflating them is what caused the bug, and giving each fact its own column is
what stops it coming back.

DESTRUCTIVE-OPERATION NOTE, per .claude/rules/migrations.md: this adds a NOT
NULL column with no server default and no backfill, and clears `pending_entries`
first.

The column is written that way on purpose. A DEFAULT would let a future row be
created without an intent and quietly inherit someone's guess, and a backfill
would have to decide `settlement` vs `transfer` for rows that by definition
cannot tell you which they were — which is the exact ambiguity this column
exists to remove.

That leaves the existing rows, and the DELETE is what handles them. Adding a NOT
NULL column with no default to a populated table simply fails, and any database
where the bot has run has rows here — the earlier claim that this table is empty
in every environment was true when it was written and nothing keeps it true. So
the rows go. A `pending_entries` row is an unanswered keyboard: an amount that
has been parsed and is waiting for someone to tap an account. It has never
moved money, no `entries` row depends on it, and the worst it costs is retyping
"120 coffee". The migrations rule that forbids a data migration scopes itself to
`entries`, which this does not touch.

Both directions LOCK the table first. `ADD COLUMN` takes ACCESS EXCLUSIVE anyway,
so the lock is not redundant with it — it is redundant only if nothing runs
BEFORE it, and the DELETE does. The bot is still up while this migrates: a
`pending.create` landing between the DELETE and the ADD COLUMN repopulates the
table, and a NOT NULL column with no default cannot be added to a populated one.
The migration is one transaction so that fails safely, but it fails during a
deploy for a reason that will not reproduce on the retry. Taking the lock first
closes the window.

Revision ID: 0004_pending_entry_intent
Revises: 0003_pending_entry_columns
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_pending_entry_intent"
down_revision: str | None = "0003_pending_entry_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Before the DELETE, not after it: see the locking note above. Held until
    # this migration commits, because `env.py` runs every revision inside
    # `context.begin_transaction()` and Postgres DDL is transactional.
    op.execute("LOCK TABLE pending_entries IN ACCESS EXCLUSIVE MODE")
    # Unanswered keyboards, cleared so the NOT NULL column can be added without
    # inventing an intent for them. See the note above.
    op.execute("DELETE FROM pending_entries")
    op.add_column(
        "pending_entries",
        sa.Column("intent", sa.Text(), nullable=False),
    )
    # TEXT + CHECK, never a Postgres ENUM type — the same rule every other
    # enumerated column in this schema follows. Adding a fifth intent later is
    # then one constraint swap rather than an ALTER TYPE.
    op.create_check_constraint(
        "ck_pending_entries_intent",
        "pending_entries",
        "intent IN ('expense', 'income', 'transfer', 'settlement')",
    )


def downgrade() -> None:
    # Same lock, for a narrower reason: a row inserted between the DELETE and
    # the DROP COLUMN survives the downgrade, and the next `upgrade` then fails
    # on exactly the row this revision has just stripped the intent from.
    op.execute("LOCK TABLE pending_entries IN ACCESS EXCLUSIVE MODE")
    # Same DELETE, for the same reason in the other direction: every row here
    # was written knowing which question it was asking, the older schema cannot
    # hold that, and leaving the rows would make the next `upgrade` fail on
    # exactly the rows this revision has just stripped the intent from.
    op.execute("DELETE FROM pending_entries")
    # Dropping the column would take the constraint with it, but naming it here
    # keeps the reversal explicit rather than incidental.
    op.drop_constraint("ck_pending_entries_intent", "pending_entries", type_="check")
    op.drop_column("pending_entries", "intent")
