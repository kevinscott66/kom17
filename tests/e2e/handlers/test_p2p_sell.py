"""E2e sell-side P2P UI (cluster P2 of epic #64).

``tests/integration/services/test_p2p_service.py`` already pins the
money mechanics (escrow ledger, atomic guards, checked credits). This
file pins the WIRING of ``handlers/p2p.py``: the ``/p2p`` entry, the
4-step sell FSM ending in a real escrowed order, the legacy input
validations, my-orders + cancel (refund), and the read-only trades
list.

Self-wired dispatcher (not ``make_wired``): the shared factory builds
the whole ``build_main_router``, and a sell-side failure should point
at ``handlers/p2p.py`` rather than at anything mounted beside it. So
the suite wires the p2p router directly and injects the service through
a tiny test middleware that builds :class:`P2pService` ON THE SAME
economy session ``EconomyMiddleware`` opened (read off the bound
repos), so commit/rollback semantics match production — where
``EconomyMiddleware._bind`` does exactly this — down to the session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from pydantic import SecretStr
from sqlalchemy import func, select

from telegram_invite_bot.config.settings import (
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.p2p import P2pSellOrder, P2pTrade
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import scoped_worker
from telegram_invite_bot.handlers.p2p import build_router as build_p2p_router
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.keyboards.builders.p2p import (
    P2pCancelOrder,
    P2pMenu,
    P2pMyOrders,
    P2pMyTrades,
    P2pSellCurrency,
    P2pSellPriceMarket,
    P2pSellSkipLimits,
    P2pSellStart,
)
from telegram_invite_bot.middlewares.language import LanguageMiddleware, clear_language_cache
from telegram_invite_bot.repositories.p2p_repo import P2pRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.p2p_service import P2pService
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from aiogram.types import TelegramObject
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry

UID = 700


class _InjectP2pService(BaseMiddleware):
    """Test stand-in for ``EconomyMiddleware._bind``: builds
    ``p2p_service`` on the SAME session the economy middleware opened
    (recovered from the bound repo), so the handler sees
    production-identical commit semantics.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Any],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        economy_repo = data["economy_repo"]
        session: AsyncSession = economy_repo._session  # noqa: SLF001 — test-only session recovery
        data["p2p_service"] = P2pService(
            P2pRepo(session), economy_repo, TransactionsRepo(session), session
        )
        data["p2p_repo"] = P2pRepo(session)
        return await handler(event, data)


@pytest.fixture
async def wired(tmp_path: Path) -> AsyncIterator[tuple[Bot, Dispatcher, EngineRegistry]]:
    """(Bot, Dispatcher, registry) with ONLY the p2p router included."""
    clear_language_cache()
    settings = Settings(
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )
    registry = build_registry(settings)
    for base, db in ((EconomyBase, DBName.ECONOMY), (UsersBase, DBName.USERS)):
        engine = registry.engine(db)
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)

    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    # Root-level lang injection, mirroring prod (main_router attaches
    # LanguageMiddleware as the outer middleware on both event types).
    dispatcher.message.outer_middleware(LanguageMiddleware(registry))
    dispatcher.callback_query.outer_middleware(LanguageMiddleware(registry))

    router = build_p2p_router(registry, settings)
    # ``scoped_worker`` because the module is wrapped by
    # ``with_chat_type_refusal`` (#123): the returned router is an
    # *ancestor* of p2p's own, and aiogram resolves inner middlewares
    # from the chain head down — attaching here would put this stand-in
    # in FRONT of the module's ``EconomyMiddleware`` and leave
    # ``data["economy_repo"]`` unset. Production never appends to a
    # built router, which is why only the tests need the accessor.
    worker = scoped_worker(router)
    worker.message.middleware(_InjectP2pService())
    worker.callback_query.middleware(_InjectP2pService())
    dispatcher.include_router(router)
    try:
        yield bot, dispatcher, registry
    finally:
        await bot.session.close()
        await registry.dispose()


async def _seed_wallet(
    registry: EngineRegistry, *, user_id: int = UID, balance: int = 1000
) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _balance(registry: EngineRegistry, user_id: int = UID) -> int:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(EconomyUser, user_id)
        assert row is not None
        return int(row.balance)


async def _orders(registry: EngineRegistry) -> list[P2pSellOrder]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        return list((await session.execute(select(P2pSellOrder))).scalars())


async def _ledger_sum(registry: EngineRegistry, type_: str) -> int:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                        Transaction.type == type_
                    )
                )
            ).scalar_one()
        )


