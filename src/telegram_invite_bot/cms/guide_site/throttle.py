"""Rate limiting for failed guide-editor secret attempts (#189).

``POST /commands/edit`` authenticates on a single shared secret from
``GUIDES_EDIT_SECRET``. The comparison is constant-time, which stops a
timing oracle but says nothing about *how many* guesses an anonymous
caller gets: without a limit the answer was "as many as the network
allows", and the endpoint sits on a public domain that anyone can find
from the bot's own /help link.

Only **failed** attempts are counted. A correct secret is the operator
saving their own page, possibly several times in a row while editing —
charging them for that would make the editor worse without making the
guess any harder. An attacker who does not have the secret fails by
definition, so the limit lands entirely on them.

Two buckets, the same shape and for the same reasons as the contact
form's (:mod:`telegram_invite_bot.cms.contact.throttle`): per client so
one address cannot repeat, and global so a pool of addresses cannot do
together what none of them can do alone. The primitives are the shared
token bucket and the bounded TTL+LRU map, because a plain ``dict``
keyed on a remote-controlled address is the memory leak #72–#79 went
through the codebase to remove.

The global bucket carries a cost worth stating plainly: while it is
empty *everyone* is refused, the operator included. That is not an
oversight. A refusal has to hide whether the submitted secret was
right — a limit that still answers "wrong" on every request has
rationed nothing, because the attacker learns the same bit either way.
So the site-wide budget cannot make an exception for a correct secret
without ceasing to be a limit, and a guesser willing to sustain six
failures a minute can hold the editor shut. The trade is deliberate:
being locked out of the web editor costs the operator a detour through
the ``.md`` files and a deploy, whereas a guessed secret hands a
stranger write access to a page on the domain shown to the bank. What
the design does owe the operator is a reason, so
:class:`RefusedBy` names which budget said no and the router logs the
site-wide case loudly — an invisible lockout would be the real bug.
"""

from __future__ import annotations

import enum
import math
from typing import Final

from telegram_invite_bot.utils.rate_limit import BucketState, refill_and_consume
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

#: Five wrong guesses, refilling one per minute. An operator who
#: fat-fingers a paste gets several tries back within the minute; a
#: guesser gets 1440 attempts a day against a secret with far more than
#: 1440 possibilities.
_PER_CLIENT_CAPACITY: Final[float] = 5.0
_PER_CLIENT_REFILL_PER_SECOND: Final[float] = 1.0 / 60.0

#: Sixty wrong guesses across the whole site, refilling one per ten
#: seconds. This is the bucket that holds when the per-client key is
#: forged or the guessing is spread over a botnet, and it is why the
#: per-client one can stay forgiving enough for a shared office NAT.
_GLOBAL_CAPACITY: Final[float] = 60.0
_GLOBAL_REFILL_PER_SECOND: Final[float] = 1.0 / 10.0

#: Distinct clients tracked at once, then least-recently-seen eviction.
#: A large enough address pool can push someone else's entry out and
#: clear their cooldown; the global bucket is what makes that pointless.
_CLIENT_CAPACITY: Final[int] = 4096


class RefusedBy(enum.Enum):
    """Which budget turned an attempt away.

    The two refusals are identical to the caller and must stay that
    way, but they mean opposite things to the operator reading the
    journal: ``CLIENT`` is one address that has been guessing, while
    ``SITE`` means the whole editor is shut — including for the
    operator — until the site-wide budget refills.
    """

    CLIENT = "client"
    SITE = "site"


class EditorThrottle:
    """Per-client and global budgets for *wrong* secret submissions.

    Split into a peek (:meth:`allow_attempt`) and a charge
    (:meth:`note_failure`) rather than the contact form's single
    ``admit``, because here the price depends on the answer: the
    comparison has to happen before we know whether anything is owed.

    ``now`` is injected by the caller, as with every other limiter
    here, so the tests drive refill and expiry exactly instead of
    sleeping.
    """

    __slots__ = ("_buckets", "_global")

    def __init__(self) -> None:
        # The entry must outlive its own refill, or eviction becomes a
        # free reset: an entry that expires before the bucket refills is
        # not a limit at all.
        ttl = math.ceil(_PER_CLIENT_CAPACITY / _PER_CLIENT_REFILL_PER_SECOND)
        # Named for what it holds (buckets) rather than for the key,
        # because the #119 guard reads ``.get()``/``.put()`` on a
        # receiver named "client" as an unbounded httpx read.
        self._buckets: TTLLRUCache[str, BucketState] = TTLLRUCache(
            ttl=float(ttl), capacity=_CLIENT_CAPACITY
        )
        self._global = BucketState(tokens=_GLOBAL_CAPACITY, updated_at=0.0)

    def _refilled(
        self, state: BucketState, *, now: float, capacity: float, rate: float
    ) -> BucketState:
        """``state`` advanced to ``now`` without spending anything.

        ``refill_and_consume`` with a zero cost always admits, so its
        boolean is useless here — the state it returns is not, and it
        is the only refill arithmetic in the codebase worth trusting.
        """
        advanced, _ = refill_and_consume(
            state, now=now, capacity=capacity, refill_per_second=rate, cost=0.0
        )
        return advanced

    def _client_state(self, client: str, *, now: float) -> BucketState:
        state = self._buckets.get(client, now)
        if state is None:
            return BucketState(tokens=_PER_CLIENT_CAPACITY, updated_at=now)
        return state

    def allow_attempt(self, client: str, *, now: float) -> RefusedBy | None:
        """Which budget refuses this attempt, or ``None`` to go ahead.

        Checked before the comparison, so a refused caller learns
        nothing about the secret they submitted — answering "wrong
        secret" to a request we then decline to count would leak
        exactly the bit the limit exists to ration. The two refusals
        must therefore be indistinguishable on the wire; they are told
        apart here only so the *log* can tell them apart.

        Nothing is spent here. A correct secret costs the operator
        nothing, and only :meth:`note_failure` knows the answer.
        """
        client_state = self._refilled(
            self._client_state(client, now=now),
            now=now,
            capacity=_PER_CLIENT_CAPACITY,
            rate=_PER_CLIENT_REFILL_PER_SECOND,
        )
        self._buckets.put(client, client_state, now)
        if client_state.tokens < 1.0:
            return RefusedBy.CLIENT

        self._global = self._refilled(
            self._global,
            now=now,
            capacity=_GLOBAL_CAPACITY,
            rate=_GLOBAL_REFILL_PER_SECOND,
        )
        if self._global.tokens < 1.0:
            return RefusedBy.SITE
        return None

    def note_failure(self, client: str, *, now: float) -> None:
        """Charge one wrong guess to this client and to the site.

        Both buckets are debited: the per-client one is the limit on
        one address, the global one is the limit on a pool of them, and
        a guess that only touched the first would make the second
        unreachable by exactly the distributed attacker it exists for.
        """
        spent, _ = refill_and_consume(
            self._client_state(client, now=now),
            now=now,
            capacity=_PER_CLIENT_CAPACITY,
            refill_per_second=_PER_CLIENT_REFILL_PER_SECOND,
        )
        self._buckets.put(client, spent, now)
        self._global, _ = refill_and_consume(
            self._global,
            now=now,
            capacity=_GLOBAL_CAPACITY,
            refill_per_second=_GLOBAL_REFILL_PER_SECOND,
        )
