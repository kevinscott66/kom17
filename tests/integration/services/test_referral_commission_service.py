"""``ReferralCommissionService`` — purchase-side referrer payout (L-22/L-32).

Pins the legacy rule verbatim (``_apply_referral_commission``,
bot.py:9856-9878 + ``commission_amount``, bot.py:9782-9786):

* fires on PURCHASES, never on transfers — the transfer non-payout is
  pinned here too, against :class:`TransferService` directly, because
  the backlog wording ("route to bot/referrer") invited exactly that
  wrong implementation;
* amount is ``max(1, int(coins * percent / 100))`` — 1-coin floor;
* ledger row is ``type='referral'`` / ``to_id=inviter`` — the exact
  stream ``/commission`` (handlers/commission.py) and ``/referrals``
  (ReferralsRepo.fetch_earnings) SUM over, asserted end-to-end below.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.handlers.commission import _fetch_earnings
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.referrals_repo import ReferralsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.referral_commission_service import (
    CommissionOutcome,
    ReferralCommissionService,
    purchase_commission_amount,
    render_referral_commission_notice,
)
from telegram_invite_bot.services.transfer_service import (
    TransferConfig,
    TransferOutcome,
    TransferService,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

BUYER = 42
INVITER = 7
ADMIN = 1


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s


def _build_service(session: AsyncSession, *, percent: int = 10) -> ReferralCommissionService:
    return ReferralCommissionService(
        EconomyRepo(session),
        TransactionsRepo(session),
        ReferralsRepo(session),
        percent=percent,
    )


async def _seed_wallet(
    session: AsyncSession,
    user_id: int,
    balance: int = 1_000,
    *,
    referred_by: int | None = None,
) -> None:
    session.add(
        EconomyUser(
            user_id=user_id,
            balance=balance,
            language="ru",
            referred_by=referred_by,
        )
    )
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(
        select(EconomyUser.balance).where(EconomyUser.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    return int(row) if row is not None else -1


async def _referral_rows(session: AsyncSession) -> list[tuple[int | None, int | None, int]]:
    result = await session.execute(
        select(Transaction.from_id, Transaction.to_id, Transaction.amount).where(
            Transaction.type == "referral"
        )
    )
    return [(f, to, int(a)) for f, to, a in result.all()]


# ---------------------------------------------------------------------------
# Pure amount math (legacy commission_amount, bot.py:9782-9786)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "percent", "expected"),
    [
        (1_000, 10, 100),  # plain 10%
        (999, 10, 99),  # int() truncation, not rounding
        (5, 10, 1),  # 0.5 → FLOOR kicks in: minimum 1 coin
        (1, 1, 1),  # extreme floor case
        (1_000, 0, 0),  # percent off → disabled
        (0, 10, 0),  # nothing bought
        (-50, 10, 0),  # defensive
    ],
)
def test_purchase_commission_amount_matches_legacy(
    amount: int, percent: int, expected: int
) -> None:
    """``max(1, int(amount * percent / 100))`` — the 1-coin floor is
    the easiest detail for a refactor to lose, so every branch is
    parametrised."""
    assert purchase_commission_amount(amount, percent) == expected


# ---------------------------------------------------------------------------
# Referred buyer → inviter credited + ledger row
# ---------------------------------------------------------------------------


async def test_referred_buyer_credits_inviter(session: AsyncSession) -> None:
    await _seed_wallet(session, INVITER, balance=100)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=10)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    assert result.outcome is CommissionOutcome.CREDITED
    assert result.referrer_id == INVITER
    assert result.commission == 100
    assert await _balance(session, INVITER) == 200
    # Buyer's wallet untouched by the commission itself (their own
    # purchase debit/credit is the caller's business).
    assert await _balance(session, BUYER) == 1_000
    assert await _referral_rows(session) == [(None, INVITER, 100)]


async def test_minimum_floor_credits_one_coin(session: AsyncSession) -> None:
    """A 5-coin purchase at 10% pays 0.5 → legacy floors to 1 coin."""
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=10)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=5)
    await session.commit()

    assert result.outcome is CommissionOutcome.CREDITED
    assert result.commission == 1
    assert await _balance(session, INVITER) == 1


async def test_missing_inviter_wallet_self_heals(session: AsyncSession) -> None:
    """``referred_by`` points at a user with no economy row (wallet
    wiped / pre-backfill account) → get_or_create seeds it and the
    payout still lands instead of silently evaporating (same M-E-2
    posture as the transfer-tax treasury credit)."""
    await _seed_wallet(session, BUYER, referred_by=INVITER)  # INVITER not seeded
    service = _build_service(session, percent=10)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=500)
    await session.commit()

    assert result.outcome is CommissionOutcome.CREDITED
    assert result.commission == 50
    assert await _referral_rows(session) == [(None, INVITER, 50)]
    # Wallet row exists now and absorbed the credit.
    assert await _balance(session, INVITER) >= 50


# ---------------------------------------------------------------------------
# Skip conditions (legacy bot.py:9856-9866 parity)
# ---------------------------------------------------------------------------


async def test_non_referred_buyer_is_noop(session: AsyncSession) -> None:
    await _seed_wallet(session, INVITER, balance=100)
    await _seed_wallet(session, BUYER)  # referred_by NULL
    service = _build_service(session, percent=10)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    assert result.outcome is CommissionOutcome.NO_REFERRER
    assert await _balance(session, INVITER) == 100
    assert await _referral_rows(session) == []


async def test_self_referral_is_noop(session: AsyncSession) -> None:
    """``referred_by == user_id`` (legacy guard bot.py:9864) must not
    mint free coins on every own purchase."""
    await _seed_wallet(session, BUYER, balance=100, referred_by=BUYER)
    service = _build_service(session, percent=10)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=1_000)

    assert result.outcome is CommissionOutcome.NO_REFERRER
    assert await _balance(session, BUYER) == 100
    assert await _referral_rows(session) == []


async def test_zero_percent_disables_programme(session: AsyncSession) -> None:
    await _seed_wallet(session, INVITER, balance=100)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=0)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=1_000)

    assert result.outcome is CommissionOutcome.DISABLED
    assert await _balance(session, INVITER) == 100
    assert await _referral_rows(session) == []


async def test_non_positive_purchase_is_noop(session: AsyncSession) -> None:
    await _seed_wallet(session, INVITER)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=10)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=0)

    assert result.outcome is CommissionOutcome.INVALID_AMOUNT
    assert await _referral_rows(session) == []


async def test_credit_failure_writes_no_ledger_row(session: AsyncSession) -> None:
    """When the wallet credit is rejected (post-credit balance would
    blow the global cap), NO ``type='referral'`` row may be written —
    otherwise /commission would report earnings that never landed."""
    cap_buster = 10**15  # _MAX_AMOUNT — any credit overflows the inviter
    await _seed_wallet(session, INVITER, balance=cap_buster)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=10)

    result = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    assert result.outcome is CommissionOutcome.CREDIT_FAILED
    assert result.referrer_id == INVITER
    assert result.commission == 100
    assert await _balance(session, INVITER) == cap_buster
    assert await _referral_rows(session) == []


# ---------------------------------------------------------------------------
# Read-side pickup: /commission and /referrals SUM the row
# ---------------------------------------------------------------------------


class _StubRegistry:
    """Quacks like ``EngineRegistry`` for ``handlers.commission`` —
    one economy engine, no other databases involved in the read."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def engine(self, _name: object) -> AsyncEngine:
        return self._engine


