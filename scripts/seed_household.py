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
duplicated, and a card that already names a billing account keeps the one it
has — a re-run reports the difference instead of repointing it.

It prints the database it is about to write to and asks before writing.
`DATABASE_URL` is ambient and this is the one script that can point a Telegram
account at a household: `telegram_user_id` is globally UNIQUE, and `seed` binds
an unknown one to whichever household it was pointed at. Getting that wrong is
not data loss, it is access, and the script cannot undo it on a later run.
`--yes` skips the question for scripted use; nothing skips the echo.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
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


def _redact(url: str) -> str:
    """The connection string with the password taken out.

    The target has to be printed and the URL carries a password. The netloc is
    rebuilt from its parsed parts rather than cut out of the string, so a
    password containing an `@` or a `:` cannot survive the edit.
    """
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _database_name(url: str) -> str:
    """Same read as `tests/conftest.py`, which guards on the same thing."""
    return urlsplit(url).path.lstrip("/")


def _interactive() -> bool:
    """Whether there is a person on the other end to answer the prompt."""
    return sys.stdin.isatty()


def _confirm(
    url: str,
    *,
    household_name: str,
    telegram_user_id: int,
    display_name: str,
    account_specs: list[tuple[str, str, str | None]],
    assume_yes: bool,
) -> bool:
    """Show the operator where this is pointed and what it will write.

    The prompt asks for the database name rather than a `y`, because a `y` can
    be typed without reading the line above it and the name cannot. The whole
    point of the question is that the echo gets looked at.
    """
    name = _database_name(url)
    print("About to write to")
    print(f"  {_redact(url)}")
    print(f"  database: {name or '(none in the URL)'}")
    print()
    print(f"  household:        {household_name!r}")
    print(f"  telegram user id: {telegram_user_id}")
    print(f"  display name:     {display_name!r}")
    for account_name, account_type, billing in account_specs:
        billing_note = f", settles from {billing!r}" if billing else ""
        print(f"  account:          {account_name!r} ({account_type}){billing_note}")
    print()
    print(
        "telegram_user_id is globally UNIQUE. This grants that Telegram account"
        " access to this household's ledger, and re-running against a different"
        " household will not move it."
    )

    if assume_yes:
        print("--yes given, so not asking.")
        return True
    if not _interactive():
        print("Not a terminal and --yes was not given, so there is nobody to ask.")
        return False
    try:
        answer = input(f"Type the database name ({name}) to continue: ")
    except (EOFError, KeyboardInterrupt):
        # `isatty` is not a promise that anyone will answer — a redirected
        # stdin reports a terminal under some shells and then reads EOF, and
        # Ctrl-C is an answer of its own. Silence is a no, not a traceback.
        print()
        return False
    return answer.strip() == name


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
        if card.billing_account_id is not None:
            # A re-run leaves what is already there alone — the same rule the
            # account and member passes follow, and it matters more here.
            # `settle_card` reads this column to decide where real money comes
            # from, so repointing it would send the next `/pay` out of an
            # account the operator never named, on a script whose log said it
            # had linked something rather than moved it.
            current = await session.scalar(
                select(Account).where(
                    Account.id == card.billing_account_id,
                    Account.household_id == household_id,
                )
            )
            current_name = current.name if current else f"#{card.billing_account_id}"
            log.append(
                f"{name!r} already settles from {current_name!r}, "
                f"not {billing_name!r} — left alone"
            )
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
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation, for scripted use. The target is still printed.",
    )
    args = parser.parse_args(argv)

    # Resolved once, here, so the operator is shown the exact URL the engine
    # will be built from — and so a declined run never builds one at all.
    url = get_settings().database_url
    if not _confirm(
        url,
        household_name=args.household,
        telegram_user_id=args.telegram_user_id,
        display_name=args.display_name,
        account_specs=args.account,
        assume_yes=args.yes,
    ):
        print("Aborted. Nothing was written.")
        return 1

    engine = make_engine(url)
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
