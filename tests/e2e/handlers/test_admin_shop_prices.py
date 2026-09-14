"""End-to-end ``/admin_shop_prices`` (Stage 25).

Pins:

* Non-developer → silent drop (enumeration defence).
* Developer in a group → also silent (price-leak defence + same
  enumeration posture as ``/admin_botstats``).
* Developer in DM → catalog rendered, sorted by id.
* ``stock == -1`` and ``stock IS NULL`` both render as ``"∞"`` —
  the prod sentinel for infinite stock plus a defensive coding
  branch for legacy NULL rows.
* Empty catalog renders an explicit "no items" line (not a blank
  reply) so a fresh deploy doesn't look like a handler bug.
* HTML in item names is escaped (admin-typed catalog input is the
  attack surface).
* #2006: a row whose ``type`` nothing can activate is still listed —
  this is the operator's view of the whole table — and carries the
  marker saying why ``/shop`` does not show it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import ShopItem
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed_items(
    registry: EngineRegistry,
    rows: list[tuple[int, str, int, int | None]],  # (id, name, price, stock)
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for item_id, name, price, stock in rows:
            session.add(ShopItem(id=item_id, name=name, price=price, stock=stock, type="unwarn"))
        await session.commit()


@pytest.mark.asyncio
async def test_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/admin_shop_prices", user_id=42))
    assert sent == []


@pytest.mark.asyncio
async def test_silent_in_group_for_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_shop_prices", user_id=555, chat_id=-100, chat_type="supergroup"
        ),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_for_developer_sorted_by_id(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=777),
    )
    # Insert out of order; renderer must sort by id ASC.
    await _seed_items(
        registry,
        rows=[
            (3, "Gamma", 30, 5),
            (1, "Alpha", 10, -1),
            (2, "Beta", 20, None),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/admin_shop_prices", user_id=777))
    assert len(sent) == 1
    body = sent[0]["text"]
    assert body.index("Alpha") < body.index("Beta") < body.index("Gamma")
    # Prices visible.
    assert "10" in body and "20" in body and "30" in body
    # Infinite-stock sentinels rendered as ∞ for both -1 and NULL.
    assert body.count("∞") >= 2
    # Numeric stock still renders.
    assert "5" in body


@pytest.mark.asyncio
async def test_renders_empty_catalog(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=888),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/admin_shop_prices", user_id=888))
    assert len(sent) == 1
    # Distinct "no items" line — not a blank string.
    assert "Товаров нет" in sent[0]["text"]


@pytest.mark.asyncio
async def test_card_answers_in_the_developers_language(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Operators are users too, and the developer roster is not
    guaranteed Russian-speaking. Both branches — the title and the
    "no items" early return — carried their own literal.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=222),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_shop_prices", user_id=222, language_code="en")
    )
    empty = sent[0]["text"]
    assert "No items" in empty
    assert not any("Ѐ" <= ch <= "ӿ" for ch in empty), empty

    await _seed_items(registry, rows=[(1, "Alpha", 10, -1)])
    sent.clear()
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_shop_prices", user_id=222, language_code="en", message_id=2, update_id=2
        ),
    )
    body = sent[0]["text"]
    assert "Items and prices" in body
    # The rows themselves still carry the catalog, not just the chrome.
    assert "Alpha" in body and "10" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


@pytest.mark.asyncio
async def test_marks_a_row_the_shop_will_not_sell(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """#2006 hides unusable types from ``/shop`` and refuses to sell
    them. This view is the operator's only window into that decision:
    without the marker, an item an admin just added would simply never
    appear to anyone and nothing would say why. So the row stays listed
    — the operator sees the whole table — and gains a line naming the
    offending ``type``.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=777),
    )
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        session.add(ShopItem(id=1, name="Sellable", price=10, stock=-1, type="unwarn"))
        session.add(ShopItem(id=2, name="Legend", price=2000, stock=10, type="legend"))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/admin_shop_prices", user_id=777))
    body = sent[0]["text"]
    # Both rows listed — the operator view does not filter.
    assert "Sellable" in body
    assert "Legend" in body
    # Exactly one marker, on the row that earned it, naming the type.
    assert body.count("не активируется") == 1
    assert "<code>legend</code>" in body
    assert body.index("Legend") < body.index("не активируется")


@pytest.mark.asyncio
async def test_escapes_html_in_item_name(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=111),
    )
    await _seed_items(registry, rows=[(1, "<b>spoof</b>", 5, -1)])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/admin_shop_prices", user_id=111))
    body = sent[0]["text"]
    assert "<b>spoof</b>" not in body
    assert "&lt;b&gt;spoof&lt;/b&gt;" in body
