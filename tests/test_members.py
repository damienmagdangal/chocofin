"""Tests for `core.members` — turning a Telegram user id into a household."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core import members
from core.models import Member
from tests.factories import build_world

pytestmark = pytest.mark.asyncio


async def test_a_known_telegram_id_resolves_to_its_member(session: AsyncSession):
    world = await build_world(session)
    member = await members.get_member_by_telegram_id(
        session, telegram_user_id=world.member_telegram_id
    )
    assert member is not None
    assert member.id == world.member_id
    assert member.household_id == world.household_id


async def test_an_unknown_telegram_id_resolves_to_nothing(session: AsyncSession):
    """Authorisation is a membership lookup, not an allowlist. A stranger is
    simply not in the table."""
    await build_world(session)
    assert (
        await members.get_member_by_telegram_id(session, telegram_user_id=999_999_999)
        is None
    )


async def test_each_telegram_id_belongs_to_one_household(session: AsyncSession):
    """`telegram_user_id` is globally UNIQUE, so this can never be ambiguous."""
    world = await build_world(session)
    outsider = await members.get_member_by_telegram_id(
        session, telegram_user_id=world.outsider_telegram_id
    )
    assert outsider is not None
    assert outsider.household_id == world.outsider_household_id


async def test_an_inactive_member_is_returned_not_hidden(session: AsyncSession):
    """Deciding what to do about `is_active` is the caller's job.

    `bot/auth.py` rejects them. A lookup that silently pretended they never
    existed could not serve an audit view that needs to put a name on last
    year's entries, and would move the rejection out of the one decorator that
    is supposed to own it.
    """
    world = await build_world(session)
    member = await session.scalar(select(Member).where(Member.id == world.member_id))
    member.is_active = False
    await session.commit()

    found = await members.get_member_by_telegram_id(
        session, telegram_user_id=world.member_telegram_id
    )
    assert found is not None
    assert found.id == world.member_id
    assert found.is_active is False
