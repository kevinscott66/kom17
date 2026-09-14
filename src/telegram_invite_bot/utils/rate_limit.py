"""Pure token-bucket helper — the deterministic core of transfer rate-limiting.

Stage 17 punted ``cmd_send``'s ``_check_transfer_rate_limit`` to "a
future shared middleware". Stage 19 lands that middleware, and this
module is its arithmetic floor: a frozen :class:`BucketState` plus
:func:`refill_and_consume`, neither of which read a clock or touch
shared state. The middleware composes them with a real ``monotonic``
clock and a per-user dict; the unit tests compose them with synthetic
times and exact assertions.

Splitting the math out from the I/O layer is the same posture as
Stages 13-16 (``utils/daily.py``, ``utils/transfer.py``): a future
swap of the storage backend (in-memory → Redis → SQLite) or the
clock source (``monotonic`` → injected ``now`` for tests) doesn't
touch the algorithm, and the algorithm doesn't have to mock either.

Token bucket — not fixed window. Legacy's
``_check_transfer_rate_limit`` (bot.py:18830-18838) was neither: its
``TransferRateTracker`` (bot.py:3953-3976) kept a sliding window log
of the last 60 seconds of timestamps and rejected at
``len(calls) >= max_per_minute``. The bucket is chosen over both
because it is O(1) in memory per user instead of O(calls), and
because a fixed window bursts twice as hard across its boundary.
The one shape difference against legacy that a user can feel is
recovery: the sliding window returned every slot at once, 60s after
the oldest call, while the bucket drips them back evenly. Callers
reproduce legacy's ceiling by choosing ``capacity`` and
``refill_per_second`` — see
:mod:`telegram_invite_bot.middlewares.transfer_rate_limit`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BucketState:
    """Immutable point-in-time bucket reading.

    Carrying ``tokens`` and ``updated_at`` together (instead of a
    bare float) lets :func:`refill_and_consume` be a pure function
    of its inputs: there's no hidden "what was the last refill" the
    caller has to thread separately. The frozen dataclass also means
    the middleware's per-user dict never accidentally mutates a
    bucket the snapshot/admin path is reading.

    ``updated_at`` is whatever monotonic-ish unit the caller uses
    (seconds-since-epoch, ``time.monotonic()`` reading, an injected
    test clock); the helper only ever computes deltas, never
    absolute times, so the unit is opaque as long as the caller is
    consistent.
    """

    tokens: float
    updated_at: float


def refill_and_consume(
    state: BucketState,
    *,
    now: float,
    capacity: float,
    refill_per_second: float,
    cost: float = 1.0,
) -> tuple[BucketState, bool]:
    """Advance ``state`` to ``now`` and try to consume ``cost`` tokens.

    Returns ``(next_state, admitted)``. The state is always advanced
    (its ``updated_at`` rolls forward to ``now`` and its ``tokens``
    reflects the refill clamp); whether tokens were actually consumed
    depends on the admit decision:

    * If post-refill tokens ``>= cost`` → admit, debit ``cost``.
    * Else → reject, leave the (refilled) token count untouched so a
      future call sees the partial accumulation rather than restarting
      from "whatever was left when the user last spammed us". This is
      the legacy behaviour and the reason a flooding user's cooldown
      keeps making forward progress.

    Negative elapsed time (a non-monotonic clock skew, or a synthetic
    test clock running backwards) is clamped to zero — refill never
    "un-happens", and a user can't earn extra tokens by waiting for
    the wall clock to jump backwards. Capacity is the ceiling: a
    bucket that's been idle for an hour at 1 token/min still holds at
    most ``capacity`` tokens, not 60.

    No I/O, no clock reads, no shared state. The middleware is the
    only place that calls ``monotonic`` and the only place that holds
    a dict; this function operates entirely on its frozen inputs.
    """
    elapsed = max(0.0, now - state.updated_at)
    refilled = min(capacity, state.tokens + elapsed * refill_per_second)
    if refilled >= cost:
        return BucketState(tokens=refilled - cost, updated_at=now), True
    # Reject path: persist the refilled-but-unspent tokens so the
    # caller's next attempt sees forward progress toward admission.
    return BucketState(tokens=refilled, updated_at=now), False
