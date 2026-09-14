"""Withdrawal write-side orchestration (T-027).

The user-side ``/withdraw`` flow and the admin approve/reject callbacks
both run through this service so the money invariants live in ONE place:

* **create** — escrow the coins out of the user's wallet *and* insert the
  ``pending`` request in one transaction. Escrowing up-front (rather than
  at approval) means a user can't spend the same coins twice while the
  request waits for an admin: the balance already reflects the hold.
* **approve_manual** — flip the row to ``completed`` with NO provider
  call (manual-payout doctrine v1: the operator pays the user
  out-of-band, exactly like legacy ``admin_confirm_withdrawal``,
  bot.py:20717). The escrow debit happened at create, so approval moves
  no on-platform coins.
* **approve** — the v2 auto-payout path: pay the user via Crypto Pay
  ``transfer`` (idempotent on a per-request ``spend_id`` so a
  double-click can't double-pay), then flip the row to ``completed``.
  Intentionally NOT wired into the admin panel in v1.
* **reject** — refund the escrowed coins back to the wallet and flip the
  row to ``rejected``, in one transaction.

Both wallet moves go through ``EconomyService.hold`` / ``.release``, not
``.debit`` / ``.credit``: an escrow and its refund must leave
``total_spent`` / ``total_earned`` untouched (#238). Legacy wrote the
balance column and nothing else on every one of its withdrawal paths —
bot.py:20425, bot.py:20488, bot.py:20625 on the way out and bot.py:20755
on the way back — and ``admin_confirm_withdrawal`` (bot.py:20717) issues
no balance UPDATE at all, so legacy never books a withdrawal as spend on
any branch. Using the counter-bumping primitives made a create/reject
cycle add ``amount_com`` to *both* lifetime totals on the ``/balance``
card at zero net money movement, repeatable up to the daily quota.

Conversion: ``amount_com / coins_per_usdt`` USDT (900 coins = 1 USDT by
default, defaulting off ``payments.rates.COINS_PER_USD`` on the top-up
side so a round-trip is value-neutral). Limits (min/max coins) are
injected — the handler sources them from config.

Two ledger-derived gates guard the create path, in order:

* **T-019 (R2)** refuses accounts with no lifetime ``purchase_*`` credit.
  Coins minted by chat activity, bonuses and promos circulate freely
  inside the ecosystem but cannot be cashed out as USDT. Without it the
  daily emission ceiling is the only thing between a grinder and the
  owner's wallet — see ``docs/ECONOMY_RATE_AUDIT.md`` §6.
* **T-020 (R6)** caps *how much* the account may ever take out at
  ``lifetime_deposits × payout_ratio`` coins. R2 alone is a threshold,
  not a bound: it is cleared forever by a single 1 USDT top-up, after
  which the near-zero house edge on the games let a user convert that
  deposit into an arbitrarily large balance and export all of it. The cap
  is what actually makes total payout ≤ total money in.

The two are independent knobs on purpose. R2 off + R6 on still bounds
the owner's downside; R2 on + R6 off is the pre-T-020 behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.repositories.withdrawals_repo import PROCESSING_STATUS
from telegram_invite_bot.services.payments.crypto_client import (
    CryptoPayError,
    CryptoPayInsufficientFunds,
    CryptoPayUnconfirmed,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.repositories.withdrawals_repo import WithdrawalsRepo
    from telegram_invite_bot.services.economy_service import EconomyService
    from telegram_invite_bot.services.payments.crypto_client import CryptoPayClient

log = logger.bind(component="services.withdraw")

# Ledger ``type`` tags — kept distinct so ``/admin_donations`` and any
# future audit can tell an escrow hold from its refund. The ledger rows
# are ours, not legacy's (legacy books no transaction for a withdrawal);
# the lifetime *counters* stay legacy-exact — see the module docstring.
_ESCROW_TYPE = "withdraw_escrow"
_REFUND_TYPE = "withdraw_refund"


class CreateOutcome(Enum):
    OK = "ok"
    BELOW_MIN = "below_min"
    ABOVE_MAX = "above_max"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    # L-92: the request would push the user past their daily / monthly
    # withdrawal cap for the current cycle. The caps reset implicitly on
    # the day / month boundary (usage is derived from request timestamps,
    # not a stored counter), so the user simply has to wait for the next
    # window — no admin action is needed.
    DAILY_QUOTA_EXCEEDED = "daily_quota_exceeded"
    MONTHLY_QUOTA_EXCEEDED = "monthly_quota_exceeded"
    # T-019: the account has never paid for coins, so every coin it holds
    # was minted by the bot. Paying it out converts the owner's money into
    # nothing at all — see ``docs/ECONOMY_RATE_AUDIT.md`` §6 / R2. This is
    # a permanent state for the account until it makes a purchase, not a
    # window that reopens, so the handler tells the user to top up rather
    # than to wait.
    NO_DEPOSITS = "no_deposits"
    # T-020 (R6): the account HAS paid in, but has already withdrawn its
    # whole lifetime deposit (times ``payout_ratio``). Unlike the daily /
    # monthly quotas this does not reopen on a clock boundary and unlike
    # ``NO_DEPOSITS`` it is not a permanent refusal either — it lifts the
    # moment the user tops up again, so the copy points at /topup while
    # being clear that nothing is lost meanwhile.
    PAYOUT_CAP_EXCEEDED = "payout_cap_exceeded"


class ApproveOutcome(Enum):
    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    ALREADY_PROCESSED = "already_processed"
    APP_WALLET_EMPTY = "app_wallet_empty"
    PROVIDER_ERROR = "provider_error"
    # SEC: the provider never told us whether the transfer executed
    # (timeout / 5xx / unreadable body). Distinct from PROVIDER_ERROR
    # because the two demand opposite handling: a refusal returns the
    # request to the queue, an unknown must NOT — a queued request is
    # rejectable, and a reject refunds the escrow on top of a payout
    # that may already have landed. The request stays leased; re-driving
    # the approve is the resolution, and ``spend_id`` makes that free.
    PAYOUT_UNCONFIRMED = "payout_unconfirmed"


class RejectOutcome(Enum):
    REJECTED = "rejected"
    NOT_FOUND = "not_found"
    ALREADY_PROCESSED = "already_processed"
    # SEC: the refund credit failed (balance-cap overflow / vanished
    # wallet). The whole reject is rolled back so the request stays
    # ``pending`` — the user's escrowed coins are NOT lost and an admin
    # can retry — instead of marking it rejected without refunding.
    REFUND_FAILED = "refund_failed"


@dataclass(frozen=True, slots=True)
class CreateResult:
    outcome: CreateOutcome
    request_id: int | None = None
    amount_crypto: float | None = None
    # Remaining quota in the offending window, set on a quota rejection so
    # the handler can tell the user exactly how much they may still
    # withdraw this cycle (``0`` when the cap is already fully used).
    remaining: int | None = None


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """A user's withdrawal quota snapshot for the current day / month.

    ``*_used`` is the coins already withdrawn (pending + completed) in the
    window; ``*_remaining`` is ``max(0, limit - used)``. Both windows reset
    implicitly at their boundary because usage is recomputed from request
    timestamps each call — there is no stored counter to clear.
    """

    daily_limit: int
    daily_used: int
    daily_remaining: int
    monthly_limit: int
    monthly_used: int
    monthly_remaining: int

    @property
    def remaining(self) -> int:
        """The binding remaining allowance — the tighter of the two
        windows, i.e. the most a fresh request could be right now."""
        return min(self.daily_remaining, self.monthly_remaining)


@dataclass(frozen=True, slots=True)
class ApproveResult:
    outcome: ApproveOutcome
    user_id: int | None = None
    amount_crypto: float | None = None
    asset: str | None = None
    tx_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RejectResult:
    outcome: RejectOutcome
    user_id: int | None = None
    amount_com: int | None = None


def _now_iso() -> str:
    """ISO-8601 naive-UTC string — matches the legacy TEXT ``created_at``
    convention the column was mapped against."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _day_start_iso(now: datetime | None = None) -> str:
    """Start-of-current-UTC-day as the ``"YYYY-MM-DD 00:00:00"`` text the
    timestamp-window quota query compares against.

    #245(b): UTC is a deliberate divergence from legacy, which rolled
    its daily counter on the *server's* local date
    (``date.today()``, ``bot.py:10007``) — MSK on this host, so the
    boundary moves by three hours. UTC wins on two counts. It matches
    the data: every ``created_at`` this window is compared against is
    UTC, whether written by :func:`_now_iso` or by legacy's
    ``DEFAULT CURRENT_TIMESTAMP`` (``bot.py:5165``), which SQLite
    evaluates in UTC. And it is reproducible: a boundary read off the
    host clock silently changes meaning when the bot is moved to a
    machine with another ``TZ``, which is precisely the bug a strangler
    port should not carry across. Following legacy to MSK would mean
    hard-coding one deployment's timezone into the quota arithmetic.
    """
    n = (now or datetime.now(UTC)).replace(tzinfo=None)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
        sep=" ", timespec="seconds"
    )


