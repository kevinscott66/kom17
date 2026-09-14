"""RankService — the ONE enforcement seam of the ranks epic (R1).

DESIGN_RANKS.md §2.2. Two questions, answered with legacy-exact
precedence:

* :meth:`RankService.check` — "may this actor use this permission in
  this chat?" Precedence (legacy ``require_group_moderation`` →
  ``has_group_admin_rights`` → ``check_permission_and_reply``,
  bot.py:7555-7577 + 6990-7020):

  1. developer (``settings.bot.is_developer``) — always allowed;
  2. live Telegram admin of the chat — bypasses ranks entirely. NOT
     bare ADMINISTRATOR/CREATOR: the probe is ``is_user_admin`` →
     ``has_moderation_rights`` (utils/telegram_admin.py:70-85), i.e.
     the creator, or an administrator holding at least one moderation
     right. Bare status was exactly the privilege hole #337 closed —
     see the module docstring of ``utils.telegram_admin`` — so do not
     "simplify" this back to a status comparison. An API error during
     the probe does NOT grant (fail-closed on grants); the check falls
     through to the rank path;
  3. global rank vs the merged permission matrix (rank 0 → no
     permissions, legacy bot.py:7003-7005).

* :meth:`RankService.can_moderate` — "may this actor target this user?"
  Legacy ``can_moderate`` guard order VERBATIM (bot.py:7580-7622):
  self → chat creator (probe failure = guard skipped, legacy
  ``except: pass``) → developer allow → ``target_rank >= actor_rank``
  deny. Denial reasons reuse the existing yaml keys
  ``can_moderate_self`` / ``can_moderate_creator`` /
  ``can_moderate_higher`` so handlers render legacy-parity copy.

Sessions: the service is constructed with the APP-scoped
:class:`~telegram_invite_bot.db.engines.EngineRegistry` + ``Settings``
and opens its own short read sessions via :func:`session_for` (users.db
for ranks, moderation.db for the matrix) — so handlers can build it
per-call without threading two extra ``AsyncSession`` dependencies.

Caching mirrors :mod:`telegram_invite_bot.middlewares.language`:
module-level :class:`~telegram_invite_bot.utils.ttl_lru_cache.TTLLRUCache`
tables on ``time.monotonic``, 300s rank TTL (legacy
bot.py:6656) / 600s creator TTL (legacy bot.py:7096), with explicit
``invalidate_rank_cache`` (called by :meth:`RankService.set_rank`) and
``clear_rank_caches`` (test isolation; also clears the repo-level
matrix/override caches).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from loguru import logger

from telegram_invite_bot.core.ranks import (
    DEFAULT_RANK_PERMISSIONS,
    MANAGE_RANKS_PERMISSION,
    MAX_SETTABLE_RANK,
    MIN_SETTABLE_RANK,
    RankLevel,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.rank_repo import (
    RankRepo,
    clear_command_override_cache,
    clear_rank_matrix_cache,
)
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.utils.cache_generation import CacheGeneration
from telegram_invite_bot.utils.telegram_admin import chat_creator_id, is_user_admin
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from aiogram import Bot

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db.engines import EngineRegistry

log = logger.bind(component="services.rank_service")

RANK_CACHE_TTL_SECONDS: Final[float] = 300.0  # legacy bot.py:6657
CREATOR_CACHE_TTL_SECONDS: Final[float] = 600.0  # legacy bot.py:7097

# Ceilings on the two tables (#74 growth class). Both used to be plain
# dicts with an expiry stored in the value: entries went stale on read
# but nothing ever removed them, so the rank table grew by one entry per
# distinct user who ever ran a gated command and the creator table by
# one per chat — for the lifetime of the process. LRU eviction is free
# here in a way it is not for a rate limit (``transfer_rights``): the
# worst an evicted entry costs is one extra DB read or one extra
# ``getChatAdministrators`` call, never a permission somebody shouldn't
# have. Sized like ``middlewares.language._MAX_CACHED_USERS`` — every
# plausible active user / chat stays resident, a one-off visitor cannot
# pin memory.
_MAX_CACHED_USERS: Final[int] = 10_000
_MAX_CACHED_CHATS: Final[int] = 5_000

# Module-level caches (language-middleware pattern): user_id → rank and
# chat_id → creator_id. Module scope lets the /staff_me, bang-command
# and /perm handlers invalidate without threading the service instance
# through DI.
_RANK_CACHE: TTLLRUCache[int, int] = TTLLRUCache(RANK_CACHE_TTL_SECONDS, _MAX_CACHED_USERS)
_CREATOR_CACHE: TTLLRUCache[int, int] = TTLLRUCache(CREATOR_CACHE_TTL_SECONDS, _MAX_CACHED_CHATS)

# #1942: both fills below sit behind an ``await``, and both caches have
# an invalidator that can land inside it. See
# ``utils.cache_generation`` for what that used to cost — for the rank
# table it is a demoted moderator keeping their permissions, globally,
# for the rest of the TTL.
_RANK_GENERATION = CacheGeneration()
_CREATOR_GENERATION = CacheGeneration()


def invalidate_rank_cache(user_id: int) -> None:
    """Drop one user's cached rank (call after any rank write)."""
    _RANK_CACHE.discard(user_id)
    _RANK_GENERATION.bump()


