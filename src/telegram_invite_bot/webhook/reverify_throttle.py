"""Rate limit for the one payment route nobody signs.

Three of the four payment webhooks authenticate the caller before they
do any work: Crypto Pay, RollyPay and Stripe all carry a signature, and
``hmac.compare_digest`` over the raw body settles whether the request is
real for the cost of a hash. YooKassa signs nothing. Its route
authenticates by calling *back* to the merchant API and asking whether
the payment it was told about exists — a blocking HTTPS round trip, run
off-loop in a four-thread pool with a deadline (see
``webhook/payments.py``).

That makes one unauthenticated POST worth one outbound merchant-API
call, which is an amplifier: the attacker spends a TCP connection and
we spend a round trip to our acquirer, a thread out of four, and — once
the pool is full — the redelivery of whatever genuine notification was
queued behind the flood. The pool bounds how much of the bot a flood
can take (#1439); it does not bound how often we call YooKassa, and the
acquirer has an opinion about that too.

So: a token bucket in front of the pool. Refused deliveries answer 429,
which YooKassa's contract treats the way it treats every non-200 — the
notification is redelivered for 24 hours — so a limit that occasionally
catches a genuine burst delays a credit rather than dropping one. That
is the same trade the reverify timeout already makes one branch down.

Two buckets, for the reason :mod:`cms.contact.throttle` documents at
length: the per-client one keeps a single source from repeating, and
the global one is what actually holds when the client key is forged or
the flood is distributed. A webhook client key is *especially* weak —
the header ``client_key`` prefers is Cloudflare's, and a request that
reaches the origin directly writes its own — so here the global bucket
is the real control.

The per-client bucket is doing something subtler than usual, and it is
worth being explicit about it, because it is also why both budgets
below look generous. On a contact form every visitor is their own
client; on this route *all* honest traffic shares one client key,
because it all comes from the acquirer. So the per-client bucket is not
"one visitor's fair share" here — it is the merchant's entire
notification stream, and it has to be wide enough for the worst honest
minute there is. What it still buys is isolation: an attacker POSTing
from somewhere else spends their own budget first, and the genuine
acquirer's stream is untouched until the global ceiling is reached.
"""

from __future__ import annotations

import math
from typing import Final

from telegram_invite_bot.utils.rate_limit import BucketState, refill_and_consume
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

__all__ = ["ReverifyThrottle"]

#: Sixty in hand per client, two back a second. Deliberately loose for
#: a limiter, because of who the honest client is: every genuine
#: notification on this route comes from the acquirer, so one client
#: key covers *all* real traffic and a budget sized for a single
#: payment would ration the merchant's own business. The figure that
#: has to be survivable is the backlog YooKassa replays after an
#: outage — hundreds of notifications as fast as it can open sockets,
#: every one of them a payment somebody actually made. Sixty go
#: straight through and the rest drain at two a second, which for a
#: redelivery storm is a delay and never a loss.
_PER_CLIENT_CAPACITY: Final[float] = 60.0
_PER_CLIENT_REFILL_PER_SECOND: Final[float] = 2.0

#: Four times that across every client at once. The multiple is the
#: point: the global bucket must sit far enough above one sender's
#: budget that a lone honest acquirer never trips it, and still be a
#: ceiling when the flood arrives from a thousand addresses and the
#: per-client bucket has nothing to say. Note what this is *not*
#: defending: concurrency is already bounded by the four-thread pool,
#: which answers 503 without spending an outbound call. This bounds the
#: sustained rate at which the bot can be made to telephone its own
#: acquirer.
_GLOBAL_CAPACITY: Final[float] = 240.0
_GLOBAL_REFILL_PER_SECOND: Final[float] = 8.0

#: Distinct client keys tracked at once. The key is remote-controlled,
#: so the map has to be bounded or it is a memory leak with a sender
#: attached (#72-#79). Eviction hands a large enough address pool a way
#: to clear its own cooldown; the global bucket is why that buys little.
_CLIENT_CAPACITY: Final[int] = 4096


class ReverifyThrottle:
    """Per-client and global token buckets over an injected clock.

    ``now`` comes from the caller, as with every other limiter here, so
    tests drive refill and expiry exactly instead of sleeping.
    """

    __slots__ = ("_buckets", "_global")

    def __init__(self) -> None:
        # The entry must outlive the refill it is tracking, or eviction
        # is a free reset.
        ttl = math.ceil(_PER_CLIENT_CAPACITY / _PER_CLIENT_REFILL_PER_SECOND)
        self._buckets: TTLLRUCache[str, BucketState] = TTLLRUCache(
            ttl=float(ttl), capacity=_CLIENT_CAPACITY
        )
        self._global = BucketState(tokens=_GLOBAL_CAPACITY, updated_at=0.0)

    def admit(self, client: str, *, now: float) -> bool:
        """Whether this delivery may reach the reverify pool.

        Per client first, so one sender over their own limit cannot also
        drain the shared budget. When the *global* bucket is the one
        that refuses, the per-client token is handed back: the sender
        did nothing wrong, and charging them for someone else's flood
        would turn a shared limit into a way to lock one client out.
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
