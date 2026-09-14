"""``WithdrawService`` integration — escrow-on-create lifecycle (T-027).

Pins the money invariants end-to-end against a real economy session:

* **create** escrows coins (wallet debited) + writes a ``pending`` row;
  below-min / above-max / insufficient-funds short-circuit with NO side
  effects.
* **approve** pays via Crypto Pay (faked) and flips the row to
  ``completed`` with the transfer id as ``tx_hash``; the escrow was
  already taken at create, so the balance does not move again.
* **approve** maps an empty app wallet / provider error to the right
  outcome and leaves the row ``pending`` (retryable).
* **reject** refunds the escrow and flips to ``rejected``; a re-reject is
  a no-op (no double refund).
* approve-after-reject (and vice-versa) hits the conditional-claim guard
  and reports ``ALREADY_PROCESSED`` — the wallet is touched exactly once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    Transaction,
    WithdrawalRequest,
)
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.withdrawals_repo import WithdrawalsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.payments.crypto_client import (
    CryptoPayError,
    CryptoPayInsufficientFunds,
    TransferResult,
)
from telegram_invite_bot.services.withdraw_service import (
    ApproveOutcome,
    CreateOutcome,
    RejectOutcome,
    WithdrawService,
    _day_start_iso,
    _month_start_iso,
)

_USER = 777
_COINS_PER_USDT = 900.0
_MIN = 4500
_MAX = 90000
# L-92 quota caps for the test service (well above _MAX so the band
# checks don't mask quota behaviour, but low enough to exercise it).
_DAILY = 9000
_MONTHLY = 18000


class _FakeClient:
    """Stand-in for :class:`CryptoPayClient.transfer`.

    ``mode`` picks the behaviour: ``"ok"`` returns a TransferResult and
    records the call; ``"empty"`` raises insufficient-funds; ``"error"``
    raises a generic provider error. ``calls`` lets a test assert the
    transfer happened exactly once (idempotency proof).
    """

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[dict[str, object]] = []

    async def transfer(
        self,
        *,
        user_id: int,
        asset: str,
        amount: str,
        spend_id: str,
        comment: str | None = None,
    ) -> TransferResult:
        self.calls.append(
            {"user_id": user_id, "asset": asset, "amount": amount, "spend_id": spend_id}
        )
        if self.mode == "empty":
            raise CryptoPayInsufficientFunds("app wallet short", name="NOT_ENOUGH_COINS")
        if self.mode == "error":
            raise CryptoPayError("boom")
        return TransferResult(transfer_id=4242, spend_id=spend_id, asset=asset, amount=amount)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _service(
    session: AsyncSession,
    *,
    daily_limit: int = 1_000_000,
    monthly_limit: int = 10_000_000,
) -> WithdrawService:
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))
    return WithdrawService(
        session=session,
        economy=economy,
        withdrawals=WithdrawalsRepo(session),
        coins_per_usdt=_COINS_PER_USDT,
        min_coins=_MIN,
        max_coins=_MAX,
        asset="USDT",
        # Generous caps by default so the pre-L-92 lifecycle tests are
        # unaffected; the quota tests pass tight caps explicitly.
        daily_limit_coins=daily_limit,
        monthly_limit_coins=monthly_limit,
    )


async def _seed(session: AsyncSession, balance: int) -> None:
    session.add(EconomyUser(user_id=_USER, balance=balance, language="ru"))
    await session.commit()


async def _balance(session: AsyncSession) -> int | None:
    row = await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == _USER))
    val = row.scalar_one_or_none()
    return int(val) if val is not None else None


async def _status(session: AsyncSession, request_id: int) -> str | None:
    row = await session.execute(
        select(WithdrawalRequest.status).where(WithdrawalRequest.id == request_id)
    )
    return row.scalar_one_or_none()


async def _pending_count(session: AsyncSession) -> int:
    row = await session.execute(select(func.count()).select_from(WithdrawalRequest))
    return int(row.scalar_one())


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_escrows_and_inserts_pending(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    result = await svc.create(user_id=_USER, amount_com=4500)
    assert result.outcome is CreateOutcome.OK
    assert result.request_id is not None
    # 4500 / 900 = 5.0 USDT.
    assert result.amount_crypto == pytest.approx(5.0)
    # Escrowed: balance dropped by the full amount.
    assert await _balance(session) == 10_000 - 4500
    assert await _status(session, result.request_id) == "pending"


async def test_create_below_min_no_side_effects(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    result = await svc.create(user_id=_USER, amount_com=_MIN - 1)
    assert result.outcome is CreateOutcome.BELOW_MIN
    assert await _balance(session) == 10_000
    assert await _pending_count(session) == 0


async def test_create_above_max_no_side_effects(session: AsyncSession) -> None:
    await _seed(session, balance=1_000_000)
    svc = _service(session)
    result = await svc.create(user_id=_USER, amount_com=_MAX + 1)
    assert result.outcome is CreateOutcome.ABOVE_MAX
    assert await _balance(session) == 1_000_000
    assert await _pending_count(session) == 0


async def test_create_insufficient_funds_no_row(session: AsyncSession) -> None:
    await _seed(session, balance=4500)
    svc = _service(session)
    # Wants 9000 but only has 4500.
    result = await svc.create(user_id=_USER, amount_com=9000)
    assert result.outcome is CreateOutcome.INSUFFICIENT_FUNDS
    assert await _balance(session) == 4500
    assert await _pending_count(session) == 0


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


async def test_approve_pays_and_completes(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    client = _FakeClient(mode="ok")
    result = await svc.approve(request_id=created.request_id, admin_id=1, client=client)  # type: ignore[arg-type]
    assert result.outcome is ApproveOutcome.COMPLETED
    assert result.tx_hash == "4242"
    # Paid exactly once, with the deterministic spend_id.
    assert len(client.calls) == 1
    assert client.calls[0]["spend_id"] == f"wd_{created.request_id}"
    assert client.calls[0]["amount"] == "5"
    assert await _status(session, created.request_id) == "completed"
    # Balance unchanged by approval — escrow was taken at create.
    assert await _balance(session) == 10_000 - 4500


async def test_approve_app_wallet_empty_stays_pending(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    result = await svc.approve(
        request_id=created.request_id,
        admin_id=1,
        client=_FakeClient(mode="empty"),  # type: ignore[arg-type]
    )
    assert result.outcome is ApproveOutcome.APP_WALLET_EMPTY
    # Retryable: row left pending, coins still escrowed.
    assert await _status(session, created.request_id) == "pending"
    assert await _balance(session) == 10_000 - 4500


async def test_approve_provider_error_stays_pending(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    result = await svc.approve(
        request_id=created.request_id,
        admin_id=1,
        client=_FakeClient(mode="error"),  # type: ignore[arg-type]
    )
    assert result.outcome is ApproveOutcome.PROVIDER_ERROR
    assert await _status(session, created.request_id) == "pending"


async def test_approve_unknown_request(session: AsyncSession) -> None:
    svc = _service(session)
    result = await svc.approve(request_id=999, admin_id=1, client=_FakeClient())  # type: ignore[arg-type]
    assert result.outcome is ApproveOutcome.NOT_FOUND


# ---------------------------------------------------------------------------
# T-020 R13: the ``processing`` lease around the provider call
# ---------------------------------------------------------------------------


class _RejectMidTransferClient(_FakeClient):
    """Pays, but runs a concurrent ``reject`` while the payout is in flight.

    Models the admin race the lease exists to stop: one admin approves,
    a second rejects during the seconds the Crypto Pay round-trip takes.
    Calling ``reject`` from inside ``transfer`` puts it exactly in that
    window without needing real concurrency.
    """

    def __init__(self, svc: WithdrawService, request_id: int) -> None:
        super().__init__(mode="ok")
        self._svc = svc
        self._request_id = request_id
        self.reject_outcome: RejectOutcome | None = None

    async def transfer(self, **kwargs: object) -> TransferResult:
        result = await self._svc.reject(request_id=self._request_id, admin_id=99)
        self.reject_outcome = result.outcome
        return await super().transfer(**kwargs)  # type: ignore[arg-type]


async def test_reject_cannot_refund_a_payout_already_in_flight(
    session: AsyncSession,
) -> None:
    """R13: the escrow must not come back while the USDT is on its way.

    Before the ``processing`` lease, ``approve`` read ``pending``,
    released its transaction, and paid — so a ``reject`` landing in that
    window flipped the row to ``rejected`` AND credited the escrow back.
    The user kept the coins *and* the crypto; ``approve``'s late claim
    failed and reported the double payout as "already processed".
    """
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    assert await _balance(session) == 10_000 - 4500

    client = _RejectMidTransferClient(svc, created.request_id)
    result = await svc.approve(request_id=created.request_id, admin_id=1, client=client)  # type: ignore[arg-type]

    # The interleaved reject found a leased row and refused it.
    assert client.reject_outcome is RejectOutcome.ALREADY_PROCESSED
    # The payout completed normally...
    assert result.outcome is ApproveOutcome.COMPLETED
    assert len(client.calls) == 1
    assert await _status(session, created.request_id) == "completed"
    # ...and the escrow was NOT refunded. This is the assertion that
    # fails on the pre-R13 code: the balance would be back at 10_000
    # with the USDT already sent.
    assert await _balance(session) == 10_000 - 4500


async def test_leased_request_is_not_manually_approvable(session: AsyncSession) -> None:
    """The lease blocks ``approve_manual`` too — one payout, one route.

    Without this the operator could pay out-of-band and mark the row
    ``completed`` while the automatic transfer was still in flight,
    paying the same request twice through two different rails (where
    ``spend_id`` can't dedupe, because only one of them is Crypto Pay).
    """
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    leased = await WithdrawalsRepo(session).claim_processing(created.request_id, processed_by=1)
    await session.commit()
    assert leased is True

    result = await svc.approve_manual(request_id=created.request_id, admin_id=2)
    assert result.outcome is ApproveOutcome.ALREADY_PROCESSED
    assert await _status(session, created.request_id) == "processing"


async def test_provider_failure_releases_the_lease_for_a_retry(
    session: AsyncSession,
) -> None:
    """A refused transfer must hand the row back, not strand it.

    ``processing`` is invisible to every admin surface (they list
    ``pending``), so a lease that outlives its payout attempt would bury
    the request. The release is what keeps ``APP_WALLET_EMPTY`` the
    retryable outcome its name promises.
    """
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None

    failed = await svc.approve(
        request_id=created.request_id,
        admin_id=1,
        client=_FakeClient(mode="empty"),  # type: ignore[arg-type]
    )
    assert failed.outcome is ApproveOutcome.APP_WALLET_EMPTY
    assert await _status(session, created.request_id) == "pending"

    # The retry succeeds against the very same row.
    retried = await svc.approve(
        request_id=created.request_id,
        admin_id=1,
        client=_FakeClient(mode="ok"),  # type: ignore[arg-type]
    )
    assert retried.outcome is ApproveOutcome.COMPLETED
    assert await _status(session, created.request_id) == "completed"


async def test_in_flight_payout_still_consumes_quota(session: AsyncSession) -> None:
    """A leased request keeps counting against the rolling caps.

    ``processing`` means escrow held and money leaving — dropping it from
    the quota sum would open a window exactly as wide as the provider
    round-trip, through which a second withdrawal could slip.
    """
    await _seed(session, balance=100_000)
    svc = _service(session, daily_limit=_DAILY, monthly_limit=_MONTHLY)
    created = await svc.create(user_id=_USER, amount_com=_DAILY)
    assert created.request_id is not None
    await WithdrawalsRepo(session).claim_processing(created.request_id, processed_by=1)
    await session.commit()
    assert await _status(session, created.request_id) == "processing"

    quota = await svc.quota_status(_USER)
    assert quota.daily_remaining == 0
    second = await svc.create(user_id=_USER, amount_com=_MIN)
    assert second.outcome is CreateOutcome.DAILY_QUOTA_EXCEEDED


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------


async def test_reject_refunds_and_marks_rejected(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    assert await _balance(session) == 10_000 - 4500
    result = await svc.reject(request_id=created.request_id, admin_id=1, note="bad details")
    assert result.outcome is RejectOutcome.REJECTED
    # Refunded back to full balance.
    assert await _balance(session) == 10_000
    assert await _status(session, created.request_id) == "rejected"


async def test_reject_twice_no_double_refund(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    await svc.reject(request_id=created.request_id, admin_id=1)
    second = await svc.reject(request_id=created.request_id, admin_id=2)
    assert second.outcome is RejectOutcome.ALREADY_PROCESSED
    # Balance refunded exactly once.
    assert await _balance(session) == 10_000


async def test_approve_after_reject_is_already_processed(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    await svc.reject(request_id=created.request_id, admin_id=1)
    client = _FakeClient(mode="ok")
    result = await svc.approve(request_id=created.request_id, admin_id=2, client=client)  # type: ignore[arg-type]
    assert result.outcome is ApproveOutcome.ALREADY_PROCESSED
    # No payout attempted for an already-rejected request.
    assert client.calls == []
    assert await _balance(session) == 10_000


async def test_reject_refund_failure_keeps_request_pending(session: AsyncSession) -> None:
    """SEC: if the refund credit fails (balance-cap overflow), the reject
    rolls back — the request stays pending and the escrowed coins are NOT
    lost (before the fix it was marked rejected without refunding)."""
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    # Push the user to the balance cap so refunding 4500 overflows →
    # EconomyService.credit returns None.
    await EconomyRepo(session).set_balance(_USER, 10**15)
    await session.commit()

    result = await svc.reject(request_id=created.request_id, admin_id=1)
    assert result.outcome is RejectOutcome.REFUND_FAILED
    # Still pending (not silently marked rejected) → an admin can retry.
    assert await _status(session, created.request_id) == "pending"
    # No partial refund — balance is exactly the cap we set.
    assert await _balance(session) == 10**15


async def test_reject_refund_failure_keeps_the_handlers_other_writes(
    session: AsyncSession,
) -> None:
    """The refusal must undo the claim and nothing else.

    ``reject`` runs on the update's session (``middlewares/base``
    hands the same one to every repo and service), so rolling *the
    session* back to undo the claim discards every uncommitted row the
    handler wrote before calling in — the exact defect #209/#1520/#1985
    /#1986 removed from the p2p, purchase and transfer paths, where the
    remedy is a ``SAVEPOINT`` around the claim and its compensation.

    Today's only caller (``handlers/admin/withdrawals.py:758``) writes
    nothing else to economy.db, so the blast radius is zero right now.
    That is what makes this the moment to pin the property: the first
    handler that does write something else would otherwise lose it
    silently, on a branch that only fires when a refund is already
    failing.
    """
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None
    # Same trick as the test above: at the cap, refunding overflows and
    # ``release`` returns None, which is the branch under test.
    await EconomyRepo(session).set_balance(_USER, 10**15)
    await session.commit()

    # Stand-in for whatever else the update had already written into
    # this transaction. A ledger row is the shape that would really be
    # there, and it is uncommitted at the instant the refund fails.
    session.add(
        Transaction(
            from_id=None,
            to_id=_USER,
            amount=1,
            type="unrelated",
            reason="written by the handler before the reject",
            date=datetime(2026, 1, 1, 12, 0),
        )
    )
    await session.flush()

    result = await svc.reject(request_id=created.request_id, admin_id=1)
    assert result.outcome is RejectOutcome.REFUND_FAILED

    await session.commit()
    kept = await session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.type == "unrelated")
    )
    assert int(kept.scalar_one()) == 1, (
        "the refusal rolled the whole session back and took the handler's unrelated row with it"
    )
    # ...and the claim is still undone, which is what the rollback was
    # for. Narrowing the scope must not lose the guarantee.
    assert await _status(session, created.request_id) == "pending"
    assert await _balance(session) == 10**15


# ---------------------------------------------------------------------------
# L-92: rolling daily / monthly quota (reset implicit via timestamps)
# ---------------------------------------------------------------------------


async def _backdate(session: AsyncSession, request_id: int, created_at: str) -> None:
    """Rewrite a request's ``created_at`` so it falls outside the current
    window — simulates the cycle boundary having passed."""
    row = await session.get(WithdrawalRequest, request_id)
    assert row is not None
    row.created_at = created_at
    await session.commit()


async def test_quota_status_starts_empty(session: AsyncSession) -> None:
    await _seed(session, balance=1_000_000)
    svc = _service(session, daily_limit=_DAILY, monthly_limit=_MONTHLY)
    q = await svc.quota_status(_USER)
    assert q.daily_used == 0
    assert q.daily_remaining == _DAILY
    assert q.monthly_remaining == _MONTHLY
    assert q.remaining == _DAILY  # tighter window binds


async def test_pending_request_consumes_quota(session: AsyncSession) -> None:
    await _seed(session, balance=1_000_000)
    svc = _service(session, daily_limit=_DAILY, monthly_limit=_MONTHLY)
    created = await svc.create(user_id=_USER, amount_com=_MIN)
    assert created.outcome is CreateOutcome.OK
    q = await svc.quota_status(_USER)
    # A still-pending request counts against the cap.
    assert q.daily_used == _MIN
    assert q.daily_remaining == _DAILY - _MIN


async def test_daily_quota_blocks_second_request(session: AsyncSession) -> None:
    await _seed(session, balance=1_000_000)
    svc = _service(session, daily_limit=_DAILY, monthly_limit=_MONTHLY)
    first = await svc.create(user_id=_USER, amount_com=_MIN)
    assert first.outcome is CreateOutcome.OK
    # _DAILY (9000) - _MIN (4500) = 4500 left; a second _MIN exactly fits.
    second = await svc.create(user_id=_USER, amount_com=_MIN)
    assert second.outcome is CreateOutcome.OK
    # Now the daily cap is fully used → a third is refused with headroom 0.
    third = await svc.create(user_id=_USER, amount_com=_MIN)
    assert third.outcome is CreateOutcome.DAILY_QUOTA_EXCEEDED
    assert third.remaining == 0
    # The refused request escrowed nothing.
    assert await _balance(session) == 1_000_000 - 2 * _MIN


async def test_rejected_request_does_not_consume_quota(session: AsyncSession) -> None:
    await _seed(session, balance=1_000_000)
    svc = _service(session, daily_limit=_DAILY, monthly_limit=_MONTHLY)
    first = await svc.create(user_id=_USER, amount_com=_MIN)
    assert first.request_id is not None
    second = await svc.create(user_id=_USER, amount_com=_MIN)
    assert second.outcome is CreateOutcome.OK
    # Reject the first → its escrow is refunded and it must free its quota.
    await svc.reject(request_id=first.request_id, admin_id=1)
    q = await svc.quota_status(_USER)
    assert q.daily_used == _MIN  # only the still-pending second counts
    # A fresh _MIN now fits again.
    third = await svc.create(user_id=_USER, amount_com=_MIN)
    assert third.outcome is CreateOutcome.OK


async def test_daily_reset_via_timestamp_boundary(session: AsyncSession) -> None:
    """The cap 'resets' purely by the timestamp window moving — no stored
    counter, no scheduled job. Backdating yesterday's request to before
    today's start frees today's quota."""
    await _seed(session, balance=1_000_000)
    svc = _service(session, daily_limit=_DAILY, monthly_limit=_MONTHLY)
    first = await svc.create(user_id=_USER, amount_com=_DAILY)
    assert first.outcome is CreateOutcome.OK
    assert first.request_id is not None
    # Cap is now fully used today.
    blocked = await svc.create(user_id=_USER, amount_com=_MIN)
    assert blocked.outcome is CreateOutcome.DAILY_QUOTA_EXCEEDED
    # Move that request far into the past — it leaves today's daily window,
    # which is all this test is about. (Monthly-window behaviour is covered
    # by ``test_monthly_quota_blocks_even_when_daily_ok``, which keeps its
    # backdated rows inside the current month on purpose.)
    await _backdate(session, first.request_id, "2000-01-02 12:00:00")
    q = await svc.quota_status(_USER)
    assert q.daily_used == 0
    assert q.daily_remaining == _DAILY
    nxt = await svc.create(user_id=_USER, amount_com=_MIN)
    assert nxt.outcome is CreateOutcome.OK


async def test_monthly_quota_blocks_even_when_daily_ok(session: AsyncSession) -> None:
    """The monthly window binds on its own, with the daily one wide open.

    The daily cap is lifted out of the way rather than dodged by
    spreading the fill over several *past* days: both windows are
    open-ended (``created_at >= start``), so a day-of-month that has not
    arrived yet is still inside today's daily window — the old shape
    filled days 05-08 and therefore blew up on its own daily cap for the
    first week of every month. Day 01 is the one in-month timestamp that
    is never in the future, and with a non-binding daily cap it does not
    matter that on the 1st it is also "today".
    """
    await _seed(session, balance=1_000_000)
    svc = _service(session, daily_limit=_MONTHLY * 2, monthly_limit=_MONTHLY)
    month = _month_start_iso()[:7]  # "YYYY-MM"
    for hour in ("09", "10", "11", "12"):
        r = await svc.create(user_id=_USER, amount_com=_MIN)
        assert r.request_id is not None
        # Backdated inside the month → still counts monthly. The month
        # window has no per-day component; that is what is under test.
        await _backdate(session, r.request_id, f"{month}-01 {hour}:00:00")
    # 4 * _MIN = _MONTHLY → month full while daily is nowhere near its cap.
    q = await svc.quota_status(_USER)
    assert q.daily_remaining >= _MIN  # daily is not what blocks below
    assert q.monthly_remaining == 0
    blocked = await svc.create(user_id=_USER, amount_com=_MIN)
    assert blocked.outcome is CreateOutcome.MONTHLY_QUOTA_EXCEEDED
    assert blocked.remaining == 0


def test_period_start_helpers_format() -> None:
    """Day/month starts are fixed-width ISO text matching the write-side
    ``created_at`` format so lexicographic ``>=`` is chronological."""
    from datetime import UTC, datetime

    sample = datetime(2026, 6, 9, 15, 30, 45, tzinfo=UTC)
    assert _day_start_iso(sample) == "2026-06-09 00:00:00"
    assert _month_start_iso(sample) == "2026-06-01 00:00:00"


# --------------------------------------------------------------------------
# approve_manual — manual-payout doctrine v1 (L-97/L-99)


@pytest.mark.asyncio
async def test_approve_manual_completes_without_provider(session: AsyncSession) -> None:
    """approve_manual flips the row to completed with tx_hash='manual'
    and moves no coins — the escrow was taken at create and the payout
    happens off-platform. No Crypto Pay client is even constructible
    here: the method takes none."""
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=_MIN)
    assert created.request_id is not None
    assert await _balance(session) == 10_000 - _MIN

    result = await svc.approve_manual(request_id=created.request_id, admin_id=1)

    assert result.outcome is ApproveOutcome.COMPLETED
    assert result.user_id == _USER
    assert await _status(session, created.request_id) == "completed"
    assert await _balance(session) == 10_000 - _MIN  # untouched by approval
    row = (
        await session.execute(
            select(WithdrawalRequest).where(WithdrawalRequest.id == created.request_id)
        )
    ).scalar_one()
    assert row.tx_hash == "manual"
    assert row.processed_by == 1
    assert row.processed_at is not None


@pytest.mark.asyncio
async def test_approve_manual_twice_is_already_processed(session: AsyncSession) -> None:
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=_MIN)
    assert created.request_id is not None
    first = await svc.approve_manual(request_id=created.request_id, admin_id=1)
    second = await svc.approve_manual(request_id=created.request_id, admin_id=2)
    assert first.outcome is ApproveOutcome.COMPLETED
    assert second.outcome is ApproveOutcome.ALREADY_PROCESSED
    row = (
        await session.execute(
            select(WithdrawalRequest).where(WithdrawalRequest.id == created.request_id)
        )
    ).scalar_one()
    assert row.processed_by == 1  # the first claimer won


@pytest.mark.asyncio
async def test_approve_manual_unknown_request(session: AsyncSession) -> None:
    svc = _service(session)
    result = await svc.approve_manual(request_id=12345, admin_id=1)
    assert result.outcome is ApproveOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_reject_after_manual_approve_no_refund(session: AsyncSession) -> None:
    """The claim guard makes approve_manual → reject a no-op: the wallet
    is never credited after a completed payout."""
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=_MIN)
    assert created.request_id is not None
    await svc.approve_manual(request_id=created.request_id, admin_id=1)

    result = await svc.reject(request_id=created.request_id, admin_id=2)

    assert result.outcome is RejectOutcome.ALREADY_PROCESSED
    assert await _balance(session) == 10_000 - _MIN  # no refund
    assert await _status(session, created.request_id) == "completed"


# ---------------------------------------------------------------------------
# T-020 (R6) — the lifetime payout cap
#
# R2 (``require_deposit``) is a threshold: it asks *whether* money ever
# came in. These pin the bound: *how much* may ever go out. The gap
# between the two is the win-and-withdraw route — deposit once, run the
# balance up in the near-zero-edge games, export the winnings.
# ---------------------------------------------------------------------------


async def _deposit(session: AsyncSession, amount: int) -> None:
    """Credit a ``purchase_*`` row — the only ledger type that counts as
    real money in (see ``TransactionsRepo.lifetime_deposits``)."""
    session.add(
        Transaction(
            from_id=None,
            to_id=_USER,
            amount=amount,
            reason="test top-up",
            date=datetime(2026, 1, 1),
            type="purchase_crypto",
        )
    )
    await session.commit()


def _capped_service(
    session: AsyncSession,
    *,
    payout_ratio: float = 1.0,
    require_deposit: bool = True,
) -> WithdrawService:
    """``_service`` plus the two ledger gates wired in. The plain
    ``_service`` leaves ``transactions=None``, which disables both."""
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))
    return WithdrawService(
        session=session,
        economy=economy,
        withdrawals=WithdrawalsRepo(session),
        transactions=TransactionsRepo(session),
        require_deposit=require_deposit,
        payout_ratio=payout_ratio,
        coins_per_usdt=_COINS_PER_USDT,
        min_coins=_MIN,
        max_coins=_MAX,
        asset="USDT",
        daily_limit_coins=1_000_000,
        monthly_limit_coins=10_000_000,
    )


async def test_cap_refuses_more_than_was_deposited(session: AsyncSession) -> None:
    """Paid for 1 000, holds 10 000, wants 4 500 out. R2 waves it through
    (it HAS deposited); the cap is what stops it."""
    await _seed(session, balance=10_000)
    await _deposit(session, 1_000)
    svc = _capped_service(session)

    result = await svc.create(user_id=_USER, amount_com=4500)

    assert result.outcome is CreateOutcome.PAYOUT_CAP_EXCEEDED
    assert result.remaining == 1_000
    assert await _balance(session) == 10_000  # nothing escrowed
    assert await _pending_count(session) == 0


async def test_cap_allows_a_full_honest_exit(session: AsyncSession) -> None:
    """A buyer who paid for exactly the amount they want back is not
    clipped. If this ever fails the cap has become a spread."""
    await _seed(session, balance=10_000)
    await _deposit(session, _MIN)
    svc = _capped_service(session)

    result = await svc.create(user_id=_USER, amount_com=_MIN)

    assert result.outcome is CreateOutcome.OK
    assert await _balance(session) == 10_000 - _MIN


async def test_cap_counts_a_still_pending_request(session: AsyncSession) -> None:
    """Headroom is consumed at create, not at approval. Otherwise a user
    could stack N pending requests against the same allowance and an
    admin approving them all would pay N times over."""
    await _seed(session, balance=100_000)
    await _deposit(session, _MIN)
    svc = _capped_service(session)

    first = await svc.create(user_id=_USER, amount_com=_MIN)
    assert first.outcome is CreateOutcome.OK

    second = await svc.create(user_id=_USER, amount_com=_MIN)

    assert second.outcome is CreateOutcome.PAYOUT_CAP_EXCEEDED
    assert second.remaining == 0


async def test_rejected_request_returns_its_headroom(session: AsyncSession) -> None:
    """A rejected request refunded the coins, so it must also give the
    allowance back — otherwise an admin's reject silently burns the
    user's lifetime quota."""
    await _seed(session, balance=100_000)
    await _deposit(session, _MIN)
    svc = _capped_service(session)
    first = await svc.create(user_id=_USER, amount_com=_MIN)
    assert first.request_id is not None
    await svc.reject(request_id=first.request_id, admin_id=1)

    retry = await svc.create(user_id=_USER, amount_com=_MIN)

    assert retry.outcome is CreateOutcome.OK


