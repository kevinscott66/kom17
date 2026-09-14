"""Unit tests for the shared TTL+LRU cache (utils.ttl_lru_cache).

Consolidates the cache tests that previously lived next to the three
identical private classes in ``handlers/antiflood.py``,
``handlers/group_aliases.py`` and ``handlers/wordfilter.py``:

* hit within TTL / miss after TTL expiry;
* LRU eviction past capacity;
* explicit ``put_until`` deadlines (antiflood mute bookkeeping);
* ``clear`` empties the cache;
* #1492: capacity is validated, and a read does not reorder eviction.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache


def test_cache_hit_within_ttl() -> None:
    cache: TTLLRUCache[str, str] = TTLLRUCache(ttl=10.0, capacity=4)
    cache.put("k", "v", now=100.0)
    assert cache.get("k", now=105.0) == "v"


def test_cache_expires_at_ttl() -> None:
    cache: TTLLRUCache[str, str] = TTLLRUCache(ttl=10.0, capacity=4)
    cache.put("k", "v", now=100.0)
    assert cache.get("k", now=110.0) is None


def test_cache_expires_after_ttl() -> None:
    cache: TTLLRUCache[int, dict[str, str]] = TTLLRUCache(ttl=10.0, capacity=10)
    cache.put(1, {"бал": "balance"}, now=100.0)
    assert cache.get(1, now=111.0) is None


def test_cache_evicts_lru_over_capacity() -> None:
    cache: TTLLRUCache[int, str] = TTLLRUCache(ttl=100.0, capacity=2)
    cache.put(1, "a", now=0.0)
    cache.put(2, "b", now=0.0)
    cache.put(3, "c", now=0.0)  # evicts key 1 (oldest)
    assert cache.get(1, now=1.0) is None
    assert cache.get(2, now=1.0) == "b"
    assert cache.get(3, now=1.0) == "c"


def test_cache_put_until_explicit_deadline() -> None:
    cache: TTLLRUCache[str, bool] = TTLLRUCache(ttl=1.0, capacity=4)
    cache.put_until("k", True, deadline=500.0)
    assert cache.get("k", now=499.0) is True
    assert cache.get("k", now=500.0) is None


def test_cache_clear_empties() -> None:
    cache: TTLLRUCache[str, str] = TTLLRUCache(ttl=10.0, capacity=4)
    cache.put("k", "v", now=100.0)
    cache.clear()
    assert cache.get("k", now=100.0) is None


@pytest.mark.parametrize("capacity", [0, -1])
def test_a_capacity_below_one_is_rejected_at_construction(capacity: int) -> None:
    """#1492. Both bad values used to fail later and quietly.

    Zero made every ``put`` evict itself, so the cache became a no-op
    that nothing reported; negative made ``popitem`` raise KeyError
    from inside ``put``, in the middle of whatever update was running.
    """
    with pytest.raises(ValueError, match="capacity must be >= 1"):
        TTLLRUCache[str, str](ttl=10.0, capacity=capacity)


def test_a_read_does_not_save_a_dying_entry_at_a_live_ones_expense() -> None:
    """#1492. The exact sequence that read-promotion got wrong.

    ``a`` is read at t=9, one tick before it dies. Under the old
    ``move_to_end`` in ``get`` that parked it at the back, so the
    insert at t=11 evicted the front — ``b``, which had four more
    seconds to live — and kept a corpse. Insertion order is expiry
    order here, so the front is always the right thing to drop.
    """
    cache: TTLLRUCache[str, str] = TTLLRUCache(ttl=10.0, capacity=3)
    cache.put("a", "A", now=0.0)
    cache.put("b", "B", now=5.0)
    cache.put("c", "C", now=6.0)

    assert cache.get("a", now=9.0) == "A"

    cache.put("d", "D", now=11.0)

    assert cache.get("b", now=11.0) == "B"
    assert cache.get("c", now=11.0) == "C"
    assert cache.get("d", now=11.0) == "D"
    assert cache.get("a", now=11.0) is None


def test_a_read_still_does_not_extend_the_deadline() -> None:
    """#1492. Reading is not a renewal, and never was — pinned so the
    fix above cannot be mistaken for one and "restored" later."""
    cache: TTLLRUCache[str, str] = TTLLRUCache(ttl=10.0, capacity=4)
    cache.put("k", "v", now=0.0)
    assert cache.get("k", now=9.0) == "v"
    assert cache.get("k", now=10.0) is None
