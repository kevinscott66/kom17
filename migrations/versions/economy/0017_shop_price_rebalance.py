"""economy: bring the shop catalog back in line with what users earn

Revision ID: 0017_shop_price_rebalance
Revises: 0016_withdrawal_alerted_at

The live catalog was rescaled by 10x at some point — prices and the
gift spans moved together — while the faucets it is meant to drain did
not move at all. A ``/daily`` claim pays ~17 coins at streak 1 and ~160
at the 30-day cap; a chat message pays 1, capped at 150 a day. Against
that, a warning removal at 800 was weeks of play and a one-shot x2
daily at 2000 cost more than a month of the bonus it doubles. The shop
is the economy's main sink, and a sink nobody can afford sinks nothing.

New prices, chosen per effect rather than by one factor, and set on
the rouble grid users actually think in (10 coins = 1 RUB at the 90
RUB/USD fallback of ``payments.rates``): 9, 19, 29, 49 and 99 RUB.

* buffs that pay coins back (``double_daily``, ``xp_boost``) are priced
  near what they return to an engaged user, so they are worth buying
  but cannot be farmed;
* moderation items (``unwarn`` 29 RUB, ``mute_protection`` 49 RUB) stay
  dearer than the buffs so buying out of a sanction is never trivial;
  cosmetics are a few days of active play;
* VIP is priced at roughly what its +1-per-message bonus returns over
  the 30 days to a chat regular (~30 messages a day), so it pays for
  itself for the people it is meant for;
* the two gifts keep their weighted shapes (``inventory_use_planner``
  remaps them onto the row's own span) and a ~20 % house edge, which
  stays under the 15 % group rebate that #1930 subtracts before the
  #1790 sink check — so a group-scoped buy still redeems.

Rows are matched on ``type`` plus the old ``price`` (and, for gifts,
the old ``data`` span), never on the name or id: a catalog the operator
has already repriced, or a fresh database seeded at legacy scale, does
not match and is left exactly as it is. A description is rewritten only
when it is still the one that quoted the old span.

Gifts are special because a gift's payout is read off the catalog row
at the moment it is USED, not when it was bought. Repricing a gift row
in place would shrink the prize of every gift already paid for at the
old price. So a gift row that still has unused inventory keeps its old
price and span, is taken off sale (``stock = 0`` — zero stock hides a
row from ``/shop`` without bricking what is already owned), and a new
row carrying the new values and the old stock goes on sale instead. A
gift row nobody holds is simply repriced in place.

Idempotent: once a new-values row exists for a spec, the spec is done.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, NamedTuple

import sqlalchemy as sa
from alembic import op

revision: str = "0017_shop_price_rebalance"
down_revision: str | None = "0016_withdrawal_alerted_at"
branch_labels = None
depends_on = None

_TABLE = "shop_items"
_INVENTORY = "inventory"


class _Reprice(NamedTuple):
    # A NamedTuple, not a dataclass: alembic executes this file without
    # registering it in ``sys.modules``, which dataclasses needs to
    # resolve postponed annotations.
    type: str
    old_price: int
    new_price: int
    # Gifts only: the payout span and the description that quotes it.
    old_data: dict[str, int] | None = None
    new_data: dict[str, int] | None = None
    old_description: str | None = None
    new_description: str | None = None


_SPECS: tuple[_Reprice, ...] = (
    _Reprice(
        type="luck",
        old_price=500,
        new_price=190,
        old_data={"min": 100, "max": 1000},
        new_data={"min": 30, "max": 400},
        old_description="🎲 Случайный приз от 100 до 1000 монет",
        new_description="🎲 Случайный приз от 30 до 400 🪙",
    ),
    _Reprice(
        type="luck",
        old_price=2500,
        new_price=990,
        old_data={"min": 1000, "max": 5000},
        new_data={"min": 350, "max": 2000},
        old_description="🎲 Случайный приз от 1000 до 5000 монет",
        new_description="🎲 Случайный приз от 350 до 2000 🪙",
    ),
    _Reprice(type="double_daily", old_price=2000, new_price=90),
    _Reprice(type="xp_boost", old_price=2500, new_price=90),
    _Reprice(type="color_nick", old_price=500, new_price=190),
    _Reprice(type="unwarn", old_price=800, new_price=290),
    _Reprice(type="mute_protection", old_price=1500, new_price=490),
    _Reprice(type="custom_title", old_price=3000, new_price=490),
    _Reprice(type="vip", old_price=5000, new_price=990),
)


def _now() -> str:
    return datetime.now(tz=UTC).replace(tzinfo=None).isoformat(sep=" ")


def _decode(raw: str | None) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return None


def _rows(
    bind: sa.Connection, spec: _Reprice, price: int, data: dict[str, int] | None
) -> list[Any]:
    rows = bind.execute(
        sa.text(
            f"SELECT id, name, description, price, stock, type, data "  # noqa: S608 — constant
            f"FROM {_TABLE} WHERE type = :type AND price = :price ORDER BY id"
        ),
        {"type": spec.type, "price": price},
    ).all()
    if data is None:
        return list(rows)
    return [row for row in rows if _decode(row.data) == data]


def _unused_entries(bind: sa.Connection, item_id: int) -> int:
    if _INVENTORY not in set(sa.inspect(bind).get_table_names()):
        return 0
    return int(
        bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {_INVENTORY} "  # noqa: S608 — constant
                "WHERE item_id = :id AND (used = 0 OR used IS NULL)"
            ),
            {"id": item_id},
        ).scalar_one()
    )


def _description(current: str | None, spec: _Reprice, *, forward: bool) -> str | None:
    old, new = (
        (spec.old_description, spec.new_description)
        if forward
        else (spec.new_description, spec.old_description)
    )
    return new if old is not None and current == old else current


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    for spec in _SPECS:
        if spec.new_data is not None and _rows(bind, spec, spec.new_price, spec.new_data):
            continue
        for row in _rows(bind, spec, spec.old_price, spec.old_data):
            if spec.new_data is not None and _unused_entries(bind, row.id) > 0:
                if row.stock == 0:
                    continue
                bind.execute(
                    sa.text(
                        f"INSERT INTO {_TABLE} "  # noqa: S608 — constant
                        "(name, description, price, stock, type, data, added, updated) "
                        "VALUES (:name, :description, :price, :stock, :type, :data, :now, :now)"
                    ),
                    {
                        "name": row.name,
                        "description": _description(row.description, spec, forward=True),
                        "price": spec.new_price,
                        "stock": row.stock,
                        "type": row.type,
                        "data": json.dumps(spec.new_data),
                        "now": _now(),
                    },
                )
                bind.execute(
                    sa.text(f"UPDATE {_TABLE} SET stock = 0, updated = :now WHERE id = :id"),  # noqa: S608
                    {"id": row.id, "now": _now()},
                )
                continue
            bind.execute(
                sa.text(
                    f"UPDATE {_TABLE} "  # noqa: S608 — constant
                    "SET price = :price, data = COALESCE(:data, data), "
                    "description = :description, updated = :now WHERE id = :id"
                ),
                {
                    "id": row.id,
                    "price": spec.new_price,
                    "data": json.dumps(spec.new_data) if spec.new_data is not None else None,
                    "description": _description(row.description, spec, forward=True),
                    "now": _now(),
                },
            )


def downgrade() -> None:
    """Put the old prices back, as far as that cannot take anything from a buyer.

    A gift that was split keeps both rows while the new one has been
    bought and not yet used — deleting it would orphan those entries —
    and is merged back (stock returned to the old row, new row deleted)
    once nobody holds it. Everything repriced in place is repriced back.
    """
    bind = op.get_bind()
    if _TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    for spec in _SPECS:
        for row in _rows(bind, spec, spec.new_price, spec.new_data):
            retired = [
                old
                for old in _rows(bind, spec, spec.old_price, spec.old_data)
                if spec.new_data is not None and old.stock == 0 and old.name == row.name
            ]
            if retired:
                if (
                    _INVENTORY in set(sa.inspect(bind).get_table_names())
                    and bind.execute(
                        sa.text(f"SELECT 1 FROM {_INVENTORY} WHERE item_id = :id LIMIT 1"),  # noqa: S608
                        {"id": row.id},
                    ).first()
                ):
                    continue
                bind.execute(
                    sa.text(f"UPDATE {_TABLE} SET stock = :stock, updated = :now WHERE id = :id"),  # noqa: S608
                    {"id": retired[0].id, "stock": row.stock, "now": _now()},
                )
                bind.execute(sa.text(f"DELETE FROM {_TABLE} WHERE id = :id"), {"id": row.id})  # noqa: S608
                continue
            bind.execute(
                sa.text(
                    f"UPDATE {_TABLE} "  # noqa: S608 — constant
                    "SET price = :price, data = COALESCE(:data, data), "
                    "description = :description, updated = :now WHERE id = :id"
                ),
                {
                    "id": row.id,
                    "price": spec.old_price,
                    "data": json.dumps(spec.old_data) if spec.old_data is not None else None,
                    "description": _description(row.description, spec, forward=False),
                    "now": _now(),
                },
            )