async def test_ratio_above_one_hands_back_winnings(session: AsyncSession) -> None:
    """``payout_ratio`` is the owner's generosity dial: 1.5 lets a buyer
    take out 150 % of what they paid. Floored, never rounded up."""
    await _seed(session, balance=100_000)
    await _deposit(session, 5_001)
    svc = _capped_service(session, payout_ratio=1.5)

    # floor(5001 * 1.5) == 7501, so 7501 passes and 7502 does not.
    refused = await svc.create(user_id=_USER, amount_com=7_502)
    assert refused.outcome is CreateOutcome.PAYOUT_CAP_EXCEEDED
    assert refused.remaining == 7_501

    ok = await svc.create(user_id=_USER, amount_com=7_501)
    assert ok.outcome is CreateOutcome.OK


async def test_ratio_zero_disables_the_cap(session: AsyncSession) -> None:
    """``0`` is off, matching the ``MESSAGE_REWARD_DAILY_CAP`` convention.
    The R2 threshold is independent and still applies."""
    await _seed(session, balance=100_000)
    await _deposit(session, 1)
    svc = _capped_service(session, payout_ratio=0.0)

    result = await svc.create(user_id=_USER, amount_com=_MAX)

    assert result.outcome is CreateOutcome.OK


async def test_cap_still_binds_with_the_r2_threshold_off(
    session: AsyncSession,
) -> None:
    """The two gates are independent knobs. With R2 off a never-paying
    account reaches the cap check, where its allowance is 0 — so turning
    R2 off alone does NOT reopen the drain."""
    await _seed(session, balance=100_000)
    svc = _capped_service(session, require_deposit=False)

    result = await svc.create(user_id=_USER, amount_com=_MIN)

    assert result.outcome is CreateOutcome.PAYOUT_CAP_EXCEEDED
    assert result.remaining == 0
    assert await _pending_count(session) == 0


