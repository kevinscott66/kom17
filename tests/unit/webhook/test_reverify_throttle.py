"""The limiter in front of the unsigned YooKassa route.

What is worth testing here is not the token arithmetic — that lives in
:mod:`utils.rate_limit` and has its own tests — but the two decisions
this class makes on top of it: which bucket refuses first, and who pays
for the refusal.
"""

from __future__ import annotations

from telegram_invite_bot.webhook.reverify_throttle import (
    _GLOBAL_CAPACITY,
    _PER_CLIENT_CAPACITY,
    _PER_CLIENT_REFILL_PER_SECOND,
    ReverifyThrottle,
)


def _drain(throttle: ReverifyThrottle, client: str, *, count: float, now: float) -> int:
    """POST ``count`` times from one client at a standstill clock."""
    return sum(throttle.admit(client, now=now) for _ in range(int(count)))


def test_a_clients_burst_is_its_capacity_and_then_it_waits() -> None:
    throttle = ReverifyThrottle()

    admitted = _drain(throttle, "1.2.3.4", count=_PER_CLIENT_CAPACITY, now=100.0)

    assert admitted == int(_PER_CLIENT_CAPACITY)
    assert not throttle.admit("1.2.3.4", now=100.0)


def test_the_wait_is_over_when_the_bucket_has_refilled() -> None:
    """A cooldown that never ends is an outage, not a limit."""
    throttle = ReverifyThrottle()
    _drain(throttle, "1.2.3.4", count=_PER_CLIENT_CAPACITY, now=100.0)

    assert not throttle.admit("1.2.3.4", now=100.0)
    assert throttle.admit("1.2.3.4", now=100.0 + 1.0 / _PER_CLIENT_REFILL_PER_SECOND)


def test_one_floods_client_does_not_spend_anothers_budget() -> None:
    """The acquirer's stream must survive somebody else's loop.

    This is the whole reason the per-client bucket is consulted first:
    the flood exhausts what is its own before it touches anything
    shared.
    """
    throttle = ReverifyThrottle()

    _drain(throttle, "attacker", count=_PER_CLIENT_CAPACITY * 2, now=100.0)

    assert throttle.admit("acquirer", now=100.0)


def test_the_global_ceiling_holds_when_the_client_key_does_not() -> None:
    """A thousand addresses, one budget.

    ``client_key`` reads a header on a route that answers anonymous
    POSTs, so a flood can hand out a fresh key per request and never
    meet its own per-client bucket. The global one is what is left.
    """
    throttle = ReverifyThrottle()

    admitted = sum(
        throttle.admit(f"forged-{i}", now=100.0) for i in range(int(_GLOBAL_CAPACITY) + 50)
    )

    assert admitted == int(_GLOBAL_CAPACITY)


def test_a_client_refused_by_the_global_bucket_is_not_charged_for_it() -> None:
    """Otherwise a distributed flood also locks out its victim.

    The honest sender did nothing wrong; it arrived during someone
    else's storm. It gets a 429 and a redelivery, but its own budget has
    to be intact the moment the storm passes, or the shared limit has
    become a way to silence one particular client.
    """
    throttle = ReverifyThrottle()
    forged = (f"forged-{i}" for i in range(int(_GLOBAL_CAPACITY)))
    for key in forged:
        assert throttle.admit(key, now=100.0)

    # The global bucket is empty, so this is refused …
    assert not throttle.admit("acquirer", now=100.0)

    # … and one refill tick later the acquirer still has its whole
    # burst, not its burst minus the tokens the flood cost it.
    later = 100.0 + _GLOBAL_CAPACITY
    admitted = _drain(throttle, "acquirer", count=_PER_CLIENT_CAPACITY, now=later)

    assert admitted == int(_PER_CLIENT_CAPACITY)


def test_two_applications_in_one_process_do_not_share_a_drained_bucket() -> None:
    """``reset_reverify_throttle`` exists for the test suite's sake.

    Constructing a new limiter is the reset, so what this pins is that
    the state really lives on the instance and not in a module-level
    dict that a second instance would inherit.
    """
    first = ReverifyThrottle()
    _drain(first, "1.2.3.4", count=_PER_CLIENT_CAPACITY, now=100.0)
    assert not first.admit("1.2.3.4", now=100.0)

    assert ReverifyThrottle().admit("1.2.3.4", now=100.0)
