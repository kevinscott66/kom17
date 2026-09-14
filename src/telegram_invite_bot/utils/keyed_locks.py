"""Per-key :class:`asyncio.Lock` registry that does not leak entries.

The stake flows (``/roll``, ``/flip``, ``/roulette``) serialise one
user's check→play→record section with a lock keyed by ``user_id``.
The obvious spelling — ``async with table.setdefault(uid,
asyncio.Lock()):`` — is correct but keeps one Lock object per user
who ever played, for the life of the process. On a bot that lives in
public groups that table only grows.

Why reference counting and not an LRU cap
-----------------------------------------
A bounded table has to evict *something*, and dropping a lock that
someone is currently holding or waiting on is not a memory
optimisation — it is a lost mutual exclusion. The next caller would
``setdefault`` a *fresh* lock, acquire it immediately, and run
concurrently with the holder of the old one. That is exactly the
double-spend window the lock exists to close.

So the registry counts users instead: the slot is created on the
first waiter and removed when the last one leaves. A lock that nobody
is using holds no state worth keeping (an unlocked ``asyncio.Lock``
is indistinguishable from a brand-new one), so dropping it is free;
a lock that somebody *is* using is never dropped.

The counter is incremented before the first ``await`` and decremented
in ``finally``, so on a single event loop no other coroutine can
observe a slot mid-update.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

K = TypeVar("K")


@dataclass(slots=True)
class _Entry(Generic[K]):
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    """Coroutines holding OR waiting for :attr:`lock`. The slot lives
    exactly as long as this is non-zero."""


class KeyedLocks(Generic[K]):
    """A lock per key, allocated on demand and freed when idle."""

    def __init__(self) -> None:
        self._entries: dict[K, _Entry[K]] = {}

    @asynccontextmanager
    async def acquire(self, key: K) -> AsyncIterator[asyncio.Lock]:
        """Hold the lock for ``key`` for the duration of the block."""
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry()
            self._entries[key] = entry
        # Claim the slot BEFORE awaiting the lock — a coroutine parked
        # in ``async with`` still counts as a user, so the holder's
        # exit can't free the slot out from under it.
        entry.users += 1
        try:
            async with entry.lock:
                yield entry.lock
        finally:
            entry.users -= 1
            # Re-read rather than popping blindly: a slot that was
            # already freed and re-created belongs to whoever created
            # it, not to us.
            if entry.users <= 0 and self._entries.get(key) is entry:
                del self._entries[key]

    def __len__(self) -> int:
        """Live slots — the assertion surface for the leak regression."""
        return len(self._entries)
