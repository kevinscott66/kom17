"""Shared live-Telegram admin checks (ranks epic R1).

Two questions live here, and #337 split them apart because collapsing
them into one was a privilege hole:

* **"May this person moderate?"** — :func:`is_user_admin`, built on
  :func:`has_moderation_rights`. A CREATOR always may. An ADMINISTRATOR
  may only when Telegram actually granted at least one moderation
  right. This is the legacy predicate ``telegram_admin_has_mod_rights``
  (``bot.py:7455-7476``), which every legacy authority gate reached
  through ``is_telegram_group_admin`` → ``has_group_admin_rights``
  (``bot.py:7533-7565``) and thus through
  ``require_group_moderation`` (``bot.py:7568-7577``).
* **"Is this person an admin at all?"** — :func:`is_chat_admin_any`,
  bare status membership. Correct only where the answer is not an
  authority verdict: courtesy exemptions and cosmetics.

Until #337 both questions used bare status, so a member promoted for a
title — ``can_invite_users`` / ``can_change_info`` and nothing else —
was handed ``/ban``, ``/kick``, ``/mute``, ``/warn``, ``/clear``,
``/modcfg`` and, through
:meth:`~telegram_invite_bot.services.rank_service.RankService.check`,
a bypass of the whole rank matrix. Legacy dropped that person into the
rank branch, where rank 0 grants nothing.

Note that ``can_ban_users`` is carried in the field list for parity with
``bot.py:7471`` only. The Bot API has never sent it — banning is
``can_restrict_members`` — so it reads as absent on both sides and
changes no verdict.

A Telegram-API error surfaces as ``None`` from both probes — NEVER a
bool — so each caller decides its own fail direction explicitly.

Why these live here rather than being imported from
``handlers/moderation``: ``services/`` must not depend on ``handlers/``
(it would invert the layering — handlers already import services), and
re-exporting a ``_``-private from the handler module would freeze its
internals as API. ``handlers/moderation._is_user_admin`` stays as the
in-handler twin but now delegates to :func:`has_moderation_rights`, so
the predicate itself exists exactly once and the two cannot drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.enums import ChatMemberStatus
from loguru import logger

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import ChatMemberUnion

log = logger.bind(component="utils.telegram_admin")

_ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}

#: Telegram rights that make an ADMINISTRATOR an actual moderator.
#: Byte-for-byte the legacy list (``bot.py:7469-7473``); see the module
#: docstring on why ``can_ban_users`` is inert.
_MODERATION_RIGHTS = (
    "can_restrict_members",
    "can_delete_messages",
    "can_ban_users",
    "can_pin_messages",
    "can_promote_members",
)


def has_moderation_rights(member: object) -> bool:
    """Port of legacy ``telegram_admin_has_mod_rights`` (bot.py:7455-7476).

    Pure and synchronous so it can be unit-tested against a handful of
    constructed ``ChatMember`` objects without a Bot. ``getattr`` with a
    default rather than attribute access: aiogram models the statuses as
    separate classes, so ``can_restrict_members`` simply does not exist
    on ``ChatMemberMember``, and ``model_construct``-built test doubles
    may omit fields the real model requires.
    """
    status = getattr(member, "status", "") or ""
    if status == ChatMemberStatus.CREATOR:
        return True
    if status != ChatMemberStatus.ADMINISTRATOR:
        return False
    return any(bool(getattr(member, right, False)) for right in _MODERATION_RIGHTS)


async def _fetch_member(bot: Bot, chat_id: int, user_id: int) -> ChatMemberUnion | None:
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — re-surface via the None contract
        log.warning(
            "get_chat_member failed (chat={chat}, user={user}): {exc!r}",
            chat=chat_id,
            user=user_id,
            exc=exc,
        )
        return None


async def is_user_admin(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    """True/False for confirmed live moderation authority, ``None`` on API error.

    "Admin" here means what it meant in legacy: creator, or an
    administrator Telegram actually gave a moderation right to. A
    title-only administrator gets ``False`` — see the module docstring
    and #337. Use :func:`is_chat_admin_any` where the question is not an
    authority verdict.

    Callers MUST handle ``None`` explicitly — leaking it into a truthy
    branch is the fail-open bug class R-FIX-007 closed.
    """
    member = await _fetch_member(bot, chat_id, user_id)
    if member is None:
        return None
    return has_moderation_rights(member)


async def is_chat_admin_any(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    """True when the user holds ANY admin status, ``None`` on API error.

    The deliberately broad twin of :func:`is_user_admin`, for the places
    where a title-only administrator should still be treated as staff:
    an automatic sanction we would rather skip than misfire on, or a
    cosmetic label. Never use it to decide whether an action is allowed.
    """
    member = await _fetch_member(bot, chat_id, user_id)
    if member is None:
        return None
    return member.status in _ADMIN_STATUSES


async def chat_creator_id(bot: Bot, chat_id: int) -> int | None:
    """user_id of the chat creator, or ``None`` when unknown/API error.

    Port of legacy ``get_chat_creator_id`` (bot.py:7100-7122) minus the
    cache — :class:`~telegram_invite_bot.services.rank_service
    .RankService` layers its own TTL cache (legacy used 600s there too)
    so this stays a pure API call.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as exc:  # noqa: BLE001 — unknown creator, caller decides
        log.debug(
            "get_chat_administrators failed (chat={chat}): {exc!r}",
            chat=chat_id,
            exc=exc,
        )
        return None
    for admin in admins:
        if admin.status == ChatMemberStatus.CREATOR:
            return admin.user.id
    return None
