"""``/promo`` (gift-code) redeem + ``/promo_create`` mint, composed atomically.

Thin orchestration over :class:`PromoRepo` + :class:`EconomyRepo` +
:class:`TransactionsRepo`, all sharing ONE ``economy`` session so a
redemption (reserve-use + redemption-row + wallet credit + ledger row)
is a single atomic transaction.

Money invariants this service upholds (real coins):

* MINT never moves coins: ``/promo_create`` only inserts a code row.
  There is no funding wallet — a promo code mints coins on redemption
  (developer-gated, hence the dev auth on the create handler).
* REDEEM never double-credits: the global ``max_uses`` cap is held by
  the race-safe :meth:`PromoRepo.reserve_use` UPDATE, and the
  per-user-once guard is the conditional INSERT in
  :meth:`PromoRepo.insert_redemption_once`. The credit happens only
  AFTER both guards pass.

Atomicity / rollback contract
-----------------------------
The shared session is owned by :class:`EconomyMiddleware`, which commits
on handler success and rolls back only on a RAISED exception. So a redeem
that loses the per-user-once race cannot simply ``return`` — the
middleware would COMMIT the partial work (the ``used_count`` bump)
without a redemption row, burning a use slot for nothing. The reserve, the
redemption row and the credit are therefore held in ONE **SAVEPOINT**, and
the two exits reachable after the reserve succeeded — ALREADY_REDEEMED and
CREDIT_FAILED — roll that savepoint back. EXHAUSTED needs no rollback: the
reserve UPDATE matched no row, so there is nothing to undo. On both
rollback paths the credit has either not run yet or is undone with the
reserve, so no coins were created.

#1986: this used to be ``session.rollback()``. It undid the redeem, and
everything else on the session with it — ``middlewares/base.py`` hands out
exactly ONE session per update, so an unlucky redeemer silently discarded
writes made by other components of the same update. A savepoint undoes our
work and only ours. Same argument as :meth:`P2pService.cancel_order`
(#1985) and #209's ``expire_pending``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.promo_repo import PromoRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


# T-019 mint budget (docs/ECONOMY_RATE_AUDIT.md §2.4 / R3). A promo code
# mints coins from nothing, so each one carries a hard ceiling: the most
# a single redeem may pay, and the most the code can ever issue in total.
# 100 000 COM ≈ 111 USDT at the 900 COM/USDT withdrawal rate — generous
# for a campaign, small enough that a typo is survivable.
MAX_PROMO_REWARD_COINS = 10_000
MAX_PROMO_MINT_TOTAL = 100_000


class CreateOutcome(StrEnum):
    """Mutually-exclusive results the mint flow can produce."""

    OK = "ok"
    INVALID = "invalid"
    """Empty code, bad reward/max-uses, or a mint budget over the cap."""
    DUPLICATE = "duplicate"
    """A code with this string already exists."""


class RedeemOutcome(StrEnum):
    """Mutually-exclusive results the redeem flow can produce."""

    OK = "ok"
    EMPTY_CODE = "empty_code"
    NOT_FOUND = "not_found"
    """No active code with this string."""
    EXHAUSTED = "exhausted"
    """The global ``max_uses`` cap is reached (lost the reserve race)."""
    ALREADY_REDEEMED = "already_redeemed"
    """A ``per_user_once`` code this user has already redeemed."""
    CREDIT_FAILED = "credit_failed"
    """Crediting the redeemer failed AFTER reserve — whole redeem rolled back."""


@dataclass(frozen=True, slots=True)
class CreateResult:
    """What a mint call produced. Read ``outcome`` first."""

    outcome: CreateOutcome
    code: str = ""
    reward_coins: int = 0
    max_uses: int = 0
    per_user_once: bool = True


@dataclass(frozen=True, slots=True)
class RedeemResult:
    """What a redeem call produced. Read ``outcome`` first."""

    outcome: RedeemOutcome
    reward_coins: int = 0
    new_balance: int = 0
    code: str = ""


class PromoService:
    """Atomic mint + redeem execution against the shared economy session."""

    def __init__(
        self,
        promo_repo: PromoRepo,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        session: AsyncSession,
    ) -> None:
        self._promo = promo_repo
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._session = session

    @staticmethod
    def normalize_code(code: str) -> str:
        """Canonical form of a code: stripped + uppercased.

        Codes are stored uppercased so redemption is case-insensitive;
        centralising the rule here keeps mint and redeem in lockstep.
        """
        return (code or "").strip().upper()

    async def create_code(
        self,
        *,
        code: str,
        reward_coins: int,
        max_uses: int,
        per_user_once: bool,
        created_by: int | None,
        description: str | None = None,
        now: datetime | None = None,
    ) -> CreateResult:
        """Mint a new promo code (developer-gated upstream in the handler).

        Validates the shape, then inserts the row. A ``UNIQUE(code)``
        collision is detected by a pre-INSERT existence probe → DUPLICATE.

        #1943: the probe is a ``SELECT``, so it takes no writer lock and
        two mints of the same code can both pass it. The constraint is
        the real guard and the loser's INSERT raises
        :class:`~sqlalchemy.exc.IntegrityError` — which used to escape
        this method entirely and surface as the generic error card,
        telling a developer their mint had crashed when in truth the
        code already existed. It is caught here and reported as the same
        DUPLICATE the probe reports, so the two paths are
        indistinguishable to the caller.

        The rollback is not optional. ``EconomyMiddleware`` owns the
        session and rolls back only on a RAISED exception, so returning
        normally after a failed statement would hand it a session it
        cannot commit; the same argument as
        :meth:`CheckService.claim_check`. It is a SAVEPOINT rollback
        (#1986), which clears the failed INSERT — the part that makes the
        session usable again — while leaving anything else the update
        wrote on this shared session intact.

        T-019 adds a **mint budget** the original port did not have (see
        ``docs/ECONOMY_RATE_AUDIT.md`` §2.4 / R3). Minting is
        developer-gated, so this is not an attack surface — it is a
        footgun guard: ``max_uses`` used to default to 0 = *unlimited*,
        so a single mistyped reward could issue coins without bound and
        could not be recalled once redeemed. Every new code now carries a
        finite ceiling on both the per-redeem reward and the total it can
        ever mint. Only the MINT path is bounded — existing unlimited
        rows keep redeeming exactly as before.
        """
        normalized = self.normalize_code(code)
        if not normalized or reward_coins <= 0 or max_uses < 0:
            return CreateResult(outcome=CreateOutcome.INVALID)
        if reward_coins > MAX_PROMO_REWARD_COINS:
            return CreateResult(outcome=CreateOutcome.INVALID)
        # ``max_uses == 0`` means unlimited, i.e. an unbounded total —
        # rejected outright rather than silently budget-checked as zero.
        if max_uses == 0 or reward_coins * max_uses > MAX_PROMO_MINT_TOTAL:
            return CreateResult(outcome=CreateOutcome.INVALID)

        if await self._promo.get_by_code(normalized) is not None:
            return CreateResult(outcome=CreateOutcome.DUPLICATE)

        async with self._session.begin_nested() as savepoint:
            try:
                await self._promo.create_code(
                    code=normalized,
                    reward_coins=reward_coins,
                    max_uses=max_uses,
                    per_user_once=per_user_once,
                    created_by=created_by,
                    description=description,
                    now=now,
                )
            except IntegrityError:
                # A concurrent mint of the same code committed between our
                # probe and this INSERT. Same answer as the probe's.
                await savepoint.rollback()
                return CreateResult(outcome=CreateOutcome.DUPLICATE)
        return CreateResult(
            outcome=CreateOutcome.OK,
            code=normalized,
            reward_coins=reward_coins,
            max_uses=max_uses,
            per_user_once=per_user_once,
        )

    async def redeem(self, *, user_id: int, code: str, now: datetime) -> RedeemResult:
        """Redeem ``code`` for ``user_id``, crediting them atomically.

        Order (the order matters for the money invariant):

        1. Normalise; empty → EMPTY_CODE.
        2. Load the code; missing OR inactive → NOT_FOUND.
        3. ``per_user_once`` fast pre-check (``has_redeemed``) →
           ALREADY_REDEEMED. (Friendly short-circuit; the conditional
           insert at step 5 is the authoritative guard.)
        4. Atomic reserve (``reserve_use``); ``False`` → EXHAUSTED
           (cap reached / lost the reserve race).
        5. Record the redemption. For ``per_user_once`` codes this is the
           conditional ``WHERE NOT EXISTS`` insert: ``False`` means a
           concurrent redeem from the same user beat us — roll back
           (undoing the reserve) and report ALREADY_REDEEMED. For
           non-once codes it is an unconditional append.
        6. Seed the wallet, then credit the redeemer + write the
           ledger row. A ``None`` credit (balance-cap overflow) rolls
           the whole redeem back so a use slot is never burned without
           delivery — mirrors the check-claim CREDIT_FAILED guard.
        """
        normalized = self.normalize_code(code)
        if not normalized:
            return RedeemResult(outcome=RedeemOutcome.EMPTY_CODE)

        promo = await self._promo.get_by_code(normalized)
        if promo is None or not promo.active:
            return RedeemResult(outcome=RedeemOutcome.NOT_FOUND)

        code_id = int(promo.id)
        reward = int(promo.reward_coins)
        max_uses = int(promo.max_uses or 0)
        per_user_once = bool(promo.per_user_once)

        # Gate 3: friendly fast per-user-once pre-check.
        if per_user_once and await self._promo.has_redeemed(code_id, user_id):
            return RedeemResult(outcome=RedeemOutcome.ALREADY_REDEEMED, code=normalized)

        # Gates 4-6 are one SAVEPOINT (#1986): the reserve, the redemption
        # row and the credit either all land or none do — and undoing them
        # no longer takes the rest of the update's writes with it.
        async with self._session.begin_nested() as savepoint:
            # Gate 4: atomic global-cap reserve.
            if not await self._promo.reserve_use(code_id, max_uses):
                return RedeemResult(outcome=RedeemOutcome.EXHAUSTED, code=normalized)

            # Gate 5: record the redemption (authoritative per-user-once guard).
            if per_user_once:
                inserted = await self._promo.insert_redemption_once(code_id, user_id, reward, now)
                if not inserted:
                    # A concurrent redeem from the SAME user beat us. Roll
                    # the savepoint back so the reserve (Gate 4) is undone —
                    # otherwise the middleware would commit a burned use slot
                    # with no redemption row. The credit (Gate 6) has not run.
                    await savepoint.rollback()
                    return RedeemResult(outcome=RedeemOutcome.ALREADY_REDEEMED, code=normalized)
            else:
                await self._promo.insert_redemption(code_id, user_id, reward, now)

            # Gate 6: credit + ledger. Seed the wallet first: ``credit``
            # is a guarded ``UPDATE ... WHERE user_id = :id`` (economy_repo
            # .py:286-308), so a user with no ``economy.users`` row matches
            # zero rows and comes back ``None`` — CREDIT_FAILED on a
            # perfectly valid code. ``/promo`` is reachable without the
            # ``/start`` bootstrap (handlers/start.py:104 and :229 are the
            # only seeders; neither EconomyMiddleware nor the activity
            # middleware seeds on a slash command), so this is the same
            # first-time-claimer hole CheckService closes at
            # check_service.py:547. Check the credit return too — a silent
            # None would burn a use slot without paying the user.
            await self._economy.get_or_create(user_id, now=now)
            credited = await self._economy.credit(user_id, reward)
            if credited is None:
                await savepoint.rollback()
                return RedeemResult(outcome=RedeemOutcome.CREDIT_FAILED, code=normalized)
            await self._ledger.record(
                from_id=None,
                to_id=user_id,
                amount=reward,
                reason="promo_redeem",
                type="promo_redeem",
                date=now,
            )
        return RedeemResult(
            outcome=RedeemOutcome.OK,
            reward_coins=reward,
            new_balance=credited.balance,
            code=normalized,
        )
