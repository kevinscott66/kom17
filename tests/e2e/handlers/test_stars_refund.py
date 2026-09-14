"""End-to-end Telegram Stars refund (#1987).

Telegram delivers a ``message.refunded_payment`` update when Stars a
user spent on an invoice are returned to them — by the owner calling
``refundStarPayment`` or by Telegram support acting on a request. Until
#1987 nothing in ``src/`` listened for that update at all: the coins the
refund cancels stayed in the wallet, ``processed_webhooks.reversed_at``
stayed NULL, and so ``TransactionsRepo.lifetime_deposits`` — the only
automatic chargeback defence the withdrawal gate has — kept counting the
returned money as a real deposit.

Pins:

* the refund stamps the credit it cancels (matched on
  ``telegram_payment_charge_id``, which Stars reuses between the
  ``successful_payment`` and the ``refunded_payment``);
* the owner is told, with the user and the coin count named;
* a refund for a charge this bot never credited still alerts, and does
  not invent a user;
* and the point of the stamp: the returned money stops counting toward
  the withdrawal gate (#1988). That last one is the assertion that
  joins the two halves — every other test here pins ``reversed_at`` as
  a column, and a column nothing reads would be a fix in name only.

No wallet is debited here, deliberately — the same non-decision
``webhook/payments._alert_reversal`` documents for every other provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.types import Chat, Message, RefundedPayment, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.processed_webhooks_repo import (
    ProcessedWebhooksRepo,
)
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_OWNER = 555
_PAYER = 42
_CHARGE = "stars_charge_1987"


def _refund_update(*, charge_id: str, update_id: int = 1) -> Update:
    """A ``refunded_payment`` message, the shape Telegram sends."""
    user = TelegramUser(id=_PAYER, is_bot=False, first_name="T")
    return Update(
        update_id=update_id,
        message=Message(
            message_id=1,
            date=1_700_000_000,  # type: ignore[arg-type]
            chat=Chat(id=_PAYER, type="private"),
            from_user=user,
            refunded_payment=RefundedPayment(
                currency="XTR",
                total_amount=50,
                invoice_payload=f"stars_{_PAYER}_50_900",
                telegram_payment_charge_id=charge_id,
            ),
        ),
    )


async def _seed_credit(registry: EngineRegistry) -> None:
    """Record the credit exactly as ``credit_stars_payment`` does."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session, session.begin():
        await ProcessedWebhooksRepo(session).mark_processed(
            provider="stars",
            external_id=_CHARGE,
            user_id=_PAYER,
            credited_amount=900,
        )


async def _seed_ledger_purchase(registry: EngineRegistry) -> None:
    """The ledger half of the same credit.

    ``PaymentsService.credit`` writes both rows in one economy
    transaction — ``purchase_stars`` here and the
    ``processed_webhooks`` row above — and ``lifetime_deposits`` is
    the difference between them. Seeding only one would measure
    nothing.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session, session.begin():
        await TransactionsRepo(session).record(
            amount=900,
            type="purchase_stars",
            to_id=_PAYER,
            reason="stars top-up",
        )


async def _lifetime_deposits(registry: EngineRegistry) -> int:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        return await TransactionsRepo(session).lifetime_deposits(_PAYER)


async def _reversed_at(registry: EngineRegistry, charge_id: str) -> object:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        record = await ProcessedWebhooksRepo(session).get(provider="stars", external_id=charge_id)
        return None if record is None else record.reversed_at


@pytest.mark.asyncio
async def test_stars_refund_stamps_the_credit_and_alerts_the_owner(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), ADMIN_CHAT_ID=_OWNER),
    )
    await _seed_credit(registry)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _refund_update(charge_id=_CHARGE))

    assert await _reversed_at(registry, _CHARGE) is not None
    owner_cards = [m for m in sent if m.get("chat_id") == _OWNER]
    assert owner_cards, sent
    body = owner_cards[0]["text"]
    assert str(_PAYER) in body
    assert "900" in body


@pytest.mark.asyncio
async def test_a_refund_stops_counting_toward_the_withdrawal_gate(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#1988: the stamp has to move the number the gate actually reads.

    ``lifetime_deposits`` is what tells the withdrawal desk this is a
    customer and not an account the bot has only ever paid. Before
    #1987 a Stars refund left it at the full credited amount, so the
    loop was: buy coins with Stars, ask Telegram for the refund, keep
    the raised R2 threshold and R6 cap, repeat. The money came back
    out of the owner's pocket on the second lap.

    Note what is NOT asserted: the wallet. The coins stay where they
    are on purpose — debiting them automatically can drive a balance
    negative or collide with money already spent, so it stays the
    owner's decision. The gate figure is the half that must move by
    itself.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), ADMIN_CHAT_ID=_OWNER),
    )
    await _seed_credit(registry)
    await _seed_ledger_purchase(registry)
    capture_outgoing(bot)

    assert await _lifetime_deposits(registry) == 900

    await dispatcher.feed_update(bot, _refund_update(charge_id=_CHARGE))

    assert await _lifetime_deposits(registry) == 0


@pytest.mark.asyncio
async def test_stars_refund_for_an_unknown_charge_still_alerts(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), ADMIN_CHAT_ID=_OWNER),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _refund_update(charge_id="never_credited"))

    owner_cards = [m for m in sent if m.get("chat_id") == _OWNER]
    assert owner_cards, sent
    assert "не определён" in owner_cards[0]["text"]
