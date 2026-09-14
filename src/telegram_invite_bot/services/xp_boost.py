"""Read-side resolver for the active per-user XP/coin multiplier (L-21).

The shop ``xp_boost`` item grants a timed ``user_privileges`` row under
privilege_type ``xp_boost`` carrying ``{"multiplier": <int>}`` (written
by :class:`InventoryUseService` for ``XP_BOOST`` plans; see
``services/inventory_use_planner.py``). The *consumption* point is the
per-message earn path in ``middlewares/message_activity.py``: legacy
multiplied the coins it credited for a qualifying message by
``ItemEffects.get_xp_multiplier(user_id)`` (``bot.py:13700`` /
``bot.py:13841-13845`` — the seed describes the boost as "x2 монеты за
сообщения на 1 час").

This module is the new pipeline's equivalent of
``ItemEffects.get_xp_multiplier``: given a session and a user id, return
the multiplier (a ``float`` so a future fractional boost — e.g. 1.5x —
needs no signature change; today the stored values are integers).

Why a TTL cache
===============
The earn path runs on EVERY qualifying group message — a hot path. A
naive "read the privilege row per message" would add an economy-DB
round-trip to every chatter's message even though the vast majority of
users hold no boost. Legacy cached the boost in-process
(``cache_set/cache_get`` with the boost's own TTL, ``bot.py:13574`` /
``bot.py:13710``). We mirror that with a small process-local
:class:`~telegram_invite_bot.utils.ttl_lru_cache.TTLLRUCache`:

* The cache stores the resolved multiplier keyed by ``user_id``.
* The cached value is short-lived (``_CACHE_TTL_SECONDS``) so a
  just-purchased boost shows up within seconds — the cache is a
  read-amplification damper, NOT the source of truth (the privilege
  row's own ``expires_at`` is). A user who buys a boost mid-stream waits
  at most ``_CACHE_TTL_SECONDS`` for it to take effect, which matches
  legacy's cache-staleness window.
* An entry NEVER outlives the grant it describes: when the row carries a
  deadline, the entry's own expiry is clamped to whichever comes first,
  the TTL or that deadline (``put_until``). The staleness the cache is
  allowed to introduce is one-directional — it may under-pay a boost
  bought seconds ago, never over-pay one that has already lapsed.
* ``1.0`` (no boost) is cached too, so a chatter with no boost pays the
  DB cost at most once per TTL window rather than once per message.

The cache uses ``time.monotonic()`` (the :class:`TTLLRUCache` contract)
for its OWN entry expiry; that is independent of the privilege row's
wall-clock ``expires_at``, which is compared against the caller-supplied
``now`` inside :meth:`PrivilegesRepo.get_active`.

Process-local, not shared across workers — exactly like legacy's
in-process cache. A restart resets it; the next message re-reads the row.
"""

from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING

from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.utils.time import unix_ts
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

# No boost → multiplier 1.0 (coins/XP unchanged). Mirrors legacy
# ``get_xp_multiplier`` returning ``1`` when no active boost
# (``bot.py:13713``).
_NO_BOOST = 1.0

# Sanity ceiling on a stored multiplier. The canonical catalog boost is
# ×2 (``_XP_BOOST_NAME_SPECS`` in ``inventory_use_planner``), but the
# legacy monolith writes ``data.get("multiplier", 2)`` straight off an
# operator-seeded shop-item blob, so the column is only as trustworthy
# as whatever an operator once typed into /admin_shop. ×100 leaves any
# plausible promotion room while keeping a fat-fingered row from
# minting a fortune out of one chat message.
_MAX_MULTIPLIER = 100.0

# Cache freshness window: the UPPER bound on how long an entry may live.
# Short enough that a purchase reflects within seconds on the hot path;
# long enough that a single busy user doesn't hammer the economy DB once
# per message. It is only ever a ceiling — a boost with less than this
# left to run is cached only until it lapses (see the ``put_until`` call
# in :func:`active_xp_multiplier`), because legacy never paid out past a
# lapsed grant either: its cache entry expired exactly with the boost
# (``bot.py:13574-13577``, ``ttl=duration * 60``), ``cache_get`` evicted
# on that TTL (``bot.py:4352``), and the reader re-checked the stored
# ``expires`` on top of both (``bot.py:13711-13712``).
_CACHE_TTL_SECONDS = 30.0

