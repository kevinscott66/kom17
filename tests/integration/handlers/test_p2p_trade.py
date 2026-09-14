"""handlers/p2p_trade.py — buy-side + trade lifecycle UI over a real
economy session (#64 P3).

Same harness as tests/integration/services/test_p2p_service.py (real
SQLite economy.db, real P2pService) plus duck-typed aiogram fakes (the
test_rank_self pattern) and a real in-memory FSMContext.

Covered surface:

* buy menu render: order lines price-ASC, per-order buy buttons,
  paging row appears past PAGE_SIZE, currency filter narrows;
* order detail: FSM enters awaiting_buy_amount, buy-all + stats
  buttons present, self-trade and dead orders rejected as toasts;
* buy amount: bad input keeps the state, a valid amount creates the
  pending trade, renders the buyer card (p2p_trade_important) and
  notifies the seller with the confirm button;
* buy-all: full remainder, order flips to completed, and both
  buy paths commit before their card — refusals included (#1868);
* express: fills cheapest-first across sellers, per-fill buyer card
  + per-fill seller notification, and the #694 checkpoint fires before
  a single one of those goes out;
* paid → confirm lifecycle: mark_paid notifies the seller, confirm
  credits the buyer exactly once (double-confirm rejected);
* dispute: opener toast, other-party DM, admin card with the THREE
  D1 resolve buttons in the configured admin chat;
* admin resolve: non-developer press is a silent no-op (no money),
  developer refund_buyer credits the buyer + notifies both parties;
* seller-stats popup answers with an alert (D3 counters, no rating).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.handlers.checks import CheckCreateStates
from telegram_invite_bot.handlers.p2p_trade import (
    P2pBuyStates,
    _paid_card_markup,
    handle_admin_resolve,
    handle_buy_all,
    handle_buy_amount,
    handle_buy_menu,
    handle_dispute_open,
    handle_express_currency,
    handle_express_fiat,
    handle_order_book,
    handle_order_view,
    handle_seller_confirm,
    handle_seller_stats,
    handle_trade_paid,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.p2p import (
    P2pBuyAll,
    P2pBuyMenu,
    P2pDisputeOpen,
    P2pDisputeResolve,
    P2pMyTrades,
    P2pOrderBook,
    P2pOrderView,
    P2pSellerStats,
    P2pTradeConfirm,
    P2pTradePaid,
)
from telegram_invite_bot.keyboards.builders.p2p import (
    P2pExpressCurrency as ExpressCurrencyCb,
)
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.p2p_repo import (
    ORDER_COMPLETED,
    TRADE_CONFIRMED,
    TRADE_DISPUTE_RETURNED_SELLER,
    TRADE_DISPUTED,
    TRADE_PAID,
    TRADE_PENDING,
    P2pRepo,
)
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.p2p_service import (
    MAX_OPEN_TRADES_PER_BUYER,
    BuyOutcome,
    P2pService,
)
from telegram_invite_bot.utils.numbers import MAX_DB_INT

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint

SELLER = 111
SELLER2 = 112
BUYER = 222
DEV = 999
ADMIN_CHAT = -100123
NOW = datetime(2026, 6, 11, 12, 0, 0)
LANG = "ru"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def service(session: AsyncSession) -> P2pService:
    return P2pService(P2pRepo(session), EconomyRepo(session), TransactionsRepo(session), session)


async def _seed(session: AsyncSession, user_id: int, balance: int) -> None:
    repo = EconomyRepo(session)
    await repo.get_or_create(user_id, now=NOW)
    await repo.set_balance(user_id, balance)
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int:
    wallet = await EconomyRepo(session).get(user_id)
    assert wallet is not None
    return wallet.balance


async def _order(service: P2pService, seller: int, amount: int, currency: str = "RUB") -> int:
    result = await service.create_sell_order(
        seller_id=seller, amount_com=amount, currency=currency, now=NOW
    )
    assert result.order_id > 0
    return result.order_id


def _settings(admin_chat_id: int = ADMIN_CHAT) -> Settings:
    bot_cfg = SimpleNamespace(
        admin_chat_id=admin_chat_id,
        is_developer=lambda uid: uid == DEV,
    )
    return cast("Settings", SimpleNamespace(bot=bot_cfg))


class FakeBot:
    """send_message recorder."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))

    def to(self, chat_id: int) -> list[tuple[int, str, Any]]:
        return [item for item in self.sent if item[0] == chat_id]


def fake_bot() -> Bot:
    return cast("Bot", FakeBot())


class FakeMessage:
    """Just enough Message: reply/edit/answer recorders.

    Subclasses nothing — handlers only isinstance-check against
    ``aiogram.types.Message`` inside ``_edit_or_answer``, so callbacks
    built on this fake exercise the "inaccessible message" skip path;
    the edited-card content is asserted via ``edits`` where the test
    swaps in ``edit_calls`` capture below.
    """

    def __init__(self, *, text: str | None = None, user_id: int = BUYER) -> None:
        self.text = text
        self.chat = SimpleNamespace(type="private", id=user_id)
        self.from_user = SimpleNamespace(
            id=user_id, is_bot=False, full_name=f"user{user_id}", language_code=None
        )
        self.replies: list[tuple[str, Any]] = []

    async def reply(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, kwargs.get("reply_markup")))


def msg(**kwargs: Any) -> Message:
    return cast("Message", FakeMessage(**kwargs))


