"""#75 — finish the commission ports: developer commission + wiring.

Pins the second half of legacy ``apply_purchase_commissions``
(bot.py:9900-9903) and its two remaining call-site wirings:

* ``apply_developer_commission`` — port of ``_apply_developer_commission``
  (bot.py:9881-9897): ``DEVELOPER_COMMISSION_PERCENT`` (default 5,
  bot.py:3173) of every commissionable purchase is minted to the
  ``ADMIN_CHAT_ID`` wallet, ledger row ``type='system'`` (the
  ``add_coins`` default for ``admin_id=None, transaction_type=None``,
  bot.py:9742).
* SHOP BUY wiring — legacy calls ``apply_purchase_commissions(user_id,
  item.price)`` after a successful ``buy_item`` (bot.py:13243). #75
  ported that; **T-020/R10 removed it**, and the shop section below now
  pins the removal: a coin-paid buy mints nothing, because it burns
  coins that already exist and a kickback would hand 15% of the bot's
  largest sink straight back as new supply.
* TOP-UP wiring — ``PaymentsService.handle_event`` already paid the
  referral half; now it runs BOTH halves and exposes the outcome via
  ``last_commissions`` so the webhook router can DM the inviter
  post-commit (legacy bot.py:9870-9876).

Verified discrepancy vs. the task premise: the developer commission is
NOT top-up-only — legacy fires it from the shop buy too, because
bot.py:13243 calls the combined ``apply_purchase_commissions``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.core.entities.shop import PurchaseStatus
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    ProcessedWebhook,
    ShopItem,
    Transaction,
)
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.processed_webhooks_repo import (
    ProcessedWebhooksRepo,
)
from telegram_invite_bot.repositories.referrals_repo import ReferralsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.payments.base import ParsedEvent, Provider
from telegram_invite_bot.services.payments_service import (
    CreditOutcome,
    PaymentsService,
)
from telegram_invite_bot.services.purchase_service import PurchaseService
from telegram_invite_bot.services.referral_commission_service import (
    CommissionOutcome,
    ReferralCommissionService,
)

BUYER = 42
INVITER = 7
DEVELOPER = 999


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as s:
            yield s
    finally:
        await engine.dispose()


def _build_service(
    session: AsyncSession,
    *,
    percent: int = 10,
    developer_percent: int = 5,
    developer_id: int = DEVELOPER,
) -> ReferralCommissionService:
    return ReferralCommissionService(
        EconomyRepo(session),
        TransactionsRepo(session),
        ReferralsRepo(session),
        percent=percent,
        developer_percent=developer_percent,
        developer_id=developer_id,
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


async def _rows_of_type(
    session: AsyncSession, txn_type: str
) -> list[tuple[int | None, int | None, int]]:
    result = await session.execute(
        select(Transaction.from_id, Transaction.to_id, Transaction.amount).where(
            Transaction.type == txn_type
        )
    )
    return [(f, to, int(a)) for f, to, a in result.all()]


# ---------------------------------------------------------------------------
# apply_developer_commission — legacy _apply_developer_commission parity
# ---------------------------------------------------------------------------


async def test_developer_commission_credits_dev_wallet(session: AsyncSession) -> None:
    """5% of a 1000-coin purchase → +50 to the dev wallet, one
    ``type='system'`` ledger row (legacy add_coins default, bot.py:9742)."""
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER)
    service = _build_service(session, developer_percent=5)

    result = await service.apply_developer_commission(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    assert result.outcome is CommissionOutcome.CREDITED
    assert result.referrer_id == DEVELOPER
    assert result.commission == 50
    assert await _balance(session, DEVELOPER) == 50
    assert await _rows_of_type(session, "system") == [(None, DEVELOPER, 50)]


async def test_developer_commission_floor_pays_one_coin(session: AsyncSession) -> None:
    """A 10-coin purchase at 5% pays 0.5 → legacy floors to 1
    (``commission_amount``, bot.py:9782-9786)."""
    await _seed_wallet(session, DEVELOPER, balance=0)
    service = _build_service(session)

    result = await service.apply_developer_commission(buyer_id=BUYER, coins_purchased=10)
    await session.commit()

    assert result.outcome is CommissionOutcome.CREDITED
    assert result.commission == 1
    assert await _balance(session, DEVELOPER) == 1


async def test_developer_commission_disabled_at_zero_percent(
    session: AsyncSession,
) -> None:
    """Percent 0 = admin switched the fee off (legacy bot.py:9886) —
    also the constructor DEFAULT, so pre-#75 call sites pay nothing."""
    await _seed_wallet(session, DEVELOPER, balance=0)
    service = _build_service(session, developer_percent=0)

    result = await service.apply_developer_commission(buyer_id=BUYER, coins_purchased=1_000)

    assert result.outcome is CommissionOutcome.DISABLED
    assert await _balance(session, DEVELOPER) == 0
    assert await _rows_of_type(session, "system") == []


