"""Look up who is talking to us.

One function, but it lives in `core/` rather than `bot/` because it is a
database query and `core/` owns those. It is also the single point where a
Telegram user id becomes a household — the fact that authorisation is a
membership lookup and not an allowlist is a domain decision, not an adapter
detail.

`members.telegram_user_id` is globally UNIQUE, so one Telegram account belongs
to at most one household and this can never be ambiguous.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Member


async def get_member_by_telegram_id(
    session: AsyncSession, *, telegram_user_id: int
) -> Member | None:
    """The member behind a Telegram user id, or None.

    Returns inactive members too. Deciding what to do about `is_active` is the
    caller's job — `bot/auth.py` rejects them, but a future audit view may want
    to resolve a name for someone who has since left the household, and a
    lookup that silently pretends they never existed cannot serve that.
    """
    return await session.scalar(
        select(Member).where(Member.telegram_user_id == telegram_user_id)
    )
