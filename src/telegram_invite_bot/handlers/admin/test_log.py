"""``/admin_test_log`` — emit a synthetic ERROR-level log line.

Legacy ``/test_logs`` (bot.py:26015) writes an ERROR-level record and
expects to see it land in the configured "log bot" channel — a custom
Telegram-based log sink legacy used before Sentry was wired in. The
new pipeline doesn't have a log-bot channel; observability goes
through loguru → Sentry (when configured) → stderr.

We keep the *intent* of the legacy command (operator can confirm the
log pipeline is alive without leaving Telegram) and re-target the
output:

* Emit one ``logger.error`` event with structured context: caller's
  user id + a wall-clock timestamp. Loguru forwards to stderr always;
  Sentry-handler attaches when ``SENTRY_DSN`` is configured.
* Reply confirming the line was emitted, and which sinks should have
  received it (so the operator knows where to look). The Sentry
  branch reports "on" / "off" identically to ``/admin_status`` —
  same DSN truthiness check.

Same silent-drop posture as the other admin commands for non-devs
(see :mod:`handlers.admin.status` docstring for the rationale).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.test_log")


def _sentry_enabled(settings: Settings) -> bool:
    """Treat empty-string DSN as off too — operators sometimes leave
    the env var blank rather than removing it. Same logic as
    :mod:`handlers.admin.status`; duplicated rather than imported so a
    future change to the truthiness rule has one obvious place per
    handler to update (the alternative — sharing a helper — couples
    two admin commands together in a way that makes "change the
    rule only for /admin_status" require a refactor)."""
    dsn = settings.observability.sentry_dsn
    return dsn is not None and bool(dsn.get_secret_value().strip())


async def handle_admin_test_log(
    message: Message,
    settings: Settings,
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_test_log; silently dropped"
        )
        return

    # Wall clock — operators correlate this with their log-tail
    # output. UTC explicitly so a multi-region deploy doesn't make
    # the comparison ambiguous (loguru's default formatter renders
    # local time, but the *embedded* timestamp here is the one the
    # operator pastes into a grep).
    now = datetime.now(tz=UTC).isoformat()
    # ``logger.error`` with bound context — same shape as production
    # error paths so the test line exercises the actual sink chain
    # (loguru → Sentry handler when configured → stderr). A bare
    # ``print`` would bypass Sentry; a ``logger.info`` would bypass
    # the ERROR-level filter Sentry usually has.
    log.bind(triggered_by=user.id, timestamp=now).error(
        "Synthetic ERROR via /admin_test_log — sink-chain smoke test"
    )

    sentry_state = "on" if _sentry_enabled(settings) else "off"
    await message.answer(
        "🧪 <b>Test log emitted</b>\n\n"
        f"• timestamp: <code>{now}</code>\n"
        f"• Sentry: {sentry_state}\n"
        "• stderr/loguru: ✅ always-on\n\n"
        "<i>Check your log sinks for an ERROR-level entry from "
        "<code>handlers.admin.test_log</code>.</i>"
    )


def build_router(settings: Settings) -> Router:
    """Private-only at the router level — legacy short-circuits in
    non-private chats (bot.py implicit via admin_only), and emitting
    a test ERROR from a group adds noise to non-operator observers."""
    router = Router(name="admin.test_log")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_test_log(message, settings)

    router.message.register(_entry, Command("admin_test_log", ignore_case=True))
    return router