def _month_start_iso(now: datetime | None = None) -> str:
    """Start-of-current-UTC-month as ``"YYYY-MM-01 00:00:00"`` text.

    UTC for the same reason as :func:`_day_start_iso` — see #245(b)
    there. Legacy's month boundary came off ``date.today()`` too
    (``bot.py:10008``).
    """
    n = (now or datetime.now(UTC)).replace(tzinfo=None)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(
        sep=" ", timespec="seconds"
    )


def format_crypto_amount(amount_crypto: float) -> str:
    """Decimal string for the Crypto Pay API — up to 6 dp, trailing
    zeros trimmed (``5.0`` → ``"5"``, ``0.123456`` stays). The API wants
    a string; floats would risk ``1e-05``-style scientific notation."""
    s = f"{amount_crypto:.6f}".rstrip("0").rstrip(".")
    return s or "0"


class WithdrawService:
    """Escrow-on-create withdrawal lifecycle over one economy session."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        economy: EconomyService,
        withdrawals: WithdrawalsRepo,
        coins_per_usdt: float,
        min_coins: int,
        max_coins: int,
        asset: str = "USDT",
        daily_limit_coins: int = 10_000,
        monthly_limit_coins: int = 100_000,
        transactions: TransactionsRepo | None = None,
        require_deposit: bool = True,
        payout_ratio: float = 1.0,
    ) -> None:
        self._session = session
        self._economy = economy
        self._withdrawals = withdrawals
        # T-019 (R2): the deposit gate. ``transactions`` is optional so
        # existing constructions keep working; without it the gate cannot
        # read the ledger and stays off, which is why the wiring passes it
        # explicitly. ``require_deposit`` is the operator's off switch.
        self._transactions = transactions
        self._require_deposit = require_deposit
        # T-020 (R6): lifetime payout ceiling as a multiple of lifetime
        # deposits. ``0`` disables the cap. Negatives can't reach here
        # (the config field is ``ge=0``), but clamp anyway — this service
        # is constructed directly in tests and a negative ratio would
        # silently refuse every withdrawal instead of failing loudly.
        self._payout_ratio = max(0.0, payout_ratio)
        self._coins_per_usdt = coins_per_usdt
        self._min_coins = min_coins
        self._max_coins = max_coins
        self._asset = asset
        # L-92: per-user rolling caps. Defaults mirror the legacy
        # ``user_withdrawal_limits`` row defaults (10000 / day, 100000 /
        # month). Usage is derived from request timestamps, so the caps
        # "reset" on the day / month boundary with no scheduled job.
        self._daily_limit_coins = daily_limit_coins
        self._monthly_limit_coins = monthly_limit_coins

    def to_crypto(self, amount_com: int) -> float:
        """Coins → asset units (USDT)."""
        return amount_com / self._coins_per_usdt

    @property
    def daily_limit_coins(self) -> int:
        """Configured per-user daily withdrawal cap (coins)."""
        return self._daily_limit_coins

    @property
    def monthly_limit_coins(self) -> int:
        """Configured per-user monthly withdrawal cap (coins)."""
        return self._monthly_limit_coins

    async def quota_status(self, user_id: int, *, now: datetime | None = None) -> QuotaStatus:
        """Current daily / monthly quota usage + remaining for ``user_id``.

        Read-only: sums the user's non-rejected requests in each window
        from their timestamps. The handler calls this to show the user
        their remaining allowance up-front (and on a quota rejection).
        """
        n = now or datetime.now(UTC)
        daily_used = await self._withdrawals.period_usage(user_id, since_iso=_day_start_iso(n))
        monthly_used = await self._withdrawals.period_usage(user_id, since_iso=_month_start_iso(n))
        return QuotaStatus(
            daily_limit=self._daily_limit_coins,
            daily_used=daily_used,
            daily_remaining=max(0, self._daily_limit_coins - daily_used),
            monthly_limit=self._monthly_limit_coins,
            monthly_used=monthly_used,
            monthly_remaining=max(0, self._monthly_limit_coins - monthly_used),
        )

    async def payout_headroom(self, user_id: int) -> int | None:
        """Coins ``user_id`` may still export over their whole lifetime.

        ``None`` means the T-020 (R6) cap is disarmed for this deployment
        — ``payout_ratio`` is ``0`` or no ledger is wired — and the caller
        should show only the rolling quotas. Read-only; ``create`` does
        its own read inside the escrow transaction rather than trusting a
        value the handler computed one round-trip ago.
        """
        if self._transactions is None or self._payout_ratio <= 0.0:
            return None
        deposited = await self._transactions.lifetime_deposits(user_id)
        return await self._headroom(user_id, deposited)

    async def check_lifetime_gate(self, user_id: int, amount_com: int) -> CreateResult | None:
        """Run both ledger-derived gates; ``None`` if the request clears.

        Returns exactly the :class:`CreateResult` ``create`` would return
        — same outcome, same ``remaining`` — so the handler can refuse at
        the amount step, before the confirm card is drawn, and render it
        through the same branches. This is a *courtesy* check: ``create``
        runs the identical gate again, this time under the writer lock it
        takes first (see :meth:`WithdrawalsRepo.lock_writer`), so a stale
        card or a request that raced in between still cannot slip past.
        Called on its own — as the amount step does — it is a plain read
        with no isolation, and that is fine: it only decides whether to
        draw a confirm card.
        """
        # T-019 (R2): only accounts that have actually bought coins may
        # cash out. Without this the emission side alone funds withdrawals
        # — a script clearing the 4 500 COM floor in about a day of
        # chatting turns into 5 USDT of the owner's money, repeatable up
        # to the rolling caps, with nothing ever paid in.
        # T-020 (R6) shares the same ledger read: the threshold gate asks
        # whether money ever came in, the cap asks how much. One query
        # answers both, so read it once when either gate is armed.
        if self._transactions is None:
            return None
        if not self._require_deposit and self._payout_ratio <= 0.0:
            return None
        deposited = await self._transactions.lifetime_deposits(user_id)
        if self._require_deposit and deposited <= 0:
            log.bind(uid=user_id, amount_com=amount_com).info(
                "withdraw refused — account has never purchased coins"
            )
            return CreateResult(CreateOutcome.NO_DEPOSITS)
        if self._payout_ratio > 0.0:
            remaining = await self._headroom(user_id, deposited)
            if amount_com > remaining:
                log.bind(
                    uid=user_id,
                    amount_com=amount_com,
                    deposited=deposited,
                    ratio=self._payout_ratio,
                    remaining=remaining,
                ).info("withdraw refused — lifetime payout cap reached")
                return CreateResult(CreateOutcome.PAYOUT_CAP_EXCEEDED, remaining=remaining)
        return None

    async def _headroom(self, user_id: int, deposited: int) -> int:
        """Lifetime allowance minus lifetime usage, floored at zero.

        Split out so the display path and the enforcing path in ``create``
        can never drift: a headroom the card promises has to be the same
        arithmetic the gate applies.
        """
        # Floor, so the cap can never round *up* into paying out more than
        # was paid in. ``lifetime_usage`` counts pending requests too — an
        # unapproved request is escrowed money on its way out, and letting
        # a second request be created against the same headroom is exactly
        # the double-spend the escrow-on-create design exists to prevent.
        allowance = int(deposited * self._payout_ratio)
        withdrawn = await self._withdrawals.lifetime_usage(user_id)
        return max(0, allowance - withdrawn)

    async def create(self, *, user_id: int, amount_com: int) -> CreateResult:
        """Escrow ``amount_com`` and insert a ``pending`` request, atomically.

        Returns :attr:`CreateOutcome.OK` with the new id on success, or a
        limit / funds outcome with no side effects. The escrow debit and
        the row insert are committed together — if the wallet is short the
        debit returns ``None``, no row is written, and the commit persists
        nothing.

        #776: "atomically" needs a writer lock, not merely one commit.
        SQLAlchemy's autobegin does group the statements, but this project
        opens the *SQLite* transaction lazily and only for writes
        (``db/engines.py:204-210``), so the gate reads below would run
        outside any transaction and two callers could both be admitted
        against the same pre-debit totals. :meth:`WithdrawalsRepo.
        lock_writer` is therefore called before the first gate read, and
        every refusal path commits to hand the lock back.
        """
        # #245(c): the floor is policy this port introduced, not
        # behaviour it inherited. Legacy's three withdraw rails refused
        # only ``amount <= 0`` and ``amount > balance``
        # (``bot.py:20408``, ``:20466-20471``, ``:20606``) — there is
        # no minimum anywhere in ``bot.py``. It stays: below it a
        # Crypto Pay transfer costs more in network fees than it moves.
        # Recorded here so the divergence is documented, not silent.
        if amount_com < self._min_coins:
            return CreateResult(CreateOutcome.BELOW_MIN)
        if amount_com > self._max_coins:
            return CreateResult(CreateOutcome.ABOVE_MAX)

        # #776: take the writer lock before the first gate read. The
        # bounds above are pure arithmetic on the argument and need no
        # isolation, so they stay outside it — a request that is simply
        # out of range must not queue behind another user's escrow.
        # Everything from here to the commit is the critical section:
        # the totals the gates read must not move under us between the
        # read and the debit that consumes the headroom they granted.
        await self._withdrawals.lock_writer()

        # The two lifetime gates (R2 threshold, R6 cap) run first, so a
        # refused request never touches the wallet or the quota reads.
        refusal = await self.check_lifetime_gate(user_id, amount_com)
        if refusal is not None:
            # Release the writer lock now. ``commit`` rather than
            # ``rollback``: nothing of ours needs discarding (the lock
            # statement matched no rows), and the caller's session may
            # carry unrelated writes that the middleware would commit
            # anyway (``middlewares/base.py:157-158``) — rolling those
            # back to hand a lock over would be a silent data loss.
            # Without this the lock would be held across the refusal's
            # Telegram round-trip, which is exactly the stall #778 is
            # about.
            await self._session.commit()
            return refusal

        # L-92: enforce the rolling daily / monthly caps. Usage is summed
        # live from the user's request timestamps (see ``quota_status``),
        # so the cap self-resets at the cycle boundary with no counter and
        # no scheduled job. Check before the escrow debit so a rejected
        # request never moves coins. The read runs under the writer lock
        # taken above, so the totals it sums cannot grow between here and
        # the debit below; the request that would tip the user over is
        # refused and ``remaining`` tells them the headroom.
        #
        # #245(c): the daily half is a faithful port — legacy enforced
        # it on every rail (``bot.py:20411``, ``:20472``, ``:20609``).
        # The monthly half is new enforcement of an old number: legacy
        # declared and reset ``monthly_limit_com`` (``bot.py:5223``,
        # ``:10023``) but never compared a request against it.
        quota = await self.quota_status(user_id)
        if amount_com > quota.daily_remaining:
            await self._session.commit()  # release the #776 writer lock
            return CreateResult(CreateOutcome.DAILY_QUOTA_EXCEEDED, remaining=quota.daily_remaining)
        if amount_com > quota.monthly_remaining:
            await self._session.commit()  # release the #776 writer lock
            return CreateResult(
                CreateOutcome.MONTHLY_QUOTA_EXCEEDED, remaining=quota.monthly_remaining
            )

        amount_crypto = self.to_crypto(amount_com)
        request_id: int | None = None
        wallet = await self._economy.hold(
            user_id,
            amount_com,
            type=_ESCROW_TYPE,
            reason="withdraw escrow hold",
        )
        # ``hold`` returns None without mutating anything when the wallet
        # is short, so the commit below persists nothing — no orphan row,
        # no phantom debit.
        if wallet is not None:
            request_id = await self._withdrawals.create(
                user_id=user_id,
                amount_com=amount_com,
                amount_crypto=amount_crypto,
                asset=self._asset,
                created_at=_now_iso(),
            )
        await self._session.commit()
        if request_id is None:
            return CreateResult(CreateOutcome.INSUFFICIENT_FUNDS)
        log.bind(uid=user_id, request_id=request_id, amount_com=amount_com).info(
            "withdraw request created (escrowed)"
        )
        return CreateResult(CreateOutcome.OK, request_id=request_id, amount_crypto=amount_crypto)

    async def approve(
        self, *, request_id: int, admin_id: int, client: CryptoPayClient
    ) -> ApproveResult:
        """Lease the request, pay via Crypto Pay, then mark it ``completed``.

        Idempotency, stated precisely because the two halves protect
        different things (#1992). ``spend_id = wd_<id>`` is the ONLY
        thing that keeps two concurrent approvals from becoming two USDT
        payments: the lease taken below does not exclude a second
        approver — :meth:`WithdrawalsRepo.claim_processing` matches
        ``processing`` as well as ``pending`` on purpose, so a payer that
        dies mid-transfer cannot strand the row — and both admins really
        do reach ``client.transfer``. The conditional ``claim_terminal``
        at the end protects the ROW, not the money: it decides which
        writer records the completion, long after both calls went out.
        So there is one layer here, not two, and it lives at the
        provider. ``tests/regression/test_withdraw_double_approve``
        pins both facts. The escrow was debited at create, so approval
        moves no on-platform coins.

        T-020 R13 — why the row is claimed BEFORE the provider call.
        A Crypto Pay transfer is seconds of network I/O and a DB
        transaction must not be held open across it, so this method used
        to read ``status == 'pending'``, release the read transaction,
        pay, and only then claim. ``spend_id`` made that safe against a
        *second approve*, and the conditional claim made it safe against
        a *double completion* — but neither guards the third race:

            approve   reads pending ─┐
                                     ├─ reject() claims pending → rejected
                                     │  and CREDITS THE ESCROW BACK
            approve   pays USDT ─────┘
            approve   claim_terminal fails → "already processed"

        The old code called that outcome benign, reasoning about the
        provider's dedup — which is the wrong invariant. The user was
        paid once, correct; they also got every escrowed coin refunded.
        Money left the owner's pocket twice for one request, and the log
        line said the request was merely already processed.

        The fix is a ``processing`` lease taken and COMMITTED before the
        transfer. ``reject`` and :meth:`approve_manual` both guard on
        ``pending``, so a leased row is untouchable by either; on a
        provider refusal the lease is released and the request returns
        to the queue with no money moved. Note what the lease is NOT:
        mutual exclusion between approvers. It shuts out the refund
        paths, which is the race that cost money — see the idempotency
        note above for the one it deliberately leaves open.

        T-020 R14 — why "refusal" is narrower than "exception". The
        release above is correct only when the provider is *known* not to
        have paid. A read timeout is not that: the request reached the
        socket, and the reply that never arrived may have been a success.
        Releasing there reopens exactly the race the lease was built to
        close, just with a slower fuse —

            approve   pays USDT, reply lost ─→ lease released → pending
            admin     sees it back in the queue, taps «Отклонить»
            reject    refunds every escrowed coin

        — and the user keeps both sides again.
        :class:`CryptoPayUnconfirmed` separates the two, and its branch
        keeps the lease. A stuck ``processing`` row is the deliberate
        cost: it is visible, un-refundable, and clears the moment an
        admin re-approves, which the provider dedupes on ``spend_id``.
        """
        # Claim first, ask questions later: the lease is a conditional
        # UPDATE, so a missing row and an already-terminal row both come
        # back as ``False``. Read afterwards to tell them apart and to
        # get the payout fields.
        leased = await self._withdrawals.claim_processing(request_id, processed_by=admin_id)
        if not leased:
            await self._session.rollback()
            row = await self._withdrawals.get(request_id)
            await self._session.rollback()
            if row is None:
                return ApproveResult(ApproveOutcome.NOT_FOUND)
            return ApproveResult(ApproveOutcome.ALREADY_PROCESSED)
        row = await self._withdrawals.get(request_id)
        if row is None:  # pragma: no cover — the lease just matched it
            await self._session.rollback()
            return ApproveResult(ApproveOutcome.NOT_FOUND)
        # Snapshot the columns we need before the commit below so the
        # values are in hand across the network call that follows.
        # NOT because a commit expires the row: ``expire_on_commit=False``
        # (db/engines.py:213, stated as an invariant at
        # db/session.py:132-136), which is also what lets ``reject``
        # read ``row.user_id`` after ITS commit. This comment used to
        # claim the opposite and cite a lazy reload that cannot happen;
        # the sibling in ``approve_manual`` states the rule correctly.
        user_id = row.user_id
        currency = row.currency
        amount_com = row.amount_com
        row_amount_crypto = row.amount_crypto
        # Commit the lease before the external Crypto Pay call: it has to
        # be VISIBLE to the concurrent session a rejecting admin runs in,
        # and we must not hold a DB transaction open across network I/O.
        await self._session.commit()

        asset = currency or self._asset
        amount_crypto = row_amount_crypto
        if amount_crypto is None:
            amount_crypto = self.to_crypto(amount_com)
        amount_str = format_crypto_amount(amount_crypto)
        try:
            result = await client.transfer(
                user_id=user_id,
                asset=asset,
                amount=amount_str,
                spend_id=f"wd_{request_id}",
                comment="Withdrawal payout",
            )
        except CryptoPayInsufficientFunds:
            log.bind(request_id=request_id).warning("approve: app wallet empty")
            await self._release_lease(request_id)
            return ApproveResult(ApproveOutcome.APP_WALLET_EMPTY)
        except CryptoPayUnconfirmed as exc:
            # SEC: ordered BEFORE the generic handler on purpose — it is
            # a CryptoPayError subclass, and the wrong order would put it
            # straight back into the release-the-lease branch.
            #
            # No ``_release_lease`` here: we do not know that the money
            # stayed put, and a released row can be rejected for a full
            # escrow refund. Leave it ``processing`` — un-rejectable,
            # still re-approvable — and make the log loud, because this
            # is the one outcome that needs a human to look at the Crypto
            # Pay dashboard.
            log.bind(request_id=request_id, err=str(exc)).error(
                "approve: payout outcome UNKNOWN — request left leased, "
                "re-approve is safe (spend_id dedupe)"
            )
            return ApproveResult(ApproveOutcome.PAYOUT_UNCONFIRMED)
        except CryptoPayError as exc:
            log.bind(request_id=request_id, err=str(exc)).error("approve: provider error")
            await self._release_lease(request_id)
            return ApproveResult(ApproveOutcome.PROVIDER_ERROR)

        tx_hash = str(result.transfer_id)
        claimed = await self._withdrawals.claim_terminal(
            request_id,
            status="completed",
            processed_by=admin_id,
            processed_at=_now_iso(),
            tx_hash=tx_hash,
            from_status=PROCESSING_STATUS,
        )
        await self._session.commit()
        if not claimed:
            # Lost the race to finalize against another admin who leased
            # and completed the same row. The provider's spend_id dedup
            # means the user was paid exactly once regardless, and the
            # lease means no refund could have interleaved — so this one
            # really is benign. Surface "already done" to the admin.
            return ApproveResult(ApproveOutcome.ALREADY_PROCESSED)
        log.bind(request_id=request_id, admin_id=admin_id, tx=tx_hash).info(
            "withdraw request approved + paid"
        )
        return ApproveResult(
            ApproveOutcome.COMPLETED,
            user_id=user_id,
            amount_crypto=amount_crypto,
            asset=asset,
            tx_hash=tx_hash,
        )

    async def _release_lease(self, request_id: int) -> None:
        """Return a leased request to the queue after a *refused* payout.

        Only for outcomes where the provider is known not to have paid —
        an empty app wallet or an ``ok:false`` API error. An unconfirmed
        outcome must never come here; putting the row back in the queue
        is what makes a refund possible, so
        :class:`CryptoPayUnconfirmed` keeps the lease instead.

        A release that matches no row (another admin finalised the payout
        while our transfer was failing) is not an error — the conditional
        UPDATE simply changes nothing, and the commit is a no-op.
        """
        await self._withdrawals.release_processing(request_id)
        await self._session.commit()

    async def approve_manual(
        self, *, request_id: int, admin_id: int, note: str | None = None
    ) -> ApproveResult:
        """Mark the request ``completed`` — manual-payout doctrine v1.

        Mirrors :meth:`reject`'s claim-first shape, minus the wallet
        release: the escrow was held at create and the operator pays
        the user out-of-band (exactly the legacy contract —
        ``bot.py:20717`` ``admin_confirm_withdrawal`` flips the row to
        ``completed`` and DMs the user with NO provider call; the
        payout itself is a manual CryptoPay/card transfer by the
        operator). No Crypto Pay auto-transfer happens here — that
        auto-payout path exists as :meth:`approve` but is intentionally
        NOT wired in v1 (L-99-adjacent decision: ``withdraw_instant``
        was EOL in legacy, payouts stay manual).

        ``claim_terminal``'s conditional UPDATE is the only write, so a
        two-admin race resolves to exactly one ``COMPLETED`` and one
        ``ALREADY_PROCESSED`` — no money moves either way. ``tx_hash``
        is stamped ``"manual"`` so an audit can tell a manual payout
        from a provider transfer id.
        """
        row = await self._withdrawals.get(request_id)
        if row is None:
            return ApproveResult(ApproveOutcome.NOT_FOUND)
        if row.status != "pending":
            return ApproveResult(ApproveOutcome.ALREADY_PROCESSED)
        # Snapshot up front so the values are in hand whatever the
        # transaction does next. NOT because a commit expires the row:
        # ``expire_on_commit=False`` (db/engines.py:213, stated as an
        # invariant at db/session.py:132-136). The sibling snapshot in
        # ``reject`` is the mandatory one — a *rollback* does expire.
        user_id = row.user_id
        amount_com = row.amount_com
        amount_crypto = row.amount_crypto
        if amount_crypto is None:
            amount_crypto = self.to_crypto(amount_com)
        asset = row.currency or self._asset

        claimed = await self._withdrawals.claim_terminal(
            request_id,
            status="completed",
            processed_by=admin_id,
            processed_at=_now_iso(),
            tx_hash="manual",
            admin_note=note,
        )
        await self._session.commit()
        if not claimed:
            return ApproveResult(ApproveOutcome.ALREADY_PROCESSED)
        log.bind(request_id=request_id, admin_id=admin_id, uid=user_id, com=amount_com).info(
            "withdraw request approved (manual payout)"
        )
        return ApproveResult(
            ApproveOutcome.COMPLETED,
            user_id=user_id,
            amount_crypto=amount_crypto,
            asset=asset,
        )

    async def reject(
        self, *, request_id: int, admin_id: int, note: str | None = None
    ) -> RejectResult:
        """Refund the escrow and mark the request ``rejected``, atomically.

        The conditional ``claim_terminal`` runs first: if it returns
        ``False`` the row was already processed and we refund nothing
        (avoids a double refund on a reject-after-approve or two-admin
        race). The claim and the refund share one ``SAVEPOINT``, so the
        pair is all-or-nothing without the compensation reaching past
        them into whatever else the update has written.
        """
        row = await self._withdrawals.get(request_id)
        if row is None:
            return RejectResult(RejectOutcome.NOT_FOUND)
        if row.status != "pending":
            return RejectResult(RejectOutcome.ALREADY_PROCESSED)

        # #245(a): legacy wrote ``'cancelled'`` here (``bot.py:20754``);
        # this port writes ``'rejected'``, and prod already holds one row
        # of each. The literal is NOT changed back — ``RejectOutcome``
        # and four tests pin it, and no reader anywhere compares against
        # either spelling (``withdrawals_repo._QUOTA_STATUSES`` excludes
        # both). The only place the difference was ever visible was the
        # user's own ``/withdraw_status`` card, and that is where it is
        # reconciled: ``handlers/withdraw_status._STATUS_KEYS`` maps both
        # spellings onto one label. Documented here so the divergence is
        # on the record rather than waiting to be rediscovered.
        # #209/#1520/#1985/#1986: the claim and its compensation share
        # one SAVEPOINT. What must be undone when the refund fails is
        # the claim — and *only* the claim. This is the handler's
        # session, so rolling the session back would also discard every
        # uncommitted row the update wrote before calling in; the
        # p2p, purchase and transfer paths all made that argument
        # already, and this was the last service still rolling back the
        # whole thing.
        async with self._session.begin_nested() as savepoint:
            claimed = await self._withdrawals.claim_terminal(
                request_id,
                status="rejected",
                processed_by=admin_id,
                processed_at=_now_iso(),
                admin_note=note,
            )
            # Only refund if THIS call won the claim. A rowcount-0 claim
            # (already processed by a racing approve/reject) made no DB
            # change, so skipping the release means the commit is a no-op —
            # no double refund.
            if claimed:
                # Capture the fields BEFORE the release/rollback: a rollback
                # expires the ORM ``row``, so reading ``row.user_id`` after it
                # would trigger lazy IO outside the async context.
                refund_uid = row.user_id
                refund_amount = row.amount_com
                refunded = await self._economy.release(
                    refund_uid,
                    refund_amount,
                    type=_REFUND_TYPE,
                    reason="withdraw rejected refund",
                )
                if refunded is None:
                    # SEC: the refund failed (balance cap / vanished wallet).
                    # ``claim_terminal`` already flipped the row to "rejected"
                    # in this (uncommitted) savepoint; committing now would
                    # mark it rejected WITHOUT returning the escrowed coins —
                    # the user would silently lose them. Roll the savepoint
                    # back so the request stays pending and retryable. This
                    # path RETURNS, so the middleware never sees an
                    # exception — it commits, and what it commits must
                    # therefore no longer contain the claim.
                    await savepoint.rollback()
                    log.bind(request_id=request_id, uid=refund_uid).error(
                        "withdraw refund release failed — request kept pending"
                    )
                    return RejectResult(RejectOutcome.REFUND_FAILED)
        await self._session.commit()
        if not claimed:
            return RejectResult(RejectOutcome.ALREADY_PROCESSED)
        log.bind(request_id=request_id, admin_id=admin_id, uid=row.user_id).info(
            "withdraw request rejected + refunded"
        )
        return RejectResult(RejectOutcome.REJECTED, user_id=row.user_id, amount_com=row.amount_com)
