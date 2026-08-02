# ChocoFin — household ledger

Telegram-first expense/income tracker for one household. Self-hosted.

## Commands
- Install: `uv sync`
- Test: `pytest -q`
- Lint: `ruff check . && ruff format --check .`
- Migrate: `alembic upgrade head`
- Run bot locally: `python -m bot`
- Run API locally: `uvicorn api.main:app --reload`

## Architecture
- `core/` owns ALL ledger logic and is the ONLY module that writes to the DB.
- `bot/` and `api/` are thin adapters over `core/`. They must never contain
  business rules, arithmetic on money, or raw SQL.
- Domain model and rationale: @docs/architecture.md

## Invariants — never violate
- Money is BIGINT minor units (centavos) named `amount_minor`. Never float, never
  Decimal in the DB layer. Convert to display units only in formatters.
- Currency is PHP only. `currency` is CHAR(3) DEFAULT 'PHP' with a CHECK. Never
  write conversion, FX-rate, or multi-currency logic.
- `kind` is TEXT with a CHECK constraint, not a Postgres ENUM type. Same for
  `source` and `role`. Values: income, expense, transfer.
- Card settlements are `kind='transfer'` from the billing account to the card
  account. They are NEVER expenses.
- Transfers are excluded from every income/expense total, in every period and
  every view: summary queries filter `kind <> 'transfer'`. Transfers ARE included
  in account balance math. Two different code paths, never merged.
- Signed amounts live in `entry_legs`. `entries.amount_minor` is unsigned display
  only. Leg shape is enforced by a DEFERRABLE constraint trigger at COMMIT:
  an expense has exactly one `source` leg with a negative amount; income has
  exactly one `destination` leg with a positive amount; a transfer has exactly
  two legs, one `source` (negative) and one `destination` (positive), which sum
  to zero. Every entry has at least one leg. Sum-to-zero applies to TRANSFERS
  ONLY — income and expense have no counterparty account to balance against,
  because every `accounts.type` is real money and there are no nominal accounts.
  The trigger also cross-checks `entries.amount_minor` against its legs, so the
  display amount can never drift from the money actually moved.
- A transfer fee is NEVER a leg on the transfer. It is its own one-leg expense
  entry pointing at the transfer via `related_entry_id`. A fee leg would break
  sum-to-zero and would hide real spending from every total.
- Account balances are always derived (opening balance + SUM of legs). Never add
  a stored or cached balance column.
- `exclude_from_totals` applies to balance and net-worth totals ONLY. It NEVER
  filters a spending summary — money spent from an excluded account is still
  spending. `summarise` must not reference the flag at all.
- Entries are append-only. Corrections set `voided_at` and insert a replacement.
  Never UPDATE or DELETE a row in `entries`.
- A replacement entry copies `occurred_at` from the entry it replaces. Never
  `now()` — correcting a January entry in March must leave the money in January.
  `created_at` is the only timestamp that moves.
- All timestamps are TIMESTAMPTZ stored in UTC. Period boundaries resolve in
  `Asia/Manila` first, then convert to UTC for querying.
- `household_id` is on every table and in every WHERE clause.
- Telegram authorisation is enforced in ONE decorator in `bot/auth.py`.
  Never add an inline user-id check inside a handler.
- No secrets in code or tests. Config comes from env vars via `core/config.py`.

## Conventions
- Python 3.13, async throughout, SQLAlchemy 2.x async, Alembic for all schema
  changes. Never hand-write DDL outside a migration.
- python-telegram-bot v22.x, long polling only. No webhooks, no inbound ports.
- No entry is written until an account is chosen from the inline keyboard.
  Uncommitted entries live in `pending_entries`, never in process memory.
- Correcting an entry voids it and inserts a replacement carrying
  `replaces_entry_id`, in ONE transaction. Never UPDATE the original.
- `callback_data` must stay under 64 bytes: ids only, never note or raw text.
  Category names contain emoji (4 bytes each) — never put a name in callback_data.
  Every callback handler calls `answer_callback_query` on every path and is
  idempotent against double-taps. Never use PTB `arbitrary_callback_data`.
- Frontend is Next.js in `web/`, talking to FastAPI over same-origin `/api/*`.
  Never add CORS middleware — if you think you need it, the routing is wrong.
  Session cookies are HttpOnly; validate them server-side, never in the client.
- Tests: pytest. Every `core/` function needs a test. Bot/API handlers need at
  least one happy-path and one rejection test.
- DB-backed tests read `TEST_DATABASE_URL`, never `DATABASE_URL`, and abort at
  session start unless the target database name ends in `_test`. Test setup may
  TRUNCATE; the shared Postgres LXC holds live household data.

## Workflow
- Propose a plan and wait for approval before writing code.
- After changing `core/`, run `pytest -q` and report the result.
- Prefer editing existing files over creating new ones.