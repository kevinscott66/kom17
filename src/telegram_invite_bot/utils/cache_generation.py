"""#1942: make "was this cache invalidated while I was away?" answerable.

Every process-global cache in this tree fills the same way — miss, read,
store:

.. code-block:: python

    cached = _CACHE.get(key, now)
    if cached is not None:
        return cached
    value = await <a database read>
    _CACHE.put(key, value, now)

The ``await`` is the whole problem. An invalidation that lands while the
coroutine is suspended clears an entry that does not exist yet, and the
store afterwards puts the value that invalidation existed to remove back
for the FULL TTL. The write it was racing has already committed, so
nothing will correct it: the cache is not stale by a few milliseconds,
it is wrong until the TTL runs out.

That is not hypothetical. ``/setrank`` demoting a moderator, ``/perm``
revoking a permission and ``/cmdcfg`` raising a command's minimum rank
all invalidate right after they commit, and all three lose to any
handler that happened to be reading the same table at that moment — the
demoted moderator keeps their old permissions for five more minutes,
process-wide.

A counter is enough to see it. Take a snapshot before the read, compare
after: if anything invalidated in between, drop the value on the floor
rather than storing it. The caller still RETURNS what it read — that
answer is as fresh as the read that produced it — it just does not
speak for the next five minutes on the strength of it.

Deliberately one counter per cache rather than one per key. A key-level
version map is another unbounded dict to size and evict, and it buys
nothing: the only cost of a false positive — some other key was
invalidated during our read — is one skipped fill, which is one extra
database read the next time that key is asked for. Being wrong in that
direction is free; being wrong in the other direction is the bug above.

No locking, on purpose. There is one event loop, and neither
:meth:`CacheGeneration.bump` nor :meth:`CacheGeneration.snapshot`
awaits, so no other coroutine can interleave inside them.
"""

from __future__ import annotations


class CacheGeneration:
    """Monotone counter guarding one cache's fill-after-await window."""

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value = 0

    def snapshot(self) -> int:
        """Take this BEFORE the read; hand it to :meth:`unchanged` after."""
        return self._value

    def bump(self) -> None:
        """Call from every invalidator, next to the clear itself.

        Bumping without clearing is harmless (a skipped fill); clearing
        without bumping is the defect this module exists to close, so
        the two belong on adjacent lines.
        """
        self._value += 1

    def unchanged(self, snapshot: int) -> bool:
        """``True`` when nothing invalidated since ``snapshot`` was taken."""
        return self._value == snapshot


__all__ = ["CacheGeneration"]
