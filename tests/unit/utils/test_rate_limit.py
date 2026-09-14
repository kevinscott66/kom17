"""Unit tests for the pure token-bucket helper.

The middleware that composes :func:`refill_and_consume` with a real
clock is e2e-tested via ``tests/e2e/handlers/test_send.py``; here we
pin the math itself — the part that has to be deterministic to be
debuggable when a production cool-down behaves "wrong".

Cases pinned:

* Empty bucket admits when capacity allows; exhausted bucket rejects.
* Partial refill restores the right *fractional* token count
  (the legacy implementation rounded oddly under load — this is the
  invariant the strangler port keeps).
* Capacity is the ceiling — an idle user doesn't earn unbounded
  tokens.
* Reject path persists the refilled-but-unspent token count so the
  user's next attempt makes forward progress.
* Negative elapsed (non-monotonic skew / synthetic test clock running
  backwards) is clamped to zero.
* Cost > 1 (a future "expensive" transfer command) consumes exactly
  what was asked.
* Capacity exactly equals available tokens — boundary admit.
* Successive admit calls debit exactly one token each, not more.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.utils.rate_limit import BucketState, refill_and_consume


def test_full_bucket_admits_and_debits_one() -> None:
    state = BucketState(tokens=3.0, updated_at=0.0)
    next_state, admitted = refill_and_consume(state, now=0.0, capacity=3.0, refill_per_second=1.0)
    assert admitted is True
    assert next_state.tokens == pytest.approx(2.0)
    assert next_state.updated_at == 0.0


def test_exhausted_bucket_rejects_and_does_not_go_negative() -> None:
    state = BucketState(tokens=0.0, updated_at=0.0)
    next_state, admitted = refill_and_consume(state, now=0.0, capacity=3.0, refill_per_second=1.0)
    assert admitted is False
    assert next_state.tokens == pytest.approx(0.0)


def test_partial_refill_restores_fractional_tokens() -> None:
    """0.5s @ 2 tokens/sec = 1.0 token replenished — exactly the
    refill arithmetic the middleware relies on for sub-second
    resolution under load."""
    state = BucketState(tokens=0.0, updated_at=10.0)
    next_state, admitted = refill_and_consume(state, now=10.5, capacity=5.0, refill_per_second=2.0)
    assert admitted is True
    # 0.0 + 0.5 * 2.0 = 1.0 → consume 1.0 → 0.0 remaining
    assert next_state.tokens == pytest.approx(0.0)
    assert next_state.updated_at == 10.5


def test_capacity_clamps_long_idle_periods() -> None:
    """An hour of idle at 1 token/min would naively give 60 tokens.
    The ceiling pins it at ``capacity`` so a user can't bank
    unlimited burst by simply not playing for a while."""
    state = BucketState(tokens=1.0, updated_at=0.0)
    next_state, admitted = refill_and_consume(
        state, now=3600.0, capacity=3.0, refill_per_second=1.0 / 60.0
    )
    assert admitted is True
    # Clamped to capacity (3.0), then debited 1.0
    assert next_state.tokens == pytest.approx(2.0)


def test_reject_persists_refilled_tokens_for_forward_progress() -> None:
    """If the user is denied at t=0 with 0.3 tokens and tries again
    at t=10 (one more token's worth at 0.1 tps), the second attempt
    should see ~1.3 tokens — admitting. The reject path must persist
    the refilled-but-unspent partial accumulation, otherwise a user
    being throttled would never accrue progress toward their next
    transfer and the cool-down would be effectively permanent."""
    state = BucketState(tokens=0.3, updated_at=0.0)
    state, admitted = refill_and_consume(state, now=0.0, capacity=3.0, refill_per_second=0.1)
    assert admitted is False
    # Persisted the 0.3 (no refill yet — elapsed=0).
    assert state.tokens == pytest.approx(0.3)
    state, admitted = refill_and_consume(state, now=10.0, capacity=3.0, refill_per_second=0.1)
    # 0.3 + 10 * 0.1 = 1.3 → admit, debit 1.0 → 0.3 left.
    assert admitted is True
    assert state.tokens == pytest.approx(0.3)


def test_backwards_clock_does_not_un_refill() -> None:
    """Synthetic test clocks (and very rarely a non-monotonic system
    clock) can deliver ``now < state.updated_at``. The helper clamps
    elapsed to zero so a user can't lose tokens by moving "backwards
    in time" — and can't earn them either."""
    state = BucketState(tokens=2.0, updated_at=100.0)
    next_state, admitted = refill_and_consume(state, now=50.0, capacity=3.0, refill_per_second=1.0)
    assert admitted is True
    # No refill from "negative" elapsed; just debits one.
    assert next_state.tokens == pytest.approx(1.0)
    # updated_at still rolls forward to ``now`` — the helper's
    # contract is "advance state to now", not "leave it stale".
    assert next_state.updated_at == 50.0


def test_cost_greater_than_one_consumes_exactly_that() -> None:
    """A future "expensive" transfer (large /send, /gift to a group)
    can charge multiple tokens per call. The helper supports it
    without changing shape so the middleware can pass ``cost`` per
    handler if needed."""
    state = BucketState(tokens=3.0, updated_at=0.0)
    next_state, admitted = refill_and_consume(
        state, now=0.0, capacity=3.0, refill_per_second=1.0, cost=2.0
    )
    assert admitted is True
    assert next_state.tokens == pytest.approx(1.0)


def test_cost_greater_than_available_rejects() -> None:
    state = BucketState(tokens=1.5, updated_at=0.0)
    next_state, admitted = refill_and_consume(
        state, now=0.0, capacity=3.0, refill_per_second=1.0, cost=2.0
    )
    assert admitted is False
    # Refill kept (it's still 1.5 because elapsed=0), no debit.
    assert next_state.tokens == pytest.approx(1.5)


def test_consecutive_admits_each_debit_one_token() -> None:
    """Four back-to-back calls at the same instant on a capacity-3
    bucket admit three and reject one. Pinned so a future refactor
    that accidentally debits twice (or zero) shows up immediately."""
    state = BucketState(tokens=3.0, updated_at=0.0)
    decisions: list[bool] = []
    for _ in range(4):
        state, admitted = refill_and_consume(state, now=0.0, capacity=3.0, refill_per_second=1.0)
        decisions.append(admitted)
    assert decisions == [True, True, True, False]
    assert state.tokens == pytest.approx(0.0)


def test_boundary_tokens_equals_cost_admits() -> None:
    """``tokens >= cost`` is the admit predicate (``>=``, not ``>``),
    so a bucket with exactly enough tokens for one more transfer
    admits — pinning the inclusive boundary against accidental
    off-by-one refactors."""
    state = BucketState(tokens=1.0, updated_at=0.0)
    next_state, admitted = refill_and_consume(state, now=0.0, capacity=3.0, refill_per_second=1.0)
    assert admitted is True
    assert next_state.tokens == pytest.approx(0.0)
