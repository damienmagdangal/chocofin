"""Translation between PTB objects and `bot.flows`. Nothing else lives here.

Every handler is the same four lines: read what the update carries, call one
flow, render the `Reply`. If a handler here ever grows a decision, it belongs
in `flows.py` where it can be tested.

Every handler is wrapped in `@authorised` — the ONE decorator — which supplies
the `Actor` and the session, and answers the callback query exactly once. No
handler reads a user id, opens a transaction, or calls `answer()`.
"""

from __future__ import annotations

import contextlib
import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot import flows
from bot.auth import Actor, BotContext, authorised
from bot.callbacks import Action, decode
from bot.flows import Reply
from bot.formatting import PARSE_MODE
from bot.keyboards import Keyboard

STALE_BUTTON = "That button is from an older version of me. Send it again."


def _markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if keyboard is None:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(button.label, callback_data=button.data)
                for button in row
            ]
            for row in keyboard
        ]
    )


async def _render(update: Update, reply: Reply) -> str | None:
    """Put a `Reply` on screen and hand its toast back to the decorator."""
    markup = _markup(reply.keyboard)
    query = update.callback_query

    if reply.edit and query is not None and query.message is not None:
        # A used keyboard should stop looking like one. The edit failing is not
        # worth surfacing: by this point the money is already written, and
        # "message is not modified" or "message to edit not found" changes
        # nothing about that.
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                reply.text, parse_mode=PARSE_MODE, reply_markup=markup
            )
        return reply.toast

    message = update.effective_message
    if message is not None:
        with contextlib.suppress(TelegramError):
            await message.reply_text(
                reply.text, parse_mode=PARSE_MODE, reply_markup=markup
            )
    return reply.toast


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _text(update: Update) -> str:
    message = update.effective_message
    return (message.text or "") if message is not None else ""


def _argument(update: Update) -> str | None:
    """Whatever followed a /command, as one string."""
    _, _, rest = _text(update).strip().partition(" ")
    return rest.strip() or None


# --- commands ---------------------------------------------------------------


@authorised
async def cmd_start(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    return await _render(update, flows.start_reply(actor))


@authorised
async def cmd_help(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    return await _render(update, flows.help_reply())


@authorised
async def cmd_entry(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    """`/expense`, `/income`, and bare text all parse identically."""
    reply = await flows.start_entry(session, actor, raw=_text(update), now=_now())
    return await _render(update, reply)


@authorised
async def cmd_transfer(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    reply = await flows.start_transfer(session, actor, raw=_text(update), now=_now())
    return await _render(update, reply)


@authorised
async def cmd_pay(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    reply = await flows.start_settlement(session, actor, raw=_text(update), now=_now())
    return await _render(update, reply)


@authorised
async def cmd_balances(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    return await _render(update, await flows.show_balances(session, actor))


@authorised
async def cmd_last(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    return await _render(update, await flows.show_last(session, actor))


@authorised
async def cmd_void(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    reply = await flows.start_void(session, actor, argument=_argument(update))
    return await _render(update, reply)


# --- callbacks --------------------------------------------------------------


@authorised
async def on_callback(
    update: Update, context: BotContext, actor: Actor, session: AsyncSession
) -> str | None:
    """Every inline button. One handler, so the dispatch table is visible.

    A payload that does not decode is answered politely rather than dropped: a
    silent no-op leaves the client spinning, and a raise would only reach the
    error handler. Both of those look like a broken bot to someone holding a
    phone.
    """
    query = update.callback_query
    parsed = decode(query.data if query is not None else None)
    if parsed is None:
        return await _render(update, Reply(text=STALE_BUTTON, toast=STALE_BUTTON))

    now = _now()

    match parsed.action:
        case Action.PICK_ACCOUNT if len(parsed.ids) == 2:
            reply = await flows.commit_account_choice(
                session,
                actor,
                pending_id=parsed.first,
                account_id=parsed.second,
                now=now,
            )
        case Action.PICK_SOURCE if len(parsed.ids) == 2:
            reply = await flows.pick_transfer_source(
                session,
                actor,
                pending_id=parsed.first,
                account_id=parsed.second,
                now=now,
            )
        case Action.PICK_DESTINATION if len(parsed.ids) == 2:
            reply = await flows.commit_destination(
                session,
                actor,
                pending_id=parsed.first,
                account_id=parsed.second,
                now=now,
            )
        case Action.SHOW_ALL if len(parsed.ids) == 1:
            reply = await flows.show_all_accounts(
                session, actor, pending_id=parsed.first, now=now
            )
        case Action.CANCEL_PENDING if len(parsed.ids) == 1:
            reply = await flows.cancel_pending(session, actor, pending_id=parsed.first)
        case Action.CONFIRM_VOID if len(parsed.ids) == 1:
            reply = await flows.confirm_void(session, actor, entry_id=parsed.first)
        case Action.DISMISS:
            reply = flows.dismiss()
        case _:
            # Right verb, wrong number of ids. Same treatment as an unknown
            # verb: it is not a button this version ever sent.
            reply = Reply(text=STALE_BUTTON, toast=STALE_BUTTON)

    return await _render(update, reply)


# --- registration -----------------------------------------------------------


def build_handlers() -> list:
    """Every handler, in dispatch order.

    The bare-text handler is registered LAST and filtered with `~COMMAND`, so
    an unrecognised `/something` is not silently parsed as an amount and
    rejected with a confusing "no amount found".
    """
    return [
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler(["expense", "income"], cmd_entry),
        CommandHandler("transfer", cmd_transfer),
        CommandHandler("pay", cmd_pay),
        CommandHandler("balances", cmd_balances),
        CommandHandler("last", cmd_last),
        CommandHandler("void", cmd_void),
        CallbackQueryHandler(on_callback),
        MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_entry),
    ]
