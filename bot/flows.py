"""What each command actually does, with no `telegram` import anywhere in it.

Every function here takes a session, an `Actor` and plain arguments, and
returns a `Reply`: text, an optional keyboard as data, and an optional toast.
`handlers.py` does the translation to and from PTB objects.

The split is not decoration. It means the whole behaviour of the bot — the
parse, the pending row, the claim, the ledger write, every rejection — is
testable against a real Postgres with no bot token, no network and no fake
`Update` objects. What is left in `handlers.py` is too thin to hide a bug.

Nothing here computes with money. Amounts arrive as centavos from the parser,
travel as centavos, and are handed to `core.ledger` as centavos. The only thing
done to them is `bot.formatting.format_minor`, which turns one into characters.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from bot import callbacks, keyboards
from bot.auth import Actor
from bot.formatting import (
    KIND_LABELS,
    account_label,
    esc,
    format_date,
    format_datetime,
    format_minor,
    to_manila,
)
from bot.keyboards import Keyboard
from core import accounts as core_accounts
from core import balances, ledger, pending
from core.errors import (
    AccountNotFoundError,
    CardHasNoBillingAccountError,
    EntryAlreadyVoidedError,
    EntryNotFoundError,
    LedgerError,
    NotACreditCardError,
    SameAccountTransferError,
)
from core.models import Entry
from core.parser import ParsedEntry, ParseError, parse
from core.periods import manila_today, to_utc

# --- replies ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    """What the adapter should put on screen.

    `edit` asks for the originating message to be rewritten rather than
    answered with a new one — a keyboard that has been used should stop
    looking like a keyboard.
    """

    text: str
    keyboard: Keyboard | None = None
    toast: str | None = None
    edit: bool = False


# --- copy -------------------------------------------------------------------

HELP = (
    "<b>ChocoFin</b>\n"
    "\n"
    "Send an amount and what it was for:\n"
    "<code>120 coffee</code>\n"
    "<code>85.50 jeep #commute</code>\n"
    "<code>1200 groceries @yesterday</code>\n"
    "\n"
    "<b>Commands</b>\n"
    "/expense <i>amount note</i> — same as sending it plain\n"
    "/income <i>amount note</i> — money in\n"
    "/transfer <i>amount</i> — move money between your accounts\n"
    "/pay <i>amount</i> — settle a credit card\n"
    "/balances — every account, and net worth\n"
    "/last — the ten most recent entries\n"
    "/void — undo the last entry, or /void <i>id</i>\n"
    "\n"
    "<b>Extras</b>\n"
    "<code>@today</code> <code>@yesterday</code> <code>@2026-08-01</code> "
    "to date it, <code>#tag</code> to tag it."
)

ALREADY_DONE = "Already recorded."
EXPIRED = "That one expired. Send it again."
GONE = "That's no longer waiting for an answer."
CANCELLED = "Cancelled. Nothing was recorded."
NO_ACCOUNTS = (
    "This household has no active accounts yet, so there is nowhere to put "
    "the money. Add one first."
)
TRANSFER_USAGE = "How much? Try <code>/transfer 500</code>."
PAY_USAGE = "How much? Try <code>/pay 3000</code>."
NOTHING_TO_VOID = "There's nothing to void."
BAD_ENTRY_ID = "That doesn't look like an entry id. Try <code>/void 412</code>."
NO_CARDS = "There are no credit cards in this household to settle."


# --- shared helpers ---------------------------------------------------------


def _occurred_at(parsed: ParsedEntry, now: dt.datetime) -> dt.datetime:
    """When the money moved, as a UTC instant.

    An undated message happened just now. A dated one lands on Manila midnight
    of that day: the user said which DAY, not which second, and midnight is the
    only instant that does not invent a time they did not give. It is also
    stable — the same `@2026-08-01` always resolves to the same instant, so
    period boundaries cannot wobble.
    """
    if parsed.occurred_on is None:
        return now
    return to_utc(parsed.occurred_on)


def _strip_command(raw: str) -> str:
    """Drop a leading /command token, keeping the rest of the message.

    `/transfer 500 gcash top-up` still holds a perfectly good amount, note,
    date and tags. `core.parser.parse` refuses the word "transfer" outright —
    correctly, since text alone cannot name two accounts — so the command is
    removed and the remainder goes through the parser as ordinary text. Every
    amount rejection still comes from the one parser, rather than a second
    implementation growing here.
    """
    head, _, rest = raw.strip().partition(" ")
    if head.startswith("/"):
        return rest.strip()
    return raw.strip()


def _describe(parsed_kind: str, amount_minor: int, note: str | None) -> str:
    label = KIND_LABELS.get(parsed_kind, parsed_kind.title())
    body = f"<b>{label} {format_minor(amount_minor)}</b>"
    if note:
        body += f"\n{esc(note)}"
    return body


def _dated(occurred_at: dt.datetime, now: dt.datetime) -> str:
    """A date line, shown only when the entry is not for today.

    "Today" is a Manila question. Comparing the two UTC dates would call
    anything logged after 08:00 Manila "yesterday" for the last eight hours of
    every day.
    """
    if to_manila(occurred_at).date() == to_manila(now).date():
        return ""
    return f"\n<i>{format_date(occurred_at)}</i>"


async def _account_choice(
    session: AsyncSession,
    actor: Actor,
    *,
    pending_id: int,
    build_data: keyboards.DataBuilder,
    prompt: str,
    header: str,
    types: Sequence[str] | None = None,
    exclude: Sequence[int] = (),
    empty_message: str = NO_ACCOUNTS,
    edit: bool = False,
    show_all: bool = False,
) -> Reply:
    """Render an account keyboard, MRU-ordered, or explain that there is none."""
    available = await core_accounts.recent_accounts(
        session,
        household_id=actor.household_id,
        member_id=actor.member_id,
        types=types,
        exclude=exclude,
    )
    if not available:
        return Reply(text=empty_message, edit=edit)

    if show_all:
        keyboard = keyboards.account_keyboard(
            available, pending_id=pending_id, build_data=build_data
        )
    else:
        keyboard = keyboards.mru_keyboard(
            available, pending_id=pending_id, build_data=build_data
        )

    return Reply(text=f"{header}\n\n{prompt}", keyboard=keyboard, edit=edit)


# --- starting an entry ------------------------------------------------------


async def start_entry(
    session: AsyncSession, actor: Actor, *, raw: str, now: dt.datetime
) -> Reply:
    """`/expense`, `/income`, or a bare "120 coffee".

    All three land here because they are the same thing: the parser reads the
    leading command itself and defaults to expense when there is none.

    Writes the pending row and returns the interpretation AND the keyboard in
    ONE message, so the user confirms what was understood by the same tap that
    chooses the account.
    """
    parsed = parse(raw, today=manila_today(now))
    if isinstance(parsed, ParseError):
        return Reply(text=esc(parsed.message))

    occurred_at = _occurred_at(parsed, now)
    row = await pending.create(
        session,
        household_id=actor.household_id,
        member_id=actor.member_id,
        raw_input=raw,
        parsed_kind=parsed.kind,
        parsed_amount_minor=parsed.amount_minor,
        parsed_note=parsed.note or None,
        parsed_tags=parsed.tags,
        occurred_at=occurred_at,
        now=now,
    )

    header = _describe(parsed.kind, parsed.amount_minor, parsed.note) + _dated(
        occurred_at, now
    )
    prompt = "Which account?" if parsed.kind == "expense" else "Into which account?"
    return await _account_choice(
        session,
        actor,
        pending_id=row.id,
        build_data=callbacks.pick_account,
        prompt=prompt,
        header=header,
    )


async def start_transfer(
    session: AsyncSession, actor: Actor, *, raw: str, now: dt.datetime
) -> Reply:
    """`/transfer 500` — amount from text, both accounts from keyboards."""
    remainder = _strip_command(raw)
    if not remainder:
        return Reply(text=TRANSFER_USAGE)

    parsed = parse(remainder, today=manila_today(now))
    if isinstance(parsed, ParseError):
        return Reply(text=esc(parsed.message))

    occurred_at = _occurred_at(parsed, now)
    row = await pending.create(
        session,
        household_id=actor.household_id,
        member_id=actor.member_id,
        raw_input=raw,
        # The parser read this as an expense because that is its default; the
        # command said otherwise and the command wins.
        parsed_kind="transfer",
        parsed_amount_minor=parsed.amount_minor,
        parsed_note=parsed.note or None,
        parsed_tags=parsed.tags,
        occurred_at=occurred_at,
        now=now,
    )

    header = _describe("transfer", parsed.amount_minor, parsed.note) + _dated(
        occurred_at, now
    )
    return await _account_choice(
        session,
        actor,
        pending_id=row.id,
        build_data=callbacks.pick_source,
        prompt="From which account?",
        header=header,
    )


async def start_settlement(
    session: AsyncSession, actor: Actor, *, raw: str, now: dt.datetime
) -> Reply:
    """`/pay 3000` — pick the card; the paying account resolves itself.

    A settlement is a transfer into the card, never an expense: the purchases
    were already expensed when they happened, and booking the payment as
    spending too would count the same money twice.

    The card is asked for first because in the normal case it is the ONLY
    question — `core.ledger.settle_card` reads the card's billing account and
    the whole thing is one tap.
    """
    remainder = _strip_command(raw)
    if not remainder:
        return Reply(text=PAY_USAGE)

    parsed = parse(remainder, today=manila_today(now))
    if isinstance(parsed, ParseError):
        return Reply(text=esc(parsed.message))

    occurred_at = _occurred_at(parsed, now)
    row = await pending.create(
        session,
        household_id=actor.household_id,
        member_id=actor.member_id,
        raw_input=raw,
        parsed_kind="transfer",
        parsed_amount_minor=parsed.amount_minor,
        parsed_note=parsed.note or None,
        parsed_tags=parsed.tags,
        occurred_at=occurred_at,
        now=now,
    )

    header = f"<b>Settlement {format_minor(parsed.amount_minor)}</b>" + _dated(
        occurred_at, now
    )
    return await _account_choice(
        session,
        actor,
        pending_id=row.id,
        build_data=callbacks.pick_destination,
        prompt="Which card?",
        header=header,
        types=("credit_card",),
        empty_message=NO_CARDS,
    )


# --- committing -------------------------------------------------------------


def _confirmation(entry: Entry, account_name: str, now: dt.datetime) -> str:
    label = KIND_LABELS.get(entry.kind, entry.kind.title())
    lines = [f"<b>{label} {format_minor(entry.amount_minor)}</b>"]
    if entry.note:
        lines.append(esc(entry.note))
    lines.append(f"<i>{esc(account_name)} · {format_datetime(entry.occurred_at)}</i>")
    lines.append(f"<code>#{entry.id}</code>")
    return "\n".join(lines)


async def commit_account_choice(
    session: AsyncSession,
    actor: Actor,
    *,
    pending_id: int,
    account_id: int,
    now: dt.datetime,
) -> Reply:
    """A tap on the account keyboard: write the expense or income.

    `claim` and the ledger write run in the transaction the auth decorator
    opened, so the pending row cannot disappear without an entry appearing and
    an entry cannot appear twice. A second tap finds nothing to claim and says
    so, which is also the answer for a button found in old scrollback.
    """
    claimed = await pending.claim(
        session, household_id=actor.household_id, pending_id=pending_id
    )
    if claimed is None:
        return Reply(text=ALREADY_DONE, toast=ALREADY_DONE)
    if claimed.is_expired(now):
        return Reply(text=EXPIRED, toast=EXPIRED, edit=True)

    if claimed.parsed_kind == "expense":
        write = ledger.create_expense
    elif claimed.parsed_kind == "income":
        write = ledger.create_income
    else:
        # A transfer reached the single-leg button. Our own keyboards never do
        # this, so it is a hand-made payload; refuse rather than guess which
        # half of the transfer was meant.
        return Reply(text=GONE, toast=GONE, edit=True)

    try:
        entry = await write(
            session,
            household_id=actor.household_id,
            member_id=actor.member_id,
            account_id=account_id,
            amount_minor=claimed.parsed_amount_minor,
            occurred_at=claimed.occurred_at,
            category_id=claimed.parsed_category_id,
            note=claimed.parsed_note,
            source="telegram",
            raw_input=claimed.raw_input,
            tags=claimed.parsed_tags,
        )
    except LedgerError as exc:
        return Reply(text=esc(str(exc)), toast="Rejected.", edit=True)

    account = await core_accounts.get_account(
        session, household_id=actor.household_id, account_id=account_id
    )
    name = account.name if account else str(account_id)
    return Reply(text=_confirmation(entry, name, now), toast="Recorded", edit=True)


async def pick_transfer_source(
    session: AsyncSession,
    actor: Actor,
    *,
    pending_id: int,
    account_id: int,
    now: dt.datetime,
) -> Reply:
    """First half of a transfer: remember where the money leaves from.

    The choice goes onto the pending ROW, not into this process. The user can
    close Telegram, the service can be redeployed, and the second keyboard
    still commits against the account picked before all that.
    """
    row = await pending.get(
        session, household_id=actor.household_id, pending_id=pending_id
    )
    if row is None:
        return Reply(text=GONE, toast=GONE, edit=True)
    if now >= row.expires_at:
        return Reply(text=EXPIRED, toast=EXPIRED, edit=True)

    source = await core_accounts.get_account(
        session, household_id=actor.household_id, account_id=account_id
    )
    if source is None:
        raise AccountNotFoundError(
            f"account {account_id} is not in household {actor.household_id}"
        )

    await pending.set_source_account(
        session,
        household_id=actor.household_id,
        pending_id=pending_id,
        account_id=account_id,
    )

    header = (
        f"<b>Transfer {format_minor(row.parsed_amount_minor)}</b>\n"
        f"From {esc(account_label(source.name, source.type))}"
    )
    return await _account_choice(
        session,
        actor,
        pending_id=pending_id,
        build_data=callbacks.pick_destination,
        prompt="To which account?",
        header=header,
        # A transfer to itself is rejected by core anyway; not offering it is
        # simply a keyboard that cannot ask a question with no good answer.
        exclude=(account_id,),
        edit=True,
    )


async def commit_destination(
    session: AsyncSession,
    actor: Actor,
    *,
    pending_id: int,
    account_id: int,
    now: dt.datetime,
) -> Reply:
    """A tap on a destination or card button. Writes the transfer.

    Two shapes arrive here and the pending row tells them apart with no rule of
    this module's own:

    * a source is already stored — the user has picked both ends, so this is
      `create_transfer` between them. A settlement whose payer was chosen by
      hand is exactly that, and produces exactly the same row.
    * no source — this is `/pay`'s first tap, and `settle_card` resolves the
      payer from the card's billing account. If the card names none, it says
      so instead of guessing, and the user is asked who is paying.
    """
    claimed = await pending.claim(
        session, household_id=actor.household_id, pending_id=pending_id
    )
    if claimed is None:
        return Reply(text=ALREADY_DONE, toast=ALREADY_DONE)
    if claimed.is_expired(now):
        return Reply(text=EXPIRED, toast=EXPIRED, edit=True)

    try:
        if claimed.source_account_id is not None:
            entry = await ledger.create_transfer(
                session,
                household_id=actor.household_id,
                member_id=actor.member_id,
                source_account_id=claimed.source_account_id,
                destination_account_id=account_id,
                amount_minor=claimed.parsed_amount_minor,
                occurred_at=claimed.occurred_at,
                note=claimed.parsed_note,
                source="telegram",
                raw_input=claimed.raw_input,
                tags=claimed.parsed_tags,
            )
        else:
            entry = await ledger.settle_card(
                session,
                household_id=actor.household_id,
                member_id=actor.member_id,
                card_id=account_id,
                amount_minor=claimed.parsed_amount_minor,
                occurred_at=claimed.occurred_at,
                note=claimed.parsed_note,
                source="telegram",
                raw_input=claimed.raw_input,
            )
    except CardHasNoBillingAccountError:
        # Recoverable, and the only branch that puts the row back. Re-creating
        # it rather than rolling back keeps the amount and the date the user
        # already confirmed, and asks the one question that is actually
        # missing.
        return await _reopen_for_payer(session, actor, claimed=claimed, now=now)
    except (NotACreditCardError, SameAccountTransferError, LedgerError) as exc:
        return Reply(text=esc(str(exc)), toast="Rejected.", edit=True)

    route = await _transfer_route(session, actor, entry=entry)
    lines = [
        f"<b>Transfer {format_minor(entry.amount_minor)}</b>",
        esc(route),
        f"<i>{format_datetime(entry.occurred_at)}</i>",
        f"<code>#{entry.id}</code>",
    ]
    return Reply(text="\n".join(lines), toast="Recorded", edit=True)


async def _transfer_route(session: AsyncSession, actor: Actor, *, entry: Entry) -> str:
    """ "Payer → Card", read back from the legs the ledger actually wrote.

    Not re-derived from what this module passed in. On the `/pay` path the
    payer was chosen by `core.ledger.settle_card`, and asking the entry is the
    only way to name it without repeating that rule here.
    """
    legs = await ledger.list_legs(
        session, household_id=actor.household_id, entry_id=entry.id
    )
    names: dict[str, str] = {}
    for leg in legs:
        account = await core_accounts.get_account(
            session, household_id=actor.household_id, account_id=leg.account_id
        )
        names[leg.leg_role] = account.name if account else str(leg.account_id)
    return f"{names.get('source', '?')} → {names.get('destination', '?')}"


async def _reopen_for_payer(
    session: AsyncSession,
    actor: Actor,
    *,
    claimed: pending.ClaimedPending,
    now: dt.datetime,
) -> Reply:
    """The card names no billing account, so ask who is paying.

    The claimed row was deleted by the claim; a fresh one carries the same
    amount, note and date forward so the user is not asked to retype anything.
    """
    row = await pending.create(
        session,
        household_id=actor.household_id,
        member_id=actor.member_id,
        raw_input=claimed.raw_input,
        parsed_kind="transfer",
        parsed_amount_minor=claimed.parsed_amount_minor,
        parsed_note=claimed.parsed_note,
        parsed_tags=claimed.parsed_tags,
        occurred_at=claimed.occurred_at,
        now=now,
    )
    header = (
        f"<b>Settlement {format_minor(claimed.parsed_amount_minor)}</b>\n"
        "That card has no billing account set."
    )
    return await _account_choice(
        session,
        actor,
        pending_id=row.id,
        build_data=callbacks.pick_source,
        prompt="Which account is paying?",
        header=header,
        edit=True,
    )


async def show_all_accounts(
    session: AsyncSession,
    actor: Actor,
    *,
    pending_id: int,
    now: dt.datetime,
) -> Reply:
    """[Other…]: the same question, with every account on screen."""
    row = await pending.get(
        session, household_id=actor.household_id, pending_id=pending_id
    )
    if row is None:
        return Reply(text=GONE, toast=GONE, edit=True)
    if now >= row.expires_at:
        return Reply(text=EXPIRED, toast=EXPIRED, edit=True)

    if row.parsed_kind != "transfer":
        build_data = callbacks.pick_account
        prompt = "Which account?"
        exclude: tuple[int, ...] = ()
        header = _describe(
            row.parsed_kind or "expense", row.parsed_amount_minor or 0, row.parsed_note
        )
    elif row.source_account_id is None:
        build_data = callbacks.pick_source
        prompt = "From which account?"
        exclude = ()
        header = f"<b>Transfer {format_minor(row.parsed_amount_minor or 0)}</b>"
    else:
        build_data = callbacks.pick_destination
        prompt = "To which account?"
        exclude = (row.source_account_id,)
        header = f"<b>Transfer {format_minor(row.parsed_amount_minor or 0)}</b>"

    return await _account_choice(
        session,
        actor,
        pending_id=pending_id,
        build_data=build_data,
        prompt=prompt,
        header=header,
        exclude=exclude,
        edit=True,
        show_all=True,
    )


async def cancel_pending(
    session: AsyncSession, actor: Actor, *, pending_id: int
) -> Reply:
    """[Cancel]: drop the pending row. Nothing was ever written."""
    removed = await pending.cancel(
        session, household_id=actor.household_id, pending_id=pending_id
    )
    text = CANCELLED if removed else ALREADY_DONE
    return Reply(text=text, toast=text, edit=True)


# --- reading ----------------------------------------------------------------


def _balance_line(balance: balances.AccountBalance) -> str:
    line = (
        f"{esc(account_label(balance.name, balance.type))}  "
        f"<b>{format_minor(balance.balance_minor)}</b>"
    )
    available = balance.available_credit_minor
    if available is not None:
        line += f"\n<i>  {format_minor(available)} available</i>"
    if balance.exclude_from_totals:
        line += "\n<i>  not in net worth</i>"
    return line


async def show_balances(session: AsyncSession, actor: Actor) -> Reply:
    """Every active account, and net worth.

    `exclude_from_totals` accounts still show their own balance — the flag
    keeps them out of the total, not out of sight.
    """
    rows = await balances.account_balances(session, household_id=actor.household_id)
    if not rows:
        return Reply(text=NO_ACCOUNTS)

    net = await balances.net_worth_minor(session, household_id=actor.household_id)
    body = "\n".join(_balance_line(row) for row in rows)
    return Reply(text=f"{body}\n\n<b>Net worth {format_minor(net)}</b>")


async def show_last(session: AsyncSession, actor: Actor, *, limit: int = 10) -> Reply:
    """The most recent entries, newest first."""
    entries = await ledger.list_entries(
        session, household_id=actor.household_id, newest_first=True, limit=limit
    )
    if not entries:
        return Reply(text="Nothing recorded yet.")

    lines = []
    for entry in entries:
        label = KIND_LABELS.get(entry.kind, entry.kind)
        note = f" · {esc(entry.note)}" if entry.note else ""
        lines.append(
            f"<code>#{entry.id}</code>  {label} "
            f"<b>{format_minor(entry.amount_minor)}</b>{note}\n"
            f"<i>  {format_datetime(entry.occurred_at)}</i>"
        )
    return Reply(text="\n".join(lines))


# --- voiding ----------------------------------------------------------------


async def start_void(
    session: AsyncSession, actor: Actor, *, argument: str | None = None
) -> Reply:
    """`/void` or `/void <id>` — show the target and ask for confirmation.

    Voiding does not delete anything; the entry stays readable forever. It does
    change every total that entry was part of, which is why it asks first.
    """
    if argument:
        if not argument.isdigit():
            return Reply(text=BAD_ENTRY_ID)
        try:
            entry = await ledger.get_entry(
                session, household_id=actor.household_id, entry_id=int(argument)
            )
        except EntryNotFoundError as exc:
            return Reply(text=esc(str(exc)))
    else:
        recent = await ledger.list_entries(
            session, household_id=actor.household_id, newest_first=True, limit=1
        )
        if not recent:
            return Reply(text=NOTHING_TO_VOID)
        entry = recent[0]

    if entry.voided_at is not None:
        return Reply(text=f"Entry #{entry.id} is already voided.")

    label = KIND_LABELS.get(entry.kind, entry.kind)
    note = f"\n{esc(entry.note)}" if entry.note else ""
    text = (
        f"{label} <b>{format_minor(entry.amount_minor)}</b>{note}\n"
        f"<i>{format_datetime(entry.occurred_at)}</i>\n"
        f"<code>#{entry.id}</code>\n\nVoid this?"
    )
    return Reply(text=text, keyboard=keyboards.void_keyboard(entry.id))


async def confirm_void(session: AsyncSession, actor: Actor, *, entry_id: int) -> Reply:
    """A tap on [Void it]."""
    try:
        entry = await ledger.void_entry(
            session,
            household_id=actor.household_id,
            entry_id=entry_id,
            voided_by=actor.member_id,
        )
    except EntryAlreadyVoidedError:
        text = f"Entry #{entry_id} was already voided."
        return Reply(text=text, toast=ALREADY_DONE, edit=True)
    except EntryNotFoundError as exc:
        return Reply(text=esc(str(exc)), toast="Not found.", edit=True)

    return Reply(
        text=(
            f"<s>{KIND_LABELS.get(entry.kind, entry.kind)} "
            f"{format_minor(entry.amount_minor)}</s>\nVoided."
        ),
        toast="Voided",
        edit=True,
    )


def dismiss() -> Reply:
    """A tap on a [Cancel] that never had a pending row behind it."""
    return Reply(text=CANCELLED, toast=CANCELLED, edit=True)


def help_reply() -> Reply:
    return Reply(text=HELP)


def start_reply(actor: Actor) -> Reply:
    return Reply(text=f"Hello {esc(actor.display_name)}.\n\n{HELP}")


__all__ = [
    "Reply",
    "cancel_pending",
    "commit_account_choice",
    "commit_destination",
    "confirm_void",
    "dismiss",
    "help_reply",
    "pick_transfer_source",
    "show_all_accounts",
    "show_balances",
    "show_last",
    "start_entry",
    "start_reply",
    "start_settlement",
    "start_transfer",
    "start_void",
]
