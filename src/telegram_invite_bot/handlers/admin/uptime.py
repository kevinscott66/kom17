"""``/admin_uptime`` — process boot time + elapsed wall-clock.

Operator question during an incident: "did the process restart
recently?". The legacy systemd unit's restart-on-fail policy can
mask a crash-loop as long as the bot answers commands — the
process keeps coming back, but with state lost and metrics
counters reset. Surfacing the boot time inside Telegram lets an
operator confirm "this is the same process I deployed an hour
ago" without ``systemctl status`` access.

Implementation notes:

* Boot time is captured at *module import* (:data:`_BOOT_UTC`),
  not at first invocation. The handler must report when the
  process *started*, not when the operator first ran the
  command — those diverge dramatically on a long-running bot.
* Both UTC wall-clock and monotonic-clock elapsed are used:
  - ``datetime.now(UTC)`` for the "started at" label, because
    that's what operators correlate against deploy logs and
    Sentry timestamps.
  - ``time.monotonic()`` for the *elapsed* duration, because
    wall-clock subtraction is wrong under NTP step adjustments
    (and we've been bitten by exactly that on a deploy where
    the host's clock was slewed by 30s during boot).
* Module import sets the timestamp, so under ``-m
  telegram_invite_bot`` this is the process boot time. If
  someone imports the module from a long-lived REPL and then
  spins up a dispatcher, the timestamp would be the REPL
  import time — but that's not a deployment shape we run, so
  the optimisation is fine.

Same posture as every other ``/admin_*``: silent-drop for
non-devs, private-only at the router level.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.uptime")


# Captured at import time — see module docstring for why this isn't
# computed at first request.
_BOOT_UTC: datetime = datetime.now(UTC)
_BOOT_MONO: float = time.monotonic()


def _format_elapsed(seconds: float) -> str:
    """Render a duration as the largest two natural units.

    "3d 4h" beats "3 days, 4 hours, 12 minutes, 7.3 seconds" for
    a scanning operator — they want a quick "is this fresh or
    stale?". Two units is the sweet spot: one is too coarse (a
    "5h" uptime hides whether minutes-since-restart is 5 or 305),
    three is noise.
    """
    seconds = max(0.0, seconds)
    days, rem = divmod(int(seconds), 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _render(*, boot_utc: datetime, elapsed_seconds: float) -> str:
    lines = ["⏱ <b>Process uptime</b>", ""]
    lines.append(f"• booted at: <code>{boot_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>")
    lines.append(f"• elapsed: <code>{_format_elapsed(elapsed_seconds)}</code>")
    lines.append("")
    lines.append("<i>Wall-clock from import; elapsed from monotonic clock (NTP-safe).</i>")
    return "\n".join(lines)


async def handle_admin_uptime(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_uptime; silently dropped"
        )
        return
    elapsed = time.monotonic() - _BOOT_MONO
    await message.answer(_render(boot_utc=_BOOT_UTC, elapsed_seconds=elapsed))
    log.bind(user_id=user.id, elapsed_s=elapsed).info("/admin_uptime rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.uptime")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_uptime(message, settings)

    router.message.register(_entry, Command("admin_uptime", ignore_case=True))
    return router
