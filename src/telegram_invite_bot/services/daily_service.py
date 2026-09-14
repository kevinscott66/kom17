"""``/daily`` claim flow composed over EconomyRepo + EconomyService.

The legacy ``DailyBonus.claim`` (``bot.py:12007``) is a 140-line
method tangling four concerns (cooldown / streak / reward /
persistence). Stages 8–10 split those:

* :mod:`telegram_invite_bot.utils.daily` owns the *arithmetic* —
  cooldown remaining, streak progression, reward formula, weighted
  random distribution. Pure, fully unit-tested.
* :class:`EconomyRepo.mark_daily_claimed` owns the *race guard* — one
  SQL ``UPDATE ... WHERE julianday(now) - julianday(last_daily) >= 1
  RETURNING ...`` so two concurrent claims can't both succeed even
  if the Python cooldown check let them through.
* :class:`EconomyService.credit` owns the *balance + ledger write* —
  wallet update + Transaction row in one composed step.

This service is the *flow* — the order of those steps and the
``ClaimOutcome`` taxonomy a handler needs to render the right
message. It's intentionally thin: every piece of logic worth
testing in isolation already lives elsewhere.

Result shape
------------
A handler wants to know: did the claim succeed (and for how much,
with what new streak)? Or was it rejected (and how long until
retry)? A single ``Wallet | None`` would force the handler to
re-query the wallet to learn the new streak, and to re-derive the
cooldown for the error message. So we return a small frozen
dataclass with the fields the handler renders directly.

The dataclass is preferred over a ``tuple[bool, int, int, str]``
(legacy's shape) because tuples in this position were a recurring
source of bugs — swapping ``streak`` and ``amount`` is silent at
the type level. Named fields make every call site obvious.

Future composition
------------------
The VIP percent and double-buster effects (``ItemEffects`` in
legacy) plug in via the optional ``effects`` argument. The current
strangler-state has those still living in legacy modules; once
those modules are ported, the handler passes the resolved
``DailyEffects`` and the math composes naturally.
"""

from __future__ import annotations

import random
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.utils.daily import (
    DEFAULT_COOLDOWN_SECONDS,
    daily_cooldown_remaining,
    daily_reward,
    daily_reward_breakdown,
    new_user_lockout_remaining,
    next_streak_value,
    weighted_random_choices,
)
from telegram_invite_bot.utils.rng import money_rng

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.economy_service import EconomyService

log = logger.bind(module="daily_service")


class ClaimOutcome(StrEnum):
    """Mutually-exclusive outcomes a handler can branch on.

    A string-valued enum so log lines render the *name* without
    custom formatting and so a future external audit log can store
    the outcome as a plain text value.
    """

    SUCCESS = "success"
    """Wallet credited; ``amount`` and ``streak`` are populated."""

    COOLDOWN = "cooldown"
    """Less than 24h since last claim; ``cooldown_remaining`` is set."""

    NO_WALLET = "no_wallet"
    """User has no wallet row — caller should ``get_or_create`` first
    if the auto-create posture is wanted, but most call sites have
    already ensured the wallet exists by the time we get here."""

    RACE_LOST = "race_lost"
    """The SQL guard rejected the UPDATE despite the Python cooldown
    check passing — a concurrent claim won. Operationally
    indistinguishable from COOLDOWN for the user; we surface the
    distinct outcome so monitoring can spot a real race storm vs.
    routine cooldowns."""

    NEW_USER_LOCKED = "new_user_locked"
    """#1946: the wallet is younger than the anti-abuse lockout window,
    so the bonus is refused until it ages. ``cooldown_remaining`` holds
    the wait. Distinct from COOLDOWN because the two say different
    things to the user — "come back tomorrow" versus "this account is
    too new" — and because a lockout is a policy the operator switched
    on, which monitoring should be able to count separately from the
    routine once-a-day rejection."""

    CREDIT_FAILED = "credit_failed"
    """The claim was marked but the coins could not be credited — the
    whole claim is rolled back so the day is NOT burned. Reachable
    when the wallet sits at the balance ceiling (``EconomyRepo.credit``
    returns ``None``), which is a real user state, not just corruption."""


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """What a /daily call produced. Read the ``outcome`` first."""

    outcome: ClaimOutcome
    amount: int = 0
    """Coins credited on success; 0 otherwise."""
    streak: int = 0
    """Post-claim streak on success; 0 otherwise. (We *could* surface
    the existing streak on cooldown, but the handler's "wait Xh Ym"
    message doesn't need it — only the success path renders streak.)"""
    cooldown_remaining: timedelta = timedelta(0)
    """Wait time until the next legal claim. Non-zero only when
    ``outcome`` is ``COOLDOWN`` or ``RACE_LOST``."""
    base: int = 0
    """Base roll that fed the payout (success only) — for the receipt."""
    streak_bonus: int = 0
    """Streak contribution to the payout (success only)."""
    vip_percent: int = 0
    """VIP percent applied (success only; 0 for non-VIPs)."""
    vip_bonus: int = 0
    """Coins added by the VIP percent (success only)."""
    doubled: bool = False
    """Whether the x2 buster fired this claim (success only)."""
    double_bonus: int = 0
    """Coins added by the x2 buster (success only)."""
    base_min: int = 0
    """Low end of the base-roll range (success only) — receipt context."""
    base_max: int = 0
    """High end of the base-roll range (success only)."""


