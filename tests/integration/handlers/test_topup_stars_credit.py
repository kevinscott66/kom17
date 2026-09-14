"""Integration test: Stars payment credits through the shared pipeline (L-80).

Drives :func:`telegram_invite_bot.handlers.topup.credit_stars_payment`
against real in-memory SQLite engines — the same posture as
``tests/integration/test_payment_webhooks.py`` for the webhook
providers. Asserts the three invariants the Stars path inherits from
PaymentsService:

* the wallet credit + ledger row + ``processed_webhooks`` row land
  (provider ``"stars"``, external_id = ``telegram_payment_charge_id``);
* a redelivered update is IDEMPOTENT (no double credit);
* an unknown wallet is seeded and credited rather than refused (#770).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    PaymentsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db import EngineRegistry
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    ProcessedWebhook,
    Transaction,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.topup import credit_stars_payment
from telegram_invite_bot.services.payments_service import CreditOutcome

_USER = 4242


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(
            WEBHOOK_PATH="/webhook",
            WEBHOOK_SECRET_TOKEN=SecretStr("dummy"),
        ),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
        payments=PaymentsConfig(CRYPTO_PAY_TOKEN=None),
    )


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    reg = build_registry(_settings(tmp_path))
    engine = reg.engine(DBName.ECONOMY)
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    async with reg.session(DBName.ECONOMY)() as s:
        s.add(EconomyUser(user_id=_USER, balance=100, language="ru"))
        await s.commit()
    try:
        yield reg
    finally:
        await reg.dispose()


async def _balance(registry: EngineRegistry, user_id: int) -> int | None:
    async with registry.session(DBName.ECONOMY)() as s:
        row = await s.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
        value = row.scalar_one_or_none()
        return int(value) if value is not None else None


async def _processed_stars(registry: EngineRegistry) -> int:
    async with registry.session(DBName.ECONOMY)() as s:
        row = await s.execute(
            select(func.count())
            .select_from(ProcessedWebhook)
            .where(ProcessedWebhook.provider == "stars")
        )
        return int(row.scalar_one())


@pytest.mark.asyncio
async def test_stars_credit_then_idempotent_redelivery(
    registry: EngineRegistry, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)

    outcome, _ = await credit_stars_payment(
        registry=registry,
        settings=settings,
        user_id=_USER,
        coins=550,
        charge_id="chg_abc",
    )
    assert outcome is CreditOutcome.CREDITED
    assert await _balance(registry, _USER) == 650
    assert await _processed_stars(registry) == 1

    # Ledger reason matches legacy byte-for-byte (bot.py:18260) so
    # reason-filtered audits span the cutover.
    async with registry.session(DBName.ECONOMY)() as s:
        row = await s.execute(select(Transaction.reason).where(Transaction.to_id == _USER))
        assert row.scalar_one() == "Покупка за Telegram Stars"

    # Telegram redelivers the update → IDEMPOTENT, no double credit.
    outcome, kickback = await credit_stars_payment(
        registry=registry,
        settings=settings,
        user_id=_USER,
        coins=550,
        charge_id="chg_abc",
    )
    assert outcome is CreditOutcome.IDEMPOTENT
    assert kickback is None
    assert await _balance(registry, _USER) == 650
    assert await _processed_stars(registry) == 1


@pytest.mark.asyncio
async def test_stars_credit_seeds_an_unknown_wallet(
    registry: EngineRegistry, tmp_path: Path
) -> None:
    """#770: Telegram already took the stars, so a missing wallet row
    cannot be the reason the coins never arrive.

    This inherits from ``PaymentsService`` like the other three
    invariants above — the Stars rail builds one and calls
    ``handle_event``, so it gets the ``ensure_wallet`` seed for free.
    The case used to assert the opposite (terminal no-op, nothing
    written), which on the Stars rail is the worst of the three: unlike
    a crypto or card provider there is no redelivery to retry into, and
    a refund needs the operator.
    """
    outcome, kickback = await credit_stars_payment(
        registry=registry,
        settings=_settings(tmp_path),
        user_id=999999,
        coins=550,
        charge_id="chg_zzz",
    )
    assert outcome is CreditOutcome.CREDITED
    assert kickback is None
    # 100 welcome credit on the fresh row (``EconomyUser`` docstring) plus
    # the purchase — the same total the seeded ``_USER`` reaches above.
    assert await _balance(registry, 999999) == 100 + 550
    assert await _processed_stars(registry) == 1


async def test_stars_credit_records_the_star_charge(
    registry: EngineRegistry, tmp_path: Path
) -> None:
    """#239: Stars are the charge, so Stars are what gets recorded.

    The figure comes from ``SuccessfulPayment.total_amount``, which is
    server-authoritative — the invoice payload also carries a star
    count, but that one is ours and a mismatch between the two is
    exactly the thing an audit is supposed to be able to see.

    ``fx_rate`` stays ``None``: nothing was converted. Telegram bills
    in XTR and we credit coins from a fixed table, so there is no rate
    to record and inventing one would be worse than recording nothing.
    """
    outcome, _ = await credit_stars_payment(
        registry=registry,
        settings=_settings(tmp_path),
        user_id=_USER,
        coins=550,
        charge_id="chg_fiat",
        stars_charged=250,
        stars_currency="XTR",
    )
    assert outcome is CreditOutcome.CREDITED

    async with registry.session(DBName.ECONOMY)() as s:
        row = await s.execute(
            select(ProcessedWebhook).where(ProcessedWebhook.external_id == "chg_fiat")
        )
        pw = row.scalars().one()
    assert pw.fiat_amount == "250"
    assert pw.fiat_currency == "XTR"
    assert pw.fx_rate is None
