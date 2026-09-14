"""``/admin_bot_session`` — aiogram Bot session + defaults snapshot.

Complements /admin_settings (config we read from env) by surfacing
the **runtime** Bot-client config: which Telegram API server this
process talks to, the HTTP session timeout, and the
``DefaultBotProperties`` (parse_mode, link-preview behaviour, …)
that every outbound ``answer`` / ``send_message`` inherits unless
explicitly overridden.

Why an operator wants this:

* Verify a local Bot API server switch. After pointing the bot at
  a self-hosted Bot API (``--api-id`` deployment), the only
  Telegram-visible verification path is this card — without it,
  the operator is reading code or hitting the API and inferring
  from latency. The card surfaces ``session.api.base`` directly.
* Confirm parse_mode default. Legacy used HTML implicitly via
  ``parse_mode=HTML`` in the Bot constructor; the new pipeline
  uses :class:`DefaultBotProperties`. Drift here would break
  every handler's HTML envelope assumptions silently (raw
  ``<b>...</b>`` would render as literal angle-brackets).
* Spot a session timeout drift. The 60s default is documented
  in aiogram's source but configurable; if a deploy passes a
  shorter timeout via ``AiohttpSession(timeout=…)`` and someone
  forgets to update the runbook, slow Telegram-side responses
  start timing out without an obvious cause. The card pins the
  effective value.

Pure read off the running :class:`aiogram.Bot`. No IO, no DB.
Same posture as every other ``/admin_*``: silent-drop for non-devs,
private-only at the router level (the API base URL is operator
context — surfacing it in a shared admin group leaks deploy shape
to anyone watching the chat).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.bot_session")


class _SessionSnapshot:
    """One read of the Bot's session + defaults.

    ``base_url`` is rendered with the literal ``{token}``/``{method}``
    placeholders aiogram uses — we **don't** substitute the real
    token because this card may render and the operator should
    see the URL **shape**, not the bot token. The ``{token}``
    placeholder is a deliberate identity-protection feature, not a
    rendering bug.
    """

    __slots__ = (
        "base_url",
        "is_local_api",
        "parse_mode",
        "session_kind",
        "timeout_s",
    )

    def __init__(
        self,
        *,
        session_kind: str,
        base_url: str,
        is_local_api: bool,
        timeout_s: float | None,
        parse_mode: str | None,
    ) -> None:
        self.session_kind = session_kind
        self.base_url = base_url
        self.is_local_api = is_local_api
        self.timeout_s = timeout_s
        self.parse_mode = parse_mode


def _capture(bot: Bot) -> _SessionSnapshot:
    """Snapshot bot.session and bot.default.

    Defensive ``getattr`` on each surface: aiogram has reshaped
    DefaultBotProperties across the 3.x series, and a future
    refactor that renames ``parse_mode`` or relocates the timeout
    onto a sub-object should degrade to ``None``/``"unknown"``
    rather than crash this diagnostic. The card has to keep
    rendering through any aiogram bump.
    """
    session = bot.session
    api = getattr(session, "api", None)
    base_url = str(getattr(api, "base", "<unknown>")) if api else "<unknown>"
    is_local_api = bool(getattr(api, "is_local", False)) if api else False
    timeout_s = getattr(session, "timeout", None)
    if timeout_s is not None:
        timeout_s = float(timeout_s)
    default = bot.default
    parse_mode = getattr(default, "parse_mode", None) if default else None
    return _SessionSnapshot(
        session_kind=type(session).__name__,
        base_url=base_url,
        is_local_api=is_local_api,
        timeout_s=timeout_s,
        parse_mode=parse_mode,
    )


def _render(snap: _SessionSnapshot) -> str:
    lines = ["🤖 <b>Bot session</b>", ""]
    lines.append(f"• Session class: <code>{html.escape(snap.session_kind)}</code>")
    # Local-API badge is load-bearing: an operator scanning quickly
    # needs the boolean answer to "are we on api.telegram.org or a
    # self-hosted server?" without parsing the URL.
    badge = "local" if snap.is_local_api else "remote"
    lines.append(f"• API base: <code>{html.escape(snap.base_url)}</code> (<i>{badge}</i>)")
    if snap.timeout_s is not None:
        lines.append(f"• HTTP timeout: <code>{snap.timeout_s:.1f}s</code>")
    else:
        lines.append("• HTTP timeout: <code>&lt;unset&gt;</code>")
    if snap.parse_mode:
        lines.append(f"• Default parse_mode: <code>{html.escape(snap.parse_mode)}</code>")
    else:
        # Unset parse_mode is itself a finding — every admin card
        # in this codebase renders HTML, so a drift to None would
        # turn ``<b>`` into literal angle brackets in every reply.
        lines.append("• Default parse_mode: <code>&lt;unset&gt;</code> ⚠")
    lines.append("")
    lines.append(
        "<i>The <code>{token}</code> placeholder in the URL is "
        "deliberate — aiogram substitutes it at call-time so the "
        "bot token never leaks into a rendered card.</i>"
    )
    return "\n".join(lines)


async def handle_admin_bot_session(message: Message, settings: Settings, bot: Bot) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_bot_session; silently dropped"
        )
        return
    snap = _capture(bot)
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_bot_session rendered")


def build_router(settings: Settings) -> Router:
    """Bot is resolved per-request via the dependency-injection
    middleware aiogram populates on every update (``bot`` keyword
    in the handler signature). Passing it at router-build time
    would bake a closure over the bot — but the same Bot instance
    is consistent for the process lifetime, so either shape is
    correct here; we use the DI path to match every other admin
    handler that needs the runtime Bot.
    """
    router = Router(name="admin.bot_session")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message, bot: Bot) -> None:
        await handle_admin_bot_session(message, settings, bot)

    router.message.register(_entry, Command("admin_bot_session", ignore_case=True))
    return router
