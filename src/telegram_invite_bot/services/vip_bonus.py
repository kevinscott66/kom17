"""Read-side resolver for the VIP per-message coin bonus (#490).

Legacy credited a qualifying group message in two steps
(``bot.py:43839-43842``)::

    multiplier = ItemEffects.get_xp_multiplier(user_id)
    reward = COINS_MESSAGE_REWARD * multiplier
    reward += ItemEffects.get_message_reward_bonus(user_id)
    add_coins(user_id, reward, "За сообщение в чате")

The port carried the multiplier over (:mod:`.xp_boost`) but dropped the
second line, so :attr:`VipProfile.message_bonus` — an advertised, paid
perk (``i18n/data/ru.yaml`` VIP card, ``handlers/vip.py:177``) — was
read by nothing. This module is the new pipeline's equivalent of
``ItemEffects.get_message_reward_bonus`` (``bot.py:13537-13542``): given
an economy session and a user id, return the extra coins to add.

The bonus is an *addend*, not a factor: legacy added it AFTER the boost
multiplication, so an ``xp_boost`` never multiplies the VIP bonus. The
caller must preserve that order.

Why a TTL cache
===============
Same reasoning as :mod:`.xp_boost`, which this module deliberately
mirrors: the earn path runs on every qualifying message, and the vast
majority of chatters hold no VIP grant. Caching the resolved bonus —
including the ``0`` no-VIP answer — keeps the common case at one
economy-DB read per user per TTL window instead of one per message.

The cache is a read-amplification damper, not the source of truth: the
grant's own ``vip_till`` is compared against the caller-supplied ``now``
inside :meth:`VipRepo.get_active_profile`. A user whose VIP is granted
or expires mid-stream sees the change within
:data:`_CACHE_TTL_SECONDS`. Process-local; a restart resets it.

Only the GLOBAL grant is read (``group_id=None``), matching legacy: its
``get_message_reward_bonus`` called ``get_vip_profile(user_id)`` with a
single argument, which reads ``users.vip_till`` and never the
per-chat ``user_group_vip`` table.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

# No active VIP grant → no extra coins. Mirrors legacy
# ``get_message_reward_bonus`` returning ``0`` for a non-VIP
# (``bot.py:13540-13541``).
_NO_BONUS = 0

# Sanity ceiling on the resolved bonus. Today :class:`VipProfile` is a
# frozen dataclass with ``message_bonus = 1``, so this is unreachable —
# it exists so that a future per-tier VIP table cannot turn one edited
# row into an unbounded mint on the highest-volume credit path in the
# bot. A value past the ceiling is treated as corruption, not generosity.
_MAX_BONUS = 100

# Matched to :mod:`.xp_boost` so both hot-path lookups age out together
# and a VIP purchase and a boost purchase become visible on the same
# timescale.
_CACHE_TTL_SECONDS = 30.0

# Bounded so a flood of distinct chatters cannot grow the map without
# limit; past capacity the least-recently-used users are evicted and
# re-read on their next message.
_CACHE_CAPACITY = 2000

# Module-global, process-local. Keyed by user_id → resolved bonus.
_BONUS_CACHE: TTLLRUCache[int, int] = TTLLRUCache(ttl=_CACHE_TTL_SECONDS, capacity=_CACHE_CAPACITY)


async def active_message_bonus(
    session: AsyncSession,
    user_id: int,
    now: datetime,
) -> int:
    """Return the user's VIP per-message coin bonus (``0`` if not VIP).

    ``now`` is the caller's wall-clock instant, forwarded to
    :meth:`VipRepo.get_active_profile` so an expired grant resolves to
    ``0``. ``session`` is the caller's economy-DB session — the earn path
    already opens one to credit the coins, and passing it here avoids a
    second session on the hot path.

    The result is cached process-locally for :data:`_CACHE_TTL_SECONDS`,
    the ``0`` answer included; see the module docstring.
    """
    mono = time.monotonic()
    cached = _BONUS_CACHE.get(user_id, mono)
    if cached is not None:
        return cached

    profile = await VipRepo(session).get_active_profile(user_id, now=now)
    bonus = _NO_BONUS if profile is None else int(profile.message_bonus)
    if bonus < _NO_BONUS or bonus > _MAX_BONUS:
        bonus = _NO_BONUS
    _BONUS_CACHE.put(user_id, bonus, mono)
    return bonus


def clear_cache() -> None:
    """Drop every cached bonus. For tests and a future /admin cache-bust;
    not used on the hot path."""
    _BONUS_CACHE.clear()
