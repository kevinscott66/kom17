"""Ceilings on the two rank-system caches (#74 growth class).

Both tables used to be plain dicts holding an expiry inside the value:
entries went stale on read but nothing ever removed them, so the rank
table grew by one entry per distinct user who ever ran a gated command
and the creator table by one per chat, for the lifetime of the process.

The mechanics of TTL + LRU belong to
:class:`~telegram_invite_bot.utils.ttl_lru_cache.TTLLRUCache` and are
pinned in ``tests/unit/utils/test_ttl_lru_cache.py``. What is pinned
*here* is the wiring: that these two tables are bounded at all, that the
bound is the one the module documents, that expiry survived the swap —
and, the reason an LRU cap is acceptable in this module at all, that
losing an entry to eviction is a plain miss. A miss sends ``get_rank``
back to the DB; it can never serve a rank the caller has not earned.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.services import rank_service as rs

RANK_TTL = rs.RANK_CACHE_TTL_SECONDS
CREATOR_TTL = rs.CREATOR_CACHE_TTL_SECONDS


@pytest.fixture(autouse=True)
def _clean_caches() -> None:
    rs.clear_rank_caches()


def test_the_rank_table_stops_growing_at_its_ceiling() -> None:
    """One entry per distinct user, forever, was the leak."""
    for uid in range(rs._MAX_CACHED_USERS + 1):
        rs._RANK_CACHE.put(uid, 1, now=0.0)

    # The oldest entry made room for the newest instead of adding to it.
    assert rs._RANK_CACHE.get(0, now=1.0) is None
    assert rs._RANK_CACHE.get(rs._MAX_CACHED_USERS, now=1.0) == 1


def test_the_creator_table_stops_growing_at_its_ceiling() -> None:
    for chat_id in range(rs._MAX_CACHED_CHATS + 1):
        rs._CREATOR_CACHE.put(-chat_id, 777, now=0.0)

    assert rs._CREATOR_CACHE.get(-0, now=1.0) is None
    assert rs._CREATOR_CACHE.get(-rs._MAX_CACHED_CHATS, now=1.0) == 777


def test_eviction_is_a_miss_never_a_grant() -> None:
    """Why an LRU cap is safe here and not in ``transfer_rights``.

    Somebody able to make the bot see enough distinct users can push any
    chosen entry out of the table. That has to cost a DB read and
    nothing else — if an evicted rank could come back, the cap would be
    a privilege escalation instead of a memory bound.
    """
    victim = 1_000_000_000  # outside the flood range below
    rs._RANK_CACHE.put(victim, 6, now=0.0)
    for uid in range(rs._MAX_CACHED_USERS):
        rs._RANK_CACHE.put(uid, 0, now=0.0)

    assert rs._RANK_CACHE.get(victim, now=1.0) is None


def test_a_cached_rank_still_expires() -> None:
    rs._RANK_CACHE.put(1, 3, now=0.0)

    assert rs._RANK_CACHE.get(1, now=RANK_TTL - 1) == 3
    assert rs._RANK_CACHE.get(1, now=RANK_TTL) is None


def test_a_cached_creator_still_expires() -> None:
    rs._CREATOR_CACHE.put(-1, 777, now=0.0)

    assert rs._CREATOR_CACHE.get(-1, now=CREATOR_TTL - 1) == 777
    assert rs._CREATOR_CACHE.get(-1, now=CREATOR_TTL) is None


def test_invalidation_drops_one_user_and_leaves_the_rest() -> None:
    rs._RANK_CACHE.put(1, 3, now=0.0)
    rs._RANK_CACHE.put(2, 4, now=0.0)

    rs.invalidate_rank_cache(1)

    assert rs._RANK_CACHE.get(1, now=1.0) is None
    assert rs._RANK_CACHE.get(2, now=1.0) == 4


def test_invalidating_an_absent_user_is_silent() -> None:
    """The rank write path invalidates unconditionally; a user whose
    entry already expired (or lost the LRU race) must not raise."""
    rs.invalidate_rank_cache(99_999)


def test_clearing_empties_both_tables() -> None:
    rs._RANK_CACHE.put(1, 3, now=0.0)
    rs._CREATOR_CACHE.put(-1, 777, now=0.0)

    rs.clear_rank_caches()

    assert rs._RANK_CACHE.get(1, now=1.0) is None
    assert rs._CREATOR_CACHE.get(-1, now=1.0) is None
