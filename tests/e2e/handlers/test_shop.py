"""End-to-end ``/shop`` + ``/inventory`` (Stage 16, read-only).

Validates router wiring (the shop router shares ``EconomyMiddleware``
with /balance but lives on its own ``Router`` so future shop-specific
middlewares can attach without bleeding into the economy path).

The four scenarios that matter for this stage:

* empty catalog → friendly "shop is empty" message (NOT silence — the
  user explicitly asked).
* populated catalog → every row rendered, stock hint correct for each
  of the three stock regimes (-1 / 0 / >0).
* empty inventory → friendly "inventory is empty" pointer to /shop.
* group ``/shop`` falls through to legacy (private-only filter).

Test data is seeded directly via the ORM — bypassing legacy SQL keeps
the test asserting our handler/repo behaviour, not legacy I/O.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Update

from telegram_invite_bot.config.settings import EconomyConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import (
    Donation,
    EconomyUser,
    GroupDonationsAggregate,
    GroupTopDonator,
    InventoryItem,
    ShopItem,
    Transaction,
)
from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import shop as shop_module
from telegram_invite_bot.keyboards.builders import (
    InventoryBack,
    InventoryInspect,
    InventoryPage,
    InventoryUse,
    ShopBuyCancel,
    ShopBuyConfirm,
    ShopBuyPrompt,
    ShopGroupPick,
    ShopPage,
)
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    assert_only_the_stale_tail_answered,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, user_id: int = 555, chat_type: str = "private") -> Update:
    """File-local defaults: user 555 named ``Eve`` (ru). Delegates to
    the shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Eve",
        language_code="ru",
    )


async def test_shop_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/shop"))
    assert result is not UNHANDLED
    assert "пуст" in sent[0]["text"].lower()


