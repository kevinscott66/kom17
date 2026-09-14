"""Pure helpers for the ``/daily`` reward + streak rules.

The legacy ``DailyBonus.claim`` in ``bot.py:12007`` interleaves four
distinct concerns inside one 140-line method:

1. **Cooldown** — has 24h passed since ``last_daily``? (timestamp math)
2. **Streak progression** — same-day re-claim blocked, +1 next day,
   reset on skip, capped at ``MAX_STREAK``.
3. **Reward computation** — base reward (fixed or weighted-random) +
   ``(streak-1) * streak_bonus`` + optional VIP percent + optional x2
   buster.
4. **Atomic persistence** — SQL UPDATE guarded by ``julianday`` diff so
   two concurrent claims can't both succeed.

The Stage 9 ``EconomyService`` already owns (4) — what's left is the
arithmetic. Pulling the four numeric rules out of the legacy class
makes them parametrizable and pinnable in tests without any
``datetime.now`` mocking or DB fixture wiring. The eventual
``DailyService`` port then becomes a thin compose of "ask these
helpers what to do, then call ``EconomyService.credit`` with the
result", which keeps the service layer free of arithmetic that's
already covered here.

The functions are deliberately ``datetime``-naive (callers pass
``now``) and randomness-free (the weighted-random helper returns the
``(values, weights)`` pair for ``random.choices``, not the rolled
value — that keeps tests deterministic and lets the caller use a
seeded RNG when desired).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Legacy default in ``bot.py:11994`` — the cooldown between successful
# claims. Exposed as a parameter on every helper so a future config
# tweak doesn't require an audit of literal ``86400``s across the
# codebase, but the default keeps the behaviour pinned.
DEFAULT_COOLDOWN_SECONDS: int = 86400


def daily_cooldown_remaining(
    last_claim: datetime | None,
    now: datetime,
    *,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
) -> timedelta:
    """Return how long until the next claim is allowed.

    ``timedelta(0)`` means "claim now is legal". A positive delta is
    the remaining wait; the caller renders it as ``Xh Ym``.

    Notes
    -----
    * ``last_claim is None`` → 0 (never claimed = legal). Matches
      legacy's ``last_daily is None`` short-circuit at ``bot.py:11968-11969``.
    * Clock skew where ``now < last_claim`` returns the full cooldown
      from ``last_claim`` (i.e. treats the future timestamp as the
      anchor). Legacy did NOT return a negative wait here: at
      ``bot.py:11994-11998`` a negative ``time_diff`` still satisfies
      ``time_diff < 86400``, and ``86400 - time_diff`` then renders an
      INFLATED positive wait — a timestamp an hour in the future is
      shown as "25ч 0м". Capping at exactly one cooldown is therefore a
      deliberate improvement on legacy, not a repair of garbage output:
      it bounds the damage of a clock-rewind incident at one day instead
      of letting it scale with the size of the skew. The only way to hit
      it at all is a DB-written timestamp newer than ``now``.
    """
    if last_claim is None:
        return timedelta(0)
    elapsed = now - last_claim
    if elapsed.total_seconds() < 0:
        return timedelta(seconds=cooldown_seconds)
    remaining = timedelta(seconds=cooldown_seconds) - elapsed
    if remaining.total_seconds() <= 0:
        return timedelta(0)
    return remaining


def next_streak_value(
    current_streak: int,
    last_claim: datetime | None,
    now: datetime,
    *,
    max_streak: int,
) -> int:
    """Compute the post-claim streak value.

    Legacy rule (``bot.py:12059-12073``), restated:

    * No prior claim (``last_claim is None``) → 1 (first day).
    * Exactly one day gap (``(now - last).days == 1``) → ``current + 1``
      capped at ``max_streak``.
    * Multi-day gap (``days > 1``) → 1 (streak reset).
    * Same calendar day (``days == 0``) → ``current_streak`` unchanged.
      Reaching this branch means the caller bypassed the cooldown check
      (a bug at the call site); we return the prior streak rather than
      bumping it so a double-claim race can't fast-forward the streak.

    The "days" granularity is ``(now - last).days``: the whole-day part
    of the ELAPSED interval, floored toward minus infinity. It is not a
    calendar-day count — no date boundary is consulted. So ``days_diff
    == 1`` holds across the whole ``[24h, 48h)`` window and a claim
    47h59m after the last one still continues the streak. That is
    deliberate parity with legacy (``bot.py:12059-12073`` runs the same
    arithmetic), not a calendar rule: a user can hold a streak while
    skipping every other calendar day. Changing it would silently
    re-price ``streak_7`` / ``streak_30``, so it stays until that is a
    decision rather than a side effect.
    """
    if last_claim is None:
        return 1
    days_diff = (now - last_claim).days
    if days_diff == 1:
        return min(current_streak + 1, max_streak)
    if days_diff > 1:
        return 1
    return current_streak


@dataclass(frozen=True, slots=True)
class DailyRewardBreakdown:
    """The component parts of a daily payout, for the receipt card.

    The legacy success message (``bot.py:12124``) itemised the reward
    — base roll, streak add, VIP %, x2 buster — so the user could see
    *why* they got what they got. Each field below is the additive
    contribution at its step; they sum to :attr:`total`.
    """

    base: int
    """The base reward — the random roll (or fixed reward)."""
    streak_bonus: int
    """``max(0, streak - 1) * streak_bonus`` — the streak contribution."""
    vip_percent: int
    """The VIP percent applied (0 for non-VIPs)."""
    vip_bonus: int
    """Coins added by the VIP percent (0 when ``vip_percent`` is 0)."""
    doubled: bool
    """Whether a one-shot x2 buster was applied."""
    double_bonus: int
    """Coins added by the x2 buster (equals the pre-double subtotal when
    ``doubled``, else 0)."""
    total: int
    """The final payout — sum of all the parts above."""


def daily_reward_breakdown(
    *,
    base: int,
    streak: int,
    streak_bonus: int,
    vip_percent: int = 0,
    double: bool = False,
) -> DailyRewardBreakdown:
    """Compute the payout *and* its itemised parts in one pass.

    Same formula and ordering as :func:`daily_reward` (which now
    delegates here) — see that function's contract. Returning the
    breakdown lets the success card show the base roll, streak add,
    VIP %, and x2 buster as separate lines without re-deriving the
    arithmetic at the handler edge.
    """
    streak_add = max(0, streak - 1) * streak_bonus
    subtotal = base + streak_add
    vip_bonus = int(subtotal * vip_percent / 100) if vip_percent > 0 else 0
    subtotal += vip_bonus
    double_bonus = subtotal if double else 0
    total = subtotal + double_bonus
    return DailyRewardBreakdown(
        base=base,
        streak_bonus=streak_add,
        vip_percent=vip_percent,
        vip_bonus=vip_bonus,
        doubled=double,
        double_bonus=double_bonus,
        total=total,
    )


def daily_reward(
    *,
    base: int,
    streak: int,
    streak_bonus: int,
    vip_percent: int = 0,
    double: bool = False,
) -> int:
    """Compute the coin payout for a successful claim.

    Formula (``bot.py:12083-12094``):

    ``payout = base + (streak - 1) * streak_bonus``

    then if ``vip_percent > 0``:

    ``payout += int(payout * vip_percent / 100)``  -- floor-toward-zero

    then if ``double`` (a one-shot x2 buster from the shop):

    ``payout *= 2``

    Order matters: the VIP percent applies to the streak-inflated base
    (so a long streak benefits VIPs more than vice versa), and the x2
    buster applies on top of everything (so it stacks with VIP rather
    than replacing it). Swapping the order would shift payouts by up
    to 50% for the small population of users that hit both bonuses, so
    the order is part of the contract and pinned by tests.

    ``streak - 1`` floors at 0 for ``streak <= 1`` — a first claim is
    just the base. Negative inputs are not validated; the caller is
    expected to compute ``streak`` via :func:`next_streak_value` which
    is non-negative by construction.
    """
    return daily_reward_breakdown(
        base=base,
        streak=streak,
        streak_bonus=streak_bonus,
        vip_percent=vip_percent,
        double=double,
    ).total


def new_user_lockout_remaining(
    registered_at: datetime | None,
    now: datetime,
    *,
    lockout_hours: int,
) -> timedelta | None:
    """Anti-abuse: block /daily for accounts younger than ``lockout_hours``.

    Mirrors the legacy guard at ``bot.py:12027``: an attacker who
    creates a fresh account specifically to farm the daily bonus
    gets nothing for the first N hours after registration. Returns:

    * ``None`` if the account is old enough — claim is allowed.
    * A ``timedelta`` if still locked — caller renders "wait Xh".

    Edge cases:

    * ``lockout_hours <= 0`` (feature disabled) → always ``None``.
      Matches legacy's ``if ANTI_ABUSE_NEW_USER_NO_DAILY_HOURS > 0``
      gate at the call site.
    * ``registered_at is None`` (account has no recorded reg time —
      shouldn't happen but legacy users predate the column) →
      ``None``. The alternative would be locking out all legacy
      users on every claim, which is the wrong default.
    * Clock skew (``now < registered_at``) → returns the full
      lockout duration, same defensive posture as
      :func:`daily_cooldown_remaining`. A future-dated registration
      timestamp is a data-integrity bug and we'd rather not
      silently unlock it.

    The caller is responsible for whitelisting developers /
    operators — legacy does this with ``user_id not in DEVELOPER_IDS``
    around the call site, which is policy that doesn't belong in a
    pure helper.
    """
    if lockout_hours <= 0:
        return None
    if registered_at is None:
        return None
    elapsed = now - registered_at
    lockout = timedelta(hours=lockout_hours)
    if elapsed.total_seconds() < 0:
        return lockout
    remaining = lockout - elapsed
    if remaining.total_seconds() <= 0:
        return None
    return remaining


def weighted_random_choices(
    rnd_min: int,
    rnd_max: int,
) -> tuple[list[int], list[int]]:
    """Return ``(values, weights)`` for ``random.choices`` to roll a
    weighted-random daily base reward.

    Legacy rolls ``random.choices(values, weights, k=1)[0]`` where
    ``values = range(rnd_min, rnd_max + 1)`` and ``weights = rnd_max - v + 1``
    so the minimum value is most likely and the maximum value is
    least likely (linear decreasing). The intent is to make daily
    payouts feel rewarding-but-not-too-rich on average — the mean of
    the distribution sits well below the midpoint.

    Returning the pair instead of the rolled value:

    1. **Deterministic tests** — the weight distribution can be
       asserted directly without seeding an RNG.
    2. **Caller controls the RNG** — production rolls with
       ``utils.rng.money_rng``, a ``random.SystemRandom`` instance
       (``services/daily_service.py:227`` takes ``rng or money_rng``),
       NOT the module-level Mersenne Twister legacy used. That is
       intentional for a payout path: the sequence must not be
       predictable from observed rolls. Tests pass a seeded ``Random``;
       a future replay-mode runner could pass a recorded sequence.

    Edge case: reversed bounds are SWAPPED, not rejected — ``low`` takes
    the smaller of the two and ``high`` the larger, so ``(10, 5)`` rolls
    the same 5..10 distribution as ``(5, 10)``. It collapses to a
    single-value distribution (one value, weight 1) only when the two
    are equal, or when both are non-positive and the ``max(0, …)`` floor
    pins them together. The caller can detect that (single element in
    ``values``) and skip the roll if it wants — ``random.choices``
    handles it fine either way and returns the sole value.
    """
    low = max(0, min(rnd_min, rnd_max))
    high = max(0, rnd_min, rnd_max)
    if low >= high:
        return [low], [1]
    values = list(range(low, high + 1))
    weights = [high - v + 1 for v in values]
    return values, weights
