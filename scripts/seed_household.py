"""Create the first household, its owner, and optionally some accounts.

Authorisation is membership: the bot answers a Telegram user because a row in
`members` says so. Until `/link` arrives in a later phase there is no way to
write that first row from inside the bot, so this writes it from outside.

    python -m scripts.seed_household \
        --household "Home" \
        --telegram-user-id 123456789 \
        --display-name "Alex" \
        --account "Cash:cash" \
        --account "BPI:bank" \
        --account "Visa:credit_card:BPI"

Reads `DATABASE_URL` through `core.config`, like everything else. It is NOT an
onboarding feature — there is no rule here for a later phase to unwind. It is a
typed-in INSERT with a `--help`, and it is re-runnable: an existing
`telegram_user_id`, household name or account name is left alone rather than
duplicated.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import make_engine, make_sessionmaker, session_scope
from core.models import ACCOUNT_TYPES, Account, Household, Member


def _parse_account(spec: str) -> tuple[str, str, str | None]:
    """ "BPI:bank" or "Visa:credit_card:BPI" -> (name, type, billing account)."""
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(
            f"{spec!r} should be NAME:TYPE or NAME:TYPE:BILLING_ACCOUNT_NAME"
        )
    name, account_type = parts[0].strip(), parts[1].strip()
    if not name:
        raise argparse.ArgumentTypeError(f"{spec!r} has an empty account name")
    if account_type not in ACCOUNT_TYPES:
        raise argparse.ArgumentTypeError(
            f"{account_type!r} is not one of {', '.join(ACCOUNT_TYPES)}"
        )
    billing = parts[2].strip() if len(parts) == 3 else None
    if billing and account_type != "credit_card":
        raise argparse.ArgumentTypeError(
            f"{name!r} is a {account_type}, so it cannot have a billing account"
        )
    return name, account_type, billing


async def _find_account(
    session: AsyncSession, *, household_id: int, name: str
) -> Account | None:
    """Case-insensitively, to match `uq_accounts_household_name_lower`."""
    return await session.scalar(
        select(Account).where(
            Account.household_id == household_id,
            func.lower(Account.name) == name.lower(),
        )
    )


async def seed(
    session: AsyncSession,
    *,
    household_name: str,
    telegram_user_id: int,
    display_name: str,
    account_specs: list[tuple[str, str, str | None]],
) -> list[str]:
    """Do the work and return a line per action, for the operator to read."""
    log: list[str] = []

    member = await session.scalar(
        select(Member).where(Member.telegram_user_id == telegram_user_id)
    )
    if member is not None:
        household = await session.get(Household, member.household_id)
        log.append(
            f"member {display_name!r} already exists as #{member.id} in "
            f"household #{member.household_id} "
            f"({household.name if household else '?'})"
        )
        household_id = member.household_id
    else:
        household = await session.scalar(
            select(Household).where(
                func.lower(Household.name) == household_name.lower()
            )
        )
        if household is None:
            household = Household(name=household_name)
            session.add(household)
            await session.flush()
            log.append(f"created household #{household.id} {household_name!r}")
        else:
            log.append(f"using existing household #{household.id}")
        household_id = household.id

        member = Member(
            household_id=household_id,
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            # The first member is the owner; there is nobody to be owned by.
            role="owner",
        )
        session.add(member)
        await session.flush()
        log.append(f"created member #{member.id} {display_name!r} (owner)")

    # Two passes: a card's billing account may be named later on the command
    # line than the card itself.
    for name, account_type, _ in account_specs:
        if await _find_account(session, household_id=household_id, name=name):
            log.append(f"account {name!r} already exists")
            continue
        session.add(Account(household_id=household_id, name=name, type=account_type))
        log.append(f"created account {name!r} ({account_type})")
    await session.flush()

    for name, _, billing_name in account_specs:
        if not billing_name:
            continue
        card = await _find_account(session, household_id=household_id, name=name)
        billing = await _find_account(
            session, household_id=household_id, name=billing_name
        )
        if card is None or billing is None:
            log.append(
                f"could not link {name!r} to billing account {billing_name!r} "
                "— one of them is missing"
            )
            continue
        if card.billing_account_id == billing.id:
            continue
        card.billing_account_id = billing.id
        log.append(f"{name!r} settles from {billing_name!r}")

    return log


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_household",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--household", required=True, help="household name")
    parser.add_argument(
        "--telegram-user-id",
        required=True,
        type=int,
        help="the numeric Telegram user id of the owner",
    )
    parser.add_argument("--display-name", required=True, help="the owner's name")
    parser.add_argument(
        "--account",
        action="append",
        default=[],
        type=_parse_account,
        metavar="NAME:TYPE[:BILLING]",
        help="repeatable; e.g. Cash:cash, Visa:credit_card:BPI",
    )
    args = parser.parse_args(argv)

    engine = make_engine()
    try:
        async with session_scope(make_sessionmaker(engine)) as session:
            log = await seed(
                session,
                household_name=args.household,
                telegram_user_id=args.telegram_user_id,
                display_name=args.display_name,
                account_specs=args.account,
            )
    finally:
        await engine.dispose()

    for line in log:
        print(line)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