@dataclass(frozen=True, slots=True)
class DailyConfig:
    """Numeric knobs for the reward formula.

    Defaults match legacy ``settings`` keys (``bot.py:2547``-2555):
    ``coins_daily_reward=10``, ``coins_daily_streak_bonus=5``,
    ``coins_max_streak=30``, ``daily_random_enabled=True``,
    ``daily_random_min=1``, ``daily_random_max=50``.

    Passed in so the service is config-free and tests can pin a
    specific payout without touching global state.
    """

    base_reward: int = 10
    streak_bonus: int = 5
    max_streak: int = 30
    random_enabled: bool = True
    random_min: int = 1
    random_max: int = 50
    # #1946: legacy ``ANTI_ABUSE_NEW_USER_NO_DAILY_HOURS`` (bot.py:3880).
    # ``0`` disables the guard, matching both the legacy default and the
    # ``> 0`` gate legacy put around its call site (bot.py:12024), so a
    # deployment that never set the key behaves exactly as before.
    new_user_lockout_hours: int = 0
    # The developer whitelist legacy applied at the same call site
    # (``user_id not in DEVELOPER_IDS``). It lives here rather than in
    # :func:`~telegram_invite_bot.utils.daily.new_user_lockout_remaining`
    # because that helper is pure arithmetic and says so; deciding *who*
    # the policy applies to is configuration.
    lockout_exempt_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class DailyEffects:
    """Per-user multipliers from the shop / VIP system.

    A separate dataclass (not kwargs on ``claim``) so adding a third
    multiplier later — say a referral-streak boost — doesn't churn
    every call site, and so the handler can compute the effects once
    via a future ``ItemEffects`` port and pass the bundle.
    """

    vip_percent: int = 0
    """Percent added to the streak-inflated base. 25 means +25%."""
    double: bool = False
    """One-shot x2 multiplier consumed by the claim; the caller is
    responsible for invalidating the buster after a successful
    claim. The service doesn't know about ItemEffects' inventory."""