async def test_both_gates_off_restores_pre_t019_behaviour(
    session: AsyncSession,
) -> None:
    """The documented escape hatch: an operator honouring balances
    accrued before either gate shipped must turn BOTH off."""
    await _seed(session, balance=100_000)
    svc = _capped_service(session, require_deposit=False, payout_ratio=0.0)

    result = await svc.create(user_id=_USER, amount_com=_MIN)

    assert result.outcome is CreateOutcome.OK


async def test_lifetime_usage_counts_pending_and_completed_only(
    session: AsyncSession,
) -> None:
    """``WithdrawalsRepo.lifetime_usage`` read side, direct: pending and
    completed count, rejected does not, and another user's rows never
    leak in."""
    repo = WithdrawalsRepo(session)
    for uid, amount, status in (
        (_USER, 100, "pending"),
        (_USER, 200, "completed"),
        (_USER, 400, "rejected"),
        (_USER + 1, 800, "completed"),
    ):
        session.add(
            WithdrawalRequest(
                user_id=uid,
                amount_com=amount,
                amount_crypto=amount / _COINS_PER_USDT,
                currency="USDT",
                status=status,
                created_at="2026-01-01 00:00:00",
            )
        )
    await session.commit()

    assert await repo.lifetime_usage(_USER) == 300


