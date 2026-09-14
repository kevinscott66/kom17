"""Pins the four arithmetic rules of /daily ahead of the handler port.

The legacy ``DailyBonus.claim`` is the most-watched economy command —
users notice if a streak resets when it shouldn't or if their payout
changes by one coin. Each helper here gets parametrized coverage of
the borderline cases the legacy code handles implicitly: streak
caps, day-boundary math, VIP-stack ordering, weighted-random shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from random import Random

import pytest

from telegram_invite_bot.utils.daily import (
    DEFAULT_COOLDOWN_SECONDS,
    daily_cooldown_remaining,
    daily_reward,
    new_user_lockout_remaining,
    next_streak_value,
    weighted_random_choices,
)

# ---------------------------------------------------------------------------
# daily_cooldown_remaining
# ---------------------------------------------------------------------------


def test_cooldown_zero_when_never_claimed() -> None:
    assert daily_cooldown_remaining(None, datetime(2024, 1, 1, 12, 0, 0)) == timedelta(0)


def test_cooldown_zero_when_exactly_24h_elapsed() -> None:
    """The ``>= cooldown`` boundary is inclusive — a claim at exactly
    24h+1ms vs 24h-1ms must not flip the user's day."""
    last = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 2, 12, 0, 0)
    assert daily_cooldown_remaining(last, now) == timedelta(0)


def test_cooldown_positive_within_window() -> None:
    last = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 1, 23, 30, 0)
    remaining = daily_cooldown_remaining(last, now)
    assert remaining == timedelta(hours=12, minutes=30)


def test_cooldown_clock_skew_returns_full_window() -> None:
    """If the DB-written ``last_claim`` is in the future (NTP jump,
    timezone bug), the helper must NOT return a negative delta — the
    UI would render it as garbage. Return the full cooldown instead."""
    last = datetime(2024, 1, 5, 12, 0, 0)
    now = datetime(2024, 1, 1, 12, 0, 0)  # 4 days behind
    remaining = daily_cooldown_remaining(last, now)
    assert remaining == timedelta(seconds=DEFAULT_COOLDOWN_SECONDS)


def test_cooldown_honours_custom_window() -> None:
    last = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 1, 12, 30, 0)
    remaining = daily_cooldown_remaining(last, now, cooldown_seconds=3600)
    assert remaining == timedelta(minutes=30)


# ---------------------------------------------------------------------------
# next_streak_value
# ---------------------------------------------------------------------------


def test_streak_first_claim_starts_at_one() -> None:
    assert next_streak_value(0, None, datetime(2024, 1, 1), max_streak=30) == 1


def test_streak_continues_on_next_day() -> None:
    last = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 2, 13, 0, 0)  # 1 day + 1 hour
    assert next_streak_value(5, last, now, max_streak=30) == 6


def test_streak_caps_at_max() -> None:
    """A user at MAX_STREAK who keeps claiming stays at MAX_STREAK
    rather than overflowing — matches legacy ``min(... , MAX_STREAK)``."""
    last = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 2, 12, 0, 0)
    assert next_streak_value(30, last, now, max_streak=30) == 30


def test_streak_resets_on_multi_day_skip() -> None:
    last = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 5, 12, 0, 0)  # 4 days skipped
    assert next_streak_value(20, last, now, max_streak=30) == 1


def test_streak_unchanged_same_day_double_claim() -> None:
    """If the cooldown check is bypassed (caller bug, or test), the
    streak must NOT bump on a same-day re-call. Returning the prior
    streak prevents a double-claim race from inflating it."""
    last = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 1, 23, 59, 59)
    assert next_streak_value(7, last, now, max_streak=30) == 7


# ---------------------------------------------------------------------------
# daily_reward
# ---------------------------------------------------------------------------


def test_reward_first_day_is_just_base() -> None:
    assert daily_reward(base=10, streak=1, streak_bonus=5) == 10


def test_reward_streak_bonus_compounds_linearly() -> None:
    # streak=5 → base + 4*streak_bonus = 10 + 20 = 30
    assert daily_reward(base=10, streak=5, streak_bonus=5) == 30


def test_reward_zero_streak_treated_as_one() -> None:
    """``streak - 1`` floors at 0 — a 0 streak is the same as 1.
    Defensive: shouldn't happen if caller uses next_streak_value,
    but the bug should degrade gracefully rather than producing a
    negative payout."""
    assert daily_reward(base=10, streak=0, streak_bonus=5) == 10


def test_reward_vip_percent_applies_to_streak_inflated_base() -> None:
    """VIP applies AFTER the streak bonus — so a long-streak VIP
    benefits more than a short-streak VIP. Order: ``payout += int(payout * vip / 100)``."""
    # base + streak = 10 + 4*5 = 30; +20% = 30 + 6 = 36
    assert daily_reward(base=10, streak=5, streak_bonus=5, vip_percent=20) == 36


def test_reward_double_buster_applies_after_vip() -> None:
    """The x2 buster is the outer multiplier — stacks with VIP rather
    than replacing it. ((10 + 20) + 20%) * 2 = 36 * 2 = 72."""
    assert daily_reward(base=10, streak=5, streak_bonus=5, vip_percent=20, double=True) == 72


