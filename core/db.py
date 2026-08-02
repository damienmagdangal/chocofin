"""Async engine and session factory.

SQLAlchemy 2.x asyncio: `create_async_engine` + `async_sessionmaker`.
`expire_on_commit=False` because an expired attribute would trigger a lazy
refresh on next access, which under asyncio raises instead of loading.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings


def make_engine(url: str | None = None, **kwargs) -> AsyncEngine:
    return create_async_engine(url or get_settings().database_url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One unit of work. Commits on success, rolls back on any exception.

    Ledger operations that must be atomic — void plus replacement, for
    instance — run inside a single scope so the pair cannot half-apply.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