async def test_lifetime_usage_includes_rows_with_no_timestamp(
    session: AsyncSession,
) -> None:
    """A legacy row with a NULL ``created_at`` is invisible to the rolling
    ``period_usage`` window by design, but it IS money that left — the
    lifetime total must count it or it becomes free headroom."""
    repo = WithdrawalsRepo(session)
    session.add(
        WithdrawalRequest(
            user_id=_USER,
            amount_com=5_000,
            amount_crypto=5_000 / _COINS_PER_USDT,
            currency="USDT",
            status="completed",
            created_at=None,
        )
    )
    await session.commit()

    assert await repo.period_usage(_USER, since_iso="1970-01-01 00:00:00") == 0
    assert await repo.lifetime_usage(_USER) == 5_000


async def test_headroom_is_none_when_the_cap_is_disarmed(
    session: AsyncSession,
) -> None:
    """``payout_ratio=0`` means "no lifetime ceiling". The intro card asks
    for the headroom before it decides whether to draw the line at all, so
    the disarmed answer has to be ``None`` and not a misleading ``0``."""
    await _deposit(session, 1_000)
    service = _capped_service(session, payout_ratio=0.0)

    assert await service.payout_headroom(_USER) is None


async def test_headroom_is_none_without_a_ledger(session: AsyncSession) -> None:
    """Same for a service built with no ``transactions`` repo: neither
    lifetime gate can run, so there is no ceiling to report."""
    assert await _service(session).payout_headroom(_USER) is None


