"""End-to-end ``/commission`` (Stage 30).

Pins:

* SUM over ``transactions`` filtered by ``to_id=caller_id AND
  type='referral'`` — other transaction kinds (transfer, shop, ...)
  must not leak into the total.
* Empty ledger renders zero (not blank, not ``None``).
* Commission percent comes from settings (``EconomyConfig``), not a
  hardcoded value.
* RU and EN variants render the right labels.
* All three command aliases route.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig, EconomyConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import Transaction
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_CALLER = 6000


async def _seed_caller(registry: EngineRegistry, *, lang: str = "ru") -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=_CALLER, first_name="Me", language_code=lang))
        await session.commit()


async def _seed_transactions(
    registry: EngineRegistry,
    rows: list[tuple[int, int, str]],  # (to_id, amount, type)
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for to_id, amount, tx_type in rows:
            session.add(
                Transaction(to_id=to_id, amount=amount, type=tx_type, date=datetime(2026, 1, 1))
            )
        await session.commit()


@pytest.mark.asyncio
async def test_commission_sums_only_referral_type(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc")),
        economy_config=EconomyConfig(REFERRAL_COMMISSION_PERCENT=12),
    )
    await _seed_caller(registry)
    await _seed_transactions(
        registry,
        rows=[
            (_CALLER, 30, "referral"),
            (_CALLER, 70, "referral"),
            (_CALLER, 999, "transfer"),  # non-referral, must be ignored
            (_CALLER, 500, "shop"),  # non-referral, must be ignored
        ],
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, make_message_update("/commission", user_id=_CALLER))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    # 30 + 70 = 100; the other 1499 coins must NOT appear.
    assert "<b>100</b>" in body
    # Percent from EconomyConfig override.
    assert "<b>12%</b>" in body
    # RU intro line.
    assert "Реферальные начисления приходят на основной баланс" in body


@pytest.mark.asyncio
async def test_commission_empty_renders_zero(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/commission", user_id=_CALLER))
    body = sent[-1]["text"]
    # Coerce: a user with no transactions must render "0", not "None".
    assert "<b>0</b>" in body
    assert "None" not in body


@pytest.mark.asyncio
async def test_commission_renders_english(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry, lang="en")
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/commission", user_id=_CALLER, language_code="en")
    )
    body = sent[-1]["text"]
    assert "My commissions" in body
    assert "Earned from referrals" in body
    assert "Current rate" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["/commission", "/комиссии", "/мои_комиссии"])
async def test_commission_aliases_route(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    cmd: str,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, make_message_update(cmd, user_id=_CALLER))
    assert result is not UNHANDLED
    assert sent, f"alias {cmd} did not produce a reply"
