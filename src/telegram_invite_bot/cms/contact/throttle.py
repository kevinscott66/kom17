"""Rate limiting for the public contact form.

The form turns an unauthenticated HTTP POST into a Telegram message in
the operator's own chat. Without a limit that is a remote "fill this
person's inbox" primitive, and — because the bot sends the message —
one that also spends the bot's Telegram send budget.

Two buckets, because one is not enough in either direction:

* **Per client**, so one sender cannot repeat. Keyed on the address
  Cloudflare reports; see :func:`client_key` for why that key is not
  trusted on its own.
* **Global**, so a pool of addresses cannot do together what none of
  them can do alone. This is the bucket that actually holds when the
  per-client key is forged or simply distributed, and it is the reason
  the per-client one can stay generous enough not to block a shared NAT.

Both are the same pure token bucket the transfer middleware uses
(:mod:`telegram_invite_bot.utils.rate_limit`), over the same bounded
TTL+LRU map the other per-key limiters were moved onto in #72–#79: an
unbounded ``dict`` keyed on a *remote-controlled* value is a memory
leak with a sender attached.
"""

from __future__ import annotations

import math
from typing import Final

from telegram_invite_bot.cms.client_ip import client_key
from telegram_invite_bot.utils.rate_limit import BucketState, refill_and_consume
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

# Re-exported: this module was ``client_key``'s home until the guide
# editor needed the same bucketing (#189), and the callers that import
# it from here should not have to care that it moved.
__all__ = ["ContactThrottle", "client_key"]

#: Three messages, refilling one per ten minutes. A person with a real
#: question sends one; a person who mistyped their address sends two.
_PER_CLIENT_CAPACITY: Final[float] = 3.0
_PER_CLIENT_REFILL_PER_SECOND: Final[float] = 1.0 / 600.0

#: Thirty across the whole site, refilling one per minute. Sized so a
#: genuine burst (a link shared somewhere busy) still gets through while
#: a distributed flood settles to one message a minute — an annoyance in
#: one chat rather than an outage of it.
_GLOBAL_CAPACITY: Final[float] = 30.0
_GLOBAL_REFILL_PER_SECOND: Final[float] = 1.0 / 60.0

#: Distinct clients tracked at once. Past this the least-recently-seen
#: bucket is evicted, which does hand a large enough address pool a way
#: to clear someone else's cooldown — the global bucket is what makes
#: that not worth doing.
_CLIENT_CAPACITY: Final[int] = 4096


class ContactThrottle:
    """Per-client and global token buckets over an injected clock.

    ``now`` is passed in by the caller (as with every other limiter in
    the codebase) so the tests can drive expiry and refill exactly
    rather than sleeping through them.
    """

    __slots__ = ("_buckets", "_global")

    def __init__(self) -> None:
        # A bucket must outlive its own refill, or eviction becomes a
        # free reset: a client whose entry expires at 60s while the
        # bucket needs 1800s to refill has no limit at all.
        ttl = math.ceil(_PER_CLIENT_CAPACITY / _PER_CLIENT_REFILL_PER_SECOND)
        # ``_buckets`` and not ``_clients``: the #119 guard reads a
        # ``.get()``/``.put()`` on a receiver named "client" as an
        # unbounded httpx read. Renaming the map is honest — it holds
        # buckets, not clients — and beats four waiver comments.
        self._buckets: TTLLRUCache[str, BucketState] = TTLLRUCache(
            ttl=float(ttl), capacity=_CLIENT_CAPACITY
        )
        self._global = BucketState(tokens=_GLOBAL_CAPACITY, updated_at=0.0)

    def admit(self, client: str, *, now: float) -> bool:
        """Whether this submission may be delivered.

        Checked per client first: a sender who is over their own limit
        must not also drain the shared bucket, or three impatient
        retries from one person would spend the site's whole minute.

        When the global bucket is the one that refuses, the per-client
        token is **not** spent. The sender did nothing wrong, and
        charging them for someone else's flood would turn a shared limit
        into a way to lock individual people out.
        """
        state = self._buckets.get(client, now)
        if state is None:
            state = BucketState(tokens=_PER_CLIENT_CAPACITY, updated_at=now)

        spent, admitted = refill_and_consume(
            state,
            now=now,
            capacity=_PER_CLIENT_CAPACITY,
            refill_per_second=_PER_CLIENT_REFILL_PER_SECOND,
        )
        if not admitted:
            # Persist the refilled-but-unspent state: the reject path is
            # how a cooldown makes forward progress.
            self._buckets.put(client, spent, now)
            return False

        self._global, global_ok = refill_and_consume(
            self._global,
            now=now,
            capacity=_GLOBAL_CAPACITY,
            refill_per_second=_GLOBAL_REFILL_PER_SECOND,
        )
        if not global_ok:
            advanced, _ = refill_and_consume(
                state,
                now=now,
                capacity=_PER_CLIENT_CAPACITY,
                refill_per_second=_PER_CLIENT_REFILL_PER_SECOND,
                cost=0.0,
            )
            self._buckets.put(client, advanced, now)
            return False

        self._buckets.put(client, spent, now)
        return True