class FakeCallback:
    def __init__(self, *, user_id: int) -> None:
        self.from_user = SimpleNamespace(
            id=user_id, is_bot=False, full_name=f"user{user_id}", language_code=None
        )
        self.message = FakeMessage(user_id=user_id)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **_: Any) -> None:
        self.answers.append((text, show_alert))


def cb(user_id: int) -> CallbackQuery:
    return cast("CallbackQuery", FakeCallback(user_id=user_id))


def fsm(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _markup_callbacks(markup: Any) -> list[str]:
    assert markup is not None
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


# -- order list ----------------------------------------------------------------


async def test_buy_menu_lists_orders_with_paging(
    session: AsyncSession, service: P2pService
) -> None:
    # #1690 caps one seller at MAX_ACTIVE_ORDERS_PER_SELLER live orders,
    # so a book long enough to page comes from a crowd, not a flooder.
    for offset in range(11):
        await _seed(session, SELLER + offset, 10_000)
        await _order(service, SELLER + offset, 100)
    callback = cb(BUYER)
    # FakeMessage is not an aiogram Message → the card would fall through
    # the edit path; spy on the renderer to capture the rendered card.
    rendered: list[tuple[str, Any]] = []

    import telegram_invite_bot.handlers.p2p_trade as mod

    original = mod._edit_or_answer

    async def _spy(target: object, text: str, markup: Any = None) -> None:
        rendered.append((text, markup))

    mod._edit_or_answer = _spy
    try:
        await handle_buy_menu(callback, P2pBuyMenu(page=0), service, LANG)
    finally:
        mod._edit_or_answer = original

    text, markup = rendered[0]
    callbacks = _markup_callbacks(markup)
    order_buttons = [c for c in callbacks if P2pOrderView.__prefix__ in c.split(":")[0]]
    assert len(order_buttons) == 10  # PAGE_SIZE
    # 11th order → next-page arrow present, no prev on page 0.
    next_buttons = [
        c for c in callbacks if c.startswith(P2pBuyMenu.__prefix__) and c.endswith(":1")
    ]
    assert next_buttons, callbacks
    assert callback.answers  # type: ignore[attr-defined]


async def test_buy_menu_past_the_last_page_snaps_back(
    session: AsyncSession, service: P2pService
) -> None:
    """A card older than the book must not read as an empty market.

    The orders that filled page 5 are long gone; rendering "нет
    ордеров" there would tell a buyer the market is dead while the
    first page is full.
    """
    await _seed(session, SELLER, 10_000)
    for _ in range(3):
        await _order(service, SELLER, 100)
    callback = cb(BUYER)
    rendered: list[tuple[str, Any]] = []

    import telegram_invite_bot.handlers.p2p_trade as mod

    original = mod._edit_or_answer

    async def _spy(target: object, text: str, markup: Any = None) -> None:
        rendered.append((text, markup))

    mod._edit_or_answer = _spy
    try:
        await handle_buy_menu(callback, P2pBuyMenu(page=5), service, LANG)
    finally:
        mod._edit_or_answer = original

    _text, markup = rendered[0]
    callbacks = _markup_callbacks(markup)
    order_buttons = [c for c in callbacks if P2pOrderView.__prefix__ in c.split(":")[0]]
    assert len(order_buttons) == 3
    # Page 0 has nothing before it, so no prev arrow is offered either.
    assert P2pBuyMenu(page=4).pack() not in callbacks


async def test_buy_menu_at_the_int64_ceiling_snaps_back_instead_of_crashing(
    session: AsyncSession, service: P2pService
) -> None:
    """#1984: the same miss as the rating board, one page size apart.

    ``P2pBuyMenu.page`` is a ``DbInt`` since #1978, so the ceiling value
    unpacks cleanly and reaches the renderer, which clamps it only from
    below. ``page * PAGE_SIZE`` then leaves the 64-bit range and the
    order-book query raises ``OverflowError`` on bind — before the
    snap-back above ever gets a chance to run.
    """
    await _seed(session, SELLER, 10_000)
    for _ in range(3):
        await _order(service, SELLER, 100)
    callback = cb(BUYER)
    rendered: list[tuple[str, Any]] = []

    import telegram_invite_bot.handlers.p2p_trade as mod

    original = mod._edit_or_answer

    async def _spy(target: object, text: str, markup: Any = None) -> None:
        rendered.append((text, markup))

    mod._edit_or_answer = _spy
    try:
        await handle_buy_menu(callback, P2pBuyMenu(page=MAX_DB_INT), service, LANG)
    finally:
        mod._edit_or_answer = original

    _text, markup = rendered[0]
    callbacks = _markup_callbacks(markup)
    order_buttons = [c for c in callbacks if P2pOrderView.__prefix__ in c.split(":")[0]]
    assert len(order_buttons) == 3


async def test_order_book_currency_filter_narrows(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 10_000)
    rub_id = await _order(service, SELLER, 100, "RUB")
    usd_id = await _order(service, SELLER, 100, "USD")
    callback = cb(BUYER)
    rendered: list[tuple[str, Any]] = []

    import telegram_invite_bot.handlers.p2p_trade as mod

    original = mod._edit_or_answer

    async def _spy(target: object, text: str, markup: Any = None) -> None:
        rendered.append((text, markup))

    mod._edit_or_answer = _spy
    try:
        await handle_order_book(callback, P2pOrderBook(currency="USD", page=0), service, LANG)
    finally:
        mod._edit_or_answer = original

    text, markup = rendered[0]
    assert "USD" in text
    callbacks = _markup_callbacks(markup)
    assert P2pOrderView(order_id=usd_id).pack() in callbacks
    assert P2pOrderView(order_id=rub_id).pack() not in callbacks
    # The filter row keeps the unfiltered "all" reset button.
    assert P2pOrderBook(currency="", page=0).pack() in callbacks


# -- order detail + buy --------------------------------------------------------


async def test_order_view_sets_state_and_buttons(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    callback = cb(BUYER)
    await handle_order_view(callback, P2pOrderView(order_id=order_id), service, state, LANG)
    assert await state.get_state() == P2pBuyStates.awaiting_buy_amount.state
    data = await state.get_data()
    assert data["order_id"] == order_id


async def test_order_view_card_shows_the_real_order_id(
    session: AsyncSession, service: P2pService
) -> None:
    """The card's headline is the order number the buyer will quote.

    ``h_p2p_order_detail`` opens with ``Ордер #{order_id}`` and the call
    site did not pass it, so ``_SafeFormat`` rendered the brace verbatim
    — on the last screen before the buyer types an amount, and the one
    identifier a dispute is filed against. Asserting the id is present
    is not enough on its own: the brace is what shipped, so assert it is
    gone as well.
    """
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    callback = cb(BUYER)
    rendered: list[tuple[str, Any]] = []

    import telegram_invite_bot.handlers.p2p_trade as mod

    original = mod._edit_or_answer

    async def _spy(target: object, text: str, markup: Any = None) -> None:
        rendered.append((text, markup))

    mod._edit_or_answer = _spy
    try:
        await handle_order_view(callback, P2pOrderView(order_id=order_id), service, state, LANG)
    finally:
        mod._edit_or_answer = original

    text, _markup = rendered[0]
    assert f"#{order_id}" in text
    assert "{" not in text, text


async def test_order_view_rejects_self_trade(session: AsyncSession, service: P2pService) -> None:
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 500)
    state = fsm(SELLER)
    callback = cb(SELLER)
    await handle_order_view(callback, P2pOrderView(order_id=order_id), service, state, LANG)
    assert await state.get_state() is None
    text, alert = callback.answers[0]  # type: ignore[attr-defined]
    assert alert is True


async def test_order_view_refuses_while_a_foreign_interview_is_open(
    session: AsyncSession, service: P2pService
) -> None:
    """#732: tapping an order card must not eat another flow's data.

    ``handle_order_view`` calls ``state.set_data``, which REPLACES the
    dict rather than merging it. With no gate, a user halfway through
    ``/check_create`` who tapped an order lost the check they were
    building — silently, with no way back. The gate answers with an
    alert and leaves the other flow exactly as it was.
    """
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    await state.set_state(CheckCreateStates.awaiting_amount)
    await state.set_data({"amount": 777})
    callback = cb(BUYER)

    await handle_order_view(callback, P2pOrderView(order_id=order_id), service, state, LANG)

    assert await state.get_state() == CheckCreateStates.awaiting_amount.state
    assert (await state.get_data())["amount"] == 777
    text, alert = callback.answers[0]  # type: ignore[attr-defined]
    assert alert is True
    assert text == t("h_p2p_buy_busy", LANG)


async def test_order_view_still_accepts_a_second_order_card(
    session: AsyncSession, service: P2pService
) -> None:
    """The gate must not break ordinary browsing of the book.

    Its own ``awaiting_buy_amount`` is the state every previous tap
    leaves behind, so a bare ``get_state() is not None`` gate — the
    idiom every other module uses — would have refused the second card
    a buyer opens. Membership in :data:`_OWN_BUY_STATES` is what makes
    the difference.
    """
    await _seed(session, SELLER, 1_000)
    first = await _order(service, SELLER, 500)
    second = await _order(service, SELLER, 400)
    state = fsm(BUYER)
    callback = cb(BUYER)

    await handle_order_view(callback, P2pOrderView(order_id=first), service, state, LANG)
    await handle_order_view(callback, P2pOrderView(order_id=second), service, state, LANG)

    assert await state.get_state() == P2pBuyStates.awaiting_buy_amount.state
    assert (await state.get_data())["order_id"] == second


async def test_express_currency_refuses_while_a_foreign_interview_is_open(
    session: AsyncSession, service: P2pService
) -> None:
    """#732: the express entry point sets data the same way."""
    state = fsm(BUYER)
    await state.set_state(CheckCreateStates.awaiting_amount)
    await state.set_data({"amount": 777})
    callback = cb(BUYER)

    await handle_express_currency(callback, ExpressCurrencyCb(currency="RUB"), state, LANG)

    assert await state.get_state() == CheckCreateStates.awaiting_amount.state
    assert (await state.get_data())["amount"] == 777


async def test_express_currency_still_accepts_a_second_currency(
    session: AsyncSession, service: P2pService
) -> None:
    """Changing one's mind about the currency stays a legal move."""
    state = fsm(BUYER)
    callback = cb(BUYER)

    await handle_express_currency(callback, ExpressCurrencyCb(currency="RUB"), state, LANG)
    await handle_express_currency(callback, ExpressCurrencyCb(currency="USD"), state, LANG)

    assert await state.get_state() == P2pBuyStates.awaiting_express_fiat.state
    assert (await state.get_data())["express_currency"] == "USD"


async def test_buy_amount_bad_input_keeps_state(session: AsyncSession, service: P2pService) -> None:
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_buy_amount)
    await state.set_data({"lang": LANG, "order_id": order_id})
    message = msg(text="not a number", user_id=BUYER)
    await handle_buy_amount(message, state, service, fake_bot(), LANG)
    assert await state.get_state() == P2pBuyStates.awaiting_buy_amount.state
    assert message.replies  # type: ignore[attr-defined]