async def test_headroom_is_what_create_would_allow(session: AsyncSession) -> None:
    """The number on the card must be the number the gate enforces —
    including the bite a still-pending request already took out of it."""
    await _seed(session, balance=30_000)
    await _deposit(session, 20_000)
    service = _capped_service(session)
    assert await service.payout_headroom(_USER) == 20_000

    result = await service.create(user_id=_USER, amount_com=5_000)
    assert result.outcome is CreateOutcome.OK

    assert await service.payout_headroom(_USER) == 15_000
    # One coin over the advertised headroom is refused; the headroom
    # itself still goes through, so the card never over-promises.
    over = await service.create(user_id=_USER, amount_com=15_001)
    assert over.outcome is CreateOutcome.PAYOUT_CAP_EXCEEDED
    assert over.remaining == 15_000


# ---------------------------------------------------------------------------
# lifetime counters (#238)
# ---------------------------------------------------------------------------


async def _totals(session: AsyncSession) -> tuple[int, int]:
    """``(total_earned, total_spent)`` — the two numbers on ``/balance``."""
    row = await session.execute(
        select(EconomyUser.total_earned, EconomyUser.total_spent).where(
            EconomyUser.user_id == _USER
        )
    )
    earned, spent = row.one()
    return int(earned), int(spent)


