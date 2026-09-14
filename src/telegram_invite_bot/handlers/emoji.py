"""``/emojis`` + friends — VIP cosmetic emoji badge (#25).

Replaces the Stage-15 static "в разработке" stub (removed from
``handlers/vip.py``) with the real equip / preview flow over
:class:`EmojiBadgeService` (injected by :class:`EconomyMiddleware`).

A badge is a cosmetic prefix the bot renders next to a VIP's display
name on surfaces it fully controls (the ``/profile`` card and the
tops) — it is NOT a Telegram premium ``custom_emoji`` entity. The
feature is **free for VIP**: there is no money path. See
``docs/CUSTOM_EMOJI_VIP_SPEC.md`` for the product rationale.

Commands
--------
* ``/emojis`` / ``/эмодзи`` — list the curated set + what's equipped
  (VIP), or an upsell (non-VIP).
* ``/emoji_set <emoji>`` — equip a badge from the set (VIP-gated +
  set-membership-validated). Bare ``/emoji_set`` clears the selection.
* ``/emoji_preview`` — show how your name renders with the badge (VIP).
* ``/emoji_buy`` — informational alias: the set is free for VIP, so this
  points at ``/emoji_set`` rather than charging coins.

Parse mode is HTML (the bot default). The badge itself is a trusted
member of ``VIP_BADGE_SET`` so it needs no escaping; the user-controlled
display name in the preview IS routed through :func:`html.escape`.
Private-chat-only — a group call gets the #123 refusal twin, matching
every other ported economy stub.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.emoji_badge_service import (
    VIP_BADGE_SET,
    EquipOutcome,
)
from telegram_invite_bot.utils.aiogram import command_args, require_from_user

log = logger.bind(component="handlers.emoji")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.services.emoji_badge_service import EmojiBadgeService


def _utcnow() -> datetime:
    """AWARE UTC ``now`` for the VIP gate.

    The gate compares against ``vip_till`` (a legacy unix timestamp) and
    :meth:`VipRepo.get_active_profile` derives the deadline from
    ``now.timestamp()`` — and ``.timestamp()`` on a *naive* value reads
    the wall clock in the HOST's zone. This helper used to strip the
    tzinfo, which on the MSK production host put the deadline 10 800 s
    in the past: a VIP that lapsed within the last three hours still
    passed the gate and could list, preview and equip badges. Aware is
    the only frame that makes the comparison mean what it says.

    :meth:`EmojiBadgeService.equip` hands this same value to
    ``EmojiBadgeRepo.upsert``'s naive ``set_at`` column; the repo
    converts there via :func:`utils.time.to_naive_utc` rather than the
    handler picking a frame that is wrong for one of the two consumers.
    """
    return datetime.now(UTC)


def _badges_str() -> str:
    """The curated set rendered as a single space-separated string."""
    return " ".join(VIP_BADGE_SET)


async def handle_emojis(
    message: Message, emoji_badge_service: EmojiBadgeService, lang: str
) -> None:
    """``/emojis`` — list the set + equipped badge (VIP) or upsell."""
    user = require_from_user(message)
    if not await emoji_badge_service.is_vip(user.id, now=_utcnow()):
        await message.reply(t("h_emoji_vip_only", lang))
        return
    current = await emoji_badge_service.equipped(user.id) or "—"
    await message.reply(t("h_emoji_list", lang, badges=_badges_str(), current=current))


async def handle_emoji_set(
    message: Message,
    command: CommandObject,
    emoji_badge_service: EmojiBadgeService,
    lang: str,
) -> None:
    """``/emoji_set <emoji>`` — equip from the set; bare → clear."""
    user = require_from_user(message)
    arg = command_args(command)
    if not arg:
        # Clearing is VIP-ungated (a lapsed VIP should still be able to
        # tidy up) and idempotent.
        await emoji_badge_service.clear(user.id)
        await message.reply(t("h_emoji_cleared", lang))
        return

    emoji = arg.split()[0]
    outcome = await emoji_badge_service.equip(user.id, emoji, now=_utcnow())
    if outcome is EquipOutcome.OK:
        await message.reply(t("h_emoji_set_ok", lang, emoji=emoji))
        log.bind(uid=user.id, emoji=emoji).info("/emoji_set equipped")
        return
    if outcome is EquipOutcome.NOT_VIP:
        await message.reply(t("h_emoji_vip_only", lang))
        return
    # NOT_IN_SET
    await message.reply(t("h_emoji_not_in_set", lang, badges=_badges_str()))


async def handle_emoji_preview(
    message: Message, emoji_badge_service: EmojiBadgeService, lang: str
) -> None:
    """``/emoji_preview`` — render the caller's name with their badge."""
    user = require_from_user(message)
    if not await emoji_badge_service.is_vip(user.id, now=_utcnow()):
        await message.reply(t("h_emoji_vip_only", lang))
        return
    emoji = await emoji_badge_service.equipped(user.id)
    if not emoji:
        await message.reply(t("h_emoji_preview_none", lang, badges=_badges_str()))
        return
    base = html.escape(user.full_name or str(user.id))
    await message.reply(t("h_emoji_preview", lang, preview=f"{emoji} {base}"))


async def handle_emoji_buy(
    message: Message, emoji_badge_service: EmojiBadgeService, lang: str
) -> None:
    """``/emoji_buy`` — no money path; informational for VIP, upsell otherwise."""
    user = require_from_user(message)
    if not await emoji_badge_service.is_vip(user.id, now=_utcnow()):
        await message.reply(t("h_emoji_vip_only", lang))
        return
    await message.reply(t("h_emoji_buy_info", lang, badges=_badges_str()))


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh ``Router`` + ``EconomyMiddleware`` per call.

    Mirrors ``handlers/checks.py``: private-chat-only filter and the
    economy middleware on the message side (injects
    ``emoji_badge_service``). Takes ``registry`` to build the middleware;
    no ``settings`` dependency (unlike checks) — there is no dev-gate.
    """
    router = Router(name="emoji")
    # Private-chat-only — a group call gets the #123 refusal twin.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.middleware(EconomyMiddleware(registry))

    async def _handle_emojis(
        message: Message, emoji_badge_service: EmojiBadgeService, lang: str
    ) -> None:
        await handle_emojis(message, emoji_badge_service, lang)

    router.message.register(
        _handle_emojis,
        Command("emojis", "эмодзи", ignore_case=True),
        F.from_user,
    )

    async def _handle_emoji_set(
        message: Message,
        command: CommandObject,
        emoji_badge_service: EmojiBadgeService,
        lang: str,
    ) -> None:
        await handle_emoji_set(message, command, emoji_badge_service, lang)

    router.message.register(
        _handle_emoji_set,
        Command("emoji_set", ignore_case=True),
        F.from_user,
    )

    async def _handle_emoji_preview(
        message: Message, emoji_badge_service: EmojiBadgeService, lang: str
    ) -> None:
        await handle_emoji_preview(message, emoji_badge_service, lang)

    router.message.register(
        _handle_emoji_preview,
        Command("emoji_preview", ignore_case=True),
        F.from_user,
    )

    async def _handle_emoji_buy(
        message: Message, emoji_badge_service: EmojiBadgeService, lang: str
    ) -> None:
        await handle_emoji_buy(message, emoji_badge_service, lang)

    router.message.register(
        _handle_emoji_buy,
        Command("emoji_buy", ignore_case=True),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