async def test_developer_commission_skips_unset_wallet(session: AsyncSession) -> None:
    """``ADMIN_CHAT_ID`` unset (0) → no recipient (legacy
    ``if not dev_id``, bot.py:9888)."""
    service = _build_service(session, developer_id=0)

    result = await service.apply_developer_commission(buyer_id=BUYER, coins_purchased=1_000)

    assert result.outcome is CommissionOutcome.NO_RECIPIENT
    assert await _rows_of_type(session, "system") == []


async def test_developer_buying_pays_no_self_commission(session: AsyncSession) -> None:
    """``dev_id == buyer_id`` guard (legacy bot.py:9890) — the operator's
    own purchases must not mint them a rebate."""
    await _seed_wallet(session, DEVELOPER, balance=100)
    service = _build_service(session, developer_id=DEVELOPER)

    result = await service.apply_developer_commission(buyer_id=DEVELOPER, coins_purchased=1_000)

    assert result.outcome is CommissionOutcome.NO_RECIPIENT
    assert await _balance(session, DEVELOPER) == 100


async def test_developer_wallet_self_heals(session: AsyncSession) -> None:
    """Dev wallet row missing → seeded before the credit (legacy
    register_user inside add_coins, bot.py:9724)."""
    service = _build_service(session)

    result = await service.apply_developer_commission(buyer_id=BUYER, coins_purchased=200)
    await session.commit()

    assert result.outcome is CommissionOutcome.CREDITED
    assert result.commission == 10
    assert await _rows_of_type(session, "system") == [(None, DEVELOPER, 10)]


@pytest.mark.parametrize("developer_percent", [1, 5, 7])
async def test_developer_half_reports_the_rate_it_charged(
    session: AsyncSession, developer_percent: int
) -> None:
    """#1626 — the CREDITED result carries the rate behind the amount.

    The developer half used to leave ``percent`` at its default 0
    while logging ``self._developer_percent`` one line above, so a
    caller trusting the field would have rendered "0%" next to a
    non-zero payout. Parametrized because a hardcoded 5 would also
    pass against a field that echoes the constructor default.
    """
    await _seed_wallet(session, DEVELOPER, balance=0)
    service = _build_service(session, developer_percent=developer_percent)

    result = await service.apply_developer_commission(buyer_id=BUYER, coins_purchased=1_000)

    assert result.outcome is CommissionOutcome.CREDITED
    assert result.percent == developer_percent


# ---------------------------------------------------------------------------
# apply_purchase_commissions — both halves, referral first (bot.py:9900-9903)
# ---------------------------------------------------------------------------


