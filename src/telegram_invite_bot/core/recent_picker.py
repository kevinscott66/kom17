"""Random pick that does not immediately repeat itself.

``random.choice`` over a small pool repeats far more often than users
expect it to: with the seven-entry ``/quote`` pool, two consecutive
calls collide 14% of the time and a run of ten calls almost certainly
shows the same quote twice. Legacy shipped exactly that, and on a
content command it reads as the bot being broken rather than as
probability.

This keeps a short per-key ring of what was served recently and picks
from what is left. It is deliberately *in-process* and *lossy*:

* Restarting the bot forgets the history — the cost is one possible
  repeat, so persisting it would buy nothing for a database write per
  joke.
* The key is the caller's choice (chat id for ``/joke``, so two groups
  don't shrink each other's pool).
* The key table is bounded; the oldest key is evicted rather than
  letting a bot in ten thousand groups hold a ring for each forever.

Not thread-safe by design: the worst outcome of a race is a repeat,
which is the exact thing we are merely *reducing*, never guaranteeing.
"""

from __future__ import annotations

import random
from collections import OrderedDict, deque
from collections.abc import Sequence
from typing import Final

#: How many keys keep a history at once. A bot in more chats than this
#: still works — the least-recently-used ring is dropped and that chat
#: simply gets an unfiltered pick next time.
_MAX_KEYS: Final = 512


def _memory_for(pool_size: int) -> int:
    """How many recent picks to exclude for a pool of this size.

    Half the pool, capped so at least two entries always remain
    eligible — excluding everything would make the pick deterministic
    (a fixed rotation), which is a different kind of obviously-not-random.
    """
    return max(0, min(pool_size // 2, pool_size - 2))


class RecentPicker:
    """Per-key random pick avoiding the recently served entries."""

    def __init__(self, *, max_keys: int = _MAX_KEYS) -> None:
        self._recent: OrderedDict[object, deque[str]] = OrderedDict()
        self._max_keys = max_keys

    def clear(self) -> None:
        """Forget every key's history.

        Exists for tests: the pickers are module-level singletons, so
        without this one test's pick narrows the next test's candidate
        list and a fixed-RNG assertion starts depending on test order.
        """
        self._recent.clear()

    def pick(self, key: object, pool: Sequence[str]) -> str:
        """Choose from ``pool``, preferring entries not served recently.

        Raises :class:`IndexError` on an empty pool — a caller with no
        content to serve has a bug, and silently returning ``""`` would
        surface as an empty Telegram message the API rejects anyway.
        """
        if not pool:
            raise IndexError("cannot pick from an empty pool")

        memory = _memory_for(len(pool))
        if memory <= 0:
            return random.choice(pool)  # noqa: S311 — cosmetic, not crypto

        ring = self._recent.get(key)
        if ring is None or ring.maxlen != memory:
            # A changed pool size (a pool edited between deploys) makes
            # the old ring's length wrong; rebuild it, keeping whatever
            # history still fits so an edit doesn't cause a repeat.
            ring = deque(ring or (), maxlen=memory)
        self._recent.pop(key, None)
        self._recent[key] = ring
        while len(self._recent) > self._max_keys:
            self._recent.popitem(last=False)

        seen = set(ring)
        candidates = [entry for entry in pool if entry not in seen] or list(pool)
        chosen = random.choice(candidates)  # noqa: S311 — cosmetic, not crypto
        ring.append(chosen)
        return chosen