def clear_rank_caches() -> None:
    """Drop ALL rank-system caches (rank, creator, matrix, overrides).

    Test-isolation hook, same contract as
    ``middlewares.language.clear_language_cache``. A no-op for prod
    correctness — TTLs + targeted invalidation cover the live process.
    """
    _RANK_CACHE.clear()
    _CREATOR_CACHE.clear()
    _RANK_GENERATION.bump()
    _CREATOR_GENERATION.bump()
    clear_rank_matrix_cache()
    clear_command_override_cache()


# Stable machine-readable verdict reasons (allowed side). Denial
# reasons double as i18n keys where legacy copy exists.
REASON_DEVELOPER: Final[str] = "developer"
REASON_TG_ADMIN: Final[str] = "tg_admin"
REASON_RANK: Final[str] = "rank"
REASON_NO_PERMISSION: Final[str] = "no_permission"
REASON_TG_ADMIN_ONLY: Final[str] = "tg_admin_only"
REASON_SELF: Final[str] = "can_moderate_self"
REASON_CREATOR: Final[str] = "can_moderate_creator"
REASON_HIGHER: Final[str] = "can_moderate_higher"


@dataclass(frozen=True, slots=True)
class RankVerdict:
    """Outcome of a permission / can-moderate check.

    ``reason`` is a stable code; for moderation-target denials it is
    the exact i18n key (``can_moderate_*``) so handlers can pass it to
    ``t()`` directly. ``target_rank`` is populated only by
    :meth:`RankService.can_moderate` (``can_moderate_higher`` rendering
    needs both rank names).
    """

    allowed: bool
    reason: str
    actor_rank: int
    target_rank: int | None = None