async def test_buy_amount_creates_trade_and_notifies_seller(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_buy_amount)
    await state.set_data({"lang": LANG, "order_id": order_id})
    message = msg(text="200", user_id=BUYER)
    bot = fake_bot()
    await handle_buy_amount(message, state, service, bot, LANG)

    assert await state.get_state() is None
    trades = await service.my_trades(BUYER)
    assert len(trades) == 1
    trade = trades[0]
    assert trade.status == TRADE_PENDING
    assert trade.amount_com == 200
    # Buyer card carries the legacy instructions + paid/dispute buttons.
    card_text, card_markup = message.replies[0]  # type: ignore[attr-defined]
    assert "Важно" in card_text
    callbacks = _markup_callbacks(card_markup)
    assert P2pTradePaid(trade_id=trade.id).pack() in callbacks
    assert P2pDisputeOpen(trade_id=trade.id).pack() in callbacks
    # Seller notify WITHOUT the confirm button (#692). The trade is
    # ``pending``: pressing it would earn a ``NOT_PAID`` refusal, and the
    # real button arrives from ``handle_trade_paid`` once the buyer has
    # actually paid — see ``test_paid_then_confirm_credits_buyer_once``.
    seller_msgs = bot.to(SELLER)  # type: ignore[attr-defined]
    assert len(seller_msgs) == 1
    assert seller_msgs[0][2] is None
    # No wallet movement at buy time — escrow slice only.
    assert await _balance(session, BUYER) == 0