class DailyService:
    """Composes the four /daily concerns into one atomic flow."""

    def __init__(
        self,
        economy_repo: EconomyRepo,
        economy_service: EconomyService,
        *,
        config: DailyConfig | None = None,
        rng: random.Random | None = None,
        clock: type[datetime] = datetime,
        session: AsyncSession | None = None,
    ) -> None:
        """``clock`` is the class used to read "now" — exposed so
        tests can pass a stub that returns a fixed time. The default
        is :class:`datetime` and the service calls
        ``clock.now(tz=UTC).replace(tzinfo=None)`` to get a naive
        UTC timestamp (legacy stores naive in ``last_daily``).

        ``session`` is the middleware-owned economy session, needed
        only to roll back a claim whose credit failed (see
        :meth:`claim`). Optional so unit tests that never exercise
        that path can keep constructing the service with two repos.
        """
        self._economy = economy_repo
        self._service = economy_service
        self._config = config or DailyConfig()
        self._rng = rng or money_rng
        self._clock = clock
        self._session = session

    def _now(self) -> datetime:
        return self._clock.now(tz=UTC).replace(tzinfo=None)

    def _base_range(self) -> tuple[int, int]:
        """The (min, max) span the base roll can land in.

        Mirrors :meth:`_roll_base_reward`: the random span when random
        rewards are enabled, else the fixed reward as a degenerate
        ``(r, r)`` span. Drives both the success-card "(range N–M)"
        context and the cooldown next-bonus preview.
        """
        cfg = self._config
        if not cfg.random_enabled:
            return cfg.base_reward, cfg.base_reward
        return cfg.random_min, cfg.random_max

    def preview_range(self, *, streak: int, effects: DailyEffects | None = None) -> tuple[int, int]:
        """Estimate the (min, max) payout for the *next* claim.

        Used by the cooldown card so the wait comes with a concrete
        "you'll get N–M next time" teaser (legacy ``bot.py:17982``).
        Applies the same streak / VIP / x2 math to both ends of the
        base-roll span at the supplied ``streak``. It's a preview, not
        a promise: the real streak the next claim lands on depends on
        when the user returns, so the caller passes the current streak.
        """
        effects = effects or DailyEffects()
        low, high = self._base_range()
        lo = daily_reward(
            base=low,
            streak=streak,
            streak_bonus=self._config.streak_bonus,
            vip_percent=effects.vip_percent,
            double=effects.double,
        )
        hi = daily_reward(
            base=high,
            streak=streak,
            streak_bonus=self._config.streak_bonus,
            vip_percent=effects.vip_percent,
            double=effects.double,
        )
        return lo, hi

    def _roll_base_reward(self) -> int:
        """Return the base reward — random if enabled, fixed otherwise.

        Kept as a method (not inlined) so test injection of a seeded
        ``rng`` produces deterministic outputs without monkey-patching
        the module-level :mod:`random`."""
        cfg = self._config
        if not cfg.random_enabled:
            return cfg.base_reward
        values, weights = weighted_random_choices(cfg.random_min, cfg.random_max)
        return self._rng.choices(values, weights, k=1)[0]

    async def claim(
        self,
        user_id: int,
        *,
        effects: DailyEffects | None = None,
    ) -> ClaimResult:
        """Run the /daily flow.

        Steps, in order:

        1. Fetch wallet (need current ``last_daily`` and ``streak``).
        2. Pure cooldown check — if not up, return COOLDOWN with the
           remaining wait. Skips the SQL guard entirely so we don't
           spam ``UPDATE ... WHERE 0=1``-shaped queries.
        2a. Anti-abuse new-account lockout (#1946), off by default —
           see the block below for why it sits after (2) and not before.
        3. Pure streak progression — compute ``new_streak``.
        4. Atomic SQL guard via ``mark_daily_claimed`` — sets
           ``last_daily`` and ``daily_streak`` *only* if the
           julianday window has truly passed. Returning None here
           after step 2 said "go" means a concurrent claim won
           between our read and write (RACE_LOST).
        5. Compute reward (post-roll, so the random value is fresh
           per attempt) and credit via :class:`EconomyService`.
           ``type='daily'`` matches legacy's ledger row so
           ``/admin_donations`` reads the new flow's rows the same
           way it reads the old flow's.

        The ordering of (4) before reward computation matters: if we
        rolled the random reward *before* the SQL guard, a race-lost
        attempt would still have consumed an RNG draw — making seeded
        tests fragile. Computing the reward only after the guard
        passes keeps a deterministic RNG sequence aligned with
        successful claims.
        """
        effects = effects or DailyEffects()
        wallet = await self._economy.get(user_id)
        if wallet is None:
            return ClaimResult(outcome=ClaimOutcome.NO_WALLET)

        now = self._now()
        remaining = daily_cooldown_remaining(
            wallet.last_daily,
            now,
            cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
        )
        if remaining > timedelta(0):
            return ClaimResult(
                outcome=ClaimOutcome.COOLDOWN,
                cooldown_remaining=remaining,
            )

        # #1946: anti-abuse lockout for freshly registered wallets, in
        # legacy's position — AFTER the cooldown check (bot.py:12017 runs
        # ``can_claim`` first, the lockout at :12024), so a user who is
        # both on cooldown and inside the window reads the cooldown card,
        # exactly as before. Both the hours and the whitelist come from
        # config; with the default 0 this whole block is one comparison.
        #
        # ``wallet.registered`` and ``now`` are both naive UTC — the
        # repo seeds the column via ``datetime.now(UTC).replace(...)``
        # and :meth:`_now` reads the same frame — so the subtraction
        # inside the helper is frame-consistent. Rows legacy wrote carry
        # naive LOCAL instead, but every one of those predates the
        # cutover by far more than any plausible lockout window, so they
        # land on the "old enough" branch either way.
        if (
            self._config.new_user_lockout_hours > 0
            and user_id not in self._config.lockout_exempt_ids
        ):
            locked_for = new_user_lockout_remaining(
                wallet.registered,
                now,
                lockout_hours=self._config.new_user_lockout_hours,
            )
            if locked_for is not None:
                log.bind(uid=user_id, wait_s=int(locked_for.total_seconds())).info(
                    "/daily refused — account inside the new-user lockout window"
                )
                return ClaimResult(
                    outcome=ClaimOutcome.NEW_USER_LOCKED,
                    cooldown_remaining=locked_for,
                )

        new_streak = next_streak_value(
            wallet.daily_streak,
            wallet.last_daily,
            now,
            max_streak=self._config.max_streak,
        )

        # #1986: the mark (step 4) and the credit (step 5) are one
        # SAVEPOINT. Undoing a failed claim is right; undoing it with
        # ``session.rollback()`` was not — this session belongs to the
        # update, ``middlewares/base.py`` keeps exactly ONE per update,
        # and the rollback took everything else uncommitted with it:
        # the ``get_or_create`` at ``handlers/daily.py:153``, for one.
        # Same argument as :meth:`P2pService.cancel_order` (#1985),
        # which #209 had already made for ``expire_pending``.
        #
        # The stack is what lets the savepoint stay optional:
        # ``session`` is ``None`` in the unit tests that never reach
        # this path, and there is nothing to nest a savepoint in there.
        async with AsyncExitStack() as stack:
            savepoint = (
                await stack.enter_async_context(self._session.begin_nested())
                if self._session is not None
                else None
            )
            marked = await self._economy.mark_daily_claimed(user_id, now=now, new_streak=new_streak)
            if marked is None:
                # Wallet existed (we read it above) → guard rejection is
                # a race, not a missing user. The full-cooldown remainder
                # is accurate here rather than a placeholder: the claim
                # that won the race wrote ``last_daily = now`` moments ago,
                # so the loser really does have ~24h to wait. That only
                # holds because the SQL guard compares at the same
                # precision as the Python check (#465) — otherwise a
                # sub-second mismatch landed here too and lied by a day.
                return ClaimResult(
                    outcome=ClaimOutcome.RACE_LOST,
                    cooldown_remaining=timedelta(seconds=DEFAULT_COOLDOWN_SECONDS),
                )

            base = self._roll_base_reward()
            breakdown = daily_reward_breakdown(
                base=base,
                streak=new_streak,
                streak_bonus=self._config.streak_bonus,
                vip_percent=effects.vip_percent,
                double=effects.double,
            )
            amount = breakdown.total

            credited = await self._service.credit(
                user_id, amount, type="daily", reason=f"daily streak {new_streak}"
            )
            if credited is None:
                # The mark (step 4) already landed, and the middleware
                # COMMITS on a plain return — so without a rollback here
                # the user's day is burned with nothing credited. Not
                # merely defensive: ``EconomyRepo.credit`` returns None
                # whenever the post-credit balance would breach the
                # balance ceiling, which a whale reaches for real. Roll
                # the whole claim back (same contract as a failed check
                # claim / promo redeem: nothing consumed, nothing paid)
                # and report a distinct outcome so the card can say what
                # happened instead of pretending it was a cooldown.
                if savepoint is not None:
                    await savepoint.rollback()
                log.bind(uid=user_id, amount=amount).warning(
                    "/daily credit failed (balance cap?) — claim rolled back"
                )
                return ClaimResult(outcome=ClaimOutcome.CREDIT_FAILED)

        # A-12: a /daily claim just moved ``daily_streak`` and ``balance``,
        # so award any streak/wealth achievements now reachable. Same
        # shared session → commits atomically with the claim. Silent: the
        # daily card doesn't surface the unlock (the user sees it via
        # /achievements), matching legacy which never checked achievements
        # on /daily at all — this is a strict improvement.
        await self._economy.award_achievements(user_id, now=now)

        base_min, base_max = self._base_range()
        return ClaimResult(
            outcome=ClaimOutcome.SUCCESS,
            amount=amount,
            streak=new_streak,
            base=breakdown.base,
            streak_bonus=breakdown.streak_bonus,
            vip_percent=breakdown.vip_percent,
            vip_bonus=breakdown.vip_bonus,
            doubled=breakdown.doubled,
            double_bonus=breakdown.double_bonus,
            base_min=base_min,
            base_max=base_max,
        )