async def test_create_escrow_does_not_count_as_a_lifetime_spend(
    session: AsyncSession,
) -> None:
    """#238: an escrow is a hold, not a spend.

    Legacy parks the coins with a bare ``UPDATE users SET balance =
    balance - ?`` on every one of its escrow paths (bot.py:20425 crypto,
    bot.py:20488 card/RUB, bot.py:20625 instant buyout) and never writes
    ``total_spent`` anywhere in the whole withdrawal region.
    """
    await _seed(session, balance=10_000)
    svc = _service(session)
    result = await svc.create(user_id=_USER, amount_com=4500)

    assert result.outcome is CreateOutcome.OK
    assert await _balance(session) == 5_500
    assert await _totals(session) == (0, 0)


async def test_reject_refund_does_not_count_as_lifetime_income(
    session: AsyncSession,
) -> None:
    """The refund leg: legacy's is ``balance = balance + ?`` (bot.py:20755).

    Handing back coins the user already owned is not income.
    """
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None

    result = await svc.reject(request_id=created.request_id, admin_id=1)

    assert result.outcome is RejectOutcome.REJECTED
    assert await _balance(session) == 10_000
    assert await _totals(session) == (0, 0)


async def test_repeated_create_reject_cycles_leave_the_balance_card_intact(
    session: AsyncSession,
) -> None:
    """The headline defect (#238), reproduced at the size that made it hurt.

    Before the fix each cycle moved zero coins yet added ``amount_com``
    to *both* lifetime counters, so the ``/balance`` card
    (handlers/economy.py:72-73) could be inflated at will, one create
    and one reject at a time, up to the daily quota. Legacy's
    ``admin_confirm_withdrawal`` (bot.py:20717) does not issue a balance
    UPDATE either — no branch of a legacy withdrawal is ever booked as
    lifetime spend.
    """
    await _seed(session, balance=10_000)
    svc = _service(session)

    for _ in range(4):
        created = await svc.create(user_id=_USER, amount_com=4500)
        assert created.outcome is CreateOutcome.OK
        assert created.request_id is not None
        rejected = await svc.reject(request_id=created.request_id, admin_id=1)
        assert rejected.outcome is RejectOutcome.REJECTED

    assert await _balance(session) == 10_000
    assert await _totals(session) == (0, 0), (
        "four round trips moved no coins; the lifetime counters must agree"
    )

    # The movements stay fully auditable: eight ledger rows, one per leg.
    rows = (await session.execute(select(Transaction))).scalars().all()
    assert [r.type for r in rows].count("withdraw_escrow") == 4
    assert [r.type for r in rows].count("withdraw_refund") == 4


async def test_approved_withdrawal_is_not_booked_as_a_lifetime_spend_either(
    session: AsyncSession,
) -> None:
    """Parity, not an oversight: legacy books no spend on approval.

    ``admin_confirm_withdrawal`` (bot.py:20717) flips the row to
    ``completed`` and issues no balance UPDATE at all — the coins left at
    create. So a *completed* withdrawal leaves ``total_spent`` where it
    was too. If the port ever wants to count a settled payout as spend,
    that is a deliberate divergence needing ``bump_totals`` and its own
    ticket, not a silent side effect of the escrow primitive.
    """
    await _seed(session, balance=10_000)
    svc = _service(session)
    created = await svc.create(user_id=_USER, amount_com=4500)
    assert created.request_id is not None

    result = await svc.approve(
        request_id=created.request_id,
        admin_id=1,
        client=_FakeClient(),  # type: ignore[arg-type]
    )

    assert result.outcome is ApproveOutcome.COMPLETED
    assert await _balance(session) == 5_500
    assert await _totals(session) == (0, 0)
