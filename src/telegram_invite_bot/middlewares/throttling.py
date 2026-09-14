"""Per-user token-bucket throttling.

Outer middleware on ``message`` and ``callback_query`` event types.
For each ``user_id`` we maintain a bucket of ``capacity`` tokens that
refills continuously at ``refill_per_second`` tokens/sec. Every
matching update costs one token; when the bucket is empty the update
is dropped — the handler chain never runs, the dispatch returns
``None``, and a Prometheus counter ticks.

Why drop silently instead of replying with "slow down"?

* Replying to a flooding client amplifies them — every reply is one
  more outbound API call we make on their behalf, and Telegram's
  per-bot send-rate limits are the actual scarce resource we're
  protecting. A bot that earnestly explains the rate limit to a
  flood will hit the *bot's* outbound limit and start dropping
  legitimate replies to other users.
* Telegram doesn't surface "you've been throttled" semantically;
  the user would see a normal-looking reply with confusing text and
  keep mashing the button.
* The metric (``tib_throttled_total``) is the operator-facing
  signal. Sustained nonzero against a real user_id is the alert.

Callback queries are the one exception, and not for UX taste: an
unanswered callback query leaves the inline button spinning on the
client for about fifteen seconds, so a dropped tap reads as "the bot
is broken" rather than as backpressure. The amplification argument
above doesn't apply to them either — a callback that *passes* the
throttle is answered by its handler anyway (``handlers/`` is full of
``await callback.answer()``), so acking a throttled one adds no
outbound call the un-throttled path wouldn't already have made.
Messages keep the silent drop: there a reply is a real amplifier and
nothing in the protocol is left waiting on it.

Bucket storage is an in-process ``OrderedDict`` capped at
``max_tracked_users``; on overflow we evict the least-recently-seen
user. Without the cap a long-lived process plus a churn of unique
user IDs (group chats with thousands of members each touching the
bot once) would leak memory unboundedly.

Anonymous events (no ``from`` user — channel posts, edited messages
from a deleted account, etc.) bypass the throttle. They're rare and
not under user control; throttling them would punish the next
genuine sender to share a bucket key like ``0``.

A message carrying ``successful_payment`` bypasses unconditionally
(#1814). Everything else here is a request, and refusing a request
costs the sender a retry; that update is a report of a charge
Telegram has already collected, arrives exactly once, and is never
redelivered. Dropping it credits nobody and alerts nobody, because
the alert is something the handler behind this middleware sends.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from telegram_invite_bot.webhook.metrics import THROTTLED_TOTAL

# M-I-3: handler-level flag name. A handler can declare a per-call cost
# by decorating with ``@flags(throttle_cost=5)``; aiogram propagates
# this into the middleware ``data`` dict, where :func:`get_flag` reads
# it. No handler declares it today — ``grep -rn throttle_cost src/``
# hits only this module — so every handler currently runs at the
# default cost of 1. The flag is a hook the throttle honours, not a
# live tuning knob; the paragraphs below describe how it *would*
# behave, not how the bot is configured.
_THROTTLE_COST_FLAG = "throttle_cost"

# Upper bound on a declared cost. Nothing declares a cost at all right
# now, so this is purely a guard for the day something does: anything
# past this is a typo in a decorator, and honouring it would lock the
# user out of the bot for the rest of the window.
_MAX_THROTTLE_COST = 1000.0

# Language-neutral by necessity, not by preference. The throttle is a
# dispatcher-level outer middleware (mounted on both observers in
# ``AppProvider.dispatcher``, ``di/providers.py``), so it runs before
# ``LanguageMiddleware`` (mounted in ``build_main_router``) has put
# ``lang`` into ``data``. Resolving the user's language here would mean
# the very DB read the throttle exists to avoid, so the ack is a bare
# glyph that reads the same in every locale.
_THROTTLED_CALLBACK_ACK = "⏳"  # hourglass


#: Message attributes that make an update a payment REPORT rather than
#: a request. These are never throttled, at any bucket level, enabled
#: or not.
#:
#: #1814 established the rule for ``successful_payment``. Everything
#: else this middleware sees is a REQUEST — refusing it costs the
#: sender a retry. A report is different: Telegram has already acted by
#: the time the update exists, it is delivered exactly once, and
#: nothing redelivers it. Dropped here, the chain never reaches
#: ``handlers/topup.py``, and everything the bot does about the event
#: happens in that handler — so the failure is silent on both ends,
#: including the owner alert, which is a thing the handler sends.
#:
#: It is reachable without any flooding intent: one shared bucket spans
#: messages and callbacks (see ``di/providers.py``), capacity 10
#: refilling at 2/s on prod, and a buyer tapping through the topup
#: keyboard drains it moments before Telegram posts the update.
#:
#: #1996 added ``refunded_payment``, which the original rule had left
#: out although its own justification covers it word for word. That
#: half matters more, not less: a refund is the ONLY thing that stamps
#: ``processed_webhooks.reversed_at``, the column
#: ``TransactionsRepo.lifetime_deposits`` subtracts. Dropped, refunded
#: money keeps counting as a deposit toward the withdrawal gate — the
#: loss ``handlers.topup.handle_refunded_payment`` names outright,
#: "refund, withdraw, repeat". And unlike a payment it costs the
#: provoking party nothing, who is also the party that gains.
#:
#: An exemption, not a refund of tokens: nothing is granted back, so a
#: client cannot buy — or un-buy — their way out of the throttle. The
#: next ordinary message meets the same empty bucket it would have met
#: anyway.
_PAYMENT_REPORTS: tuple[str, ...] = ("successful_payment", "refunded_payment")


async def _ack_throttled_callback(event: CallbackQuery) -> None:
    """Close the spinner on a callback query the throttle dropped.

    Deliberately no ``cache_time``: the client would then swallow a
    *later*, legitimate tap on the same button once the bucket has
    refilled, turning a transient limit into a button that looks
    permanently dead.

    Failures are swallowed. A callback query that expired between the
    tap and this ack raises ``TelegramBadRequest``, and the network can
    fail on its own; neither is worth propagating out of a middleware
    whose job at this point is purely cosmetic. ``TelegramAPIError`` is
    the common base for both.
    """
    with suppress(TelegramAPIError):
        await event.answer(_THROTTLED_CALLBACK_ACK)


def _resolve_throttle_cost(data: dict[str, Any]) -> float:
    """Read the per-handler throttle cost from middleware ``data``.

    Outer middlewares (like this one) run BEFORE handler selection,
    so :func:`get_flag` returns the default — but aiogram still
    forwards the flag through nested ``handler`` dicts when the
    middleware runs as an inner. Falls back to a manual ``data``
    lookup for the outer-middleware case where the handler is not
    yet resolved.

    Non-finite costs are rejected rather than clamped, because either
    one poisons the bucket permanently: ``nan`` fails every ``>`` limit
    comparison afterwards (the user is never throttled again), and
    ``inf`` fails every one in the other direction (the user is
    throttled forever). Both survive a naive ``cost < 1.0`` floor —
    ``nan < 1.0`` is ``False``. The ceiling is the same defence for a
    merely absurd finite cost.
    """
    raw: Any = None
    try:
        raw = get_flag(data, _THROTTLE_COST_FLAG, default=None)
    except Exception:  # noqa: BLE001 — never let throttle accounting raise
        raw = None
    if raw is None:
        raw = data.get(_THROTTLE_COST_FLAG)
    if raw is None:
        return 1.0
    try:
        cost = float(raw)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    if not math.isfinite(cost) or cost < 1.0 or cost > _MAX_THROTTLE_COST:
        return 1.0
    return cost


@dataclass(frozen=True, slots=True)
class ThrottlingSnapshot:
    """Read-only view of bucket state for the ``/admin_rate_stats`` card.

    Carries pre-computed scalars and a small "most-pressured" list so
    the handler doesn't touch the middleware's mutable internals (and
    the middleware doesn't grow a UI-shaped API). ``top_pressured``
    is sorted ascending by remaining tokens — fewest tokens first —
    because a user with low tokens is the operator's signal of *who
    is currently being throttled or near it*, which is exactly the
    diagnostic question this admin command answers.
    """

    tracked_users: int
    capacity: int
    refill_per_second: float
    top_pressured: tuple[tuple[int, float], ...]


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

    from telegram_invite_bot.config.settings import ThrottlingConfig


class ThrottlingMiddleware(BaseMiddleware):
    """Token-bucket rate limiter keyed by ``user_id``.

    The bucket is updated lazily: instead of a background refill task
    (extra event-loop work, lifecycle hazards), we record the timestamp
    of the last touch and on each event credit ``elapsed *
    refill_per_second`` tokens before deciding. This makes the bucket
    correct without any clock granularity assumptions and zero idle
    cost — buckets for inactive users sit untouched until the LRU
    evicts them.
    """

    def __init__(self, config: ThrottlingConfig) -> None:
        self._config = config
        # OrderedDict gives O(1) LRU semantics via ``move_to_end`` +
        # ``popitem(last=False)``. Value tuple: (tokens, last_refill_ts).
        self._buckets: OrderedDict[int, tuple[float, float]] = OrderedDict()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # #1814/#1996: a payment REPORT is never dropped, at any bucket
        # level, enabled or not. See :data:`_PAYMENT_REPORTS`.
        if any(getattr(event, attr, None) is not None for attr in _PAYMENT_REPORTS):
            return await handler(event, data)

        if not self._config.enabled:
            return await handler(event, data)

        # aiogram populates ``event_from_user`` in ``data`` from the
        # update's ``from_user`` attribute. Falling back to ``getattr``
        # covers tests that bypass aiogram's dispatcher event-context
        # setup and inject events directly.
        user = data.get("event_from_user") or getattr(event, "from_user", None)
        if user is None or getattr(user, "id", None) is None:
            return await handler(event, data)

        user_id = int(user.id)
        cost = _resolve_throttle_cost(data)
        if not self._allow(user_id, cost):
            event_type = type(event).__name__.lower()
            THROTTLED_TOTAL.labels(event_type=event_type).inc()
            if isinstance(event, CallbackQuery):
                # #217: clear the button spinner. See the module
                # docstring for why callbacks, and only callbacks, get
                # an answer on the dropped path.
                await _ack_throttled_callback(event)
            return None  # dropped — see module docstring

        return await handler(event, data)

    def _allow(self, user_id: int, cost: float = 1.0) -> bool:
        """Charge one token for ``user_id``; return False if the bucket
        was empty at consume time.

        Pure function over ``(self._buckets, self._config, monotonic())``.
        Kept separate from ``__call__`` so unit tests can poke the
        decision logic without faking a TelegramObject + aiogram
        middleware chain.
        """
        now = monotonic()
        capacity = float(self._config.capacity)
        refill = self._config.refill_per_second

        # M-I-3: ``cost`` is the per-handler token cost. It is 1.0 for
        # every handler today because none of them declares the
        # ``throttle_cost`` flag; the clamp below is what an expensive
        # handler would get if one ever did. A cold-started user with
        # cost > capacity would otherwise be permanently locked out —
        # we clamp the effective deduction to ``capacity`` so a single
        # expensive call costs at most a full bucket, never more.
        effective_cost = max(1.0, min(cost, capacity))

        entry = self._buckets.get(user_id)
        if entry is None:
            # New bucket starts full. The first event is always allowed
            # (we never punish a cold start) and the bucket holds
            # ``capacity - cost`` for the next event.
            tokens = capacity - effective_cost
            self._buckets[user_id] = (tokens, now)
            self._evict_if_needed()
            return True

        tokens, last_ts = entry
        elapsed = max(0.0, now - last_ts)
        tokens = min(capacity, tokens + elapsed * refill)
        # Touch order BEFORE the early-return so a throttled user
        # still keeps their slot warm — otherwise a flooding bot
        # would self-evict from the tracking map every cycle and
        # the next request would see a "fresh" full bucket.
        self._buckets.move_to_end(user_id)

        if tokens < effective_cost:
            self._buckets[user_id] = (tokens, now)
            return False

        self._buckets[user_id] = (tokens - effective_cost, now)
        return True

    def snapshot(self, *, top: int = 5) -> ThrottlingSnapshot:
        """Capture a point-in-time view of the bucket table.

        Pure read — no clock advancement, no bucket refill. We
        deliberately do NOT refill tokens before reporting because a
        snapshot taken during quiet periods would otherwise *always*
        show every tracked user at full capacity (the lazy refill
        catches up the moment we touch them), erasing the diagnostic
        signal we want: *who was active just before the operator
        looked?*. The raw stored tokens — frozen at the user's last
        event — preserve that history.

        Bounded by ``top`` (default 5) so the message never grows
        unreadable; on a heavily-loaded bot ``len(self._buckets)``
        can be in the thousands.
        """
        # ``self._buckets.items()`` is safe to iterate without copy here
        # because the middleware runs on the same event loop as any
        # handler that would call this method — no concurrent mutation.
        # ``sorted`` materialises a list anyway, so we don't risk
        # observing a partial mutation if an async-context switch
        # happened in the future (defence-in-depth, not strictly
        # required today).
        ranked = sorted(
            ((uid, tokens) for uid, (tokens, _ts) in self._buckets.items()),
            key=lambda kv: kv[1],
        )[:top]
        return ThrottlingSnapshot(
            tracked_users=len(self._buckets),
            capacity=self._config.capacity,
            refill_per_second=self._config.refill_per_second,
            top_pressured=tuple(ranked),
        )

    def _evict_if_needed(self) -> None:
        # Bound the dict size. ``popitem(last=False)`` evicts the
        # oldest entry by insertion / move_to_end order — that's the
        # least-recently-seen user, which is the right victim:
        # they're idle and re-seeing them later costs one cheap
        # bucket-init (full bucket) anyway.
        while len(self._buckets) > self._config.max_tracked_users:
            self._buckets.popitem(last=False)
