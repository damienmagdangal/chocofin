"""`python -m bot` — long polling, no webhooks, no inbound ports.

Long polling is a deployment decision, not a preference: this runs on a
self-hosted box behind a home connection, and a webhook would need an inbound
port, a public name and a certificate. Polling needs none of those, so there is
nothing listening for anyone to find.

The database engine is created in `post_init` and disposed in `post_shutdown`,
which is where PTB runs the two around the polling loop. Building the engine at
import time would open a pool before there is an event loop to attach it to.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

from bot.auth import SESSIONMAKER_KEY
from bot.handlers import build_handlers
from core.config import get_telegram_token
from core.db import make_engine, make_sessionmaker

logger = logging.getLogger(__name__)

ENGINE_KEY = "engine"


async def _post_init(application: Application) -> None:
    engine = make_engine()
    application.bot_data[ENGINE_KEY] = engine
    application.bot_data[SESSIONMAKER_KEY] = make_sessionmaker(engine)
    logger.info("database engine ready")


async def _post_shutdown(application: Application) -> None:
    engine = application.bot_data.get(ENGINE_KEY)
    if engine is not None:
        await engine.dispose()
        logger.info("database engine disposed")


async def _on_error(update: object, context) -> None:
    """Log and move on. One bad update must not stop the poller."""
    logger.exception("update failed", exc_info=context.error)


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(get_telegram_token())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    for handler in build_handlers():
        application.add_handler(handler)
    application.add_error_handler(_on_error)
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    # httpx logs every getUpdates call at INFO, which is one line per poll
    # forever.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    application = build_application()
    application.run_polling(
        # Only what this bot acts on. Anything else is bandwidth and log noise.
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        # Deliberately NOT dropping pending updates: a message sent while the
        # service was restarting is a real expense someone typed, and silently
        # discarding it is worse than handling it late.
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