async def test_buy_all_completes_order(session: AsyncSession, service: P2pService) -> None:
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 300)
    callback = cb(BUYER)
    bot = fake_bot()
    await handle_buy_all(callback, P2pBuyAll(order_id=order_id), service, fsm(BUYER), bot, LANG)
    order = await service.get_order(order_id)
    assert order is not None
    assert order.status == ORDER_COMPLETED
    assert order.remaining_com == 0
    trades = await service.my_trades(BUYER)
    assert trades[0].amount_com == 300
    assert bot.to(SELLER)  # type: ignore[attr-defined]


async def test_buy_amount_commits_before_the_card(
    session: AsyncSession, service: P2pService
) -> None:
    """#1868: the fill is durable before the buyer card leaves.

    ``state.clear()`` above the reply runs on the FSM's own connection
    (``di/providers.py:85-106``), so it survives a rollback of the
    economy session. Without the checkpoint a rejected card reply takes
    the trade row and the ``remaining_com`` decrement with it and
    leaves the buyer with no card, no trade and no step to retype into.
    """
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_buy_amount)
    await state.set_data({"lang": LANG, "order_id": order_id})
    message = msg(text="200", user_id=BUYER)
    bot = fake_bot()
    calls_when_fired: list[int] = []

    async def checkpoint() -> None:
        calls_when_fired.append(
            len(bot.sent) + len(message.replies)  # type: ignore[attr-defined]
        )

    await handle_buy_amount(message, state, service, bot, LANG, cast("Checkpoint", checkpoint))

    assert calls_when_fired == [0]
    # ...and the card + seller notify really did follow it.
    assert len(message.replies) == 1  # type: ignore[attr-defined]
    assert len(bot.to(SELLER)) == 1  # type: ignore[attr-defined]


async def test_buy_amount_commits_even_when_the_amount_is_refused(
    session: AsyncSession, service: P2pService
) -> None:
    """#1868: the checkpoint sits after the call, not in the OK branch.

    :meth:`P2pService.buy` opens with ``lock_writer`` before any gate,
    so a refusal holds ``economy.db``'s single writer slot exactly as
    firmly as a fill does — and then spends a Telegram round-trip
    telling the buyer no. Move the ``await checkpoint()`` inside the OK
    branch and this test fails while the one above still passes.
    """
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_buy_amount)
    await state.set_data({"lang": LANG, "order_id": order_id})
    message = msg(text="9999", user_id=BUYER)
    calls_when_fired: list[int] = []

    async def checkpoint() -> None:
        calls_when_fired.append(len(message.replies))  # type: ignore[attr-defined]

    await handle_buy_amount(
        message, state, service, fake_bot(), LANG, cast("Checkpoint", checkpoint)
    )

    assert calls_when_fired == [0]
    # The refusal is the INVALID_AMOUNT one, and no trade was opened.
    assert len(message.replies) == 1  # type: ignore[attr-defined]
    assert await service.my_trades(BUYER) == []


async def test_buy_all_commits_before_the_card_is_drawn(
    session: AsyncSession, service: P2pService
) -> None:
    """#1868: same commit, sharper failure mode.

    :func:`_edit_or_answer` has already drawn the card citing
    ``result.trade_id`` by the time the unwrapped ``callback.answer``
    runs; a reject there would leave the buyer holding a card for a
    trade that no longer exists.
    """
    await _seed(session, SELLER, 1_000)
    order_id = await _order(service, SELLER, 300)
    callback = cb(BUYER)
    bot = fake_bot()
    calls_when_fired: list[int] = []

    async def checkpoint() -> None:
        calls_when_fired.append(
            len(bot.sent) + len(callback.answers)  # type: ignore[attr-defined]
        )

    await handle_buy_all(
        callback,
        P2pBuyAll(order_id=order_id),
        service,
        fsm(BUYER),
        bot,
        LANG,
        cast("Checkpoint", checkpoint),
    )

    assert calls_when_fired == [0]
    assert len(callback.answers) == 1  # type: ignore[attr-defined]
    assert len(bot.to(SELLER)) == 1  # type: ignore[attr-defined]