async def test_commission_and_referrals_totals_pick_up_credits(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Two purchases by the same referred buyer → both reports show the
    summed earnings: ``ReferralsRepo.fetch_earnings`` (backs
    ``/referrals``) and ``handlers.commission._fetch_earnings`` (backs
    ``/commission``) run their real queries against the same DB the
    service wrote to."""
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, BUYER, balance=10_000, referred_by=INVITER)
    service = _build_service(session, percent=10)

    first = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=1_000)
    second = await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=250)
    await session.commit()

    assert first.outcome is CommissionOutcome.CREDITED
    assert second.outcome is CommissionOutcome.CREDITED
    expected_total = 100 + 25

    # /referrals read path.
    assert await ReferralsRepo(session).fetch_earnings(INVITER) == expected_total
    # /commission read path (own engine connection, post-commit).
    assert await _fetch_earnings(_StubRegistry(engine), INVITER) == expected_total  # type: ignore[arg-type]
    # And the inviter's wallet actually holds the coins the reports claim.
    assert await _balance(session, INVITER) == expected_total
    # Sanity: exactly two ledger rows, both attributable to the buyer.
    assert await _referral_rows(session) == [
        (None, INVITER, 100),
        (None, INVITER, 25),
    ]


async def test_unrelated_referral_rows_not_attributed(session: AsyncSession) -> None:
    """Earnings are per-inviter: a credit to INVITER must not bleed
    into another user's /commission total."""
    other = 99
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, other, balance=0)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=10)

    await service.apply_purchase_commission(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    repo = ReferralsRepo(session)
    assert await repo.fetch_earnings(INVITER) == 100
    assert await repo.fetch_earnings(other) == 0


# ---------------------------------------------------------------------------
# L-22 verdict pin: transfers pay NO referrer share
# ---------------------------------------------------------------------------


async def test_transfer_does_not_credit_referrer(session: AsyncSession) -> None:
    """Legacy transfers (bot.py:10244-10295) route the whole tax to the
    admin wallet — no inviter split. Pinned against TransferService so
    a future reading of the L-22 backlog wording can't reintroduce a
    payout legacy never had."""
    recipient = 99
    await _seed_wallet(session, ADMIN, balance=0)
    await _seed_wallet(session, INVITER, balance=100)
    await _seed_wallet(session, BUYER, balance=500, referred_by=INVITER)
    await _seed_wallet(session, recipient, balance=0)
    transfer = TransferService(
        EconomyRepo(session),
        TransactionsRepo(session),
        config=TransferConfig(base_tax_rate=0.05, admin_user_id=ADMIN),
    )

    result = await transfer.send(from_id=BUYER, to_id=recipient, amount=200)
    await session.commit()

    assert result.outcome is TransferOutcome.SUCCESS
    assert result.tax == 10
    # Full tax to admin; inviter untouched; zero referral rows.
    assert await _balance(session, ADMIN) == 10
    assert await _balance(session, INVITER) == 100
    assert await _referral_rows(session) == []
    types = (
        (await session.execute(select(Transaction.type).order_by(Transaction.id))).scalars().all()
    )
    assert types == ["transfer", "tax"]


# ---------------------------------------------------------------------------
# Notice rendering (referrer DM body)
# ---------------------------------------------------------------------------


def test_notice_renders_for_both_langs() -> None:
    """Both catalogues render the notice with the numbers filled in.

    The referrer's only view of the payout is this DM, so a missing key
    (``t()`` echoing it back) or an unsubstituted placeholder is a
    silent loss of the one number that matters.
    """
    for lang in ("ru", "en"):
        body = render_referral_commission_notice(lang, commission=100, percent=10)
        assert "h_referral_commission_credited" not in body
        assert "{" not in body
        assert "100" in body
        assert "10%" in body
