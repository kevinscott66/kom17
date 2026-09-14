"""Rank self-service: /staff_me + bang-commands + lazy staff-sync (R3).

DESIGN_RANKS.md §2.3, legacy anchors verified:

* **/staff_me** — port of ``cmd_staff_me`` (bot.py:41462-41513):
  DM-only (groups get the "use private messages" notice,
  bot.py:41472-41474); developers are told they already hold owner
  rights (bot.py:41477-41479); the caller must be a LIVE creator/
  administrator of the MAIN chat (``bot.get_chat_member(CHAT_ID, …)``
  with status in {creator, administrator}, exception → deny,
  bot.py:41482-41494 — fail-CLOSED on the grant, matching legacy's
  ``except: status = ""``); an already-ranked caller (rank ≥ 1 =
  JUNIOR_MOD, bot.py:41497-41502) sees their current rank; otherwise
  rank 2 (MODERATOR) is assigned via the guarded
  :meth:`RankService.set_rank` (legacy ``set_user_rank``,
  bot.py:41505-41513).

* **Bang-commands** — port of ``_parse_bang_rank_command`` /
  ``_get_bang_rank_targets`` / ``cmd_bang_rank`` (bot.py:31322-31441):
  plain-text group messages ``^(!+)\\s*(повысить|понизить|разжаловать|
  promote|demote|strip)(?:\\s+.*)?$`` case-insensitive
  (bot.py:31333); bang count 1-5, >5 → not a command at all
  (bot.py:31338-31339); promote → ``min(5, bangs)``, demote →
  ``max(0, bangs - 1)``, strip → 0 (bot.py:31340-31345). Targets:
  reply author first; only when the reply yields nothing, message
  entities (``text_mention`` → embedded user, ``mention`` → username
  lookup), bots excluded (bot.py:31349-31381). The permission gate is
  :data:`core.ranks.MANAGE_RANKS_PERMISSION` (``can_manage_mods``) —
  **a deliberate widening over legacy, not a parity port.** Legacy
  gated on the *name* ``can_manage_ranks`` (bot.py:31399) through
  ``require_group_moderation``, i.e. TG-admin-of-this-group OR a rank
  grant (bot.py:7568-7577); the default matrix (bot.py:2611-2712)
  never defines ``can_manage_ranks`` at any level and the lookup ends
  in ``perms.get(permission, False)`` (bot.py:7016), so the rank half
  of that OR was dead — only developers and TG admins ever passed.
  The port drops the TG-admin bypass (ranks are global, group
  adminship is not) and hands the write to ranks 4/5/6 instead
  (bot.py:2619/2636/2653), which legacy never actually granted.
  DESIGN_RANKS.md §2.3 assumed the two names were equivalent; they are
  not — see the comment on :data:`core.ranks.MANAGE_RANKS_PERMISSION`
  for the full divergence and the open owner decision.
  Per-target developer-immutability: a developer can only be "set" to
  6 (bot.py:31415-31417) — and unlike legacy the port also refuses any
  level >= the actor's own rank, which is what makes the widening
  survivable. Reply copy is the legacy key set (``rank_set_done`` /
  ``rank_demote_done`` / ``rank_strip_done`` / ``rank_done_multi`` /
  ``rank_cannot_change_dev`` / ``rank_*_fail``, bot.py:31428-31441).

  Username targets: legacy resolved ``@mention`` per-chat via
  ``find_user_by_username(chat_id, …)``; the new pipeline keeps one
  global users table, so :meth:`UsersRepo.get_by_username` (global,
  case-insensitive) is the equivalent. Legacy's group-feature gate
  (``require_group_feature(message, "moderation")``) is the root
  feature-gate middleware's job in the new pipeline, so it is not
  duplicated here.

* **Lazy staff-sync** — approved deviation from legacy's periodic
  ``sync_ranks_with_telegram_admins`` (bot.py:7479-7519): instead of an
  hourly ``getChatAdministrators`` sweep, :func:`lazy_staff_sync` is a
  cheap per-user re-verify with a 10-minute TTL cache, invoked after
  successful /staff_me and bang flows (R4 may also call it after
  rank-based moderation grants). Like the legacy sweep it looks only at
  the MAIN chat (``CHAT_ID``), never at the group the triggering
  message came from. Default posture (``RANK_AUTOSYNC=0``):
  DEMOTE-ON-LOSS ONLY — a rank ≥ 1 user who is confirmed NOT a live TG
  admin of the main chat is reset to rank 0 (legacy bot.py:7505-7511). With
  ``RANK_AUTOSYNC=1`` the legacy auto-promote also applies: a TG admin
  at rank 0 is raised to MODERATOR (bot.py:7513-7519). An API error
  (``is_user_admin`` → ``None``) performs NO action — never demote on
  uncertainty. The switch itself is ``bot.rank_autosync``
  (``RANK_AUTOSYNC`` in :class:`BotConfig`), and it defaults to off:
  promotion is a grant, and a grant should be asked for.

Bot privacy mode: bang-commands are plain text — they are only seen
where the bot is a group admin (same as automod; DESIGN_RANKS.md §3).
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, MessageEntityType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.ranks import RankLevel, rank_name
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services.rank_service import (
    REASON_TG_ADMIN_ONLY,
    RankService,
)
from telegram_invite_bot.utils.telegram_admin import is_user_admin

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db.engines import EngineRegistry

log = logger.bind(component="handlers.rank_self")


#: Legacy parser regex VERBATIM (bot.py:31333) — RU + EN verbs, bang
#: run captured for the level math, trailing payload (targets) allowed.
BANG_RANK_RE: Final[re.Pattern[str]] = re.compile(
    r"^(!+)\s*(повысить|понизить|разжаловать|promote|demote|strip)(?:\s+.*)?$",
    re.IGNORECASE,
)

# -- lazy staff-sync ----------------------------------------------------------

#: 10-minute per-(chat, user) re-verify window (DESIGN_RANKS.md §2.3).
STAFF_SYNC_TTL_SECONDS: Final[float] = 600.0

#: ``by`` marker for system-initiated rank writes (legacy used
#: ``admin_id=ADMIN_CHAT_ID`` for sync demotions, bot.py:7508).
_SYSTEM_ACTOR: Final[int] = 0

#: Ceiling on the re-verify cache. The TTL does not bound it — expiry is
#: only consulted when an entry is read, so every user who ever triggered
#: a synced flow would keep a deadline here for the life of the process.
#: Evicting a LIVE entry is harmless (unlike the rate-limit buckets it
#: guards no allowance): the next sync for that user re-verifies against
#: Telegram one window early, which is the same work the entry was going
#: to schedule anyway.
_SYNC_CACHE_MAX_ENTRIES: Final[int] = 10_000

_SYNC_CACHE: OrderedDict[tuple[int, int], float] = OrderedDict()


def clear_staff_sync_cache() -> None:
    """Test-isolation hook (same contract as ``clear_rank_caches``)."""
    _SYNC_CACHE.clear()


async def lazy_staff_sync(
    ranks: RankService,
    bot: Bot,
    settings: Settings,
    user_id: int,
) -> None:
    """Cheap per-user staff-sync (legacy bot.py:7479-7519, lazy form).

    Always re-verifies against the MAIN chat, never against whichever
    group the triggering message came from — that is what legacy's
    sweep did (``CHAT_ID``), and it is the only chat whose admin list
    the bot owner controls. Probing the incoming chat instead would be
    wrong in both directions: a globally ranked moderator would be
    demoted for not being an admin of some random group, and with
    ``RANK_AUTOSYNC=1`` an admin of any group anyone can create would
    be auto-granted a GLOBAL moderator rank.

    Never raises; an API error (``is_user_admin`` → ``None``) performs
    no action. See module docstring for the RANK_AUTOSYNC semantics.
    """
    # Bound before the ``try`` because the handler at the bottom names
    # it (#1477). ``is_developer`` runs ahead of the real assignment, so
    # anything raising there used to reach the ``except`` and come back
    # out as ``UnboundLocalError`` — the "never raises" promise breaking
    # inside the branch whose whole job is to keep it, and breaking the
    # caller's flow, which is the one thing best-effort sync must not
    # do. ``0`` rather than ``None`` so the type matches the real value
    # and the log line reads the same either way; it is also what an
    # unconfigured main chat is.
    chat_id = 0
    try:
        if settings.bot.is_developer(user_id):
            return
        chat_id = settings.bot.main_chat_id
        if not chat_id:
            return  # no main chat configured — no staff to sync against
        now = time.monotonic()
        key = (chat_id, user_id)
        expiry = _SYNC_CACHE.get(key)
        if expiry is not None and expiry > now:
            return
        _SYNC_CACHE[key] = now + STAFF_SYNC_TTL_SECONDS
        _SYNC_CACHE.move_to_end(key)
        while len(_SYNC_CACHE) > _SYNC_CACHE_MAX_ENTRIES:
            _SYNC_CACHE.popitem(last=False)

        rank = await ranks.get_rank(user_id)
        # Off by default (``BotConfig.rank_autosync``): demote-on-loss
        # needs no permission, promotion does.
        autosync = settings.bot.rank_autosync
        if rank < RankLevel.JUNIOR_MOD and not autosync:
            return  # nothing to demote, promotion disabled — skip the API call
        admin = await is_user_admin(bot, chat_id, user_id)
        if admin is None:
            return  # API error — never demote (or promote) on uncertainty
        if rank >= RankLevel.JUNIOR_MOD and admin is False:
            # Demote-on-loss (legacy bot.py:7505-7511).
            if await ranks.set_rank(user_id, RankLevel.USER, by=_SYSTEM_ACTOR):
                log.bind(user_id=user_id, chat_id=chat_id).info(
                    "staff-sync: rank reset, user lost TG adminship"
                )
        elif (
            # Auto-promote (legacy bot.py:7513-7519), opt-in only.
            autosync
            and rank == RankLevel.USER
            and admin is True
            and await ranks.set_rank(user_id, RankLevel.MODERATOR, by=_SYSTEM_ACTOR)
        ):
            log.bind(user_id=user_id, chat_id=chat_id).info(
                "staff-sync: TG admin auto-promoted to moderator"
            )
    except Exception:  # noqa: BLE001 — sync is best-effort, never break the flow
        log.opt(exception=True).warning(
            "lazy staff-sync failed (chat={chat}, user={user})",
            chat=chat_id,
            user=user_id,
        )


# -- bang-command parsing (pure) ----------------------------------------------


def parse_bang_rank_command(text: str | None) -> tuple[str, int] | None:
    """``("promote"|"demote"|"strip", target_level)`` or ``None``.

    Legacy ``_parse_bang_rank_command`` VERBATIM (bot.py:31322-31346):
    bang count outside 1..5 → ``None``; promote → ``min(5, bangs)``;
    demote → ``max(0, bangs - 1)``; strip → 0.
    """
    if not text:
        return None
    match = BANG_RANK_RE.match(text.strip())
    if match is None:
        return None
    bang_count = len(match.group(1))
    if bang_count < 1 or bang_count > 5:
        return None
    verb = match.group(2).lower()
    if verb in ("повысить", "promote"):
        return ("promote", min(5, bang_count))
    if verb in ("понизить", "demote"):
        return ("demote", max(0, bang_count - 1))
    return ("strip", 0)


async def _resolve_bang_targets(
    message: Message, registry: EngineRegistry
) -> tuple[list[int], str | None]:
    """``(target_ids, None)`` or ``([], error_i18n_key)``.

    Legacy ``_get_bang_rank_targets`` (bot.py:31349-31381): reply author
    first; entities only when the reply yielded nothing; bots excluded;
    no targets → ``rank_no_targets``.
    """
    targets: list[int] = []
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and not reply.from_user.is_bot:
        targets.append(reply.from_user.id)
    if not targets and message.entities:
        text = message.text or ""
        usernames: list[str] = []
        for entity in message.entities:
            if entity.type == MessageEntityType.TEXT_MENTION and entity.user is not None:
                if not entity.user.is_bot and entity.user.id not in targets:
                    targets.append(entity.user.id)
            elif entity.type == MessageEntityType.MENTION:
                usernames.append(text[entity.offset : entity.offset + entity.length])
        if usernames:
            async with session_for(registry, DBName.USERS) as session:
                repo = UsersRepo(session)
                for username in usernames:
                    user = await repo.get_by_username(username)
                    if user is not None and user.user_id not in targets:
                        targets.append(user.user_id)
    if not targets:
        return [], "rank_no_targets"
    return targets, None


# -- handlers -------------------------------------------------------------------


async def handle_staff_me(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    lang: str,
) -> None:
    """/staff_me — legacy ``cmd_staff_me`` (bot.py:41462-41513)."""
    if message.from_user is None:
        return
    # DM-only (legacy bot.py:41472-41474) — internal IDs stay private.
    if message.chat.type != ChatType.PRIVATE:
        await message.reply(t("h_staffme_private_only", lang))
        return
    user_id = message.from_user.id
    if settings.bot.is_developer(user_id):
        await message.reply(t("h_staffme_dev", lang))
        return

    main_chat_id = settings.bot.main_chat_id
    # is_user_admin returns None on API error — legacy coerced that to
    # status "" (bot.py:41484-41486), i.e. DENY. Grant only on True.
    is_admin = await is_user_admin(bot, main_chat_id, user_id) if main_chat_id else False
    if is_admin is not True:
        await message.reply(t("h_staffme_not_admin", lang))
        return

    ranks = RankService(registry, settings)
    current_rank = await ranks.get_rank(user_id)
    if current_rank >= RankLevel.JUNIOR_MOD:
        await message.reply(t("h_staffme_already", lang, rank=rank_name(current_rank, lang)))
        return

    if await ranks.set_rank(user_id, RankLevel.MODERATOR, by=user_id):
        await message.reply(t("h_staffme_done", lang, rank=rank_name(RankLevel.MODERATOR, lang)))
        log.bind(user_id=user_id).info("/staff_me: moderator rank self-assigned")
        await lazy_staff_sync(ranks, bot, settings, user_id)
    else:
        await message.reply(t("h_staffme_fail", lang))


async def handle_bang_rank(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    lang: str,
) -> None:
    """!повысить/!!понизить/!!!разжаловать — legacy ``cmd_bang_rank``."""
    if message.from_user is None:
        return
    parsed = parse_bang_rank_command(message.text)
    if parsed is None:
        return
    action, level = parsed
    actor_id = message.from_user.id
    chat_id = message.chat.id

    ranks = RankService(registry, settings)
    # ``may_manage_ranks``, not ``check``: the ranks written here are
    # global, so the live-TG-admin bypass must not authorize them —
    # otherwise anyone could create a group, add the bot, and hand
    # themselves rank 5 in reply to their own message.
    verdict = await ranks.may_manage_ranks(actor_id, chat_id, bot)
    if not verdict.allowed:
        key = (
            "h_rank_global_denied"
            if verdict.reason == REASON_TG_ADMIN_ONLY
            else "h_mod_no_permission"
        )
        await message.reply(t(key, lang))
        return

    # Target guards, mirroring the /groupadmin staff panel: legacy had
    # NONE here (bot.py:31411-31425), so a rank-4 admin could mint a
    # rank-5 owner — or promote themselves — with one message.
    actor_rank = (
        int(RankLevel.DEVELOPER) if settings.bot.is_developer(actor_id) else int(verdict.actor_rank)
    )
    if level >= actor_rank:
        await message.reply(t("h_rank_bang_too_high", lang))
        return

    targets, err_key = await _resolve_bang_targets(message, registry)
    if err_key is not None:
        await message.reply(t(err_key, lang))
        return

    # Legacy renders the target level's title for the singular replies
    # (bot.py:31424) — level is ≤ 5 here, so no developer masking needed.
    rank_label = rank_name(level, lang, in_group=True)
    success_count = 0
    skipped_dev = 0
    skipped_guard = 0
    for target_id in targets:
        # Developer immutability (legacy bot.py:31415-31417). set_rank
        # double-guards, but the explicit check feeds the legacy reply.
        if settings.bot.is_developer(target_id) and level != RankLevel.DEVELOPER:
            skipped_dev += 1
            continue
        # Never yourself, never someone at or above your own rank —
        # a peer must not be demotable, and self-service promotion is
        # exactly the escalation this gate exists to stop.
        if target_id == actor_id or await ranks.get_rank(target_id) >= actor_rank:
            skipped_guard += 1
            continue
        if await ranks.set_rank(target_id, level, by=actor_id):
            success_count += 1

    if skipped_dev and not success_count:
        await message.reply(t("rank_cannot_change_dev", lang))
        return
    if skipped_guard and not success_count:
        await message.reply(t("h_rank_bang_target_denied", lang))
        return
    if success_count == 0:
        fail_key = {
            "promote": "rank_set_fail",
            "demote": "rank_demote_fail",
            "strip": "rank_strip_fail",
        }[action]
        await message.reply(t(fail_key, lang))
        return
    if success_count == 1 and len(targets) == 1:
        if action == "promote":
            await message.reply(t("rank_set_done", lang, rank_name=rank_label))
        elif action == "demote":
            await message.reply(t("rank_demote_done", lang, rank_name=rank_label))
        else:
            await message.reply(t("rank_strip_done", lang))
    else:
        await message.reply(t("rank_done_multi", lang, count=success_count))
    log.bind(actor=actor_id, chat=chat_id, action=action, level=level, count=success_count).info(
        "bang rank command applied"
    )
    await lazy_staff_sync(ranks, bot, settings, actor_id)


# -- router factory --------------------------------------------------------------


def _is_bang_rank_message(message: Message) -> bool:
    """Router filter: plain-text bang rank command (legacy bot.py:31384-31388)."""
    return parse_bang_rank_command(message.text) is not None


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the rank self-service router (/staff_me + bang-commands).

    No session middleware: :class:`RankService` and the username lookup
    open their own short sessions via ``session_for``. ``lang`` is
    injected by the root :class:`LanguageMiddleware`.
    """
    router = Router(name="rank_self")

    async def _staff_me(message: Message, bot: Bot, lang: str) -> None:
        await handle_staff_me(message, bot, settings, registry, lang)

    async def _bang_rank(message: Message, bot: Bot, lang: str) -> None:
        await handle_bang_rank(message, bot, settings, registry, lang)

    # /staff_me registers for ALL chat types: the group branch replies
    # with the "DM-only" notice (legacy bot.py:41472-41474).
    router.message.register(_staff_me, Command("staff_me", ignore_case=True))
    router.message.register(
        _bang_rank,
        F.chat.type.in_(GROUP_TYPES),
        _is_bang_rank_message,
    )
    return router