class RankService:
    """Rank reads/writes + the permission/moderation verdict seam."""

    def __init__(self, registry: EngineRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings

    # -- rank read/write ----------------------------------------------------

    async def get_rank(self, user_id: int) -> int:
        """Effective global rank, 300s-cached.

        Developers are pinned to 6 BEFORE the cache/DB (legacy
        ``get_user_rank``, bot.py:6627-6629) so a stale users-row can
        never demote an owner. A DB failure degrades to rank 0
        (legacy bot.py:6652-6654) — fail-closed: no grant on error.
        """
        if self._settings.bot.is_developer(user_id):
            return RankLevel.DEVELOPER
        now = time.monotonic()
        cached = _RANK_CACHE.get(user_id, now)
        if cached is not None:
            return cached
        generation = _RANK_GENERATION.snapshot()
        try:
            async with session_for(self._registry, DBName.USERS) as session:
                rank = await UsersRepo(session).get_rank(user_id)
        except Exception:  # noqa: BLE001 — fail-closed to rank 0, never raise
            log.opt(exception=True).warning(
                "rank lookup failed for {uid}; treating as rank 0", uid=user_id
            )
            return RankLevel.USER
        # #1942: a ``/setrank`` that committed while the read above was
        # suspended has already invalidated this key. Re-storing the
        # value it removed would hand the demoted user their old rank
        # back for the whole TTL; returning it once is fine, since it
        # is exactly as fresh as the read that produced it.
        if _RANK_GENERATION.unchanged(generation):
            _RANK_CACHE.put(user_id, rank, now)
        return rank

    async def set_rank(self, user_id: int, rank: int, *, by: int) -> bool:
        """Persist a rank change with the legacy guards; True on success.

        Guards (legacy ``set_user_rank``, bot.py:6661-6671):

        * range 0..6 — BANNED (-1) is not assignable through the setter;
        * developer immutability — a developer's rank cannot be changed
          to anything but 6 (and setting 6 on a non-developer is allowed,
          matching legacy, which only special-cases DEVELOPER_IDS).

        On success the user's cached rank is invalidated immediately so
        the new rank applies on the next update, not after the TTL.
        """
        if rank < MIN_SETTABLE_RANK or rank > MAX_SETTABLE_RANK:
            log.warning("refused rank {rank} for {uid}: out of range", rank=rank, uid=user_id)
            return False
        if self._settings.bot.is_developer(user_id) and rank != RankLevel.DEVELOPER:
            log.warning("refused rank change for developer {uid} (by {by})", uid=user_id, by=by)
            return False
        async with session_for(self._registry, DBName.USERS) as session:
            await UsersRepo(session).set_rank(user_id, rank, by=by)
        invalidate_rank_cache(user_id)
        log.bind(user_id=user_id, rank=rank, by=by).info("rank changed")
        return True

    # -- verdicts ------------------------------------------------------------

    async def check(self, actor_id: int, chat_id: int, permission: str, bot: Bot) -> RankVerdict:
        """May ``actor_id`` exercise ``permission`` in ``chat_id``?"""
        if self._settings.bot.is_developer(actor_id):
            return RankVerdict(True, REASON_DEVELOPER, RankLevel.DEVELOPER)

        # Live TG-admin bypass — same posture as handlers/moderation
        # _require_admin: creator, or an administrator with at least one
        # moderation right (``has_moderation_rights``, #337 — bare
        # status was the hole). ``None`` (API
        # error) must NOT grant; the rank path below still gets its say,
        # so a Telegram hiccup degrades a TG-admin to their bot rank
        # instead of failing the whole gate open OR closed.
        if chat_id < 0 and await is_user_admin(bot, chat_id, actor_id) is True:
            actor_rank = await self.get_rank(actor_id)
            return RankVerdict(True, REASON_TG_ADMIN, actor_rank)

        return await self._check_by_rank(actor_id, permission)

    async def _check_by_rank(self, actor_id: int, permission: str) -> RankVerdict:
        """The rank-matrix half of :meth:`check`, with no TG-admin bypass."""
        actor_rank = await self.get_rank(actor_id)
        # Rank 0 (and BANNED) hold no permissions — legacy bot.py:7003.
        if actor_rank <= RankLevel.USER:
            return RankVerdict(False, REASON_NO_PERMISSION, actor_rank)
        matrix = await self._merged_matrix()
        allowed = matrix.get(actor_rank, {}).get(permission, False)
        reason = REASON_RANK if allowed else REASON_NO_PERMISSION
        return RankVerdict(allowed, reason, actor_rank)

    async def may_manage_ranks(self, actor_id: int, chat_id: int, bot: Bot) -> RankVerdict:
        """May ``actor_id`` WRITE ranks? Stricter than :meth:`check`.

        Ranks in this bot are global (``users.db``), so a grant made in
        one chat gives ``can_ban``/``can_mute``/``can_kick`` in every
        chat the bot serves — :func:`handlers.moderation._require_moderation`
        accepts "TG-admin OR rank-with-permission".

        That is why the live-Telegram-admin bypass must not open this
        door: adminship is evidence about ONE chat, and anybody can
        create a group, add the bot and be its admin. Letting that mint
        global ranks would hand a stranger moderation power in the
        owner's own groups. Developers and users who genuinely hold
        ``can_manage_mods`` by bot rank are unaffected.

        Nothing is lost for the group admin who is refused here: making
        the person a Telegram admin of their group already gives them
        the same moderation commands *in that group* through the very
        bypass this method declines to extend.

        Note this cannot delegate to :meth:`check` — that returns on the
        bypass BEFORE consulting the matrix, so a genuinely-ranked admin
        who also happens to be a chat admin (the normal case for the bot
        owner's own groups) would be refused along with the impostor.
        """
        if self._settings.bot.is_developer(actor_id):
            return RankVerdict(True, REASON_DEVELOPER, RankLevel.DEVELOPER)
        verdict = await self._check_by_rank(actor_id, MANAGE_RANKS_PERMISSION)
        if verdict.allowed:
            return verdict
        # Denied. Say WHY when the actor is a live chat admin: they are
        # about to be told "no" by a bot that lets them ban people here,
        # and the copy for that case explains the difference.
        if chat_id < 0 and await is_user_admin(bot, chat_id, actor_id) is True:
            log.bind(actor=actor_id, chat=chat_id).info(
                "rank write refused: authority is a live-TG-admin bypass only"
            )
            return RankVerdict(False, REASON_TG_ADMIN_ONLY, verdict.actor_rank)
        return verdict

    async def can_moderate(
        self, actor_id: int, target_id: int, chat_id: int, bot: Bot
    ) -> RankVerdict:
        """Target guards, legacy ``can_moderate`` order (bot.py:7580-7622)."""
        actor_rank = await self.get_rank(actor_id)

        # 1. Never yourself — even developers (legacy checks self FIRST).
        if actor_id == target_id:
            return RankVerdict(False, REASON_SELF, actor_rank)

        # 2. Never the chat creator. Legacy probes get_chat_member and
        #    swallows errors (``except: pass`` — guard skipped on API
        #    failure, bot.py:7597-7603); we keep that exact posture but
        #    via the cached creator lookup (legacy itself caches the
        #    creator id for 600s in get_chat_creator_id, bot.py:7096).
        if chat_id < 0:
            creator = await self._creator_id(bot, chat_id)
            if creator is not None and creator == target_id:
                return RankVerdict(False, REASON_CREATOR, actor_rank)

        # 3. Developers moderate everyone (below the creator guard? NO —
        #    legacy puts the developer allow AFTER self+creator probes,
        #    bot.py:7607-7609 — preserved exactly). Note this is the ONLY
        #    bypass legacy granted here: a Telegram admin was still
        #    rank-compared. See handlers.moderation._check_rank_target_ok
        #    on why the caller exempts them anyway (#338).
        if self._settings.bot.is_developer(actor_id):
            return RankVerdict(True, REASON_DEVELOPER, RankLevel.DEVELOPER)

        # 4. Not upward or sideways: target_rank >= actor_rank denies.
        target_rank = await self.get_rank(target_id)
        if target_rank >= actor_rank:
            return RankVerdict(False, REASON_HIGHER, actor_rank, target_rank)
        return RankVerdict(True, REASON_RANK, actor_rank, target_rank)

    # -- internals -----------------------------------------------------------

    async def _merged_matrix(self) -> dict[int, dict[str, bool]]:
        """Merged permission matrix; a DB failure yields the in-code
        defaults (fail towards legacy behavior, never towards a wider
        grant — overrides can only have widened or narrowed cells, and
        without them we enforce the audited legacy matrix)."""
        try:
            async with session_for(self._registry, DBName.MODERATION) as session:
                return await RankRepo(session).merged_matrix()
        except Exception:  # noqa: BLE001 — degrade to in-code defaults
            log.opt(exception=True).warning("rank matrix read failed; using in-code defaults")
            return DEFAULT_RANK_PERMISSIONS

    async def _creator_id(self, bot: Bot, chat_id: int) -> int | None:
        """Chat creator id with a 600s cache (legacy get_chat_creator_id).

        Only successful lookups are cached — an API error returns
        ``None`` uncached so the next call retries (legacy-exact)."""
        now = time.monotonic()
        cached = _CREATOR_CACHE.get(chat_id, now)
        if cached is not None:
            return cached
        generation = _CREATOR_GENERATION.snapshot()
        creator = await chat_creator_id(bot, chat_id)
        # #1942: only ``clear_rank_caches`` invalidates this table, and
        # only tests call it — but a fill landing after it is exactly
        # the cross-test leak that hook exists to prevent, and the
        # guard costs one comparison.
        if creator is not None and _CREATOR_GENERATION.unchanged(generation):
            _CREATOR_CACHE.put(chat_id, creator, now)
        return creator