def test_reward_vip_floor_truncates_toward_zero() -> None:
    """int() floors toward zero — a 33% VIP on a 10-coin payout
    gives 13, not 14 (10 + int(10*33/100) = 10 + 3 = 13)."""
    assert daily_reward(base=10, streak=1, streak_bonus=5, vip_percent=33) == 13


# ---------------------------------------------------------------------------
# weighted_random_choices
# ---------------------------------------------------------------------------


def test_weighted_choices_min_has_highest_weight() -> None:
    """The whole point: the cheap end of the range is most likely.
    Otherwise the average daily payout drifts upward over months
    and the economy inflates."""
    values, weights = weighted_random_choices(1, 5)
    assert values == [1, 2, 3, 4, 5]
    assert weights == [5, 4, 3, 2, 1]


def test_weighted_choices_collapses_when_min_ge_max() -> None:
    """A misconfigured range (min >= max) becomes a single-value
    distribution rather than crashing — matches legacy fall-through."""
    values, weights = weighted_random_choices(7, 7)
    assert values == [7]
    assert weights == [1]


def test_weighted_choices_swaps_inverted_bounds() -> None:
    """Legacy normalises ``min``/``max`` if the caller swapped them;
    we do the same so a config typo doesn't crash the daily flow."""
    values, weights = weighted_random_choices(5, 1)
    assert values == [1, 2, 3, 4, 5]
    assert weights == [5, 4, 3, 2, 1]


def test_weighted_choices_negative_clamped_to_zero() -> None:
    """A negative bound is normalised to 0 — daily payouts must never
    debit the wallet."""
    values, _ = weighted_random_choices(-5, 3)
    assert min(values) == 0
    assert max(values) == 3


def test_weighted_choices_seeded_distribution_matches_expected() -> None:
    """End-to-end: feed the helpers to a seeded RNG and check the
    rolled values cluster near the low end as the weights say.
    Pinned with a fixed seed so a refactor that broke the weight
    direction would fail this test deterministically."""
    values, weights = weighted_random_choices(1, 10)
    rng = Random(42)
    rolls = [rng.choices(values, weights, k=1)[0] for _ in range(1000)]
    # Mean of a linear-decreasing 1..10 distribution is well below
    # the midpoint of 5.5; pin the cluster.
    mean = sum(rolls) / len(rolls)
    assert 3.5 < mean < 4.5


# ---------------------------------------------------------------------------
# new_user_lockout_remaining
# ---------------------------------------------------------------------------


def test_lockout_disabled_when_hours_zero() -> None:
    """``lockout_hours <= 0`` is the legacy "feature off" sentinel —
    every account skips the check."""
    reg = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 1, 12, 1, 0)
    assert new_user_lockout_remaining(reg, now, lockout_hours=0) is None
    assert new_user_lockout_remaining(reg, now, lockout_hours=-5) is None


def test_lockout_returns_none_for_old_account() -> None:
    reg = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 1, 18, 0, 0)  # 6h later
    assert new_user_lockout_remaining(reg, now, lockout_hours=4) is None


def test_lockout_returns_remaining_for_new_account() -> None:
    reg = datetime(2024, 1, 1, 12, 0, 0)
    now = datetime(2024, 1, 1, 13, 0, 0)  # 1h later
    remaining = new_user_lockout_remaining(reg, now, lockout_hours=4)
    assert remaining == timedelta(hours=3)


def test_lockout_unknown_registration_treated_as_old() -> None:
    """Legacy users predate the registered column. Locking them out
    would punish the existing userbase for an anti-abuse change."""
    now = datetime(2024, 1, 1, 12, 0, 0)
    assert new_user_lockout_remaining(None, now, lockout_hours=4) is None


def test_lockout_clock_skew_returns_full_window() -> None:
    """Future-dated registration timestamp is a data bug. Return the
    full lockout rather than silently unlocking via negative math."""
    reg = datetime(2024, 1, 5, 12, 0, 0)
    now = datetime(2024, 1, 1, 12, 0, 0)
    assert new_user_lockout_remaining(reg, now, lockout_hours=4) == timedelta(hours=4)


# ---------------------------------------------------------------------------
# Parametrized cross-check: known legacy payouts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("streak", "vip", "double", "expected"),
    [
        (1, 0, False, 10),  # first day, no VIP, no buster
        (2, 0, False, 15),  # day 2: 10 + 5
        (7, 0, False, 40),  # day 7: 10 + 30
        (30, 0, False, 155),  # capped streak day: 10 + 145
        (1, 10, False, 11),  # first day + 10% VIP = 10 + 1
        (1, 0, True, 20),  # first day x2
        (30, 25, True, 386),  # cap + 25% VIP + x2 = (155 + 38) * 2 = 386
    ],
)
def test_reward_legacy_payouts_pinned(streak: int, vip: int, double: bool, expected: int) -> None:
    """Concrete payout values copied from manual evaluation of
    legacy ``DailyBonus.claim`` for the published defaults
    (base=10, streak_bonus=5, max_streak=30). If any of these
    numbers move, every user with that profile gets a different
    daily — the regression needs to be intentional, not silent."""
    assert (
        daily_reward(
            base=10,
            streak=streak,
            streak_bonus=5,
            vip_percent=vip,
            double=double,
        )
        == expected
    )
