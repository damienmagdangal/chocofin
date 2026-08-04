"""Test harness.

SAFETY: DB-backed tests read `TEST_DATABASE_URL` and never `DATABASE_URL`.
Setup TRUNCATEs tables, and production is a shared Postgres holding live
household data with no backups. The guard below is the only thing between a
stray environment variable and a wiped ledger, so it aborts the whole session
rather than skipping or warning:

    * TEST_DATABASE_URL unset           -> DB tests FAIL
    * ...and ALLOW_DB_TEST_SKIP=1       -> DB tests skip, deliberately
    * database name not ending `_test`  -> HARD ABORT of the entire run
    * TEST_DATABASE_URL == DATABASE_URL -> HARD ABORT

Unset used to skip. It fails now because a skipped run and a passing run look
identical at a glance — same green, same exit code — and two thirds of this
suite is the DB-backed half that proves the ledger's invariants. Opting out has
to be a thing you typed, not a thing you forgot.

Pure tests (parser, periods) need no database and always run.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

ROOT = Path(__file__).resolve().parent.parent

TEST_URL_VAR = "TEST_DATABASE_URL"
SKIP_OPT_OUT_VAR = "ALLOW_DB_TEST_SKIP"
REQUIRED_SUFFIX = "_test"

# Truncated between tests. Order is irrelevant under CASCADE, but households
# last keeps the intent readable.
TABLES = (
    "entry_tags",
    "entry_legs",
    "entries",
    "pending_entries",
    "merchant_rules",
    "accounts",
    "categories",
    "members",
    "households",
)


def _database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def pytest_configure(config: pytest.Config) -> None:
    """Session-start guard. Runs before any fixture or test."""
    url = os.environ.get(TEST_URL_VAR, "").strip()
    if not url:
        # Nothing configured, nothing to destroy. `_test_url` decides whether
        # the DB tests fail or skip; this guard only protects real databases.
        return

    name = _database_name(url)
    if not name.endswith(REQUIRED_SUFFIX):
        pytest.exit(
            f"REFUSING TO RUN: {TEST_URL_VAR} points at database {name!r}, which "
            f"does not end in {REQUIRED_SUFFIX!r}. Test setup TRUNCATEs tables. "
            "Point it at a scratch database.",
            returncode=2,
        )

    production = os.environ.get("DATABASE_URL", "").strip()
    if production and production == url:
        pytest.exit(
            f"REFUSING TO RUN: {TEST_URL_VAR} is identical to DATABASE_URL. "
            "Test setup TRUNCATEs tables.",
            returncode=2,
        )


def _test_url() -> str:
    """The test database URL, or a loud stop.

    Raised from the session-scoped `engine` fixture, so a missing URL errors
    every DB-backed test at once instead of quietly removing them from the run.
    """
    url = os.environ.get(TEST_URL_VAR, "").strip()
    if url:
        return url

    if os.environ.get(SKIP_OPT_OUT_VAR, "").strip() == "1":
        pytest.skip(
            f"{TEST_URL_VAR} is not set and {SKIP_OPT_OUT_VAR}=1 — skipping "
            "DB-backed tests on purpose. The ledger invariants are NOT covered "
            "by this run.",
            allow_module_level=True,
        )

    pytest.fail(
        f"{TEST_URL_VAR} is not set, so no DB-backed test can run — and those "
        "are the ones that prove the leg trigger, the constraints and the "
        "summary/balance split. Point it at a scratch Postgres whose name ends "
        f"in {REQUIRED_SUFFIX!r}, or set {SKIP_OPT_OUT_VAR}=1 if you really do "
        "mean to run without a database.",
        pytrace=False,
    )


def _run_migrations(sync_connection) -> None:
    """Upgrade to head on an existing connection.

    Tests migrate rather than calling `metadata.create_all`: the constraint
    triggers exist only in the migration, so create_all would build a schema
    that silently lacks the rules these tests are written to prove.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = sync_connection
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    from sqlalchemy.ext.asyncio import create_async_engine

    url = _test_url()
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.run_sync(_run_migrations)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        )
        await conn.commit()


@pytest_asyncio.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A raw connection on a truncated database, for driving transactions by
    hand — the constraint-trigger tests need real BEGIN/COMMIT boundaries."""
    await _truncate(engine)
    async with engine.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """An ORM session on a truncated database.

    Deliberately NOT bound to an outer transaction with
    `join_transaction_mode="create_savepoint"`. Releasing a savepoint does not
    fire DEFERRABLE constraint triggers, so every ledger test would run against
    a database with its central invariants effectively switched off.
    `session.commit()` here is a real COMMIT and the triggers really fire.

    `expire_on_commit=False` matches production: an expired attribute would
    lazy-load on next access, which raises under asyncio.
    """
    await _truncate(engine)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
