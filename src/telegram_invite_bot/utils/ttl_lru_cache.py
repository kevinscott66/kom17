"""Tiny generic TTL+LRU cache shared across handlers, services and CMS.

Extracted from three identical private caches that once lived in
``handlers/antiflood.py``, ``handlers/group_aliases.py`` and
``handlers/wordfilter.py``. #1655: their class names used to be spelled
out here and are deliberately not any more — none of the three survived
the extraction, so naming them only sent a reader after symbols that no
longer exist. The three modules are still consumers, alongside a dozen
others well outside the middleware layer this docstring once claimed.

The shape: an :class:`~collections.OrderedDict` of
``key -> (expires_at, value)`` keyed on a caller-supplied
``time.monotonic()`` timestamp, evicting from the front past
``capacity``. Order is insertion order and reads do NOT promote — see
:meth:`TTLLRUCache.get` for why.

The caller passes ``now`` explicitly (rather than the cache reading the
clock) so tests can drive expiry deterministically — same contract as
the three originals.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLLRUCache(Generic[K, V]):
    """TTL+LRU cache of ``key -> (expires_at, value)`` (monotonic time)."""

    def __init__(self, ttl: float, capacity: int) -> None:
        # #1492. Rejected at construction rather than tolerated. A
        # capacity of 0 silently turns the cache into a no-op — every
        # ``put`` is evicted by the very loop that stored it, every
        # ``get`` misses, and nothing anywhere says so; a negative one
        # is worse, because ``popitem`` then raises KeyError from
        # inside ``put`` in the middle of an update. Today all fifteen
        # construction sites pass a module constant, so neither is
        # reachable — the day one of them is moved into settings, this
        # fails at startup instead.
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._ttl = ttl
        self._capacity = capacity
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K, now: float) -> V | None:
        """Return the live value for ``key``, or ``None``.

        #1492: a hit deliberately does NOT move the entry to the back.
        Promoting on read is the textbook LRU move, and here it was a
        bug: it changes eviction order without extending the TTL, so a
        read taken shortly before expiry parks a soon-to-be-dead entry
        at the back and leaves a longer-lived one at the front to be
        evicted in its place. Measured: capacity 3, ttl 10, entries at
        t=0/5/6, a read of the first at t=9, an insert at t=11 — the
        first is dead by then and survives, while the entry that would
        have lived to t=15 is the one thrown away.

        With a single TTL per cache, insertion order IS expiry order,
        so evicting from the front always discards whatever dies
        soonest and read-promotion buys nothing. Should true LRU
        semantics ever be wanted, the ordering has to be fixed at the
        other end first: sweep the expired off the front in
        ``put_until`` before trimming to capacity.
        """
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if now >= expires_at:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: K, value: V, now: float) -> None:
        self.put_until(key, value, now + self._ttl)

    def put_until(self, key: K, value: V, deadline: float) -> None:
        """Store with an explicit expiry deadline (monotonic)."""
        self._data[key] = (deadline, value)
        self._data.move_to_end(key)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def discard(self, key: K) -> None:
        """Drop ``key`` if present; a no-op when it is not.

        Exists for the claim-then-release pattern: a caller that stores
        a key to reserve some work needs a way to hand the reservation
        back when the work is not going to happen after all. Deliberately
        silent on a missing key so the release path never has to know
        whether the claim survived eviction.
        """
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