async def test_combined_run_pays_both_halves(session: AsyncSession) -> None:
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=10, developer_percent=5)

    result = await service.apply_purchase_commissions(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    assert result.referral.outcome is CommissionOutcome.CREDITED
    assert result.referral.commission == 100
    assert result.developer.outcome is CommissionOutcome.CREDITED
    assert result.developer.commission == 50
    # #1626: each half reports ITS OWN rate. The two differ here on
    # purpose — a half that echoed the other one would still pass a
    # single-rate fixture.
    assert result.referral.percent == 10
    assert result.developer.percent == 5
    assert await _balance(session, INVITER) == 100
    assert await _balance(session, DEVELOPER) == 50
    assert await _rows_of_type(session, "referral") == [(None, INVITER, 100)]
    assert await _rows_of_type(session, "system") == [(None, DEVELOPER, 50)]


async def test_combined_run_dev_half_independent_of_referrer(
    session: AsyncSession,
) -> None:
    """Organic buyer (no inviter) still pays the developer fee — the
    two halves never gate each other (legacy runs both unconditionally,
    bot.py:9900-9903)."""
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER)  # referred_by NULL
    service = _build_service(session)

    result = await service.apply_purchase_commissions(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    assert result.referral.outcome is CommissionOutcome.NO_REFERRER
    assert result.developer.outcome is CommissionOutcome.CREDITED
    assert await _balance(session, DEVELOPER) == 50


# ---------------------------------------------------------------------------
# Shop /buy — T-020/R10: a coin-paid buy mints NOTHING
# ---------------------------------------------------------------------------
#
# #75 wired ``apply_purchase_commissions`` into PurchaseService because
# legacy did (bot.py:13243). R10 removed it: a shop buy is paid in coins
# that already exist and burns them, so paying 10% to the inviter and 5%
# to the developer handed 15% of the bot's largest SINK back as fresh
# supply. The tests below pin the removal, including the seam itself —
# PurchaseService takes no commission argument any more, so the wiring
# cannot be restored by a one-line kwarg.


async def _seed_item(session: AsyncSession, *, id_: int, price: int, stock: int = -1) -> None:
    session.add(
        ShopItem(id=id_, name="Plushie", description="", price=price, type="unwarn", stock=stock)
    )
    await session.commit()


async def test_shop_buy_mints_nothing(session: AsyncSession) -> None:
    """The headline R10 assertion: a successful coin-paid buy debits the
    buyer and credits nobody. Both the wallets and the ledger are
    checked — a mint that skipped its ledger row would still be new
    supply, and a ledger row without a mint would still be reported as
    earnings by /commission."""
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER, balance=500, referred_by=INVITER)
    await _seed_item(session, id_=1, price=200)
    service = PurchaseService(session)

    outcome = await service.purchase(user_id=BUYER, item_id=1)
    await session.commit()

    assert outcome.status is PurchaseStatus.OK
    assert outcome.new_balance == 300
    assert await _balance(session, INVITER) == 0
    assert await _balance(session, DEVELOPER) == 0
    assert await _rows_of_type(session, "referral") == []
    assert await _rows_of_type(session, "system") == []


async def test_shop_buy_is_a_pure_sink(session: AsyncSession) -> None:
    """Coins in the ecosystem strictly DECREASE across a shop buy.

    Stated as a conservation check rather than as three separate
    balance assertions, because that is the property R10 is really
    buying: whatever a future contributor adds to the purchase path,
    the total must not go up."""
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER, balance=500, referred_by=INVITER)
    await _seed_item(session, id_=1, price=200)
    before = sum(
        [
            await _balance(session, INVITER) or 0,
            await _balance(session, DEVELOPER) or 0,
            await _balance(session, BUYER) or 0,
        ]
    )

    await PurchaseService(session).purchase(user_id=BUYER, item_id=1)
    await session.commit()

    after = sum(
        [
            await _balance(session, INVITER) or 0,
            await _balance(session, DEVELOPER) or 0,
            await _balance(session, BUYER) or 0,
        ]
    )
    assert after == before - 200


async def test_one_coin_item_no_longer_prints_money(session: AsyncSession) -> None:
    """The sharpest edge R10 closes. ``purchase_commission_amount``
    carries a ``max(1, …)`` FLOOR, so a 1-coin item used to burn 1 coin
    and mint 2 — one to the inviter, one to the developer. With
    infinite stock (``stock=-1``, the shop default) that was a money
    printer bounded only by how fast the buy could be repeated."""
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER, balance=10, referred_by=INVITER)
    await _seed_item(session, id_=1, price=1)
    service = PurchaseService(session)

    for _ in range(5):
        assert (await service.purchase(user_id=BUYER, item_id=1)).status is PurchaseStatus.OK
    await session.commit()

    assert await _balance(session, BUYER) == 5
    assert await _balance(session, INVITER) == 0
    assert await _balance(session, DEVELOPER) == 0


async def test_purchase_service_has_no_commission_seam(session: AsyncSession) -> None:
    """R10 removed the argument, not just the call. Re-adding a
    ``commissions=`` kwarg is then a deliberate signature change with
    this test in the diff, rather than a one-word revert."""
    with pytest.raises(TypeError):
        PurchaseService(session, commissions=object())  # type: ignore[call-arg]
    assert not hasattr(PurchaseService(session), "last_commissions")