# Bounded so a flood of distinct chatters can't grow the map without
# limit (same bound shape as the per-user reward tracker in
# message_activity). Past capacity, least-recently-used users are
# evicted and simply re-read on their next message.
_CACHE_CAPACITY = 2000

# Module-global, process-local. Keyed by user_id → resolved multiplier.
_MULTIPLIER_CACHE: TTLLRUCache[int, float] = TTLLRUCache(
    ttl=_CACHE_TTL_SECONDS, capacity=_CACHE_CAPACITY
)


def _decode_multiplier(raw: str | None) -> float:
    """Pull the multiplier out of a ``user_privileges.value`` JSON blob.

    The column is a polymorphic TEXT store (legacy ``json.dumps`` of
    ``{"multiplier": N}``). A malformed / missing / non-positive payload
    degrades to :data:`_NO_BOOST` so one corrupt row never *reduces*
    a user's earnings below baseline (a multiplier < 1 would silently
    tax them). Read-only — we never rewrite the row.

    The symmetric hole is the one that costs money: Python's ``json``
    decoder accepts the non-standard ``Infinity`` token, and ``inf``
    clears every ``>= 1.0`` check, so a single corrupt row would
    multiply the user's coin earnings to ``inf``. Non-finite values and
    anything past :data:`_MAX_MULTIPLIER` clamp back to baseline — a row
    that far out of range is corruption, not a generous admin.
    """
    if not raw:
        return _NO_BOOST
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return _NO_BOOST
    if not isinstance(decoded, dict):
        return _NO_BOOST
    value = decoded.get("multiplier")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return _NO_BOOST
    multiplier = float(value)
    if not math.isfinite(multiplier):
        return _NO_BOOST
    # A stored multiplier below 1.0 would tax the user; clamp to
    # baseline. Above it, values pass through up to the sanity ceiling.
    if multiplier < _NO_BOOST or multiplier > _MAX_MULTIPLIER:
        return _NO_BOOST
    return multiplier


async def active_xp_multiplier(
    session: AsyncSession,
    user_id: int,
    now: datetime,
) -> float:
    """Return the user's active XP/coin multiplier (``1.0`` if none).

    ``now`` is the caller's wall-clock instant (the earn path already
    computes one per message); it drives the privilege row's
    ``expires_at`` comparison in :meth:`PrivilegesRepo.get_active` so an
    expired boost resolves to baseline. ``session`` is the caller's
    economy-DB session (the earn path opens one to credit coins; passing
    it here avoids a second session).

    Result is cached process-locally for at most
    :data:`_CACHE_TTL_SECONDS` — including the ``1.0`` no-boost answer —
    so the common case (a chatter with no boost) costs at most one DB
    read per TTL window rather than one per message. A boost that lapses
    sooner than that caps its own entry, so the cache can never keep
    paying out a grant the user no longer holds.
    """
    mono = time.monotonic()
    cached = _MULTIPLIER_CACHE.get(user_id, mono)
    if cached is not None:
        return cached

    row = await PrivilegesRepo(session).get_active(user_id, "xp_boost", now=now)
    multiplier = _NO_BOOST if row is None else _decode_multiplier(row.value)
    deadline = mono + _CACHE_TTL_SECONDS
    if row is not None and row.expires_at > 0:
        # Never cache a multiplier past the grant that justifies it. The
        # cache holds only the resolved float, so without this clamp an
        # entry written one second before expiry keeps DOUBLING every
        # coin credit for the rest of the TTL window — real money, paid
        # out of the owner's pocket, for a boost that has already ended.
        # ``get_active`` guarantees a returned row is still live, so the
        # remainder here is strictly positive.
        remaining = row.expires_at - unix_ts(now, where="xp_boost.active_xp_multiplier")
        deadline = min(deadline, mono + remaining)
    _MULTIPLIER_CACHE.put_until(user_id, multiplier, deadline)
    return multiplier


def clear_cache() -> None:
    """Drop every cached multiplier. For tests and a future
    /admin cache-bust; not used on the hot path."""
    _MULTIPLIER_CACHE.clear()