async def _drive_to_limits_step(
    bot: Bot, dispatcher: Dispatcher, *, amount: str = "500", currency: str = "RUB"
) -> None:
    """Run the interview up to (and including) the price step."""
    assert (
        await dispatcher.feed_update(
            bot, make_callback_update(P2pSellStart().pack(), user_id=UID, update_id=11)
        )
        is not UNHANDLED
    )
    assert (
        await dispatcher.feed_update(bot, make_message_update(amount, user_id=UID, update_id=12))
        is not UNHANDLED
    )
    assert (
        await dispatcher.feed_update(
            bot,
            make_callback_update(
                P2pSellCurrency(currency=currency).pack(), user_id=UID, update_id=13
            ),
        )
        is not UNHANDLED
    )
    assert (
        await dispatcher.feed_update(
            bot, make_callback_update(P2pSellPriceMarket().pack(), user_id=UID, update_id=14)
        )
        is not UNHANDLED
    )


async def test_p2p_command_renders_menu_private(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = wired
    await _seed_wallet(registry)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, make_message_update("/p2p", user_id=UID))
    assert result is not UNHANDLED
    # The menu goes to the caller's DM, rendered — a bare key here would
    # mean the catalogue lost ``h_p2p_menu`` and nothing else noticed.
    assert sent and sent[0]["chat_id"] == UID
    assert "h_p2p_menu" not in sent[0]["text"]


async def test_p2p_menu_offers_a_way_out(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#696: the menu has an exit row.

    Legacy's exit («🔙 Назад», bot.py:19252) pointed back at the
    /withdraw menu it was opened from. The port has no /withdraw → P2P
    button, so the row was dropped — and with it the only way out,
    leaving ``/p2p`` as the one menu in the bot a user could not leave
    with a button. The replacement is the bot-wide idiom:
    ``back_to_menu`` → ``MainMenu(action="home")``.
    """
    bot, dispatcher, registry = wired
    await _seed_wallet(registry)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/p2p", user_id=UID))

    markup = sent[0]["markup"]
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert MainMenu(action="home").pack() in payloads


async def test_p2p_group_is_refused(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A group ``/p2p`` is answered, and opens no interview (#123).

    Trading is DM-only — the order book, the escrow prompts and the
    payment details have no business in a group — so the refusal points
    at the DM instead of leaving the group silent.
    """
    bot, dispatcher, _ = wired
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("/p2p", user_id=UID, chat_id=-100123, chat_type="supergroup")
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="p2p")


