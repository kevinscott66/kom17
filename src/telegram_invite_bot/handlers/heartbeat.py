"""``/ping`` + ``/botcheck`` — heartbeat commands (Stage 21).

Two tiny, dependency-free commands legacy uses to confirm the bot is
alive and reactive. No DB, no FSM, no external API beyond ``get_me``.
Cheap to port and they remove the most-pinged-during-debugging
commands from the legacy bridge — useful while the strangler is still
mid-flight, because they're the first thing operators try when
something feels off.

Legacy reference:

* ``/ping`` / ``/kom_ping`` — bot.py:41334. Renders three numbers:
  update-delivery lag (now - message.date), Telegram API RTT (one
  ``get_me`` call), and command-processing time.
* ``/botcheck`` / ``/alive`` / ``/kom_botcheck`` — bot.py:41366.
  Static "alive" string; ported as the ``h_botcheck`` copy key so the
  reply follows the caller's locale like every other card.

Behaviour parity:

* Works in **any** chat type. Legacy gates only on
  ``ensure_user_access`` (role check, not chat-type), which the new
  pipeline doesn't model yet — we accept all chats. The role layer
  lands with the admin port (later stage); until then heartbeats are
  intentionally unguarded, same as today's legacy behaviour for non-
  banned users.
* Numbers in ``<code>...</code>`` — legacy uses backtick-Markdown but
  the bot-wide ``parse_mode=HTML`` setting (``app.py``) makes that
  parse as literal text. HTML ``<code>`` produces the same monospace
  rendering without per-message parser switching.
* ``get_me`` failure path: legacy silently shows "н/д". We do the
  same, because the alternative — letting the exception propagate —
  would make ``/ping`` itself unreliable during the very outage it's
  meant to diagnose.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t

log = logger.bind(component="handlers.heartbeat")

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message


def _delivery_lag_ms(message: Message) -> int:
    """``now - message.date`` in ms, clamped at zero.

    Negative values are possible when the worker clock drifts ahead of
    Telegram's send timestamp; clamp protects the user-visible number
    from looking nonsensical. Same shape as bot.py:41343.
    """
    if message.date is None:
        return 0
    sent_ts = message.date.timestamp()
    return max(0, int((time.time() - sent_ts) * 1000))


async def _telegram_rtt_ms(bot: Bot) -> int | None:
    """One ``get_me`` round-trip in ms. ``None`` if it raises.

    We don't differentiate timeout vs. 401 vs. network — any failure
    here means the API is unreachable enough that the user should see
    the ``h_ping_api_unavailable`` fallback. Matches legacy's bare
    ``except Exception``.
    """
    started = time.perf_counter()
    try:
        await bot.get_me()
    except Exception:  # noqa: BLE001 — see docstring
        return None
    return int((time.perf_counter() - started) * 1000)


async def handle_ping(message: Message, bot: Bot, lang: str) -> None:
    """Render the three-number heartbeat card."""
    started = time.perf_counter()
    lag_ms = _delivery_lag_ms(message)
    api_ms = await _telegram_rtt_ms(bot)
    process_ms = int((time.perf_counter() - started) * 1000)
    api_str = (
        f"<code>{api_ms} ms</code>" if api_ms is not None else t("h_ping_api_unavailable", lang)
    )
    await message.answer(t("h_ping_card", lang, lag_ms=lag_ms, api=api_str, process_ms=process_ms))
    log.bind(
        chat_id=message.chat.id if message.chat else None,
        lag_ms=lag_ms,
        api_ms=api_ms,
        process_ms=process_ms,
        lang=lang,
    ).info("/ping rendered")


async def handle_botcheck(message: Message, lang: str) -> None:
    """Static "alive" reply — no measurements, no API calls."""
    await message.answer(t("h_botcheck", lang))


def build_router() -> Router:
    """No registry needed — pure aiogram primitives, no DB session."""
    router = Router(name="heartbeat")
    router.message.register(handle_ping, Command("ping", "kom_ping", ignore_case=True))
    router.message.register(
        handle_botcheck,
        Command("botcheck", "alive", "kom_botcheck", ignore_case=True),
    )
    return router
