"""THE authorisation decorator. There is exactly one, and this is it.

CLAUDE.md forbids an inline user-id check inside a handler, for a reason worth
restating: an authorisation rule that appears in twelve places is a rule with
twelve chances to be forgotten in the thirteenth. Every handler in this bot is
wrapped by `@authorised`, and no handler anywhere reads
`update.effective_user.id`.

Authorisation is MEMBERSHIP, not an allowlist. A Telegram user is authorised
because a row in `members` says so, which is also what supplies the
`household_id` that every core call needs. There is no env var of permitted
ids to drift out of sync with the database.

The decorator does three other jobs, each of them a thing a handler could
otherwise get wrong once:

* It opens the session and commits or rolls back around the whole handler, so
  a `claim` and the ledger write it authorises land in ONE transaction.
* It answers every callback query EXACTLY once, on every path.
* It keeps the household boundary intact: the `Actor` it injects is the only
  source of `household_id` in the bot, so no handler can be handed one from a
  button and query someone else's ledger with it.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from core.db import session_scope
from core.members import get_member_by_telegram_id

logger = logging.getLogger(__name__)

type BotContext = ContextTypes.DEFAULT_TYPE

SESSIONMAKER_KEY = "sessionmaker"

UNKNOWN_USER = (
    "I don't know you, so I won't touch this household's books. "
    "Ask an owner to add you."
)
INACTIVE_MEMBER = "Your membership is inactive, so I can't record anything for you."
INTERNAL_ERROR = "Something went wrong. Nothing was recorded."


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is asking, and which household they may touch."""

    member_id: int
    household_id: int
    display_name: str


type Handler = Callable[
    [Update, BotContext, Actor, AsyncSession], Awaitable[str | None]
]
"""A handler returns an optional toast.

For a callback query the string becomes the `answer_callback_query` text; for a
message it is ignored. Handlers never call `answer()` themselves — see below.
"""


def _sessionmaker(context: BotContext) -> async_sessionmaker[AsyncSession]:
    factory = context.bot_data.get(SESSIONMAKER_KEY)
    if factory is None:  # pragma: no cover - wiring bug, not a runtime path
        raise RuntimeError(
            "no sessionmaker in bot_data; bot.__main__ must set it in post_init"
        )
    return factory


async def _reject(update: Update, message: str) -> str:
    """Tell a caller no, in whichever way suits the update, and return the toast."""
    if update.callback_query is None and update.effective_message is not None:
        with contextlib.suppress(TelegramError):
            await update.effective_message.reply_text(message)
    return message


def authorised(handler: Handler) -> Callable[[Update, BotContext], Awaitable[None]]:
    """Resolve the caller, open a transaction, and answer the callback once."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: BotContext) -> None:
        query = update.callback_query
        toast: str | None = None
        try:
            user = update.effective_user
            if user is None:
                # A channel post or an edit with no author. Nothing to authorise.
                toast = await _reject(update, UNKNOWN_USER)
                return

            async with session_scope(_sessionmaker(context)) as session:
                member = await get_member_by_telegram_id(
                    session, telegram_user_id=user.id
                )
                if member is None:
                    toast = await _reject(update, UNKNOWN_USER)
                    return
                if not member.is_active:
                    toast = await _reject(update, INACTIVE_MEMBER)
                    return

                actor = Actor(
                    member_id=member.id,
                    household_id=member.household_id,
                    display_name=member.display_name,
                )
                toast = await handler(update, context, actor, session)
        except Exception:
            # The transaction has already rolled back by the time we get here,
            # so the user is told nothing was recorded and that is true.
            toast = INTERNAL_ERROR
            logger.exception("handler %s failed", getattr(handler, "__name__", handler))
            raise
        finally:
            # EXACTLY once, on every path: success, rejection, and exception.
            #
            # Not answering leaves the client spinning on the button forever.
            # Answering twice is an API error. Both are avoided by there being
            # one call site, in a `finally`, that no handler can skip or repeat
            # — which is why handlers return a toast instead of sending one.
            if query is not None:
                with contextlib.suppress(TelegramError):
                    await query.answer(toast or None)

    return wrapper
