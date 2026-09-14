"""``/admin_rate_stats`` — developer-only throttling-bucket snapshot.

The legacy ``/rate_stats`` surfaces the in-memory rate-limiter table so
the operator can answer "is throttling firing, and against whom?"
without leaving Telegram. We keep the same intent but read from the
new aiogram :class:`ThrottlingMiddleware` instead of the legacy global
``_rate_limit_manager``.

Gated on ``settings.bot.is_developer(user.id)``; non-developers get
``return None`` (no error message), matching the silent-drop posture
of :mod:`handlers.admin.status` — the *existence* of the command must
not be a side-channel for enumerating dev IDs. (That posture is a
deliberate divergence from legacy, which replies; the reasoning is
spelled out in ``handlers.admin.status`` and not repeated here.)

PRIVATE-only router, same as ``handlers.admin.status`` and
``handlers.admin.uptime``: the snapshot prints raw user IDs of the
people currently being throttled, which is not something to render into
a shared group even when a developer is the one asking (#408).

Why a separate command rather than a section inside ``/admin_status``?

* ``/admin_status`` is a wide health board glanced at during incidents;
  cramming a user table into it makes both signals harder to read.
* The throttle snapshot is heavier to read (sorts the bucket dict) and
  fundamentally noisier (changes every event) — operators want it on
  demand, not on every status check.
* Splitting also lets the two commands evolve independently: a future
  ``/admin_rate_stats`` could grow per-bucket drill-down without
  bloating the health card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.middlewares.throttling import (
        ThrottlingMiddleware,
        ThrottlingSnapshot,
    )


log = logger.bind(component="handlers.admin.rate_stats")


def _render(snap: ThrottlingSnapshot, *, enabled: bool) -> str:
    """Format the snapshot for chat display.

    HTML output (matches the bot's default parse mode set in
    :mod:`di.providers`). User IDs render as raw integers — they're
    not PII the bot itself owns (the operator already sees them in
    logs) and a clickable mention would require an extra Telegram
    API call per row just to resolve the username.
    """
    header = "📊 <b>Rate-limit snapshot</b>\n"
    state = "on" if enabled else "off"
    config_line = (
        f"• state: {state}\n"
        f"• capacity: <code>{snap.capacity}</code>\n"
        f"• refill: <code>{snap.refill_per_second}</code>/s\n"
        f"• tracked users: <code>{snap.tracked_users}</code>"
    )
    if not snap.top_pressured:
        # Distinct from "throttling off" — tracker is enabled but no
        # user has been seen since startup. Operator should see this
        # rather than a missing section that looks like a render bug.
        body = "\n\n<i>no users tracked yet</i>"
    else:
        lines = "\n".join(
            # Two decimals — bucket math is continuous, but operators
            # don't need more precision than "near empty vs half full".
            f"  {idx}. <code>{uid}</code> — tokens: <code>{tokens:.2f}</code>"
            for idx, (uid, tokens) in enumerate(snap.top_pressured, start=1)
        )
        body = f"\n\n<b>Most pressured (lowest tokens):</b>\n{lines}"
    return header + config_line + body


async def handle_admin_rate_stats(
    message: Message,
    settings: Settings,
    throttle: ThrottlingMiddleware,
) -> None:
    """Render the snapshot card iff the caller is a recognised developer.

    The gate is defence-in-depth — middlewares should never deliver
    this to a non-dev, but the handler must not depend on the
    middleware being present (e.g. for tests bypassing the dispatcher
    setup or a future refactor that relocates filters).
    """
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_rate_stats; silently dropped"
        )
        return

    snap = throttle.snapshot()
    text_out = _render(snap, enabled=settings.throttling.enabled)
    await message.answer(text_out)
    log.bind(user_id=user.id, tracked=snap.tracked_users).info("/admin_rate_stats rendered")


def build_router(settings: Settings, throttle: ThrottlingMiddleware) -> Router:
    """``settings`` + ``throttle`` are app-scoped — captured by closure
    rather than injected per-update so the handler signature stays
    free of DI noise. Tests build a router with stubs and call
    ``feed_update`` directly.
    """
    router = Router(name="admin.rate_stats")
    # Chat-type filter on top of the handler's dev gate — see the module
    # docstring for why the two answer different questions (#408).
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_rate_stats(message, settings, throttle)

    router.message.register(_entry, Command("admin_rate_stats", ignore_case=True))
    return router
