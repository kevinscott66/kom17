"""Cross-DB effect gates for hot paths outside their owning session (T3).

Two consumers need a one-bit answer from a database their own flow does
not hold a session for:

* The per-message activity middleware (economy side) needs the group's
  ``coins_enabled`` toggle, which lives in ``moderation.group_mod_config``
  (L-54). Opening a moderation session per chat message would double the
  per-message DB cost, so :class:`GroupCoinsGate` caches the bit behind a
  short TTL — the same posture the antiflood / word-filter / alias
  middlewares already take for their per-group config reads.
* The /mute moderation flow (moderation side) needs the target's active
  ``mute_protection`` privilege, which lives in
  ``economy.user_privileges``
  (legacy ``ItemEffects.has_mute_protection`` at ``bot.py:13647``,
  consumed by ``cmd_mute`` at ``bot.py:31724``/``31766``). /mute is a
  cold admin command, so :func:`has_mute_protection` reads uncached.

Both gates **fail toward legacy behaviour** on storage errors: earning
stays ON (legacy had no per-group gate at all) and mute-protection reads
as absent (legacy's check sat inside a try that defaulted to "not
protected"). A DB hiccup must never block chat propagation or brick a
moderation command.

This module is deliberately registry-based (it opens and closes its own
short session per miss) rather than repo-injected, because both call
sites run outside the middleware that would normally inject the matching
repo — that mismatch is the entire reason the helpers exist.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="services.effect_gates")

# Cache tuning mirrors handlers.antiflood's per-group config cache: a
# toggle flip lands within a minute, and a few thousand groups fit
# comfortably.
_COINS_TTL_SECONDS = 60.0
_COINS_CACHE_CAPACITY = 4096


class GroupCoinsGate:
    """TTL-cached read of ``group_mod_config.coins_enabled`` (L-54).

    One instance per middleware (process-local cache). ``coins_enabled``
    answers "may this group's members passively earn per-message coins?"
    — the *global* ``EconomyConfig.coins_enabled`` gate stays where it
    is; this is the per-group override layered on top.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        self._registry = registry
        self._cache: TTLLRUCache[int, bool] = TTLLRUCache(
            ttl=_COINS_TTL_SECONDS, capacity=_COINS_CACHE_CAPACITY
        )

    async def coins_enabled(self, group_id: int) -> bool:
        """Return the group's earn toggle; True (legacy posture) on error."""
        now = time.monotonic()
        cached = self._cache.get(group_id, now)
        if cached is not None:
            return cached
        try:
            sessionmaker = self._registry.session(DBName.MODERATION)
            async with sessionmaker() as session:
                cfg = await GroupModConfigRepo(session).get_or_default(group_id)
            enabled = cfg.coins_enabled
        except Exception as exc:  # noqa: BLE001 — gate must never break propagation
            log.bind(group=group_id, error=str(exc)).warning(
                "coins_enabled read failed; defaulting to enabled"
            )
            return True
        self._cache.put(group_id, enabled, now)
        return enabled


async def has_mute_protection(registry: EngineRegistry, user_id: int) -> bool:
    """True iff ``user_id`` holds an active global ``mute_protection`` grant.

    Mirrors legacy ``ItemEffects.has_mute_protection`` (``bot.py:13647``):
    global scope (``group_id=0`` — the shop item is not per-chat), payload
    ignored, presence + non-expired ``expires_at`` is the entire signal.
    Uncached — /mute is a cold admin path. Fails closed to "not
    protected" so a broken economy DB cannot block moderation.

    ``now`` is aware-UTC (not :func:`utils.time.db_now`'s naive form)
    because :meth:`PrivilegesRepo.get_active` compares via
    ``now.timestamp()`` — a naive value would be interpreted in the
    process's local zone and skew the expiry check. Same convention as
    ``handlers/daily.py``'s effects resolution.
    """
    try:
        sessionmaker = registry.session(DBName.ECONOMY)
        async with sessionmaker() as session:
            row = await PrivilegesRepo(session).get_active(
                user_id, "mute_protection", now=datetime.now(UTC)
            )
        return row is not None
    except Exception as exc:  # noqa: BLE001 — moderation must keep working
        # ERROR here, WARNING in ``GroupCoinsGate`` above, and the
        # asymmetry is deliberate (#1603). The coins gate falling back
        # to True restores the legacy default and costs nobody
        # anything; this fallback silently voids a protection the user
        # PAID for, and the mute lands exactly as if it had never been
        # bought — neither the target nor the admin is told the read
        # failed. The log line is the only trace there is.
        #
        # ``chat_id`` is deliberately NOT threaded in for this line.
        # The sole caller (handlers/moderation.py:1365) goes straight
        # on to the restriction, whose own success log binds
        # ``chat_id`` and ``target`` for the same user, so the chat is
        # one line away in the same stream. Widening the signature to
        # duplicate it would buy nothing.
        log.bind(uid=user_id, error=str(exc)).error(
            "mute_protection read failed; treating as not protected"
        )
        return False
