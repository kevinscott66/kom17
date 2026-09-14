"""Group-treasury payout flow (L-28/L-41) — the money core of ``/group_pay``.

Ports legacy ``group_treasury_pay`` (bot.py:10956-10982):

1. balance check against ``groups_donations.total_donations``
   (bot.py:10965-10967, via ``get_group_total_donations``);
2. race-guarded treasury debit — ``UPDATE … WHERE total_donations >= ?``
   + rowcount check (bot.py:10969-10974);
3. credit the payee's wallet via the coin pipeline with
   ``transaction_type="treasury_payout"`` (bot.py:10976);
4. refresh the group's rating-history snapshot
   (``_save_rating_history_for_group``, bot.py:10977).

Hardenings over legacy (same class of fixes as TransferService):

* the treasury debit + wallet credit run inside a SAVEPOINT
  (``session.begin_nested``), so a failed credit rolls the debit back
  atomically instead of evaporating coins the way legacy can if
  ``add_coins`` fails after the treasury UPDATE committed
  (legacy commits the debit at bot.py:10975 BEFORE crediting);
* the ``credit()`` return value is checked (SEC-2 posture) — a ``None``
  triggers the savepoint rollback and a distinct outcome;
* the ledger row is written only AFTER the credit succeeded.

NO funding write-side ships here — see the TRUTH-RULE note in
``repositories/treasury_repo.py``: current legacy never adds to
``total_donations`` (bot.py:10915 «Сейчас не пополняется»); donations and
shop-purchase splits go to ``group_xp`` + the owner's wallet
(bot.py:10687, bot.py:10741). The payout drains residual pre-era balances.

Ownership/target policy is the HANDLER's job (owner-only, self-only,
minimum amount) — this service only enforces the arithmetic invariants,
mirroring how legacy splits ``cmd_group_pay`` (gates, bot.py:25022-25051)
from ``group_treasury_pay`` (money, bot.py:10956).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.utils.time import rating_history_date

_log = logger.bind(component="services.treasury")

if TYPE_CHECKING:
    from telegram_invite_bot.repositories.donations_rating_repo import (
        DonationsRatingRepo,
    )
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.repositories.treasury_repo import TreasuryRepo


class PayoutOutcome(StrEnum):
    """Mutually-exclusive results the /group_pay handler branches on."""

    SUCCESS = "success"
    """Treasury debited, payee credited, ledger row written, history
    snapshot refreshed."""

    INVALID_AMOUNT = "invalid_amount"
    """Non-positive amount. Legacy guard: ``if amount < 1`` at
    bot.py:10961-10962. Defensive — the handler validates first."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    """Treasury balance below ``amount`` (or the group has no
    ``groups_donations`` row / a NULL balance). Surfaces both at the
    pre-check (bot.py:10965-10967) and at the SQL race guard
    (bot.py:10969-10974) — same UX either way. ``available`` carries
    the balance for the legacy-style «Доступно: N» error line."""

    CREDIT_FAILED = "credit_failed"
    """Treasury debit landed but the wallet credit returned ``None``
    (balance-cap overflow — get_or_create precedes the credit, so a
    missing wallet self-heals). The SAVEPOINT rollback restored the
    treasury; no coins moved. Legacy has no equivalent guard — its
    debit is already committed when ``add_coins`` fails."""


@dataclass(frozen=True, slots=True)
class PayoutResult:
    """What a payout attempt produced. Read ``outcome`` first."""

    outcome: PayoutOutcome
    amount: int = 0
    """Coins moved treasury → wallet on SUCCESS; 0 otherwise."""
    available: int = 0
    """Treasury balance observed by the pre-check. On
    INSUFFICIENT_FUNDS this is the number the handler renders
    («Доступно: N», bot.py:10967); on SUCCESS it is the PRE-debit
    balance."""


class GroupTreasuryService:
    """Atomic treasury-payout execution against the shared session."""

    def __init__(
        self,
        treasury_repo: TreasuryRepo,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        rating_repo: DonationsRatingRepo,
    ) -> None:
        self._treasury = treasury_repo
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._rating = rating_repo

    async def payout(self, *, group_id: int, to_user_id: int, amount: int) -> PayoutResult:
        """Move ``amount`` coins from the group treasury to a wallet.

        Steps mirror legacy ``group_treasury_pay`` in order, with the
        debit + credit wrapped in one SAVEPOINT (see module docstring).
        """
        # Legacy: ``if amount < 1: return False`` (bot.py:10961-10962).
        if amount < 1:
            return PayoutResult(outcome=PayoutOutcome.INVALID_AMOUNT)

        # Pre-check (bot.py:10965-10967). A missing row collapses to
        # available=0 — legacy's ``donations_ensure_group`` would insert
        # an empty row first, but an empty treasury rejects identically,
        # so we skip the write-on-read.
        balance = await self._treasury.get_balance(group_id)
        available = balance or 0
        if balance is None or balance < amount:
            return PayoutResult(outcome=PayoutOutcome.INSUFFICIENT_FUNDS, available=available)

        # Debit + credit under a SAVEPOINT so a credit failure undoes
        # the debit without relying on the caller's exception-driven
        # rollback (same shape as TransferService steps 5/6, R-FIX-002).
        session = self._economy._session  # noqa: SLF001 — same-package use
        async with session.begin_nested() as savepoint:
            debited = await self._treasury.debit(group_id, amount)
            if not debited:
                # Race guard fired — a concurrent payout drained the
                # treasury between the pre-check and the UPDATE
                # (bot.py:10973-10974 returns the same refusal).
                return PayoutResult(outcome=PayoutOutcome.INSUFFICIENT_FUNDS, available=available)

            # Self-heal a missing wallet before the credit (M-E-2
            # posture) — legacy's add_coins calls register_user for the
            # same reason (bot.py:9724).
            await self._economy.get_or_create(to_user_id)
            credited = await self._economy.credit(to_user_id, amount)
            if credited is None:
                await savepoint.rollback()
                _log.bind(group_id=group_id, to=to_user_id, amount=amount).error(
                    "treasury payout credit failed — debit rolled back"
                )
                return PayoutResult(outcome=PayoutOutcome.CREDIT_FAILED, available=available)

        # Ledger row only AFTER the successful credit. Legacy records it
        # inside add_coins with transaction_type="treasury_payout"
        # (bot.py:10976); from_id=None marks a system-side payout (no
        # user wallet was debited — the treasury is not a wallet).
        await self._ledger.record(
            from_id=None,
            to_id=to_user_id,
            amount=amount,
            reason=f"group_treasury_payout:{group_id}",
            type="treasury_payout",
        )

        # Rating-history snapshot (bot.py:10977). The payout doesn't
        # change ``group_xp``, but legacy refreshes today's snapshot
        # anyway — mirrored for write-for-write parity. The calendar has
        # to be Moscow's, not UTC's: the row is an upsert keyed on the
        # date, so a UTC ``today`` made every payout in the first three
        # hours of an MSK day overwrite the PREVIOUS day's snapshot.
        await self._rating.save_history_snapshot(group_id, today=rating_history_date())

        _log.bind(group_id=group_id, to=to_user_id, amount=amount).info("treasury payout done")
        return PayoutResult(outcome=PayoutOutcome.SUCCESS, amount=amount, available=available)