async def test_sell_flow_skip_limits_creates_escrowed_order(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Full 4-step interview (skip path) → active order + escrow + ledger."""
    bot, dispatcher, registry = wired
    await _seed_wallet(registry, balance=1000)
    capture_callback_outgoing(bot)

    await _drive_to_limits_step(bot, dispatcher)
    assert (
        await dispatcher.feed_update(
            bot, make_callback_update(P2pSellSkipLimits().pack(), user_id=UID, update_id=15)
        )
        is not UNHANDLED
    )

    orders = await _orders(registry)
    assert len(orders) == 1
    order = orders[0]
    assert order.user_id == UID
    assert order.amount_com == 500
    assert order.remaining_com == 500
    assert order.fiat_currency == "RUB"
    assert order.price_per_com == pytest.approx(1.0)  # market rate, bot.py:19185
    assert order.status == "active"
    assert order.payment_methods is None
    # Escrow-on-create: wallet debited + p2p_escrow ledger row (§2.2).
    assert await _balance(registry) == 500
    assert await _ledger_sum(registry, "p2p_escrow") == 500


async def test_sell_flow_limits_text_parses_methods_and_bounds(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``методы; мин; макс`` line lands on the order (bot.py:19448-19464)."""
    bot, dispatcher, registry = wired
    await _seed_wallet(registry, balance=1000)
    capture_callback_outgoing(bot)

    await _drive_to_limits_step(bot, dispatcher, currency="USD")
    assert (
        await dispatcher.feed_update(
            bot, make_message_update("Сбер, Тинькофф; 100; 500", user_id=UID, update_id=15)
        )
        is not UNHANDLED
    )

    orders = await _orders(registry)
    assert len(orders) == 1
    order = orders[0]
    assert order.fiat_currency == "USD"
    assert order.price_per_com == pytest.approx(0.011)
    assert order.payment_methods == "Сбер, Тинькофф"
    assert order.min_amount == 100
    assert order.max_amount == 500


async def test_sell_amount_validations_keep_step(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Legacy validations (bot.py:19311-19338): NaN / ≤0 / over-balance
    all stay on the amount step with a hint; no order, no debit.
    """
    bot, dispatcher, registry = wired
    await _seed_wallet(registry, balance=100)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_callback_update(P2pSellStart().pack(), user_id=UID, update_id=21)
    )
    for update_id, bad in ((22, "abc"), (23, "0"), (24, "-5"), (25, "999999")):
        result = await dispatcher.feed_update(
            bot, make_message_update(bad, user_id=UID, update_id=update_id)
        )
        assert result is not UNHANDLED

    assert await _orders(registry) == []
    assert await _balance(registry) == 100
    # Each bad input produced a hint reply (4 texts after the FSM card edit).
    assert len([s for s in sent if s["kind"] == "text"]) == 4


async def test_stale_currency_button_answers_session_expired(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A currency press with no interview in flight → legacy-style
    «Сессия истекла» toast, never a mutation (bot.py:19352)."""
    bot, dispatcher, registry = wired
    await _seed_wallet(registry)
    sent = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_callback_update(P2pSellCurrency(currency="RUB").pack(), user_id=UID, update_id=31),
    )
    assert result is not UNHANDLED
    answers = [s for s in sent if s["kind"] == "callback_answer"]
    assert answers and answers[0]["text"] is not None
    assert await _orders(registry) == []


async def test_my_orders_cancel_refunds_escrow(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Cancel returns ``remaining_com`` + writes ``p2p_refund``
    (bot.py:19558-19588 + invariant §2.2-1)."""
    bot, dispatcher, registry = wired
    await _seed_wallet(registry, balance=1000)
    capture_callback_outgoing(bot)

    await _drive_to_limits_step(bot, dispatcher)
    await dispatcher.feed_update(
        bot, make_callback_update(P2pSellSkipLimits().pack(), user_id=UID, update_id=15)
    )
    order = (await _orders(registry))[0]
    assert await _balance(registry) == 500

    # My-orders list renders, then cancel.
    assert (
        await dispatcher.feed_update(
            bot, make_callback_update(P2pMyOrders().pack(), user_id=UID, update_id=16)
        )
        is not UNHANDLED
    )
    assert (
        await dispatcher.feed_update(
            bot,
            make_callback_update(
                P2pCancelOrder(order_id=order.id).pack(), user_id=UID, update_id=17
            ),
        )
        is not UNHANDLED
    )

    refreshed = (await _orders(registry))[0]
    assert refreshed.status == "cancelled"
    assert refreshed.remaining_com == 0
    assert await _balance(registry) == 1000
    assert await _ledger_sum(registry, "p2p_refund") == 500


async def test_cancel_foreign_order_rejected(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A forged cancel payload for someone else's order is inert."""
    bot, dispatcher, registry = wired
    await _seed_wallet(registry, balance=1000)
    await _seed_wallet(registry, user_id=UID + 1, balance=0)
    capture_callback_outgoing(bot)

    await _drive_to_limits_step(bot, dispatcher)
    await dispatcher.feed_update(
        bot, make_callback_update(P2pSellSkipLimits().pack(), user_id=UID, update_id=15)
    )
    order = (await _orders(registry))[0]

    result = await dispatcher.feed_update(
        bot,
        make_callback_update(
            P2pCancelOrder(order_id=order.id).pack(), user_id=UID + 1, update_id=18
        ),
    )
    assert result is not UNHANDLED
    refreshed = (await _orders(registry))[0]
    assert refreshed.status == "active"
    assert refreshed.remaining_com == 500
    assert await _balance(registry, UID + 1) == 0


async def test_my_trades_lists_both_seats(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Read-only trades list shows seller- and buyer-seat rows
    (bot.py:20068-20092)."""
    bot, dispatcher, registry = wired
    await _seed_wallet(registry)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            P2pSellOrder(
                user_id=UID,
                amount_com=100,
                remaining_com=0,
                price_per_com=1.0,
                fiat_currency="RUB",
                status="completed",
            )
        )
        await session.flush()
        session.add(
            P2pTrade(
                order_id=1,
                seller_id=UID,
                buyer_id=999,
                amount_com=100,
                price_per_com=1.0,
                total_fiat=100.0,
                fiat_currency="RUB",
                status="confirmed",
            )
        )
        await session.commit()

    sent = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_callback_update(P2pMyTrades().pack(), user_id=UID, update_id=41)
    )
    assert result is not UNHANDLED
    edits = [s for s in sent if s["kind"] in ("edit", "text")]
    assert edits
    text = edits[0]["text"]
    # The seeded trade is listed, rendered: neither the empty-list copy
    # nor a bare key may reach the user.
    assert "h_p2p_my_trades_empty" not in text
    assert "h_p2p_trade_line" not in text
    assert "#1" in text


async def test_menu_callback_clears_inflight_interview(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Back-to-menu mid-interview drops the FSM: a later amount text is
    no longer swallowed by the sell flow (no busy-lock leak)."""
    bot, dispatcher, registry = wired
    await _seed_wallet(registry)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_callback_update(P2pSellStart().pack(), user_id=UID, update_id=51)
    )
    await dispatcher.feed_update(
        bot, make_callback_update(P2pMenu().pack(), user_id=UID, update_id=52)
    )
    result = await dispatcher.feed_update(
        bot, make_message_update("500", user_id=UID, update_id=53)
    )
    assert result is UNHANDLED
    assert await _orders(registry) == []


async def test_sell_bad_limits_keep_the_step(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``макс < мин`` (and non-positive bounds) must not create an order.

    Legacy stored whatever the seller typed, so a "min 500 / max 100"
    order sat in the book advertising a rule no buy could satisfy. The
    step now repeats with a hint, and the very next valid line goes
    through — the seller never loses the interview.
    """
    bot, dispatcher, registry = wired
    await _seed_wallet(registry, balance=1000)
    sent = capture_callback_outgoing(bot)

    await _drive_to_limits_step(bot, dispatcher)
    before = len([s for s in sent if s["kind"] == "text"])
    for update_id, bad in ((15, "Сбер; 500; 100"), (16, "Сбер; 0; 100"), (17, "Сбер; -5; 100")):
        result = await dispatcher.feed_update(
            bot, make_message_update(bad, user_id=UID, update_id=update_id)
        )
        assert result is not UNHANDLED
        assert await _orders(registry) == []
        assert await _balance(registry) == 1000  # no escrow taken

    assert len([s for s in sent if s["kind"] == "text"]) - before == 3  # one hint each

    assert (
        await dispatcher.feed_update(
            bot, make_message_update("Сбер; 100; 500", user_id=UID, update_id=18)
        )
        is not UNHANDLED
    )
    orders = await _orders(registry)
    assert len(orders) == 1
    assert (orders[0].min_amount, orders[0].max_amount) == (100, 500)


async def test_a_created_order_survives_a_failing_callback_answer(
    wired: tuple[Bot, Dispatcher, EngineRegistry],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1867: the escrow is committed before the card is rendered.

    ``EconomyMiddleware`` commits only after the handler returns and
    rolls back on any raise (``middlewares/base.py:131-132``), and the
    skip button finishes on a bare ``callback.answer()`` — the one
    outgoing call in this flow that is not wrapped, and the one that
    fails on its own for an aged-out query. The coins were never at
    risk (the hold and the order rolled back together), but
    ``state.clear()`` ran inside ``_create_locks`` on the FSM's own
    storage, which is not transactional: it survived. So the seller
    lost the entire interview AND the order, and got an error card
    naming neither.

    ``pytest.raises`` rather than a swallowed error because this
    dispatcher is wired without an errors router — the propagation is
    the middleware's rollback path firing, which is exactly the
    condition under test.
    """
    bot, dispatcher, registry = wired
    await _seed_wallet(registry, balance=1000)
    capture_callback_outgoing(bot)

    # The interview steps answer their own callbacks, so the failure is
    # armed only once the flow is standing on the last button.
    await _drive_to_limits_step(bot, dispatcher)

    captured = bot.session.make_request

    async def failing_answer(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "AnswerCallbackQuery":
            raise RuntimeError("the callback query had already aged out")
        return await captured(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", failing_answer)

    with pytest.raises(RuntimeError):
        await dispatcher.feed_update(
            bot, make_callback_update(P2pSellSkipLimits().pack(), user_id=UID, update_id=15)
        )

    orders = await _orders(registry)
    assert len(orders) == 1
    assert orders[0].status == "active"
    assert orders[0].remaining_com == 500
    # The escrow moved with the order, in the same transaction.
    assert await _balance(registry) == 500
    assert await _ledger_sum(registry, "p2p_escrow") == 500
