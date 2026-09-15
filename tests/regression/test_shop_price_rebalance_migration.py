"""``economy/0017_shop_price_rebalance`` against the catalog prod carries.

The revision rewrites operator data, not schema, so what matters is
which rows it touches and what it leaves alone. The fixture is the live
``shop_items`` table as read off prod (names, spans, stock) plus the
legacy ``inventory`` DDL, because the gift split depends on who still
holds an unused gift.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from telegram_invite_bot.core.entities.shop import ShopItemEntity
from telegram_invite_bot.services.inventory_use_planner import (
    InventoryEffectKind,
    plan_effect_application,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_REVISION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "economy"
    / "0017_shop_price_rebalance.py"
)

_LEGACY_DDL = (
    """
    CREATE TABLE shop_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        price INTEGER NOT NULL,
        stock INTEGER DEFAULT -1,
        type TEXT NOT NULL,
        data TEXT,
        added TIMESTAMP,
        updated TIMESTAMP
    )
    """,
    """
    CREATE TABLE inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        purchase_date TIMESTAMP NOT NULL,
        used BOOLEAN DEFAULT 0,
        used_date TIMESTAMP,
        expires TIMESTAMP, group_id INTEGER,
        UNIQUE(user_id, item_id, purchase_date)
    )
    """,
)

# (id, name, description, price, stock, type, data) — prod, 2026-09-11.
_PROD_CATALOG: tuple[tuple[int, str, str, int, int, str, str], ...] = (
    (1, "🌈 Цветной ник", "Твой ник будет светиться радугой 7 дней", 500, 0, "color_nick",
     '{"duration": 7, "color": "rainbow"}'),
    (2, "🎨 Свой цвет", "Выбери свой цвет ника (отправь HEX код)", 1000, 0, "custom_color",
     '{"duration": 30}'),
    (3, "🎁 Секретный подарок", "🎲 Случайный приз от 100 до 1000 монет", 500, 82, "luck",
     '{"min": 100, "max": 1000}'),
    (4, "🎁 Большой подарок", "🎲 Случайный приз от 1000 до 5000 монет", 2500, 50, "luck",
     '{"min": 1000, "max": 5000}'),
    (5, "👑 VIP статус", "VIP на 30 дней", 5000, -1, "vip",
     '{"duration": 30, "message_bonus": 1, "daily_bonus_percent": 15, "tax_discount_percent": 50}'),
    (6, "💎 Легендарный статус", "Навсегда в истории чата", 10000, 0, "legend", "{}"),
    (7, "📢 Реклама", "Закреп твоего поста на 24 часа", 3000, 0, "ad", '{"duration": 24}'),
    (8, "🛡️ Снятие предупреждения", "Снимает одно предупреждение", 800, 40, "unwarn", "{}"),
    (9, "🔇 Защита от мута", "Защищает от мута на 24 часа", 1500, 30, "mute_protection",
     '{"duration": 24}'),
    (10, "✨ Двойной daily", "Следующий daily бонус будет x2", 2000, 20, "double_daily", "{}"),
    (11, "⚡ Ускорение", "x2 монеты за сообщения на 1 час", 2500, 6, "xp_boost",
     '{"duration": 60, "multiplier": 2}'),
    (12, "📝 Свой титул", "Персональный титул под ником на 7 дней", 3000, 0, "custom_title",
     '{"duration": 7}'),
)  # fmt: skip

_NEW_PRICES = {
    1: 190,
    2: 1000,
    5: 990,
    6: 10000,
    7: 3000,
    8: 290,
    9: 490,
    10: 90,
    11: 90,
    12: 490,
}


def _load_revision() -> Any:
    """Import the revision by path — its filename starts with a digit.

    Deliberately NOT registered in ``sys.modules``: alembic loads revision
    files the same way, and a module-level ``@dataclass`` crashes there.
    """
    spec = importlib.util.spec_from_file_location("_rev_econ_0017", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'economy.db'}")
    with eng.begin() as conn:
        for statement in _LEGACY_DDL:
            conn.execute(sa.text(statement))
        for row in _PROD_CATALOG:
            conn.execute(
                sa.text(
                    "INSERT INTO shop_items (id, name, description, price, stock, type, data) "
                    "VALUES (:id, :name, :description, :price, :stock, :type, :data)"
                ),
                dict(
                    zip(
                        ("id", "name", "description", "price", "stock", "type", "data"),
                        row,
                        strict=True,
                    )
                ),
            )
    yield eng
    eng.dispose()


def _run(engine: sa.Engine, name: str) -> None:
    module = _load_revision()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, name)()


def _hold(engine: sa.Engine, item_id: int, *, used: bool, user_id: int = 1) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO inventory (user_id, item_id, purchase_date, used) "
                "VALUES (:user_id, :item_id, :date, :used)"
            ),
            {
                "user_id": user_id,
                "item_id": item_id,
                "date": f"2026-03-19 0{user_id}:00:00",
                "used": used,
            },
        )


def _catalog(engine: sa.Engine) -> dict[int, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT id, name, description, price, stock, type, data FROM shop_items")
        ).mappings()
        return {row["id"]: dict(row) for row in rows}


def test_every_sellable_price_drops_and_the_unsellable_rows_are_untouched(
    engine: sa.Engine,
) -> None:
    _run(engine, "upgrade")
    catalog = _catalog(engine)
    for item_id, price in _NEW_PRICES.items():
        assert catalog[item_id]["price"] == price, catalog[item_id]["name"]
    # Stock and non-gift ``data`` are catalog decisions this revision does not make.
    for item_id, name, _, _, stock, _, data in _PROD_CATALOG:
        if item_id not in (3, 4):
            assert catalog[item_id]["stock"] == stock, name
            assert catalog[item_id]["data"] == data, name


def test_an_unheld_gift_is_repriced_in_place_with_its_span(engine: sa.Engine) -> None:
    _run(engine, "upgrade")
    catalog = _catalog(engine)
    assert len(catalog) == len(_PROD_CATALOG)
    assert catalog[3]["price"] == 190
    assert json.loads(catalog[3]["data"]) == {"min": 30, "max": 400}
    assert catalog[3]["description"] == "🎲 Случайный приз от 30 до 400 🪙"
    assert catalog[3]["stock"] == 82
    assert catalog[4]["price"] == 990
    assert json.loads(catalog[4]["data"]) == {"min": 350, "max": 2000}
    assert catalog[4]["description"] == "🎲 Случайный приз от 350 до 2000 🪙"


def test_a_gift_someone_still_holds_keeps_its_prize_and_leaves_the_shelf(
    engine: sa.Engine,
) -> None:
    """A gift pays out off the row at use time, so the paid-for prize must survive."""
    _hold(engine, 3, used=False)
    _hold(engine, 3, used=True, user_id=2)
    _run(engine, "upgrade")
    catalog = _catalog(engine)

    old = catalog[3]
    assert (old["price"], old["stock"]) == (500, 0)
    assert json.loads(old["data"]) == {"min": 100, "max": 1000}
    assert old["description"] == "🎲 Случайный приз от 100 до 1000 монет"

    (new,) = [row for row in catalog.values() if row["id"] > len(_PROD_CATALOG)]
    assert (new["name"], new["type"], new["price"], new["stock"]) == (old["name"], "luck", 190, 82)
    assert json.loads(new["data"]) == {"min": 30, "max": 400}
    assert new["description"] == "🎲 Случайный приз от 30 до 400 🪙"


def test_a_second_upgrade_changes_nothing(engine: sa.Engine) -> None:
    _hold(engine, 3, used=False)
    _run(engine, "upgrade")
    first = _catalog(engine)
    _run(engine, "upgrade")
    second = _catalog(engine)
    assert first == second


def test_a_hand_repriced_row_is_left_to_the_operator(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE shop_items SET price = 700 WHERE id = 8"))
    _run(engine, "upgrade")
    assert _catalog(engine)[8]["price"] == 700


@pytest.mark.parametrize("held", [False, True])
def test_downgrade_restores_the_prod_catalog(engine: sa.Engine, held: bool) -> None:
    if held:
        _hold(engine, 3, used=False)
    before = _catalog(engine)
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    _run(engine, "downgrade")
    after = _catalog(engine)
    assert after == before


def test_downgrade_keeps_a_new_gift_somebody_bought(engine: sa.Engine) -> None:
    _hold(engine, 3, used=False)
    _run(engine, "upgrade")
    new_id = max(_catalog(engine))
    _hold(engine, new_id, used=False, user_id=3)
    _run(engine, "downgrade")
    catalog = _catalog(engine)
    assert catalog[new_id]["price"] == 190
    assert catalog[3]["stock"] == 0


def test_the_upgrade_is_a_no_op_without_the_table(tmp_path: Path) -> None:
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'empty.db'}")
    try:
        _run(eng, "upgrade")
        _run(eng, "downgrade")
    finally:
        eng.dispose()


@pytest.mark.parametrize("group_rebate_percent", [0, 15])
def test_the_new_gifts_are_still_sinks_the_planner_will_redeem(
    engine: sa.Engine, group_rebate_percent: int
) -> None:
    """#1790 refuses a gift whose mean payout beats its price net of the group rebate."""
    _run(engine, "upgrade")
    for row in _catalog(engine).values():
        if row["type"] != "luck":
            continue
        item = ShopItemEntity(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            price=row["price"],
            type=row["type"],
            stock=row["stock"],
            data=json.loads(row["data"]),
        )
        plan = plan_effect_application(
            item, now=datetime(2026, 9, 14), group_rebate_percent=group_rebate_percent
        )
        assert plan.kind is InventoryEffectKind.LUCK_COIN_PAYOUT, row["name"]
        assert plan.coin_payout is not None
        assert plan.coin_payout.expected_payout() <= row["price"] * 0.81, row["name"]