# ---------------------------------------------------------------------------
# Top-up wiring — PaymentsService runs both halves + exposes the result
# ---------------------------------------------------------------------------


def _event(external_id: str = "EXT-1", coins: int = 1_000) -> ParsedEvent:
    return ParsedEvent(
        provider=Provider.CRYPTO,
        external_id=external_id,
        user_id=BUYER,
        coins=coins,
        reason="test",
    )


def _payments_service(session: AsyncSession) -> PaymentsService:
    economy_repo = EconomyRepo(session)
    transactions_repo = TransactionsRepo(session)
    return PaymentsService(
        economy=EconomyService(economy_repo, transactions_repo),
        idempotency=ProcessedWebhooksRepo(session),
        bot=None,
        referral_commission=ReferralCommissionService(
            economy_repo,
            transactions_repo,
            ReferralsRepo(session),
            percent=10,
            developer_percent=5,
            developer_id=DEVELOPER,
        ),
        session=session,
    )


async def test_topup_pays_both_commissions(session: AsyncSession) -> None:
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER, balance=0, referred_by=INVITER)
    service = _payments_service(session)

    outcome = await service.handle_event(_event(coins=1_000))
    await session.commit()

    assert outcome is CreditOutcome.CREDITED
    assert await _balance(session, BUYER) == 1_000
    assert await _balance(session, INVITER) == 100
    assert await _balance(session, DEVELOPER) == 50
    # The router reads this post-commit to DM the inviter.
    assert service.last_commissions is not None
    assert service.last_commissions.referral.outcome is CommissionOutcome.CREDITED
    assert service.last_commissions.referral.referrer_id == INVITER
    assert service.last_commissions.developer.outcome is CommissionOutcome.CREDITED


async def test_duplicate_topup_pays_no_second_commission(
    session: AsyncSession,
) -> None:
    """Idempotency short-circuits BEFORE the commission block — a
    provider retry must not double-pay the inviter or the developer."""
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER, balance=0, referred_by=INVITER)
    service = _payments_service(session)

    assert await service.handle_event(_event()) is CreditOutcome.CREDITED
    await session.commit()
    assert await service.handle_event(_event()) is CreditOutcome.IDEMPOTENT
    await session.commit()

    assert await _balance(session, INVITER) == 100
    assert await _balance(session, DEVELOPER) == 50
    assert service.last_commissions is None  # reset at the top of handle_event
    rows = (await session.execute(select(ProcessedWebhook))).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# #241 — the commissions are MINTED, so they are nobody's expense
# ---------------------------------------------------------------------------


async def test_commissions_do_not_count_as_the_buyer_spending(
    session: AsyncSession,
) -> None:
    """The guard for #241, written against the reader rather than the row.

    Both commission rows used to carry ``from_id=buyer_id``, justified in
    a comment claiming the readers filter on ``to_id`` + ``type`` only.
    They do not. ``window_stats`` sums ``ABS(amount) WHERE from_id ==
    user`` across EVERY type (transactions_repo.py:146), and ``recent``
    renders any row whose ``from_id`` is the viewer as a minus (:224). So
    a 1000-coin top-up showed the buyer 150 coins of spending he never
    did, on top of the purchase itself.

    Asserting ``from_id is None`` on the rows would pass again the moment
    someone "restores traceability" by reviving the old value, so this
    asserts what the user actually sees instead.
    """
    await _seed_wallet(session, INVITER, balance=0)
    await _seed_wallet(session, DEVELOPER, balance=0)
    await _seed_wallet(session, BUYER, referred_by=INVITER)
    service = _build_service(session, percent=10, developer_percent=5)

    await service.apply_purchase_commissions(buyer_id=BUYER, coins_purchased=1_000)
    await session.commit()

    ledger = TransactionsRepo(session)
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)

    assert (await ledger.window_stats(BUYER, since=since)).sent == 0, (
        "minted commissions are not the buyer's expense"
    )
    assert [tx.signed_amount for tx in await ledger.recent(BUYER)] == []

    # ...and the coins really were minted: the recipients see them as
    # income, which is the half of the picture that was always right.
    assert [tx.signed_amount for tx in await ledger.recent(INVITER)] == [100]
    assert (await ledger.window_stats(INVITER, since=since)).received == 100