# -- express -------------------------------------------------------------------


async def test_express_currency_sets_state(session: AsyncSession, service: P2pService) -> None:
    state = fsm(BUYER)
    callback = cb(BUYER)
    await handle_express_currency(callback, ExpressCurrencyCb(currency="RUB"), state, LANG)
    assert await state.get_state() == P2pBuyStates.awaiting_express_fiat.state
    assert (await state.get_data())["express_currency"] == "RUB"


async def test_express_fiat_fills_cheapest_first_and_notifies(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 1_000)
    await _seed(session, SELLER2, 1_000)
    # Both at the market RUB rate (1.0) — two orders drained in id order;
    # the point here is per-fill cards + per-seller notifications.
    await _order(service, SELLER, 100)
    await _order(service, SELLER2, 100)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_express_fiat)
    await state.set_data({"lang": LANG, "express_currency": "RUB"})
    message = msg(text="150", user_id=BUYER)
    bot = fake_bot()
    await handle_express_fiat(message, state, service, bot, LANG)

    assert await state.get_state() is None
    trades = await service.my_trades(BUYER)
    assert len(trades) == 2
    assert sum(trade.amount_com for trade in trades) == 150
    # Summary reply + one DM card per fill to the buyer.
    assert message.replies  # type: ignore[attr-defined]
    assert len(bot.to(BUYER)) == 2  # type: ignore[attr-defined]
    # Each seller got exactly one notify with the confirm button.
    assert len(bot.to(SELLER)) == 1  # type: ignore[attr-defined]
    assert len(bot.to(SELLER2)) == 1  # type: ignore[attr-defined]


async def test_express_fiat_commits_before_the_telegram_fan_out(
    session: AsyncSession, service: P2pService
) -> None:
    """#694: the checkpoint fires before the first outbound message.

    This is the worst fan-out in the module — the summary reply, then a
    card to the buyer and a notify to the seller for every fill, up to
    ``MAX_OPEN_TRADES_PER_BUYER`` of them. The session middleware only
    commits after the handler returns (``middlewares/base.py:124-125``),
    so without the checkpoint the express purchase holds
    ``economy.db``'s single writer slot across the whole fan-out and
    every concurrent writer burns its 5 s ``busy_timeout``
    (``db/pragma.py:63``) before failing with "database is locked".

    Recording how many Telegram calls had already happened when the
    checkpoint fired is what pins the ordering: move the
    ``await checkpoint()`` below the fill loop and this test fails.
    """
    await _seed(session, SELLER, 1_000)
    await _seed(session, SELLER2, 1_000)
    await _order(service, SELLER, 100)
    await _order(service, SELLER2, 100)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_express_fiat)
    await state.set_data({"lang": LANG, "express_currency": "RUB"})
    message = msg(text="150", user_id=BUYER)
    bot = fake_bot()
    calls_when_fired: list[int] = []

    async def checkpoint() -> None:
        calls_when_fired.append(
            len(bot.sent) + len(message.replies)  # type: ignore[attr-defined]
        )

    await handle_express_fiat(message, state, service, bot, LANG, cast("Checkpoint", checkpoint))

    assert calls_when_fired == [0]
    # ...and the fan-out really did follow, so the assertion above is
    # not vacuously true on a run that sent nothing at all.
    assert len(bot.to(BUYER)) == 2  # type: ignore[attr-defined]
    assert len(bot.to(SELLER)) == 1  # type: ignore[attr-defined]
    assert len(bot.to(SELLER2)) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "typed", ["nan", "NaN", "inf", "-inf", "Infinity", "1e400"], ids=lambda s: s
)
async def test_express_fiat_refuses_a_non_finite_budget(
    session: AsyncSession, service: P2pService, typed: str
) -> None:
    """The step answers, keeps the money, and keeps the user ON the step.

    ``float()`` accepts all six of these strings and ``max_fiat <= 0``
    lets them through — NaN because it compares False against every
    bound, Infinity because it is genuinely greater than zero. NaN then
    reached ``int(take_fiat / price)`` in the service and raised
    ``ValueError``: the state had already been cleared, so the buyer sat
    in a dead flow with no reply at all. Infinity did not raise — worse,
    it filled: an unbounded budget takes every order in the book.

    The surviving FSM state is the assertion that distinguishes the
    handler's own guard from the service's. Both refuse the budget, but
    the service is called only after ``state.clear()``, so a refusal
    that far down ends the flow and makes the buyer walk the currency
    picker again. Caught here — beside the existing ``<= 0`` branch —
    a mistyped budget costs one retyped message, which is what every
    other bad-number branch in this flow already costs.

    A live order is seeded so the fill loop is actually reachable.
    """
    await _seed(session, SELLER, 1_000)
    await _order(service, SELLER, 100)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_express_fiat)
    await state.set_data({"lang": LANG, "express_currency": "RUB"})
    message = msg(text=typed, user_id=BUYER)
    bot = fake_bot()

    await handle_express_fiat(message, state, service, bot, LANG)

    assert len(message.replies) == 1  # type: ignore[attr-defined]
    assert message.replies[0][0]  # type: ignore[attr-defined]
    assert await service.my_trades(BUYER) == []
    assert bot.to(SELLER) == []  # type: ignore[attr-defined]
    # Refused on the step, not after it — the buyer retypes the number.
    assert await state.get_state() == P2pBuyStates.awaiting_express_fiat.state
    assert (await state.get_data())["express_currency"] == "RUB"