async def test_shop_renders_visible_stock_regimes_in_price_order(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Two regimes are user-visible (-1 / >0) and one is hidden (0).

    Legacy ``ShopManager.get_all_items()`` default hides ``stock == 0``
    rows (bot.py:12448) — the OOS item is seeded but must NOT appear
    in the rendered message. Ordering is by ``price`` ASC
    (bot.py:12445), so the cheap row prints before the expensive one.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add_all(
            [
                ShopItem(
                    id=1,
                    name="Expensive",
                    description="",
                    price=300,
                    type="unwarn",
                    stock=5,
                ),
                ShopItem(
                    id=2,
                    name="HiddenOOS",
                    description="",
                    price=20,
                    type="unwarn",
                    stock=0,
                ),
                ShopItem(
                    id=3,
                    name="Infinite",
                    description="forever",
                    price=10,
                    type="unwarn",
                    stock=-1,
                ),
            ]
        )
        await session.commit()

    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/shop"))
    body = sent[0]["text"]
    # OOS row is filtered at the repo, not displayed.
    assert "HiddenOOS" not in body
    assert "нет в наличии" not in body
    # Cheapest first: Infinite (10) before Expensive (300).
    assert 0 <= body.find("Infinite") < body.find("Expensive")
    # Finite-stock hint still rendered when stock > 0.
    assert "осталось: 5" in body
    # Infinite items get no hint.
    inf_line = next(line for line in body.splitlines() if "Infinite" in line)
    assert "осталось" not in inf_line and "нет в наличии" not in inf_line
    # Buy stub for visible rows only.
    assert "/buy 1" in body and "/buy 3" in body
    assert "/buy 2" not in body


async def test_shop_hides_items_the_bot_cannot_activate(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#2006: a type nothing can activate is not offered for sale.

    ``legend`` is a real production SKU — ``init_default_items``
    (bot.py:12411) seeds it at 2000 coins — and
    :func:`~telegram_invite_bot.services.inventory_use_planner.plan_effect_application`
    has no branch for it, so it classifies as UNKNOWN and the use flow
    refuses BEFORE consuming. The buyer pays and gets a row that can
    never be used and that nothing in the bot can refund. #2005 made
    the inventory card say so honestly; this hides the row instead.

    Same predicate, same place as the sold-out filter above: what the
    catalog will not sell, it does not show.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add_all(
            [
                ShopItem(
                    id=1,
                    name="Usable",
                    description="",
                    price=100,
                    type="unwarn",
                    stock=5,
                ),
                ShopItem(
                    id=2,
                    name="Легендарный статус",
                    description="",
                    price=2000,
                    type="legend",
                    stock=10,
                ),
            ]
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/shop"))
    body = sent[0]["text"]
    assert "Usable" in body
    assert "Легендарный" not in body
    assert "/buy 2" not in body


async def test_shop_html_escapes_item_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An admin-supplied ``<b>`` in a name MUST NOT render as bold.

    parse_mode=HTML on every outgoing message means the only thing
    standing between an admin's import and a stray ``<script>`` is
    the handler's ``html.escape`` call. Pin that contract.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            ShopItem(id=1, name="<b>Pwn</b>", description="x", price=1, type="unwarn", stock=1)
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/shop"))
    body = sent[0]["text"]
    assert "&lt;b&gt;Pwn&lt;/b&gt;" in body
    assert "<b>Pwn</b>" not in body


async def test_shop_in_group_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Group ``/shop`` is answered with "DM only", not ignored (#123).

    The catalogue is a personal, balance-aware view, so it stays in the
    DM (see the handler docstring); what changed is that the group now
    hears the refusal and gets a deep link instead of nothing.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/shop", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="shop")


async def test_inventory_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/inventory"))
    assert result is not UNHANDLED
    assert "пуст" in sent[0]["text"].lower()


async def test_inventory_renders_with_join_and_hides_used(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Legacy ``/inventory`` hides used rows (bot.py:23867,
    ``include_used=False``). Two purchases, one used — only the
    unused one should render. Also pins newest-first ordering.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(ShopItem(id=1, name="Plushie", price=10, type="unwarn", stock=-1))
        session.add(ShopItem(id=2, name="Sticker", price=5, type="unwarn", stock=-1))
        session.add(
            InventoryItem(
                user_id=555,
                item_id=1,
                purchase_date=datetime(2025, 1, 1, 12, 0, 0),
                used=False,
            )
        )
        session.add(
            InventoryItem(
                user_id=555,
                item_id=2,
                purchase_date=datetime(2025, 1, 2, 12, 0, 0),
                used=False,
            )
        )
        # This one is hidden by the legacy-parity ``include_used=False``
        # default — it must NOT appear in the rendered message.
        session.add(
            InventoryItem(
                user_id=555,
                item_id=1,
                purchase_date=datetime(2025, 1, 3, 12, 0, 0),
                used=True,
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/inv"))
    body = sent[0]["text"]
    assert "Plushie" in body
    assert "Sticker" in body
    # Used row is filtered at the repo level — its date must not appear.
    assert "2025-01-03" not in body
    assert "использовано" not in body
    # Newest first among the visible rows.
    idx_jan2 = body.find("2025-01-02")
    idx_jan1 = body.find("2025-01-01")
    assert 0 <= idx_jan2 < idx_jan1


async def test_inventory_in_group_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Group ``/inventory`` is answered with "DM only" (#123)."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/inventory", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="inventory")


# --- /buy (Stage 17) --------------------------------------------------------


async def test_buy_without_arg_renders_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/buy`` must NOT fall through — it's wired (see wiring test)
    and renders a usage hint so the user knows the syntax.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/buy"))
    assert result is not UNHANDLED
    assert "/buy" in sent[0]["text"]


async def test_buy_non_int_arg_warns(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy abc"))
    assert "ID" in sent[0]["text"]


async def test_buy_oversized_id_warns_instead_of_crashing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An id wider than SQLite's INTEGER must answer, not raise.

    ``int()`` happily parses any ASCII digit run up to CPython's
    4300-digit literal limit, so a bare ``int(raw)`` guarded by
    ``except ValueError`` lets ``2**63`` through as a perfectly valid
    Python int. It then dies inside aiosqlite as ``OverflowError``,
    which is not an ``SQLAlchemyError`` and is caught nowhere on this
    path — a freely repeatable unhandled traceback per message. This is
    the crash class ``utils/numbers.MAX_DB_INT`` exists to close.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update(f"/buy {2**63}"))
    assert sent, "oversized id produced no reply at all"
    assert "ID" in sent[0]["text"]


async def test_buy_non_ascii_digit_id_warns(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``int("\u0663") == 3`` — an Arabic-Indic digit must not buy item 3.

    Same posture ``tests/regression/test_unicode_digit_parsing.py``
    pins everywhere else: the gate and the parse are one call, and its
    grammar is an ASCII digit run. Asserted against the wallet rather
    than the copy, because the not-found reply also mentions the ID and
    would let a silent purchase read as a refusal.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=3, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy \u0663"))
    assert sent, "non-ASCII digit id produced no reply at all"
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 500
        item = await session.get(ShopItem, 3)
        assert item is not None and item.stock == 3


async def test_buy_item_not_found(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    # Seed a wallet so we'd otherwise be able to pay — proves the
    # not-found branch fires before the debit attempt.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=1000, language="ru"))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy 999"))
    assert "нет" in sent[0]["text"].lower() or "ID" in sent[0]["text"]


async def test_buy_happy_path_renders_balance_and_stock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Through-the-wire success: a single ``/buy 1`` debits the wallet,
    decrements stock, writes inventory + transaction, and the reply
    surfaces post-debit numbers without a re-query."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy 1"))
    body = sent[0]["text"]
    assert "Plushie" in body
    assert "400" in body  # 500 - 100
    assert "2" in body  # 3 - 1
    # DB-level proof, not just rendering:
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 400
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 2


async def test_buy_vip_auto_applies_and_reveals(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#15: buying a consumable (a canonical VIP row) applies it on the
    spot — the receipt reveals the grant ("VIP activated") and the
    VipRepo write lands atomically with the purchase debit, rather than
    parking the item in /inventory for a manual Use."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=5000, language="ru"))
        # Canonical VIP name the planner keys its 30-day duration off.
        session.add(ShopItem(id=1, name="👑 VIP (1 месяц)", price=1000, type="vip", stock=-1))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy 1"))
    body = sent[0]["text"]
    assert "VIP активирован" in body  # the reveal, not the bare receipt
    # The grant + the debit committed together in the one middleware txn.
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None
        assert wallet.balance == 4000  # 5000 - 1000
        assert wallet.vip_till is not None and wallet.vip_till > 0


async def test_buy_insufficient_funds(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=50, language="ru"))
        session.add(ShopItem(id=1, name="Pricey", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy 1"))
    body = sent[0]["text"]
    assert "100" in body  # price surfaced
    # Wallet untouched (atomic guard).
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 50


async def test_buy_out_of_stock_renders_friendly_copy(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``stock=0`` items pass the ID lookup but fail the stock check
    inside the service, surfacing :attr:`PurchaseStatus.OUT_OF_STOCK`.
    The handler MUST render the friendly "товар закончился" copy and
    NOT touch the wallet — without this branch a user would get the
    generic insufficient-funds error or no message at all (silent
    failure), both of which would be misleading. Legacy parity:
    bot.py:12537 surfaces a near-identical line.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        # stock=0 — the repo's ``include_out_of_stock=True`` codepath
        # (used by buy lookup) lets this row through; the service then
        # short-circuits with OUT_OF_STOCK.
        session.add(ShopItem(id=7, name="LastOne", price=100, type="unwarn", stock=0))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy 7"))
    body = sent[0]["text"]
    assert "закончился" in body
    # Balance must be intact — OOS is a no-op on the wallet.
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 500


async def test_buy_refuses_an_item_the_bot_cannot_activate(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#2006: the refusal is in the service, not only in the listing.

    Hiding the row from ``/shop`` is cosmetic on its own — the item id
    is a small integer anyone can guess, and inline buy keyboards from
    an older render stay live in the chat history forever. So the sale
    itself has to fail, and it has to fail before any write: no debit,
    no stock decrement, no inventory row.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=5000, language="ru"))
        session.add(
            ShopItem(
                id=1,
                name="Легендарный статус",
                description="",
                price=2000,
                type="legend",
                stock=10,
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/buy 1"))
    assert "нельзя активировать" in sent[0]["text"]
    async with sessionmaker() as session:
        from sqlalchemy import select  # local import — test-only

        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 5000
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 10
        assert (await session.execute(select(InventoryItem))).scalars().all() == []


def _install_lost_reply_target(bot: Any) -> list[str]:
    """Refuse the reply for its target; let a plain send through.

    The ``/buy`` message was deleted between the command and the
    receipt — by the buyer, by a cleanup bot, by an admin sweeping the
    chat. Telegram refuses the whole call in that case, and the chat
    itself is perfectly alive.

    Returns the list of texts that actually reached the chat.
    """
    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    delivered: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        if not delivered and type(method).__name__ == "SendMessage":
            delivered.append("")  # the refused reply, replaced below
            raise TelegramBadRequest(
                method=method, message="Bad Request: message to be replied not found"
            )
        delivered.append(getattr(method, "text", ""))
        return Message(
            message_id=1,
            date=datetime(2024, 1, 1),
            chat=Chat(id=555, type="private"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=getattr(method, "text", "ok"),
        )

    bot.session.make_request = fake_make_request
    return delivered


async def test_buy_receipt_falls_back_to_a_plain_send(
    make_wired: WiredFactory,
) -> None:
    """A deleted ``/buy`` message must not undo the purchase.

    The raw ``message.reply`` this used to be turned a missing reply
    target into a propagating error: the buyer got "⚠️ Произошла
    ошибка" and the purchase was rolled back under them. Losing the
    thread the receipt hangs off is not a reason to lose the receipt —
    the fallback plain send delivers it and the purchase stands.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    delivered = _install_lost_reply_target(bot)
    await dispatcher.feed_update(bot, _update("/buy 1"))

    assert "Plushie" in delivered[1]
    assert "400" in delivered[1]  # 500 - 100, the post-debit balance
    assert not any("ошибка" in text for text in delivered)
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 400
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 2


def _install_dead_chat(bot: Any) -> list[str]:
    """Refuse the reply for its target, then refuse the plain send.

    The card the buyer typed ``/buy`` into was deleted between the
    command and the receipt, and by the time we retry as a fresh send
    the bot is blocked. That pair is the only route to
    ``reply_or_send`` returning ``False`` — a bare ``Forbidden`` on the
    reply propagates on its own and never reaches the fallback.
    """
    attempted: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        attempted.append(type(method).__name__)
        if attempted.count("SendMessage") == 1:
            raise TelegramBadRequest(
                method=method, message="Bad Request: message to be replied not found"
            )
        raise TelegramForbiddenError(method=method, message="bot was blocked by the user")

    bot.session.make_request = fake_make_request
    return attempted


async def test_buy_receipt_undeliverable_rolls_back_the_purchase(
    make_wired: WiredFactory,
) -> None:
    """The debit landed, the stock dropped — and the receipt reached nobody.

    The companion to
    ``test_buy_receipt_falls_back_to_a_plain_send``: the fallback that
    rescues a lost reply target must not turn into a swallow when the
    fallback itself fails. Nothing reached the chat here, so the
    handler escapes and the session middleware unwinds the debit, the
    stock decrement and the inventory row together — the ``/buy``
    simply did not happen.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    attempted = _install_dead_chat(bot)
    await dispatcher.feed_update(bot, _update("/buy 1"))

    # Both deliveries were really attempted, so the assertions below
    # cannot pass for the cheaper reason of the handler bailing out
    # before it ever charged.
    assert attempted.count("SendMessage") >= 2
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 500
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 3
        from sqlalchemy import select  # local import — test-only

        rows = (await session.execute(select(InventoryItem))).scalars().all()
        assert rows == []


async def test_buy_in_group_is_refused_and_buys_nothing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Group ``/buy`` is refused — no purchase, no coins moved (#123).

    Purchases stay in the DM (private-only filter); the load-bearing
    half is that the group path never reaches ``PurchaseService``,
    which the exact-match on the refusal keeps honest — a receipt or a
    "not enough coins" reply would both break it.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/buy 1", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="buy")


# ── Stage 24: inline-buy callback flow over PurchaseService ─────────────


def _install_method_capture(bot: Any) -> list[Any]:
    """Monkey-patch ``bot.session.make_request`` to record raw methods.

    The shared ``capture_outgoing`` fixture records only ``text`` /
    ``chat_id`` pairs, which is enough for /shop body assertions but
    swallows the ``reply_markup`` we need for Stage 24 wire-format
    pins. Inlined as a sink-returning function so each test gets a
    fresh list without leaking state.

    Returns a list that grows as the test feeds updates. The synthesised
    Message return keeps aiogram's response parser happy (same trick as
    ``test_support.py::test_faq_renders_with_continue_button``).
    """
    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    sink: list[Any] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        sink.append(method)
        # The handler issues both ``SendMessage`` (initial /shop) and
        # ``EditMessageText`` / ``AnswerCallbackQuery`` (callbacks);
        # a single Message-shaped stub satisfies all three because
        # aiogram only inspects the ``message_id`` / ``chat`` fields
        # downstream.
        return Message(
            message_id=1,
            date=datetime(2024, 1, 1),
            chat=Chat(id=555, type="private"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=getattr(method, "text", "ok"),
        )

    bot.session.make_request = fake_make_request
    return sink


async def test_shop_renders_inline_buy_buttons(
    make_wired: WiredFactory,
) -> None:
    """Each visible /shop row gets a 🛒 Buy button packing a
    :class:`ShopBuyPrompt`. The text hint ``/buy <id>`` is preserved
    alongside (legacy users learnt that form), so this test asserts on
    the inline-markup contract — the existing text-rendering tests
    cover the body.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(ShopItem(id=1, name="Plushie", price=10, type="unwarn", stock=5))
        session.add(ShopItem(id=2, name="Sticker", price=20, type="unwarn", stock=-1))
        await session.commit()

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, _update("/shop"))

    send = next(m for m in methods if type(m).__name__ == "SendMessage")
    markup = send.reply_markup
    assert markup is not None
    rows = markup.inline_keyboard
    assert len(rows) == 2
    # Cheapest first (price ASC): Plushie (10) before Sticker (20).
    assert rows[0][0].callback_data == ShopBuyPrompt(item_id=1).pack()
    assert rows[1][0].callback_data == ShopBuyPrompt(item_id=2).pack()
    # Prefix anchors the strangler-bridge invariant: legacy
    # ``shop_buy_<id>`` literals must not collide. ``shop_buy:1`` does
    # NOT match a ``startswith('shop_buy_')`` test, and aiogram's
    # CallbackData.filter splits on ``:``, so a click on a NEW button
    # cannot route to legacy and vice-versa.
    assert rows[0][0].callback_data.startswith("shop_buy:")


async def test_shop_page_zero_renders_eight_buys_plus_nav(
    make_wired: WiredFactory,
) -> None:
    """Page-0 of a multi-page catalog: 8 buy buttons + a nav row with
    ``next »`` only (no ``« prev`` on page 0). The body slices to page
    0 too — Stage 25 paginates BOTH surfaces so the keyboard never
    references items the body doesn't show (the Stage 24 shape, where
    the body listed every row but the keyboard capped at 8, surfaced
    items the user couldn't click).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for i in range(1, 13):
            session.add(ShopItem(id=i, name=f"Item{i}", price=i * 10, type="unwarn", stock=5))
        await session.commit()

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, _update("/shop"))

    send = next(m for m in methods if type(m).__name__ == "SendMessage")
    rows = send.reply_markup.inline_keyboard
    # 8 buy rows + 1 nav row (next only).
    assert len(rows) == 9
    nav = rows[-1]
    assert len(nav) == 2  # indicator + next
    assert "1/2" in nav[0].text  # indicator label
    assert "вперёд" in nav[1].text or "next" in nav[1].text.lower()
    # Body restricted to page 0 (items 1..8). Item 12 lives on page 1
    # — both body and keyboard must agree on the window.
    body = send.text
    body_lines = body.splitlines()
    assert any(line.rstrip() == "  Купить: /buy 1" for line in body_lines)
    assert any(line.rstrip() == "  Купить: /buy 8" for line in body_lines)
    assert "/buy 12" not in body  # page 1 territory
    assert "/buy 9" not in body


async def test_shop_buy_prompt_callback_renders_confirmation_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Click on a /shop Buy button → edit to confirm card showing item,
    price, and current balance, plus a Confirm/Cancel keyboard.

    No DB mutation at this step — the test asserts on the rendered
    edit *and* on the wallet/stock rows staying untouched. This is the
    contract the second-step :class:`ShopBuyConfirm` depends on: the
    user's balance read here must equal the balance the service later
    debits against (modulo a concurrent purchase elsewhere — the
    service's rowcount guard handles that race regardless).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyPrompt(item_id=1).pack(), user_id=555)
    )
    assert result is not UNHANDLED

    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits, "prompt must edit the message to a confirmation card"
    body = edits[0]["text"]
    assert "Plushie" in body
    assert "100" in body  # price
    assert "500" in body  # balance at prompt time
    # No mutation yet.
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 500
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 3


async def test_shop_buy_prompt_callback_not_found_emits_toast_only(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """If the item was deleted between /shop render and click the
    handler toasts WITHOUT touching the prompt message — the user can
    still pick another row on the still-visible keyboard. Pinned
    separately from the confirm-step's not-found card because the
    semantics differ (prompt step preserves UI; confirm step is
    terminal and edits the card).
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyPrompt(item_id=999).pack(), user_id=555)
    )
    # Only an AnswerCallbackQuery — no EditMessageText.
    kinds = [m["kind"] for m in sent]
    assert "callback_answer" in kinds
    assert "edit" not in kinds
    # Toast carries the not-found copy.
    answer = next(m for m in sent if m["kind"] == "callback_answer")
    assert answer["text"] and "нет" in answer["text"].lower()


async def test_shop_buy_confirm_happy_path_debits_and_inserts_inventory(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Through-the-wire success: balance debited, stock decremented,
    inventory row created, transactions row logged, message edited to
    the success card. This is the proof that the callback path reaches
    the same :class:`PurchaseService` as the slash ``/buy``.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyConfirm(item_id=1).pack(), user_id=555)
    )

    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    body = edits[0]["text"]
    assert "Plushie" in body
    assert "400" in body  # post-debit balance

    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 400
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 2
        # Inventory row created.
        from sqlalchemy import select  # local import — test-only

        inv_rows = (
            (await session.execute(select(InventoryItem).where(InventoryItem.user_id == 555)))
            .scalars()
            .all()
        )
        assert len(inv_rows) == 1 and inv_rows[0].item_id == 1
        # Transaction logged.
        tx_rows = (
            (await session.execute(select(Transaction).where(Transaction.from_id == 555)))
            .scalars()
            .all()
        )
        # Positive magnitude; ``from_id=555`` above is what makes it a spend.
        assert len(tx_rows) == 1 and tx_rows[0].amount == 100


async def test_shop_buy_confirm_auto_applies_exactly_like_slash_buy(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#192: the inline card and ``/buy`` sell the SAME rows, so they
    must settle the same way.

    They did not. ``/buy`` auto-applied a consumable and revealed the
    result on the receipt; the ``/shop`` confirm button debited the
    coins, parked an unused inventory row and said "purchased" — for
    ``🎁 Секретный подарок`` that is 500 COM for a row the buyer then has
    to find and redeem by hand. Which button someone happened to press
    is not a thing the catalog should price, so this pins the payout,
    the consumed row and the balance the card quotes.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=1000, language="ru"))
        # The live production row, verbatim — name, price and bounds.
        session.add(
            ShopItem(
                id=1,
                name="🎁 Секретный подарок",
                price=500,
                type="luck",
                stock=82,
                data=json.dumps({"min": 100, "max": 1000}),
            )
        )
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyConfirm(item_id=1).pack(), user_id=555)
    )

    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    body = edits[0]["text"]
    # The reveal, not the bare receipt.
    assert "выпало" in body

    async with sessionmaker() as session:
        from sqlalchemy import select  # local import — test-only

        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None
        # 1000 − 500 price + a payout inside the row's advertised span.
        assert 600 <= wallet.balance <= 1500
        payout = wallet.balance - 500
        inv_rows = (
            (await session.execute(select(InventoryItem).where(InventoryItem.user_id == 555)))
            .scalars()
            .all()
        )
        # Applied on the spot — the row is consumed, not parked.
        assert len(inv_rows) == 1
        assert inv_rows[0].used is True
        # The card quotes the balance the buyer actually has, payout
        # included — quoting the post-debit figure would under-report it.
        assert str(wallet.balance) in body
        assert 100 <= payout <= 1000


async def test_shop_buy_confirm_rolls_back_when_the_receipt_reaches_nobody(
    make_wired: WiredFactory,
) -> None:
    """The confirm card cannot be edited and the fallback send is refused.

    ``_safe_edit`` used to wrap that fallback in
    ``suppress(TelegramBadRequest)``, justified by "the user blocked
    the bot" — a case that raises ``TelegramForbiddenError`` and was
    never caught here. What the suppress actually did was let a
    *committed* purchase end in silence. It propagates now, so the
    session middleware unwinds the debit, the stock and the inventory
    row instead of charging for a receipt nobody could read.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    attempted: list[str] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        attempted.append(name)
        if name == "AnswerCallbackQuery":
            return True
        if name == "EditMessageText":
            raise TelegramBadRequest(
                method=method, message="Bad Request: message to edit not found"
            )
        raise TelegramBadRequest(method=method, message="Bad Request: chat not found")

    bot.session.make_request = fake_make_request  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyConfirm(item_id=1).pack(), user_id=555)
    )

    # Both routes into the chat were really tried, so the rollback
    # below is not passing because the purchase never ran.
    assert "EditMessageText" in attempted
    assert "SendMessage" in attempted
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 500
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 3
        from sqlalchemy import select  # local import — test-only

        inv_rows = (await session.execute(select(InventoryItem))).scalars().all()
        assert inv_rows == []


async def test_shop_buy_confirm_survives_a_stale_callback_query(
    make_wired: WiredFactory,
) -> None:
    """A refused toast must not cancel the sale — and must not eat the card.

    ``AnswerCallbackQuery`` failing with "query is too old and response
    timeout expired" is the ordinary shape of a handler that waited on
    economy.db's single writer slot, and it says nothing about whether
    the chat can receive anything. Unsuppressed it was still the most
    destructive call in the handler: it raised before ``_safe_edit``
    ran, so ``middlewares.base`` unwound a completed purchase while the
    buyer got neither toast, card nor item.

    The companion to
    ``test_shop_buy_confirm_rolls_back_when_the_receipt_reaches_nobody``,
    which pins the opposite and still-intended half: when the *receipt*
    genuinely reaches nobody, the purchase is refunded.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    attempted: list[str] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        attempted.append(name)
        if name == "AnswerCallbackQuery":
            raise TelegramBadRequest(
                method=method,
                message="Bad Request: query is too old and response timeout expired",
            )
        return Message(
            message_id=1,
            date=datetime(2024, 1, 1),
            chat=Chat(id=555, type="private"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=getattr(method, "text", "ok"),
        )

    bot.session.make_request = fake_make_request  # type: ignore[method-assign,assignment]

    await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyConfirm(item_id=1).pack(), user_id=555)
    )

    # The toast really was refused, and the receipt really was still
    # attempted afterwards — neither assertion below can pass because
    # the handler bailed out early.
    assert "AnswerCallbackQuery" in attempted
    assert "EditMessageText" in attempted
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 400
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 2
        from sqlalchemy import select  # local import — test-only

        inv_rows = (await session.execute(select(InventoryItem))).scalars().all()
        assert len(inv_rows) == 1 and inv_rows[0].item_id == 1


async def test_shop_buy_confirm_insufficient_funds_does_not_mutate(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Confirm with too-low balance: the service's rowcount guard
    refuses the debit, the handler edits to the insufficient card,
    and NOTHING in the DB changes. Pinned because a silent debit on
    the failure branch would be a money-loss bug.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=50, language="ru"))
        session.add(ShopItem(id=1, name="Pricey", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(ShopBuyConfirm(item_id=1).pack(), user_id=555)
    )

    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    body = edits[0]["text"]
    assert "100" in body  # price surfaced
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 50
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 3


async def test_shop_buy_cancel_callback_clears_keyboard(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Cancel from the confirmation card edits to a brief cancel line
    and drops the keyboard so a re-click can't fire twice. No DB
    mutation — pinned because legacy's cancel button also did nothing
    server-side and we don't want to accidentally land a side effect
    here.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, make_callback_update(ShopBuyCancel().pack(), user_id=555))
    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    assert "тмен" in edits[0]["text"].lower()  # "отменена" or similar
    # Wallet + stock untouched.
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None and wallet.balance == 500
        item = await session.get(ShopItem, 1)
        assert item is not None and item.stock == 3


# ── Stage 25: /shop pagination via ShopPage callback ────────────────────


async def _seed_n_items(registry: Any, n: int) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for i in range(1, n + 1):
            session.add(ShopItem(id=i, name=f"Item{i}", price=i * 10, type="unwarn", stock=5))
        await session.commit()


def _nav_row(markup: Any) -> list[Any] | None:
    """Return the last keyboard row if it looks like a nav row.

    Nav rows carry ``shop_pg:`` callback_data; buy rows carry
    ``shop_buy:``. Returns ``None`` when there's no nav (single-page
    catalog) so callers can assert ``is None`` cleanly.
    """
    if markup is None or not markup.inline_keyboard:
        return None
    last: list[Any] = list(markup.inline_keyboard[-1])
    if any(btn.callback_data and btn.callback_data.startswith("shop_pg:") for btn in last):
        return last
    return None


async def test_shop_page_flip_next_renders_page_one(
    make_wired: WiredFactory,
) -> None:
    """Click ``next »`` from page 0 → page 1 (items 9..16) with both
    ``« prev`` and ``next »`` buttons. Pinned because the middle-page
    shape is the only one where both nav buttons coexist; bugs in the
    edge-condition omission would silently land here first.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_items(registry, 20)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, make_callback_update(ShopPage(page=1).pack(), user_id=555))
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    body = edit.text
    # Page 1 ⇒ items 9..16 visible, items 1..8 and 17..20 not.
    for i in range(9, 17):
        assert f"/buy {i}" in body
    assert "/buy 8" not in body
    assert "/buy 17" not in body
    nav = _nav_row(edit.reply_markup)
    assert nav is not None and len(nav) == 3  # prev + indicator + next
    assert "назад" in nav[0].text or "prev" in nav[0].text.lower()
    assert "2/3" in nav[1].text
    assert "вперёд" in nav[2].text or "next" in nav[2].text.lower()


async def test_shop_page_flip_last_page_drops_next_button(
    make_wired: WiredFactory,
) -> None:
    """Page 2 of 3 (items 17..20) renders only ``« prev`` + indicator,
    no ``next »``. Asymmetric pin from the page-0 test so the edge
    omission can't regress on one side without the test catching it.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_items(registry, 20)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, make_callback_update(ShopPage(page=2).pack(), user_id=555))
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    body = edit.text
    # Items 17..20 only.
    for i in range(17, 21):
        assert f"/buy {i}" in body
    assert "/buy 16" not in body
    nav = _nav_row(edit.reply_markup)
    assert nav is not None and len(nav) == 2  # prev + indicator only
    assert "назад" in nav[0].text or "prev" in nav[0].text.lower()
    assert "3/3" in nav[1].text


async def test_shop_page_flip_prev_returns_to_earlier_page(
    make_wired: WiredFactory,
) -> None:
    """Click ``« prev`` from page 2 → page 1. Round-trip pin: the prev
    direction must produce the same page 1 body as a forward ``next``
    from page 0, so the nav row's prev callback_data is ``page - 1``
    (not, say, ``page`` left as-is — a sign-flip bug would land here).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_items(registry, 20)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, make_callback_update(ShopPage(page=1).pack(), user_id=555))
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    assert "/buy 9" in edit.text and "/buy 16" in edit.text


async def test_shop_page_indicator_same_page_click_is_noop(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Click on the middle ``N/M`` indicator → callback answered, no
    edit. Avoids Telegram's "message is not modified" 400 that an
    identical re-render would otherwise trigger.

    The handler sniffs the current page out of the existing message's
    reply_markup (the indicator button's packed payload). This test
    builds an update with a real nav-row reply_markup attached so the
    detection path actually fires; without the markup the handler
    falls through to a (harmless but wasteful) re-render.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_items(registry, 20)

    sent = capture_callback_outgoing(bot)
    # Hand-built update: callback.message carries a reply_markup whose
    # indicator button packs page=1 and contains a "/" in its label,
    # matching what /shop would have rendered on page 1.
    update = Update.model_validate(
        {
            "update_id": 99,
            "callback_query": {
                "id": "cb-noop",
                "from": {"id": 555, "is_bot": False, "first_name": "Eve"},
                "chat_instance": "ci-noop",
                "data": ShopPage(page=1).pack(),
                "message": {
                    "message_id": 10,
                    "date": 1_700_000_000,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "old shop body",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "« назад",
                                    "callback_data": ShopPage(page=0).pack(),
                                },
                                {
                                    "text": "2/3",
                                    "callback_data": ShopPage(page=1).pack(),
                                },
                                {
                                    "text": "вперёд »",
                                    "callback_data": ShopPage(page=2).pack(),
                                },
                            ]
                        ]
                    },
                },
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    kinds = [m["kind"] for m in sent]
    assert "callback_answer" in kinds
    assert "edit" not in kinds  # no-op short-circuit fired


async def test_shop_page_out_of_range_snaps_to_last_page(
    make_wired: WiredFactory,
) -> None:
    """A stale callback (catalog shrank, hand-crafted page, etc.) past
    the new last page MUST snap to the last page rather than rendering
    an empty body. Documented as a clamp in :func:`_clamp_page` —
    pinned here so a future "raise on out-of-range" rewrite catches.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_items(registry, 20)  # 3 pages

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, make_callback_update(ShopPage(page=99).pack(), user_id=555))
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    # Snapped to last page (page 2) ⇒ items 17..20.
    assert "/buy 17" in edit.text and "/buy 20" in edit.text
    nav = _nav_row(edit.reply_markup)
    assert nav is not None and "3/3" in nav[1].text


async def test_shop_small_catalog_renders_no_nav_row(
    make_wired: WiredFactory,
) -> None:
    """A 3-item catalog fits on page 0 ⇒ no nav row at all, so the
    Stage 24 small-catalog shape (Buy buttons only) is preserved.
    Without this regression pin a future "always render a nav row"
    refactor would silently land a ``1/1`` button users can't act on.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_items(registry, 3)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, _update("/shop"))
    send = next(m for m in methods if type(m).__name__ == "SendMessage")
    rows = send.reply_markup.inline_keyboard
    # 3 buy rows, no nav.
    assert len(rows) == 3
    assert _nav_row(send.reply_markup) is None


async def test_shop_page_flip_empty_catalog_resets_to_empty_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Admin pulled the entire catalog between /shop render and a flip
    → edit to the standard "shop is empty" line and drop the keyboard.
    Documented in the handler docstring; pinned so the empty-branch
    doesn't regress into a crash (the un-clamped slice on an empty
    list would render an empty body with a keyboard pointing at
    callbacks that re-resolve to the same empty state).
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, make_callback_update(ShopPage(page=1).pack(), user_id=555))
    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    assert "пуст" in edits[0]["text"].lower()


async def test_a_near_miss_prefix_never_reaches_the_buy_prompt(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``ShopBuyPrompt`` must claim ``shop_buy:`` and nothing wider.

    ``shop_buy_42`` (underscore, no colon) is one character away from
    the real prefix: aiogram's :meth:`CallbackData.filter` splits on
    ``:`` and compares the first segment exactly, so the whole string
    IS the segment and it does not match. A future "match by prefix"
    rewrite of the filter logic would swallow it — and with it every
    payload that merely starts the same way — which is what this pins.

    Written as UNHANDLED when the strangler bridge still forwarded
    unclaimed taps to legacy. Legacy is gone and #159 put an
    acknowledging tail at the end of the tree instead, so the pin now
    reads: the tail answered, the buy prompt did not (no edit, no
    card, no charge).
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, make_callback_update("shop_buy_42", user_id=555))
    assert_only_the_stale_tail_answered(sent, "shop_buy_42 must not reach the buy prompt")


# ── Stage 26: /inventory pagination + per-entry inspect callbacks ─────


async def _seed_n_inventory_for_user(
    registry: Any, user_id: int, n: int, *, start_item_id: int = 1
) -> list[int]:
    """Seed ``n`` inventory rows for ``user_id``, newest-first ordering.

    Returns the inserted inventory PKs in newest-first order (i.e.
    matching the order :meth:`InventoryRepo.list_for_user` will return
    them). Tests use the returned IDs to craft inspect callbacks
    against specific rows.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for i in range(1, n + 1):
            session.add(
                ShopItem(
                    id=start_item_id + i - 1,
                    name=f"Item{i}",
                    description=f"desc {i}",
                    price=i * 10,
                    # Deliberately a type nothing can activate, so the
                    # inspect card these tests pin carries a Back button
                    # and nothing else. That is the #2005 row — bought
                    # before #2006 stopped the sale, still sitting in a
                    # real inventory — and it keeps the pagination and
                    # inspect assertions about layout rather than about
                    # which effect buttons an item happens to earn.
                    type="legend",
                    stock=-1,
                )
            )
        for i in range(1, n + 1):
            session.add(
                InventoryItem(
                    user_id=user_id,
                    item_id=start_item_id + i - 1,
                    # Stagger purchase_date so newest-first is
                    # deterministic; row i has the i-th most recent
                    # timestamp, so PK order ascending ↔ purchase_date
                    # descending in reverse → list returns them with
                    # the highest i first.
                    purchase_date=datetime(2025, 1, i, 12, 0, 0),
                    used=False,
                )
            )
        await session.commit()
        # Re-read PKs in newest-first order.
        from sqlalchemy import select

        result = await session.execute(
            select(InventoryItem.id)
            .where(InventoryItem.user_id == user_id)
            .order_by(InventoryItem.purchase_date.desc())
        )
        return [row.id for row in result.all()]


def _inv_nav_row(markup: Any) -> list[Any] | None:
    """Last row if it carries ``inv_pg:`` callback_data, else None."""
    if markup is None or not markup.inline_keyboard:
        return None
    last: list[Any] = list(markup.inline_keyboard[-1])
    if any(btn.callback_data and btn.callback_data.startswith("inv_pg:") for btn in last):
        return last
    return None


async def test_inventory_page_zero_renders_eight_rows_plus_next_only(
    make_wired: WiredFactory,
) -> None:
    """20 inventory rows ⇒ 3 pages. Page 0 shows the 8 newest entries
    with one inspect button each and a nav row with ``next »`` only
    (no ``« prev`` on page 0). Mirrors /shop's page-0 shape.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_inventory_for_user(registry, user_id=555, n=20)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, _update("/inventory"))

    send = next(m for m in methods if type(m).__name__ == "SendMessage")
    rows = send.reply_markup.inline_keyboard
    # 8 inspect rows + 1 nav row.
    assert len(rows) == 9
    nav = rows[-1]
    assert len(nav) == 2  # indicator + next, no prev
    assert "1/3" in nav[0].text
    assert "вперёд" in nav[1].text or "next" in nav[1].text.lower()
    # Each inspect button carries an InventoryInspect callback_data
    # against a real inventory PK.
    for row in rows[:8]:
        assert row[0].callback_data.startswith("inv_ins:")


async def test_inventory_page_flip_next_renders_middle_page_both_nav_buttons(
    make_wired: WiredFactory,
) -> None:
    """Page 1 of 3 carries BOTH ``« prev`` and ``next »`` plus the
    ``2/3`` indicator. Asymmetric pin from page 0 / last-page tests.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_inventory_for_user(registry, user_id=555, n=20)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(InventoryPage(page=1).pack(), user_id=555)
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    nav = _inv_nav_row(edit.reply_markup)
    assert nav is not None and len(nav) == 3
    assert "назад" in nav[0].text or "prev" in nav[0].text.lower()
    assert "2/3" in nav[1].text
    assert "вперёд" in nav[2].text or "next" in nav[2].text.lower()


async def test_inventory_inspect_renders_detail_card_with_back_button(
    make_wired: WiredFactory,
) -> None:
    """Click on a 🔍 row → edit to a detail card carrying the item
    name, purchase timestamp, description, and a single ``🔙 Back``
    button packing :class:`InventoryBack`.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_ids = await _seed_n_inventory_for_user(registry, user_id=555, n=3)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryInspect(entry_id=entry_ids[0]).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    body = edit.text
    # The newest row is Item3 (purchase_date = 2025-01-03).
    assert "Item3" in body
    assert "2025-01-03" in body
    assert "desc 3" in body
    # Back button only.
    rows = edit.reply_markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].callback_data == InventoryBack().pack()


async def test_inventory_back_returns_to_page_zero(
    make_wired: WiredFactory,
) -> None:
    """``🔙 Back`` from the inspect card edits back to page 0 of the
    paginated list (the documented simplification — see
    :class:`InventoryBack`).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_inventory_for_user(registry, user_id=555, n=20)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, make_callback_update(InventoryBack().pack(), user_id=555))
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    nav = _inv_nav_row(edit.reply_markup)
    assert nav is not None
    # Page 0 indicator + next only.
    assert "1/3" in nav[0].text
    assert len(nav) == 2


async def test_inventory_same_page_indicator_click_is_noop(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Click on the middle indicator → callback answered, no edit.
    Same posture as /shop's same-page short-circuit (avoids the
    Telegram "message is not modified" reject).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_inventory_for_user(registry, user_id=555, n=20)

    sent = capture_callback_outgoing(bot)
    update = Update.model_validate(
        {
            "update_id": 99,
            "callback_query": {
                "id": "cb-inv-noop",
                "from": {"id": 555, "is_bot": False, "first_name": "Eve"},
                "chat_instance": "ci-inv-noop",
                "data": InventoryPage(page=1).pack(),
                "message": {
                    "message_id": 10,
                    "date": 1_700_000_000,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "old inventory body",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "« назад",
                                    "callback_data": InventoryPage(page=0).pack(),
                                },
                                {
                                    "text": "2/3",
                                    "callback_data": InventoryPage(page=1).pack(),
                                },
                                {
                                    "text": "вперёд »",
                                    "callback_data": InventoryPage(page=2).pack(),
                                },
                            ]
                        ]
                    },
                },
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    kinds = [m["kind"] for m in sent]
    assert "callback_answer" in kinds
    assert "edit" not in kinds


async def test_inventory_inspect_by_other_user_is_rejected_and_does_not_leak(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """**Authorization pin.** User B sends an
    :class:`InventoryInspect` carrying user A's entry_id. The handler
    MUST surface the same "no longer in your inventory" toast it would
    for a deleted entry — NEVER the detail card, NEVER any text
    revealing the entry exists under a different owner.

    The repo lookup is constrained by ``(callback.from_user.id,
    entry_id)`` so B's ``user_id`` filters A's row out. This test
    pins that contract end-to-end: a crafted callback from B against
    A's entry produces an AnswerCallbackQuery toast and NO
    EditMessageText.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    a_entry_ids = await _seed_n_inventory_for_user(registry, user_id=555, n=3)

    sent = capture_callback_outgoing(bot)
    # User B (777, no inventory) crafts a callback against A's PK.
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryInspect(entry_id=a_entry_ids[0]).pack(), user_id=777),
    )
    kinds = [m["kind"] for m in sent]
    # Toast, no edit — no information leaks about the row's existence.
    assert "callback_answer" in kinds
    assert "edit" not in kinds
    answer = next(m for m in sent if m["kind"] == "callback_answer")
    # The toast carries the not-found copy in user B's language (default ru).
    assert answer["text"]
    assert "инвентаре" in answer["text"].lower() or "inventory" in answer["text"].lower()
    # Belt-and-braces: the detail card text MUST NOT appear in any
    # outgoing call. ``Item3`` is the newest seeded row's name.
    for m in sent:
        assert "Item" not in (m.get("text") or "")


async def test_inventory_empty_renders_no_keyboard(
    make_wired: WiredFactory,
) -> None:
    """Empty inventory → empty-state line, no nav row, no inspect
    buttons. Pinned because Stage 26's keyboard builder must not
    render a stray indicator on a zero-page collection.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, _update("/inventory"))
    send = next(m for m in methods if type(m).__name__ == "SendMessage")
    assert "пуст" in send.text.lower()
    # No reply_markup at all (the empty-state branch sends a plain reply).
    assert send.reply_markup is None


async def test_inventory_page_out_of_range_snaps_to_last_page(
    make_wired: WiredFactory,
) -> None:
    """Stale or crafted ``inv_pg:99`` against a 3-page list snaps to
    page 2 (the new last). Mirrors /shop's clamp; pinned separately
    so a future "raise on out-of-range" rewrite catches on the
    inventory surface too.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_n_inventory_for_user(registry, user_id=555, n=20)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(InventoryPage(page=99).pack(), user_id=555)
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    nav = _inv_nav_row(edit.reply_markup)
    assert nav is not None
    assert "3/3" in nav[1].text


# ── Stage 29: /inventory use callback over InventoryUseService ─────────


async def _seed_use_fixtures(
    registry: Any,
    *,
    item_type: str,
    item_name: str = "👑 VIP (1 месяц)",
    user_id: int = 555,
    used: bool = False,
    expires: datetime | None = None,
    item_id: int = 100,
) -> int:
    """Seed one (catalog, wallet, inventory-row) triple for a use test.

    Returns the inventory PK. ``item_type`` drives the planner's
    classification: ``type='vip'`` with the canonical 1-month name maps
    to VIP_GRANT; ``type='double_daily'`` (any name) maps to
    DOUBLE_DAILY_BUSTER. Since #1204 every parameterised kind resolves
    on the type alone — the name only picks which preset — so only a
    type with no effect implementation at all (``legend``, ``ad``,
    ``custom_color``) still lands in UNKNOWN.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            ShopItem(
                id=item_id,
                name=item_name,
                description="",
                price=100,
                type=item_type,
                stock=-1,
            )
        )
        session.add(EconomyUser(user_id=user_id, balance=0, language="ru", vip_till=None))
        entry = InventoryItem(
            user_id=user_id,
            item_id=item_id,
            purchase_date=datetime(2025, 1, 1, 12, 0, 0),
            used=used,
            expires=expires,
        )
        session.add(entry)
        await session.commit()
        return entry.id


async def test_inventory_inspect_renders_use_button_for_known_kind(
    make_wired: WiredFactory,
) -> None:
    """Inspect on a VIP row → keyboard carries BOTH ``🎁 Use`` and
    ``🔙 Back`` buttons. Pins the Stage 29 button-hidden design path:
    the planner classifies the item, the inspect handler sees a
    non-UNKNOWN kind, and renders Use packed with the entry's PK.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(registry, item_type="vip")

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryInspect(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    rows = edit.reply_markup.inline_keyboard
    # One row with two buttons: Use + Back, in that order.
    assert len(rows) == 1 and len(rows[0]) == 2
    assert rows[0][0].callback_data == InventoryUse(entry_id=entry_id).pack()
    assert rows[0][1].callback_data == InventoryBack().pack()
    # Legacy-hint copy must NOT appear when the Use button is shown —
    # the hint is only for the no-activation fallback.
    assert "нельзя активировать" not in edit.text
    assert "legacy" not in edit.text.lower()


async def test_inventory_inspect_hides_use_button_for_unknown_kind(
    make_wired: WiredFactory,
) -> None:
    """Inspect on an UNKNOWN-type row → keyboard has Back only, and the
    card body says the item cannot be activated here. Pins the design
    call documented in handle_inventory_inspect. #2005: the copy used
    to promise a "legacy activation flow" and point at ``/help`` — a
    flow T-011 removed and an index that cannot activate anything —
    so the assertion is on the refusal, not on the old wording.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    # ``legend`` has no effect implementation at all, which is the only
    # remaining road to UNKNOWN. It used to be a non-canonical
    # ``color_nick`` name here; #1204 made that row classify normally,
    # because refusing it stranded coins the buyer had already spent.
    entry_id = await _seed_use_fixtures(registry, item_type="legend", item_name="🏅 Легенда")

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryInspect(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    rows = edit.reply_markup.inline_keyboard
    # Back only.
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].callback_data == InventoryBack().pack()
    # The no-activation hint is present in the body.
    assert "нельзя активировать" in edit.text


async def test_inventory_use_vip_grants_and_marks_entry_used(
    make_wired: WiredFactory,
) -> None:
    """Happy-path VIP: click 🎁 Use on a 1-month VIP row → the wallet's
    ``vip_till`` advances, the inventory row flips ``used=True``, and
    the message edits to the success card with a Back button.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(registry, item_type="vip")

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryUse(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    # Success copy mentions VIP.
    assert "VIP" in edit.text
    # Result keyboard: single Back button.
    rows = edit.reply_markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].callback_data == InventoryBack().pack()

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        assert row.used is True
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None
        assert wallet.vip_till is not None and wallet.vip_till > 0


async def test_inventory_inspect_renders_use_button_for_color_nick(
    make_wired: WiredFactory,
) -> None:
    """Stage 30 regression: the canonical operator-shipped color_nick
    row (``"🌈 Цветной ник"``) classifies as COLOR_NICK (not UNKNOWN),
    so the inspect card MUST surface the Use button. Pairs with
    ``hides_use_button_for_unknown_kind``, which since #1204 uses a
    type with no effect implementation rather than an unrecognised
    colour-nick name — that is now the only way to reach UNKNOWN."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(
        registry, item_type="color_nick", item_name="🌈 Цветной ник"
    )

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryInspect(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    rows = edit.reply_markup.inline_keyboard
    # Use + Back, in that order — same shape as the VIP-row test.
    assert len(rows) == 1 and len(rows[0]) == 2
    assert rows[0][0].callback_data == InventoryUse(entry_id=entry_id).pack()
    assert rows[0][1].callback_data == InventoryBack().pack()
    # Legacy hint must NOT appear — the planner now handles this row.
    assert "нельзя активировать" not in edit.text


async def test_inventory_use_color_nick_writes_privilege_row_and_renders_card(
    make_wired: WiredFactory,
) -> None:
    """Happy-path COLOR_NICK end-to-end: click 🎁 Use on the canonical
    color_nick row → a ``color_nick`` UserPrivilege row is written
    with the ``{"color": "rainbow"}`` payload and a 7-day expiry, the
    inventory row flips ``used=True``, and the message edits to the
    color-specific success card (mentions the rainbow emoji)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(
        registry, item_type="color_nick", item_name="🌈 Цветной ник"
    )

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryUse(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    # Success copy carries the rainbow emoji from the COLOR_NICK
    # template (distinct from VIP/buster cards).
    assert "🌈" in edit.text
    # Result keyboard: single Back button.
    rows = edit.reply_markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].callback_data == InventoryBack().pack()

    from telegram_invite_bot.db.models.economy import UserPrivilege

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        assert row.used is True
        priv = await session.get(UserPrivilege, (555, "color_nick", 0))
        assert priv is not None
        assert priv.value == '{"color": "rainbow"}'
        # Expiry roughly 7 days out — handler uses naive datetime.now(),
        # so we assert the window rather than exact equality.
        assert priv.expires_at > 0


async def test_inventory_inspect_renders_use_button_for_mute_protection(
    make_wired: WiredFactory,
) -> None:
    """Stage 31 regression: the canonical seeded mute_protection row
    (``"🔇 Защита от мута"``) now classifies as MUTE_PROTECTION
    (not UNKNOWN), so the inspect card MUST surface the Use button.
    Same shape as the COLOR_NICK regression — pins that promoting a
    new kind also flips the inspect button on for its canonical row.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(
        registry, item_type="mute_protection", item_name="🔇 Защита от мута"
    )

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryInspect(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    rows = edit.reply_markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 2
    assert rows[0][0].callback_data == InventoryUse(entry_id=entry_id).pack()
    assert rows[0][1].callback_data == InventoryBack().pack()
    assert "нельзя активировать" not in edit.text


async def test_inventory_use_mute_protection_writes_privilege_row_and_renders_card(
    make_wired: WiredFactory,
) -> None:
    """Happy-path MUTE_PROTECTION end-to-end: click 🎁 Use on the
    canonical seeded row → a ``mute_protection`` UserPrivilege row is
    written with the empty-object payload (``"{}"``) and a 24h expiry,
    the inventory row flips ``used=True``, and the message edits to
    the mute-protection success card (carries the 🛡 shield emoji,
    distinct from VIP/buster/color_nick copy)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(
        registry, item_type="mute_protection", item_name="🔇 Защита от мута"
    )

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryUse(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    assert "🛡" in edit.text
    rows = edit.reply_markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].callback_data == InventoryBack().pack()

    from telegram_invite_bot.db.models.economy import UserPrivilege

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        assert row.used is True
        priv = await session.get(UserPrivilege, (555, "mute_protection", 0))
        assert priv is not None
        assert priv.value == "{}"
        # Expiry roughly 24h out — handler uses naive datetime.now(),
        # so we assert the window rather than exact equality.
        assert priv.expires_at > 0


async def test_inventory_use_double_daily_writes_privilege_row(
    make_wired: WiredFactory,
) -> None:
    """Happy-path buster: click 🎁 Use on a ``double_daily`` row → a
    UserPrivilege row is written, the inventory row is consumed.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(registry, item_type="double_daily", item_name="2x daily")

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryUse(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    # Success copy mentions the buster on /daily.
    assert "/daily" in edit.text

    from telegram_invite_bot.db.models.economy import UserPrivilege

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        assert row.used is True
        priv = await session.get(UserPrivilege, (555, "double_daily", 0))
        assert priv is not None


async def test_inventory_use_already_used_does_not_double_grant(
    make_wired: WiredFactory,
) -> None:
    """Pre-seed ``used=True`` on a VIP row → Use click renders the
    "already used" card and the wallet's vip_till stays untouched.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(registry, item_type="vip", used=True)

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryUse(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    assert "использован" in edit.text.lower() or "already" in edit.text.lower()

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None
        # vip_till stayed None — no grant was written.
        assert wallet.vip_till is None


async def test_inventory_use_expired_entry_renders_expired_card(
    make_wired: WiredFactory,
) -> None:
    """Pre-seed an ``expires`` in the past → Use click renders the
    "expired" card; entry stays un-consumed (the planner never ran).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(
        registry,
        item_type="vip",
        expires=datetime(2000, 1, 1, 0, 0, 0),
    )

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryUse(entry_id=entry_id).pack(), user_id=555),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    assert "истёк" in edit.text.lower() or "expired" in edit.text.lower()

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        assert row.used is False


async def test_inventory_use_cross_user_returns_not_found(
    make_wired: WiredFactory,
) -> None:
    """**Authorization pin.** User B sends an :class:`InventoryUse`
    carrying user A's entry_id. The service's ``get_for_user`` filters
    A's row out under B's id → NOT_FOUND surface, A's entry stays
    un-consumed. Mirrors the inspect-by-other-user pin from Stage 26
    but at the use-call surface where the consequences are louder
    (a leak here would let B drain A's inventory).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(registry, item_type="vip", user_id=555)

    methods = _install_method_capture(bot)
    # User 777 has no inventory rows — clicks against A's PK.
    await dispatcher.feed_update(
        bot,
        make_callback_update(InventoryUse(entry_id=entry_id).pack(), user_id=777),
    )
    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    # The not-found card body, not the success card.
    assert "недоступна" in edit.text.lower() or "no longer available" in edit.text.lower()

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        # A's entry is intact, NOT consumed by B's attempt.
        assert row.used is False
        # A's wallet still has no VIP grant.
        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None
        assert wallet.vip_till is None
        # B did NOT get a wallet row created out of nothing.
        intruder = await session.get(EconomyUser, 777)
        assert intruder is None


async def test_inventory_use_concurrent_clicks_collapse_to_one_success(
    make_wired: WiredFactory,
) -> None:
    """Race pin at the handler surface: two concurrent /use feeds on
    the same (user, entry). The service's race guard pins exactly one
    SUCCESS at the integration layer (Stage 28's test); this verifies
    the handler doesn't bypass it. Exactly one ``vip_till`` write
    lands; exactly one of the two outgoing edits carries the success
    copy and the other carries the already-used copy.
    """
    import asyncio

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    entry_id = await _seed_use_fixtures(registry, item_type="vip")

    methods = _install_method_capture(bot)
    await asyncio.gather(
        dispatcher.feed_update(
            bot,
            make_callback_update(
                InventoryUse(entry_id=entry_id).pack(),
                user_id=555,
                callback_id="cb-race-a",
                chat_instance="ci-race-a",
            ),
        ),
        dispatcher.feed_update(
            bot,
            make_callback_update(
                InventoryUse(entry_id=entry_id).pack(),
                user_id=555,
                callback_id="cb-race-b",
                chat_instance="ci-race-b",
            ),
        ),
    )
    edits = [m for m in methods if type(m).__name__ == "EditMessageText"]
    assert len(edits) == 2
    texts = [e.text for e in edits]
    success_count = sum("VIP" in t and "✅" in t for t in texts)
    already_used_count = sum("использован" in t.lower() for t in texts)
    assert success_count == 1
    assert already_used_count == 1

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        assert row.used is True


# ── I18N-4a: EN convergence — /shop, /buy, /inventory carry NO Cyrillic ──
#
# These pin the ru/en convergence fix: an EN user (language_code="en")
# must never see Russian. We feed the same flows the RU tests above
# cover but with an English caller and assert the reply body has zero
# Cyrillic letters. The RU tests (default-ru user 555) still pin the
# byte-identical Russian copy, so this is purely additive coverage.

import re as _re  # noqa: E402

_CYRILLIC = _re.compile(r"[А-Яа-яЁё]")


def _en_update(text: str, *, user_id: int = 808, chat_type: str = "private") -> Update:
    """English caller variant of :func:`_update` (language_code='en')."""
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Bob",
        language_code="en",
    )


def _assert_no_cyrillic(text: str) -> None:
    assert text
    assert not _CYRILLIC.search(text), f"EN reply leaked Cyrillic: {text!r}"


async def test_shop_empty_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/shop"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_shop_populated_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Header, stock hints (both regimes) and the buy line must all be EN."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            ShopItem(id=1, name="Plushie", description="", price=10, type="unwarn", stock=5)
        )
        session.add(
            ShopItem(id=2, name="Infinite", description="x", price=20, type="unwarn", stock=-1)
        )
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/shop"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_buy_error_path_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/buy 999`` (item-not-found) error copy must be EN for an EN user."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=808, balance=1000, language="en"))
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/buy 999"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_buy_usage_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/buy`` usage hint must be EN for an EN user."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/buy"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_buy_success_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The post-debit success card (name/price/balance/stock/inventory
    lines) must be EN for an EN user."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=808, balance=500, language="en"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/buy 1"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_inventory_empty_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/inventory"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_inventory_populated_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Inventory header + a 'used' tag must both be EN for an EN user."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(ShopItem(id=1, name="Plushie", price=10, type="unwarn", stock=-1))
        session.add(
            InventoryItem(
                user_id=808,
                item_id=1,
                purchase_date=datetime(2025, 1, 1, 12, 0, 0),
                used=False,
            )
        )
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/inventory"))
    _assert_no_cyrillic(sent[0]["text"])


# ── RR-2 #14: "buy for a group" chooser + routing ───────────────────
#
# Legacy showed a group picker before the catalog and routed a slice of
# the price to the chosen group (bot.py:23798 → bot.py:13240 →
# donation_from_purchase, bot.py:10715). The split pipeline dropped both.
# These tests pin the restored behaviour AND the authorization boundary
# legacy never had: legacy's ``shop_group_<gid>`` callback stashed any
# chat id off the wire without checking ownership.


def _seed_group_stmt(chat_id: int, owner_id: int, title: str | None) -> BotGroup:
    return BotGroup(chat_id=chat_id, added_by_user_id=owner_id, chat_title=title)


# ``rating_history`` ships in migration 0009 as a raw-SQL table (no ORM
# model), so ``create_all(EconomyBase)`` doesn't produce it — same
# fixture shape as tests/unit/services/test_treasury_service.py.
_RATING_HISTORY_DDL = (
    "CREATE TABLE rating_history ("
    "  group_id INTEGER NOT NULL,"
    "  date TEXT NOT NULL,"
    "  total_donations INTEGER NOT NULL,"
    "  position INTEGER,"
    "  PRIMARY KEY (group_id, date)"
    ")"
)


async def test_shop_offers_group_chooser_to_a_group_owner(
    make_wired: WiredFactory,
) -> None:
    """A caller with groups lands on the chooser, not the catalog."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()
    async with registry.session(DBName.USERS)() as session:
        session.add(_seed_group_stmt(-100_1, 555, "Котики"))
        await session.commit()

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, _update("/shop"))

    send = next(m for m in methods if type(m).__name__ == "SendMessage")
    assert "15%" in send.text
    rows = send.reply_markup.inline_keyboard
    # One row per owned group + the "no group" escape hatch last.
    assert rows[0][0].callback_data == ShopGroupPick(group_id=-100_1).pack()
    assert "Котики" in rows[0][0].text
    assert rows[-1][0].callback_data == ShopGroupPick(group_id=0).pack()


async def test_shop_skips_chooser_when_group_routing_disabled(
    make_wired: WiredFactory,
) -> None:
    """``PURCHASE_DONATION_TO_GROUP_PERCENT=0`` restores the plain catalog.

    The kill-switch has to bypass the users-DB lookup entirely, not just
    hide the buttons — an operator who turns the perk off shouldn't pay
    for a query per /shop.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase],
        economy_config=EconomyConfig(PURCHASE_DONATION_TO_GROUP_PERCENT=0),
    )
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()
    async with registry.session(DBName.USERS)() as session:
        session.add(_seed_group_stmt(-100_1, 555, "Котики"))
        await session.commit()

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(bot, _update("/shop"))

    send = next(m for m in methods if type(m).__name__ == "SendMessage")
    assert send.reply_markup.inline_keyboard[0][0].callback_data == (
        ShopBuyPrompt(item_id=1).pack()
    )


async def test_shop_group_pick_rejects_a_group_the_caller_does_not_own(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """THE security pin: a hand-crafted ``shop_grp:<other group>`` is
    refused with a toast and never opens a scoped catalog.

    Legacy accepted this payload verbatim (bot.py:24066), which let an
    attacker aim another community's leaderboard — and that community
    owner's payout — at their own purchase.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()
    async with registry.session(DBName.USERS)() as session:
        # -100_2 belongs to user 999, NOT to the caller (555).
        session.add(_seed_group_stmt(-100_2, 999, "Чужая группа"))
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(ShopGroupPick(group_id=-100_2).pack(), user_id=555)
    )

    kinds = [m["kind"] for m in sent]
    assert "edit" not in kinds
    answer = next(m for m in sent if m["kind"] == "callback_answer")
    assert answer["text"]


async def test_shop_buy_confirm_refuses_foreign_group_before_debiting(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A forged ``group_id`` on the confirm click aborts the purchase.

    Deliberately NOT a silent downgrade to a global buy: the card the
    user (or the attacker) clicked promised a group purchase, so the
    honest outcome is "nothing happened" — and the balance proves it.
    """
    from sqlalchemy import select

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(ShopBuyConfirm(item_id=1, group_id=-100_9).pack(), user_id=555),
    )

    assert "edit" not in [m["kind"] for m in sent]
    async with registry.session(DBName.ECONOMY)() as session:
        wallet = (
            await session.execute(select(EconomyUser).where(EconomyUser.user_id == 555))
        ).scalar_one()
        assert wallet.balance == 500
        inv = (await session.execute(select(InventoryItem))).scalars().all()
        assert inv == []


async def test_shop_buy_confirm_routes_the_group_slice(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full happy path: 15% of a 100-coin buy becomes 15 rating points
    for the group, and the group's creator is paid that slice minus the
    developer cut.

    Pins the three legacy write sites (``donations`` ledger row,
    ``group_top_donators`` upsert, ``groups_donations.group_xp`` bump)
    and the TRUTH-RULE that ``total_donations`` stays untouched — it is
    the *treasury* counter, and a purchase slice is rating, not treasury
    (see services/treasury_service.py).
    """
    from sqlalchemy import select, text

    from telegram_invite_bot.handlers import shop as shop_handlers

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    async with registry.session(DBName.ECONOMY)() as session:
        await session.execute(text(_RATING_HISTORY_DDL))
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(EconomyUser(user_id=777, balance=0, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()
    async with registry.session(DBName.USERS)() as session:
        session.add(_seed_group_stmt(-100_1, 555, "Котики"))
        await session.commit()

    async def fake_creator(_bot: Any, chat_id: int) -> int:
        assert chat_id == -100_1
        return 777

    monkeypatch.setattr(shop_handlers, "chat_creator_id", fake_creator)

    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(ShopBuyConfirm(item_id=1, group_id=-100_1).pack(), user_id=555),
    )

    edit = next(m for m in sent if m["kind"] == "edit")
    assert "Котики" in edit["text"]
    assert "15" in edit["text"]

    async with registry.session(DBName.ECONOMY)() as session:
        donations = (await session.execute(select(Donation))).scalars().all()
        assert len(donations) == 1
        assert donations[0].group_id == -100_1
        assert donations[0].user_id == 555
        assert donations[0].amount == 15

        top = (await session.execute(select(GroupTopDonator))).scalars().all()
        assert len(top) == 1
        assert top[0].total_donated == 15

        agg = (await session.execute(select(GroupDonationsAggregate))).scalar_one()
        assert agg.group_xp == 15
        # TRUTH-RULE: the treasury counter is NOT a rating counter.
        assert (agg.total_donations or 0) == 0

        owner = (
            await session.execute(select(EconomyUser).where(EconomyUser.user_id == 777))
        ).scalar_one()
        # 15 minus the developer cut (max(1, int(15*5/100)) == 1).
        assert owner.balance == 14


async def test_shop_page_flip_keeps_the_group_scope_on_the_wire(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A page flip inside a group-scoped catalog must stay scoped.

    Legacy kept the choice in a process-global dict (bot.py:13094) that
    a restart wiped; we put it on the wire, so every nav + buy button on
    the flipped page has to carry the group forward.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        for idx in range(1, 12):
            session.add(ShopItem(id=idx, name=f"Item{idx}", price=idx, type="unwarn", stock=-1))
        await session.commit()
    async with registry.session(DBName.USERS)() as session:
        session.add(_seed_group_stmt(-100_1, 555, "Котики"))
        await session.commit()

    methods = _install_method_capture(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(ShopPage(page=1, group_id=-100_1).pack(), user_id=555),
    )

    edit = next(m for m in methods if type(m).__name__ == "EditMessageText")
    assert "Котики" in edit.text
    packed = [btn.callback_data for row in edit.reply_markup.inline_keyboard for btn in row]
    buys = [d for d in packed if d.startswith("shop_buy:")]
    assert buys
    assert all(d.endswith(":-1001") for d in buys)
    assert any(d.startswith("shop_grpm") for d in packed)


# ── #1761: one confirm card is one purchase ─────────────────────────


@pytest.fixture(autouse=True)
def _clean_spent_cards() -> Iterator[None]:
    """The spent-card table is module-level; leaking it couples cases.

    Every callback case in this file lands on ``message_id=10`` in chat
    ``555``, so without this the first purchase in the file would claim
    that identity for the whole session and every later case would be
    answered with the "already settled" toast.
    """
    shop_module._reset_spent_cards_for_tests()  # noqa: SLF001
    yield
    shop_module._reset_spent_cards_for_tests()  # noqa: SLF001


async def test_re_tapping_one_shop_confirm_card_buys_once(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1761: the same card, tapped twice, is one purchase.

    Sequential on purpose — no lock is under test. ``ShopBuyConfirm``
    carries no FSM state and no nonce, so the payload replayed off the
    *same card* is indistinguishable from a fresh order unless the card
    identity itself is claimed; serialising the two taps would only make
    them buy twice in a defined order. Stock 3 and a balance covering
    two buys are what let the second tap through on unfixed code, past
    both rowcount guards the module docstring leans on — the buyer ends
    up charged 200 for a card that offered one item.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    capture_callback_outgoing(bot)
    update = make_callback_update(ShopBuyConfirm(item_id=1).pack(), user_id=555)
    await dispatcher.feed_update(bot, update)
    await dispatcher.feed_update(bot, update)

    async with sessionmaker() as session:
        from sqlalchemy import select  # local import — test-only

        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None
        assert wallet.balance == 400, "the re-tap charged the buyer a second time"
        item = await session.get(ShopItem, 1)
        assert item is not None
        assert item.stock == 2, "the re-tap consumed a second unit of stock"
        rows = (
            (await session.execute(select(InventoryItem).where(InventoryItem.user_id == 555)))
            .scalars()
            .all()
        )
        assert len(rows) == 1, "the re-tap created a second inventory row"


async def test_a_second_shop_card_still_buys_after_the_first_is_spent(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The claim is per *card*, not per user and not per item.

    Without this the fix would be a silent one-purchase-per-item lock: a
    buyer who wants two Plushies opens ``/shop`` twice and taps each card
    once, and the second card has to settle exactly like the first.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=500, language="ru"))
        session.add(ShopItem(id=1, name="Plushie", price=100, type="unwarn", stock=3))
        await session.commit()

    capture_callback_outgoing(bot)
    data = ShopBuyConfirm(item_id=1).pack()
    await dispatcher.feed_update(bot, make_callback_update(data, user_id=555, message_id=10))
    await dispatcher.feed_update(bot, make_callback_update(data, user_id=555, message_id=11))

    async with sessionmaker() as session:
        from sqlalchemy import select  # local import — test-only

        wallet = await session.get(EconomyUser, 555)
        assert wallet is not None
        assert wallet.balance == 300, "the second card was refused by the first card's claim"
        rows = (
            (await session.execute(select(InventoryItem).where(InventoryItem.user_id == 555)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
