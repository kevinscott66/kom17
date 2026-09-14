"""Local-dev run mode: aiogram long polling, no FastAPI.

Coexistence warning — do NOT run this against the same bot token while
the legacy ``main.py`` webhook is live in production. Telegram allows
exactly one delivery target per token; ``start_polling`` would yank
updates away from the legacy process.
"""

from __future__ import annotations

from loguru import logger

from telegram_invite_bot.app import build_app


async def run() -> None:
    application = await build_app()
    logger.bind(component="runner.polling").info(
        "starting polling (env={env})", env=application.settings.app_env.value
    )
    # Spawn background tasks (Stage 35: FSM timeout sweeper) BEFORE
    # start_polling. start_polling has its own startup hooks but those
    # fire only in polling mode — symmetry with the webhook lifespan
    # is achieved by owning the spawn here instead of via
    # ``dispatcher.startup()``. ``application.close`` is the single
    # tear-down path that also cancels these tasks.
    #
    # #1574: the spawn belongs INSIDE the ``try``, not before it.
    # ``start_background`` is not atomic — it registers the FSM
    # sweeper first and only then does a lazy import to build the
    # economy-cleanup job, so a failure in the second half leaves the
    # first half running. Raised from outside the ``try`` that owned
    # ``application.close()``, that partial spawn leaked the sweeper
    # task, the dispatcher storage, the bot session and all five
    # engines. ``webhook/server.py`` has always put the same call
    # inside its teardown ``try`` and names this exact hazard.
    try:
        await application.start_background()
        await application.dispatcher.start_polling(application.bot, handle_signals=True)
    finally:
        await application.close()