async def _hold_every_slot(session: AsyncSession, service: P2pService) -> None:
    """Park ``BUYER`` at the D4 ceiling with open trades.

    Stamped with the real wall clock, not the frozen ``NOW`` the rest of
    this module uses: the handlers pass ``datetime.now(UTC)`` down, and
    trades dated 2026-06-11 would be swept as stale by the D2 pass the
    ceiling check runs — the buyer would sail straight through the cap
    and the test would prove nothing.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    await _seed(session, SELLER2, 10_000)
    for _ in range(MAX_OPEN_TRADES_PER_BUYER):
        order_id = await service.create_sell_order(
            seller_id=SELLER2, amount_com=100, currency="RUB", now=now
        )
        result = await service.buy(
            buyer_id=BUYER, order_id=order_id.order_id, amount_com=100, now=now
        )
        assert result.outcome is BuyOutcome.OK, result.outcome


async def test_express_at_the_ceiling_says_so_instead_of_blaming_the_amount(
    session: AsyncSession, service: P2pService
) -> None:
    """The refusal must name the real reason.

    Before the cap existed this branch did not exist either, and the
    catch-all below it answers "wrong amount" — which would send a
    buyer whose amount was perfectly fine into retyping numbers until
    they gave up. So the assertion is on the TEXT, not merely on the
    absence of a trade.
    """
    await _seed(session, SELLER, 10_000)
    await _hold_every_slot(session, service)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_express_fiat)
    await state.set_data({"lang": LANG, "express_currency": "RUB"})
    message = msg(text="100000", user_id=BUYER)
    bot = fake_bot()

    await handle_express_fiat(message, state, service, bot, LANG)

    text, _ = message.replies[0]  # type: ignore[attr-defined]
    assert text == t("h_p2p_too_many_open", LANG, max=MAX_OPEN_TRADES_PER_BUYER)
    assert text != t("h_p2p_express_amount_invalid", LANG)
    # The book is untouched: the budget covered this order many times.
    untouched = await service.get_order(order_id)
    assert untouched is not None
    assert untouched.remaining_com == 500
    assert bot.to(SELLER) == []  # type: ignore[attr-defined]


async def test_buy_amount_at_the_ceiling_refuses_and_leaves_the_order(
    session: AsyncSession, service: P2pService
) -> None:
    """The manual path is capped too — express is not the only door."""
    await _seed(session, SELLER, 10_000)
    await _hold_every_slot(session, service)
    order_id = await _order(service, SELLER, 500)
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_buy_amount)
    await state.set_data({"lang": LANG, "order_id": order_id})
    message = msg(text="200", user_id=BUYER)
    bot = fake_bot()

    await handle_buy_amount(message, state, service, bot, LANG)

    text, _ = message.replies[0]  # type: ignore[attr-defined]
    assert text == t("h_p2p_too_many_open", LANG, max=MAX_OPEN_TRADES_PER_BUYER)
    untouched = await service.get_order(order_id)
    assert untouched is not None
    assert untouched.remaining_com == 500
    assert bot.to(SELLER) == []  # type: ignore[attr-defined]
    # No amount gets past the ceiling, so the step ends rather than
    # inviting a retype.
    assert await state.get_state() is None


async def test_express_no_orders(session: AsyncSession, service: P2pService) -> None:
    state = fsm(BUYER)
    await state.set_state(P2pBuyStates.awaiting_express_fiat)
    await state.set_data({"lang": LANG, "express_currency": "TON"})
    message = msg(text="100", user_id=BUYER)
    await handle_express_fiat(message, state, service, fake_bot(), LANG)
    assert await state.get_state() is None
    text, _ = message.replies[0]  # type: ignore[attr-defined]
    assert "TON" in text


# -- paid → confirm ------------------------------------------------------------


async def _make_trade(service: P2pService, amount: int = 200) -> int:
    order_id = await _order(service, SELLER, 500)
    result = await service.buy(buyer_id=BUYER, order_id=order_id, amount_com=amount, now=NOW)
    return result.trade_id


async def test_paid_then_confirm_credits_buyer_once(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)
    bot = fake_bot()

    paid_cb = cb(BUYER)
    await handle_trade_paid(paid_cb, P2pTradePaid(trade_id=trade_id), service, bot, LANG)
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_PAID
    assert bot.to(SELLER)  # type: ignore[attr-defined]
    assert P2pTradeConfirm(trade_id=trade_id).pack() in _markup_callbacks(
        bot.to(SELLER)[0][2]  # type: ignore[attr-defined]
    )

    confirm_cb = cb(SELLER)
    await handle_seller_confirm(confirm_cb, P2pTradeConfirm(trade_id=trade_id), service, bot, LANG)
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_CONFIRMED
    assert await _balance(session, BUYER) == 200
    assert bot.to(BUYER)  # type: ignore[attr-defined]

    # Double confirm: rejected, no second credit.
    again = cb(SELLER)
    await handle_seller_confirm(again, P2pTradeConfirm(trade_id=trade_id), service, bot, LANG)
    _, alert = again.answers[0]  # type: ignore[attr-defined]
    assert alert is True
    assert await _balance(session, BUYER) == 200


async def test_paid_wrong_buyer_not_found(session: AsyncSession, service: P2pService) -> None:
    await _seed(session, SELLER, 1_000)
    trade_id = await _make_trade(service)
    callback = cb(SELLER2)  # not the buyer
    await handle_trade_paid(callback, P2pTradePaid(trade_id=trade_id), service, fake_bot(), LANG)
    _, alert = callback.answers[0]  # type: ignore[attr-defined]
    assert alert is True
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_PENDING


async def test_confirm_wrong_seller_not_found(session: AsyncSession, service: P2pService) -> None:
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)
    await service.mark_paid(buyer_id=BUYER, trade_id=trade_id, now=NOW)
    callback = cb(SELLER2)  # not the seller
    await handle_seller_confirm(
        callback, P2pTradeConfirm(trade_id=trade_id), service, fake_bot(), LANG
    )
    _, alert = callback.answers[0]  # type: ignore[attr-defined]
    assert alert is True
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_PAID  # untouched, no credit
    assert await _balance(session, BUYER) == 0


# -- #1697: neither side loses the dispute button in ``paid`` ------------------


async def test_paid_notification_offers_the_seller_a_dispute_button(
    session: AsyncSession, service: P2pService
) -> None:
    """The seller's confirm card carries an escalation row too.

    #1697. The message only exists because the buyer pressed "Я
    оплатил", so the seller reading it is in exactly the state where a
    dispute is the honest move: the transfer did not arrive, or
    arrived short. Before this the card offered "подтвердить" and
    nothing else, which left a seller who had been cheated with no
    action but silence.
    """
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)
    bot = fake_bot()

    await handle_trade_paid(cb(BUYER), P2pTradePaid(trade_id=trade_id), service, bot, LANG)

    callbacks = _markup_callbacks(bot.to(SELLER)[0][2])  # type: ignore[attr-defined]
    assert P2pTradeConfirm(trade_id=trade_id).pack() in callbacks
    assert P2pDisputeOpen(trade_id=trade_id).pack() in callbacks


def test_paid_card_markup_keeps_the_buyers_dispute_button() -> None:
    """The card that replaces the buyer's own still escalates.

    #1697. This is asserted against the builder rather than against the
    edited message because the fake ``Message`` here is not an
    ``aiogram.types.Message``, so ``_edit_or_answer`` takes its
    inaccessible-message branch and no edit is ever recorded — see the
    class docstring above. The builder is the whole of the behaviour
    either way: whatever it returns is what the buyer is left holding.
    """
    callbacks = _markup_callbacks(_paid_card_markup(77, LANG))
    assert P2pMyTrades().pack() in callbacks
    assert P2pDisputeOpen(trade_id=77).pack() in callbacks


async def test_buyer_can_still_open_a_dispute_after_marking_paid(
    session: AsyncSession, service: P2pService
) -> None:
    """The functional half: the button the card offers actually works.

    #1697 restores a surface, not a permission — :meth:`open_dispute`
    always accepted the buyer in ``paid``. Asserting the transition
    here keeps the two from drifting apart: a later gate on the service
    side would fail this test rather than silently turn the restored
    button into a dead one.
    """
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)
    bot = fake_bot()
    await handle_trade_paid(cb(BUYER), P2pTradePaid(trade_id=trade_id), service, bot, LANG)

    await handle_dispute_open(
        cb(BUYER), P2pDisputeOpen(trade_id=trade_id), service, bot, _settings(), LANG
    )

    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_DISPUTED


async def test_trade_paid_commits_before_the_seller_is_told(
    session: AsyncSession, service: P2pService
) -> None:
    """#1864: ``mark_paid`` lands before the notify, not after it.

    ``P2pRepo.mark_paid`` is a guarded ``UPDATE``, so it opens
    ``BEGIN IMMEDIATE`` on the economy session even when it matches no
    row, and the session middleware commits only after the handler
    returns (``middlewares/base.py:131-132``). The last outgoing call
    here — ``callback.answer()`` — is the one this handler does not
    wrap, so an aged-out callback query rolled the transition back
    *after* the seller had already been DM'd "the buyer has paid", and
    ``handlers/errors.py`` classed the reject as benign and told the
    buyer nothing.

    Counting the Telegram calls already made when the checkpoint fires
    is what pins the ordering: move the ``await checkpoint()`` below
    the notify and this test fails.
    """
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)
    bot = fake_bot()
    paid_cb = cb(BUYER)
    calls_when_fired: list[int] = []

    async def checkpoint() -> None:
        calls_when_fired.append(
            len(bot.sent) + len(paid_cb.answers)  # type: ignore[attr-defined]
        )

    await handle_trade_paid(
        paid_cb,
        P2pTradePaid(trade_id=trade_id),
        service,
        bot,
        LANG,
        cast("Checkpoint", checkpoint),
    )

    assert calls_when_fired == [0]
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_PAID
    # ...and the notify really did follow, so the count above is not
    # vacuously true on a run that sent nothing at all.
    assert bot.to(SELLER)  # type: ignore[attr-defined]


# -- disputes ------------------------------------------------------------------


async def test_dispute_open_notifies_other_party_and_admin_card(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 1_000)
    trade_id = await _make_trade(service)
    bot = fake_bot()
    callback = cb(BUYER)
    await handle_dispute_open(
        callback,
        P2pDisputeOpen(trade_id=trade_id),
        service,
        bot,
        _settings(),
        LANG,
    )
    text, alert = callback.answers[0]  # type: ignore[attr-defined]
    assert alert is True
    # Other party (the seller) is notified.
    assert bot.to(SELLER)  # type: ignore[attr-defined]
    # Admin card carries all THREE resolutions (D1 adds return_seller).
    admin_msgs = bot.to(ADMIN_CHAT)  # type: ignore[attr-defined]
    assert len(admin_msgs) == 1
    callbacks = _markup_callbacks(admin_msgs[0][2])
    for resolution in ("refund_buyer", "confirm_seller", "return_seller"):
        assert P2pDisputeResolve(trade_id=trade_id, resolution=resolution).pack() in callbacks


async def test_dispute_open_commits_before_the_toast(
    session: AsyncSession, service: P2pService
) -> None:
    """#1864: the dispute flag lands before anyone is told about it.

    Here the unwrapped call is the FIRST one — the alert toast to the
    opener — and both ``_notify`` calls come after it. So an aged-out
    callback query rolled ``mark_disputed`` back while the opener had
    been shown "dispute opened", and neither the counterparty nor the
    admin desk ever heard about it.
    """
    await _seed(session, SELLER, 1_000)
    trade_id = await _make_trade(service)
    bot = fake_bot()
    callback = cb(BUYER)
    calls_when_fired: list[int] = []

    async def checkpoint() -> None:
        calls_when_fired.append(
            len(bot.sent) + len(callback.answers)  # type: ignore[attr-defined]
        )

    await handle_dispute_open(
        callback,
        P2pDisputeOpen(trade_id=trade_id),
        service,
        bot,
        _settings(),
        LANG,
        cast("Checkpoint", checkpoint),
    )

    assert calls_when_fired == [0]
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_DISPUTED
    assert callback.answers  # type: ignore[attr-defined]
    assert bot.to(SELLER)  # type: ignore[attr-defined]
    assert bot.to(ADMIN_CHAT)  # type: ignore[attr-defined]


async def test_admin_resolve_gate_and_refund_buyer(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)
    await service.open_dispute(user_id=BUYER, trade_id=trade_id)
    bot = fake_bot()

    # Non-developer: silent no-op, no credit.
    intruder = cb(SELLER2)
    await handle_admin_resolve(
        intruder,
        P2pDisputeResolve(trade_id=trade_id, resolution="refund_buyer"),
        service,
        bot,
        _settings(),
        LANG,
    )
    assert intruder.answers == [(None, False)]  # type: ignore[attr-defined]
    assert await _balance(session, BUYER) == 0

    # Developer: refund-buyer credits the buyer, notifies both seats.
    dev = cb(DEV)
    await handle_admin_resolve(
        dev,
        P2pDisputeResolve(trade_id=trade_id, resolution="refund_buyer"),
        service,
        bot,
        _settings(),
        LANG,
    )
    assert await _balance(session, BUYER) == 200
    assert bot.to(BUYER)  # type: ignore[attr-defined]
    assert bot.to(SELLER)  # type: ignore[attr-defined]
    stats = await service.seller_stats(SELLER)
    assert stats.dispute_count == 1


async def test_admin_resolve_confirm_seller(session: AsyncSession, service: P2pService) -> None:
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)
    await service.open_dispute(user_id=BUYER, trade_id=trade_id)
    bot = fake_bot()
    dev = cb(DEV)
    await handle_admin_resolve(
        dev,
        P2pDisputeResolve(trade_id=trade_id, resolution="confirm_seller"),
        service,
        bot,
        _settings(),
        LANG,
    )
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_CONFIRMED
    assert await _balance(session, BUYER) == 200  # buyer credited
    stats = await service.seller_stats(SELLER)
    assert stats.successful_trades == 1
    assert bot.to(BUYER)  # type: ignore[attr-defined]
    assert bot.to(SELLER)  # type: ignore[attr-defined]


async def test_admin_resolve_return_seller_d1(session: AsyncSession, service: P2pService) -> None:
    await _seed(session, SELLER, 1_000)
    await _seed(session, BUYER, 0)
    trade_id = await _make_trade(service)  # escrows 200 of the 500 order
    await service.open_dispute(user_id=SELLER, trade_id=trade_id)
    seller_before = await _balance(session, SELLER)
    bot = fake_bot()
    dev = cb(DEV)
    await handle_admin_resolve(
        dev,
        P2pDisputeResolve(trade_id=trade_id, resolution="return_seller"),
        service,
        bot,
        _settings(),
        LANG,
    )
    trade = await service.get_trade(trade_id)
    assert trade is not None
    assert trade.status == TRADE_DISPUTE_RETURNED_SELLER
    # D1: the SELLER gets the escrow slice back, the buyer gets nothing.
    assert await _balance(session, SELLER) == seller_before + 200
    assert await _balance(session, BUYER) == 0
    assert bot.to(BUYER)  # type: ignore[attr-defined]
    assert bot.to(SELLER)  # type: ignore[attr-defined]


async def test_admin_resolve_rejects_unknown_resolution(
    session: AsyncSession, service: P2pService
) -> None:
    await _seed(session, SELLER, 1_000)
    trade_id = await _make_trade(service)
    await service.open_dispute(user_id=BUYER, trade_id=trade_id)
    dev = cb(DEV)
    await handle_admin_resolve(
        dev,
        P2pDisputeResolve(trade_id=trade_id, resolution="mint_coins"),
        service,
        fake_bot(),
        _settings(),
        LANG,
    )
    assert dev.answers == [(None, False)]  # type: ignore[attr-defined]


# -- stats popup ---------------------------------------------------------------


async def test_seller_stats_popup_alert(session: AsyncSession, service: P2pService) -> None:
    callback = cb(BUYER)
    await handle_seller_stats(callback, P2pSellerStats(seller_id=SELLER), service, LANG)
    text, alert = callback.answers[0]  # type: ignore[attr-defined]
    assert alert is True
    assert text is not None
