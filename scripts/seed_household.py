r"""Create the first household, its owner, and the accounts it starts with.

Authorisation is membership: the bot answers a Telegram user because a row in
`members` says so. Until `/link` arrives in a later phase there is no way to
write that first row from inside the bot, so this writes it from outside.

    python -m scripts.seed_household \
        --household "Home" \
        --telegram-user-id 123456789 \
        --display-name "Alex" \
        --account "Wallet:cash:opening=1500" \
        --account "BPI:bank:opening=42350.75" \
        --account "Visa:credit_card:opening=-3000:limit=50000:billing=BPI"

`NAME:TYPE` is positional; everything after it is `key=value`. The keys are
`opening`, `limit` and `billing`, and each exists because an account without it
is not usable:

* `opening` is what the account held before the ledger starts, in PESOS. Every
  balance the app shows is `opening_balance_minor + SUM(legs)`, so seeding zero
  into an account that already holds money makes every balance wrong from the
  first screen — and `entries` is append-only, so the only later fix is an
  adjusting entry for money that never moved. A card's debt is NEGATIVE:
  liabilities are negative balances, and `-3000` is three thousand pesos owed.
* `limit` is a credit card's limit, in pesos. Available credit is
  `credit_limit_minor + balance_minor`; with no limit stored there is no
  available credit to report, ever.
* `billing` names the account a card is settled from. `settle_card` refuses to
  invent one, so a card seeded without it fails every `/pay`.

A card must carry both `limit` and `billing` or the spec is rejected. Half an
account is worse than a clear error, because nothing afterwards tells you it is
half. `billing` may name an account created anywhere in the same run — accounts
are written in one pass and linked in a second, so command-line order does not
matter — or one already in the household.

Amounts are pesos here and centavos in the database. The conversion happens
once, in `_parse_pesos`, with integer arithmetic: no float ever touches money,
and nobody counts zeroes by hand on a command line.

Reads `DATABASE_URL` through `core.config`, like everything else. It is NOT an
onboarding feature — there is no rule here for a later phase to unwind. It is a
typed-in INSERT with a `--help`, and it is re-runnable: an existing
`telegram_user_id`, household name or account name is left alone rather than
duplicated, and an existing account keeps the opening balance, credit limit and
billing account it already has — a re-run reports the difference instead of
writing over it.

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
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.formatting import format_minor
from core.config import get_settings
from core.db import make_engine, make_sessionmaker, session_scope
from core.models import ACCOUNT_TYPES, Account, Household, Member

# The parser's ceiling, which is the BIGINT one. Imported rather than
# re-declared: two constants for one column drift apart, and the one that
# drifts low silently refuses money the database would have taken.
from core.parser import MAX_AMOUNT_MINOR

SPEC_KEYS = ("opening", "limit", "billing")

# ASCII digits only, with optional comma grouping and a fractional part.
# `int()` accepts Arabic-Indic and full-width digits — int("١٠٠")
# is 100 — so the character class, not a later check, is what keeps them out of
# a balance.
_PESOS_RE = re.compile(r"^(?P<sign>-?)(?P<whole>[0-9,]*)(?:\.(?P<frac>[0-9]+))?$")


class SeedError(Exception):
    """The run cannot proceed, and nothing has been written.

    For the things one `--account` cannot know on its own — a `billing=` naming
    nothing, two specs claiming one name — which are therefore checked once the
    whole command line is in view and before any account is inserted.
    """


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """One `--account`, parsed. Amounts are already centavos.

    `opening_balance_minor` is None when the operator did not say. A new
    account is then created with 0, and an existing one is not reported as
    differing — "you typed nothing" is not a disagreement about money, and a
    "left alone" line printed for an omission is the fastest way to teach
    someone to stop reading them.
    """

    name: str
    type: str
    opening_balance_minor: int | None = None
    credit_limit_minor: int | None = None
    billing_account_name: str | None = None


def _parse_pesos(text: str, *, field: str) -> int:
    """Pesos as typed to centavos, exactly. Never float, never Decimal.

    `core.parser._parse_amount` is deliberately not reused. It rejects negatives
    and zero — both legal opening balances, a card's debt being the reason
    negatives exist here at all — strips a currency prefix, and returns a
    `ParseError` rather than raising, which argparse cannot turn into a message.
    """
    token = text.strip()
    if not token:
        raise argparse.ArgumentTypeError(f"{field}= has no amount after it")

    if token.endswith("."):
        raise argparse.ArgumentTypeError(
            f"{field}={token!r} ends in a decimal point — that is a truncated "
            "number, not an amount"
        )

    match = _PESOS_RE.match(token)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"{field}={token!r} is not an amount in pesos "
            "(digits, optional commas, optional decimals)"
        )

    whole = match.group("whole").replace(",", "")
    frac = match.group("frac")
    if not whole and frac is None:
        raise argparse.ArgumentTypeError(f"{field}={token!r} has no digits in it")
    if frac is not None and len(frac) > 2:
        raise argparse.ArgumentTypeError(
            f"{field}={token!r} is finer than one centavo. Round it yourself — "
            "this will not guess."
        )

    centavos = int(whole or "0") * 100 + int(frac.ljust(2, "0") if frac else "0")
    if centavos > MAX_AMOUNT_MINOR:
        raise argparse.ArgumentTypeError(f"{field}={token!r} is too large to store")
    return -centavos if match.group("sign") else centavos


def _parse_options(spec: str, fields: list[str]) -> dict[str, str]:
    """The `key=value` tail of a spec, checked as keys before as values."""
    options: dict[str, str] = {}
    for raw in fields:
        field = raw.strip()
        key, sep, value = field.partition("=")
        if not sep:
            # The old spec was NAME:TYPE:BILLING_ACCOUNT. Anyone with that
            # command in their shell history lands exactly here, so say what it
            # is now rather than only that it is wrong.
            hint = f" — did you mean billing={field}?" if field else ""
            raise argparse.ArgumentTypeError(
                f"{field!r} in {spec!r} is not key=value{hint}"
            )
        key = key.strip()
        if key not in SPEC_KEYS:
            raise argparse.ArgumentTypeError(
                f"{key!r} in {spec!r} is not one of {', '.join(SPEC_KEYS)}"
            )
        if key in options:
            raise argparse.ArgumentTypeError(f"{spec!r} gives {key}= twice")
        options[key] = value.strip()
    return options


def _parse_account(spec: str) -> AccountSpec:
    """`NAME:TYPE[:opening=P][:limit=P][:billing=NAME]` -> an `AccountSpec`.

    Every rejection here happens during `parse_args`, which is before an engine
    is built — a bad spec costs an exit code, never a connection.
    """
    parts = spec.split(":")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(
            f"{spec!r} should be NAME:TYPE, optionally followed by "
            f"{', '.join(key + '=' for key in SPEC_KEYS)}"
        )

    name, account_type = parts[0].strip(), parts[1].strip()
    if not name:
        raise argparse.ArgumentTypeError(f"{spec!r} has an empty account name")
    if account_type not in ACCOUNT_TYPES:
        raise argparse.ArgumentTypeError(
            f"{account_type!r} is not one of {', '.join(ACCOUNT_TYPES)}"
        )

    options = _parse_options(spec, parts[2:])
    is_card = account_type == "credit_card"

    opening: int | None = None
    if "opening" in options:
        opening = _parse_pesos(options["opening"], field="opening")

    limit: int | None = None
    if "limit" in options:
        if not is_card:
            raise argparse.ArgumentTypeError(
                f"{name!r} is a {account_type}, so it cannot have a credit limit"
            )
        limit = _parse_pesos(options["limit"], field="limit")
        if limit < 0:
            # ck_accounts_credit_limit_non_negative would catch it, as a
            # Postgres error, after the confirmation and the connection.
            raise argparse.ArgumentTypeError(
                f"{name!r} has a negative credit limit. A limit is how much can "
                "be owed, so it is never below zero."
            )
    elif is_card:
        raise argparse.ArgumentTypeError(
            f"{name!r} is a credit_card, so it needs limit=<pesos>. Without it "
            "available credit cannot be computed, for the life of the card."
        )

    billing = options.get("billing") or None
    if billing and not is_card:
        raise argparse.ArgumentTypeError(
            f"{name!r} is a {account_type}, so it cannot have a billing account"
        )
    if is_card and not billing:
        raise argparse.ArgumentTypeError(
            f"{name!r} is a credit_card, so it needs billing=<account name>. "
            "Without it every /pay raises CardHasNoBillingAccountError."
        )
    if billing and billing.lower() == name.lower():
        # ck_accounts_not_self_billing, said here where it can be understood.
        raise argparse.ArgumentTypeError(f"{name!r} cannot settle from itself")

    return AccountSpec(
        name=name,
        type=account_type,
        opening_balance_minor=opening,
        credit_limit_minor=limit,
        billing_account_name=billing,
    )


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


def _describe(spec: AccountSpec) -> str:
    """One account as the operator will see it in the echo, money included.

    The amounts are the whole reason the echo is worth reading now: a limit
    typed into `opening=` is not a typo anyone spots in centavos, and it is a
    wrong balance for the life of the household.
    """
    line = f"{spec.name!r} ({spec.type})"
    line += f", opening {format_minor(spec.opening_balance_minor or 0)}"
    if spec.credit_limit_minor is not None:
        line += f", limit {format_minor(spec.credit_limit_minor)}"
    if spec.billing_account_name:
        line += f", settles from {spec.billing_account_name!r}"
    return line


def _confirm(
    url: str,
    *,
    household_name: str,
    telegram_user_id: int,
    display_name: str,
    account_specs: list[AccountSpec],
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
    for spec in account_specs:
        print(f"  account:          {_describe(spec)}")
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


def _reject_duplicate_names(account_specs: list[AccountSpec]) -> None:
    """Two specs cannot claim one account. Checked before anything is written.

    `uq_accounts_household_name_lower` would catch it, but only on the second
    INSERT — after the household, the member and some of the accounts had been
    written, and with a Postgres error naming an index instead of the two
    `--account` arguments that disagree.
    """
    seen: dict[str, str] = {}
    for spec in account_specs:
        key = spec.name.lower()
        if key in seen:
            raise SeedError(
                f"{spec.name!r} and {seen[key]!r} are the same account name — "
                "account names are case-insensitive. Nothing was written."
            )
        seen[key] = spec.name


async def _resolve_billing_accounts(
    session: AsyncSession, *, household_id: int, account_specs: list[AccountSpec]
) -> None:
    """Every `billing=` must name something, before any account is inserted.

    A card whose billing account does not resolve used to be created anyway and
    reported in a line the operator had to notice. That is the half-usable card
    this script exists to stop: `/pay` raises `CardHasNoBillingAccountError` on
    it, and by then nobody remembers a log from the seed. It is a typo in one
    argument, so it aborts the run and keeps its own transaction clean.
    """
    in_this_run = {spec.name.lower() for spec in account_specs}
    for spec in account_specs:
        billing = spec.billing_account_name
        if not billing or billing.lower() in in_this_run:
            continue
        if await _find_account(session, household_id=household_id, name=billing):
            continue
        raise SeedError(
            f"{spec.name!r} settles from {billing!r}, which is not named "
            "anywhere in this run and is not already in the household. "
            "Nothing was written."
        )


def _report_drift(existing: Account, spec: AccountSpec) -> list[str]:
    """What the command line says about an account that already exists.

    Reported, never written. An opening balance is history — every balance the
    household has ever seen was derived from it, so rewriting one silently
    moves all of them — and a credit limit follows the same rule as the billing
    link for the same reason: a re-run leaves what is there alone and says so.
    """
    lines: list[str] = []
    if (
        spec.opening_balance_minor is not None
        and existing.opening_balance_minor != spec.opening_balance_minor
    ):
        lines.append(
            f"{existing.name!r} opening balance is "
            f"{format_minor(existing.opening_balance_minor)}, not "
            f"{format_minor(spec.opening_balance_minor)} — left alone"
        )
    if (
        spec.credit_limit_minor is not None
        and existing.credit_limit_minor != spec.credit_limit_minor
    ):
        current = (
            "unset"
            if existing.credit_limit_minor is None
            else format_minor(existing.credit_limit_minor)
        )
        lines.append(
            f"{existing.name!r} credit limit is {current}, not "
            f"{format_minor(spec.credit_limit_minor)} — left alone"
        )
    return lines


async def seed(
    session: AsyncSession,
    *,
    household_name: str,
    telegram_user_id: int,
    display_name: str,
    account_specs: list[AccountSpec],
) -> list[str]:
    """Do the work and return a line per action, for the operator to read."""
    log: list[str] = []

    # Needs no database and no household, so it happens first.
    _reject_duplicate_names(account_specs)

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

    # Before the first account, so a typo in `billing=` cannot leave one card
    # created and unlinked. Raising here rolls the whole transaction back,
    # household and member included.
    await _resolve_billing_accounts(
        session, household_id=household_id, account_specs=account_specs
    )

    # Two passes: a card's billing account may be named later on the command
    # line than the card itself.
    for spec in account_specs:
        existing = await _find_account(
            session, household_id=household_id, name=spec.name
        )
        if existing is not None:
            log.append(f"account {spec.name!r} already exists")
            log.extend(_report_drift(existing, spec))
            continue
        session.add(
            Account(
                household_id=household_id,
                name=spec.name,
                type=spec.type,
                opening_balance_minor=spec.opening_balance_minor or 0,
                credit_limit_minor=spec.credit_limit_minor,
            )
        )
        log.append(f"created account {_describe(spec)}")
    await session.flush()

    for spec in account_specs:
        billing_name = spec.billing_account_name
        if not billing_name:
            continue
        card = await _find_account(session, household_id=household_id, name=spec.name)
        billing = await _find_account(
            session, household_id=household_id, name=billing_name
        )
        if card is None or billing is None:
            log.append(
                f"could not link {spec.name!r} to billing account "
                f"{billing_name!r} — one of them is missing"
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
                f"{spec.name!r} already settles from {current_name!r}, "
                f"not {billing_name!r} — left alone"
            )
            continue
        card.billing_account_id = billing.id
        log.append(f"{spec.name!r} settles from {billing_name!r}")

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
        metavar="NAME:TYPE[:opening=P][:limit=P][:billing=NAME]",
        help=(
            "repeatable. Amounts are in pesos: Wallet:cash:opening=1500, "
            "Visa:credit_card:opening=-3000:limit=50000:billing=BPI. A card "
            "needs both limit= and billing=."
        ),
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
    except SeedError as error:
        # `session_scope` has already rolled back. This is a bad argument, so
        # it reads like one rather than like a crash.
        print(error)
        print("Aborted. Nothing was written.")
        return 1
    finally:
        await engine.dispose()

    for line in log:
        print(line)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
