"""THE authorisation decorator, and the envelope every handler runs inside.

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
* It puts the reply on screen AFTER that transaction has committed, and answers
  every callback query EXACTLY once.
* It keeps the household boundary intact: the `Actor` it injects is the only
  source of `household_id` in the bot, so no handler can be handed one from a
  button and query someone else's ledger with it.

The ordering is the reason a handler returns a `Reply` instead of sending one.
The leg-shape trigger is DEFERRABLE INITIALLY DEFERRED, so the ledger's last
line of defence runs at COMMIT — after the handler has returned. A handler that
sent its own receipt would be announcing an entry the commit could still
refuse, and "Recorded #412" stays in the scrollback forever while the toast
that contradicts it fades in a second. So nothing here speaks to Telegram until
`session_scope` has exited: a receipt is a report of a committed fact, never a
prediction of one.

The same rule keeps a transaction from being held open across Telegram HTTP.
That was previously true only because PTB happens to dispatch updates one at a
time by default — a property of a builder call in `__main__.py`, not of
anything the ledger controls.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.formatting import PARSE_MODE
from bot.keyboards import Keyboard
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


@dataclass(frozen=True, slots=True)
class Reply:
    """What the adapter should put on screen once the transaction has closed.

    It lives here, next to the decorator that renders it, because it is half of
    the handler protocol: `Handler` says what a handler is given, and this says
    what it hands back. `bot.flows` re-exports it and builds every one of them.

    Everything in it is already rendered — `text` is a finished string and
    `keyboard` is plain data — so nothing here touches the ORM. That is what
    makes a `Reply` safe to send after its session is gone.

    `edit` asks for the originating message to be rewritten rather than
    answered with a new one — a keyboard that has been used should stop
    looking like a keyboard.
    """

    text: str
    keyboard: Keyboard | None = None
    toast: str | None = None
    edit: bool = False


type Handler = Callable[
    [Update, BotContext, Actor, AsyncSession], Awaitable[Reply | None]
]
"""A handler returns what to say, and says none of it itself.

Handlers do not send messages and do not call `answer()`. Both happen below,
once, on the far side of the commit — see the module docstring.
"""


def _sessionmaker(context: BotContext) -> async_sessionmaker[AsyncSession]:
    factory = context.bot_data.get(SESSIONMAKER_KEY)
    if factory is None:  # pragma: no cover - wiring bug, not a runtime path
        raise RuntimeError(
            "no sessionmaker in bot_data; bot.__main__ must set it in post_init"
        )
    return factory


def _markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    return (
        None
        if keyboard is None
        else InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(button.label, callback_data=button.data)
                    for button in row
                ]
                for row in keyboard
            ]
        )
    )


async def _render(update: Update, reply: Reply) -> None:
    """Put a `Reply` on screen.

    Reached only after `session_scope` has exited, so whatever this sends is
    already true: the money is committed, or it is gone and this is the error.
    There is no third state left for a message to be wrong about.

    It is also reached AFTER the callback has been answered — see the wrapper.
    Nothing here produces the toast, so nothing here has to run first.
    """
    markup = _markup(reply.keyboard)
    query = update.callback_query

    if reply.edit and query is not None and query.message is not None:
        # A used keyboard should stop looking like one. The edit failing is not
        # worth surfacing: the entry is committed by this point, and "message is
        # not modified" or "message to edit not found" changes nothing about
        # that.
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                reply.text, parse_mode=PARSE_MODE, reply_markup=markup
            )
        return

    message = update.effective_message
    if message is not None:
        with contextlib.suppress(TelegramError):
            await message.reply_text(
                reply.text, parse_mode=PARSE_MODE, reply_markup=markup
            )


def _reject(update: Update, message: str) -> tuple[Reply | None, str]:
    """Tell a caller no, in whichever way suits the update.

    Nothing is sent from here. This runs with the session still open, and the
    wrapper renders whatever comes back once it is not.

    A button gets the toast and nothing else, which is why the `Reply` is None
    on that path. Posting into the chat would put a bot the caller may not use
    in front of everyone else in the room, and the toast already tells the one
    person who asked.
    """
    if update.callback_query is not None:
        return None, message
    return Reply(text=message, toast=message), message


def authorised(handler: Handler) -> Callable[[Update, BotContext], Awaitable[None]]:
    """Resolve the caller, run the handler in a transaction, then reply."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: BotContext) -> None:
        query = update.callback_query
        reply: Reply | None = None
        toast: str | None = None
        try:
            user = update.effective_user
            if user is None:
                # A channel post or an edit with no author. Nothing to authorise.
                reply, toast = _reject(update, UNKNOWN_USER)
                return

            async with session_scope(_sessionmaker(context)) as session:
                member = await get_member_by_telegram_id(
                    session, telegram_user_id=user.id
                )
                if member is None:
                    reply, toast = _reject(update, UNKNOWN_USER)
                    return
                if not member.is_active:
                    reply, toast = _reject(update, INACTIVE_MEMBER)
                    return

                actor = Actor(
                    member_id=member.id,
                    household_id=member.household_id,
                    display_name=member.display_name,
                )
                reply = await handler(update, context, actor, session)
            # COMMIT happened on the line above, when the scope exited. Only now
            # is `reply` a statement about the database rather than about a
            # transaction that could still be refused.
        except Exception:
            # A commit the leg trigger rejected arrives here too, which is the
            # case this shape exists for: the handler's receipt names an entry
            # that no longer exists, so it is replaced rather than sent. The
            # rollback has already happened, so the message below is true.
            #
            # Deliberately not an edit. The rollback put the pending row back,
            # so the keyboard on screen still works; editing it away would
            # strand someone mid-entry over a failure they could just retry.
            reply = Reply(text=INTERNAL_ERROR, toast=INTERNAL_ERROR)
            toast = INTERNAL_ERROR
            # The name of the handler, and no traceback: this re-raises, and
            # `bot.__main__._on_error` logs the exception with its stack. Both
            # logging the traceback printed every handler failure twice, which
            # reads in the log as two separate faults and doubles the noise of
            # the one thing anyone greps for.
            logger.error("handler %s failed", getattr(handler, "__name__", handler))
            raise
        finally:
            # The only place in `bot/` that tells Telegram how a handler went,
            # and it runs outside the transaction on every path: success,
            # rejection, and exception.
            if reply is not None:
                toast = reply.toast
            # EXACTLY once, on every path. Not answering leaves the client
            # spinning on the button forever, and answering twice is an API
            # error. Both are avoided by there being one call site, in a
            # `finally`, that no handler can skip or repeat.
            #
            # BEFORE `_render`, not after. A callback query is only valid for
            # about 15 seconds, and `_render` is a Telegram round-trip — a slow
            # send or a retry spends that budget and the answer then lands on an
            # expired query, where the `suppress` below swallows it and the user
            # is left holding a spinner on the path where everything worked.
            # The toast is `reply.toast`, known without sending anything, so
            # there is no reason for this to wait on the message.
            if query is not None:
                with contextlib.suppress(TelegramError):
                    await query.answer(toast or None)
            # Still after the commit, which is the ordering the module docstring
            # is about. Both of these are post-COMMIT; only their order changed.
            if reply is not None:
                await _render(update, reply)

    return wrapper
