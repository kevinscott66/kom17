"""``/referral`` — caller's referral deep-link + commission percent.

Legacy ``/referral`` (bot.py:25065) renders a card with a
``https://t.me/<bot>?start=ref_<user_id>`` link and the commission
percent the inviter receives on coin purchases by the invitee.
Zero DB reads — just two pieces of state (bot username + commission
%) and the caller's user_id.

Behaviour parity & deltas:

* Any chat type. Legacy gates on ``ensure_user_access`` (role/ban
  check) which the new pipeline doesn't model yet — same gap
  ``/ping`` and ``/duel_stats`` accept.
* Bot username from ``bot.get_me()`` rather than a cached constant.
  Legacy caches at startup; we go through aiogram's session so
  username changes (rare but possible — operators rebrand) don't
  require a redeploy to surface on referral cards. ``get_me`` is
  cached client-side by aiogram in the same process anyway, so the
  extra call is one Telegram round-trip on the first request and
  free thereafter.
* ``get_me`` failure fallback (network outage during the very call
  the referral link is meant to advertise the bot through):
  ``YourBot`` placeholder. Legacy does the same with the literal
  string ``"YourBot"``; preserved verbatim so an existing referral
  reaching this fallback doesn't change shape across the cutover.
* Commission percent comes from :class:`EconomyConfig` (env-driven
  in the new pipeline; legacy reads ``bot_settings.json``). Default
  ``10`` matches legacy's hardcoded fallback for missing config.
* HTML rendering instead of legacy's Markdown to match the bot-wide
  ``parse_mode=HTML`` posture. The link itself goes inside an
  ``<a href="...">`` anchor — Telegram strips the t.me URL from
  plaintext into a previewed link card automatically, but rendering
  the anchor keeps the text scannable for users who share the card
  as a screenshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import require_from_user

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService


log = logger.bind(component="handlers.referral")

_FALLBACK_USERNAME = "YourBot"


async def _bot_username(bot: Bot) -> str:
    """Bot username via :meth:`Bot.get_me`. Falls back to
    ``YourBot`` on any failure — see module docstring for why
    swallowing the exception is the right call here.
    """
    try:
        me = await bot.get_me()
    except Exception as exc:  # pragma: no cover - network paths
        log.bind(error=str(exc)).warning("get_me failed; using fallback username")
        return _FALLBACK_USERNAME
    return (me.username or "").strip() or _FALLBACK_USERNAME


def _format(lang: str, *, username: str, user_id: int, percent: int) -> str:
    link = f"https://t.me/{username}?start=ref_{user_id}"
    # Link itself is bot-controlled (username + numeric id), so no
    # ``html.escape`` is needed on the URL. The anchor renders the
    # link visibly and keeps the card scannable in screenshots —
    # plain-text t.me URLs get auto-previewed by Telegram, but the
    # anchor variant survives "long-press → copy text" cleanly.
    return "\n".join(
        [
            t("h_referral_title", lang),
            "",
            f'<a href="{link}">{link}</a>',
            "",
            t("h_referral_body", lang, percent=percent),
        ]
    )


async def handle_referral(
    message: Message,
    bot: Bot,
    settings: Settings,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    user = await user_service.touch(require_from_user(message))
    # #1983: end the bookkeeping transaction here rather than hold
    # ``users.db``'s single writer slot across the reply below. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    username = await _bot_username(bot)
    percent = settings.economy.referral_commission_percent
    await message.answer(
        _format(user.language, username=username, user_id=user.user_id, percent=percent),
        disable_web_page_preview=True,
    )
    log.bind(uid=user.user_id, percent=percent).info("/referral rendered")


def build_router(settings: Settings) -> Router:
    """Bare-form only (``magic=F.args.is_(None)``). Trailing args
    don't change behaviour today — the link is always the caller's
    own — but pinning bare keeps room for a future
    ``/referral stats`` shortcut without colliding with this route.
    """
    router = Router(name="referral")

    async def _entry(
        message: Message,
        bot: Bot,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_referral(message, bot, settings, user_service, checkpoint)

    router.message.register(
        _entry,
        Command(
            "referral",
            "ref",
            "реферал",
            "реферальная_ссылка",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    return router
