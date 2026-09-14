"""economy: P2P marketplace — rebuild orders/trades, add seller stats (#64)

Revision ID: 0010_p2p
Revises: 0009_donations_rating_writeside
Create Date: 2026-06-11

Schema per DESIGN_P2P.md §2.1. Prod ``economy.db`` ALREADY carries
``p2p_sell_orders`` / ``p2p_trades`` in the legacy shape (created by the
legacy ``CREATE TABLE IF NOT EXISTS`` at bot.py:5175-5218; see
``docs/prod_schemas.sql:521``) with dead fields the design does not
port:

* orders: ``expires_at`` (never enforced), ``completed_count``,
  ``total_sold`` (never read);
* trades: ``payment_method``, ``payment_details``, ``dispute_reason``,
  ``escrow_release_tx`` (all write-only or never written).

SQLite can't reshape in place cleanly, so each table is REBUILT:
create ``<name>__new`` with the target shape, copy the live columns
(``dispute_resolved_by/At`` → ``resolved_by/At``), drop the old table,
rename. Existing TEXT ``CURRENT_TIMESTAMP`` values copy into the
DATETIME columns unchanged (SQLite dynamic typing; same ISO format the
new pipeline writes). On a FRESH database (no legacy tables) the
rebuild degrades to a plain create — the inspector branch below.

``p2p_seller_stats`` is NET-NEW (legacy kept the counters inside
``user_withdrawal_limits``; see ``db/models/p2p.py`` for why we don't).
Existing prod counters are seeded from ``user_withdrawal_limits`` when
that table exists, mapping ``total_withdrawn_com`` → ``total_sold_com``
(only rows with any non-zero counter, so the table stays tiny).

This migration is not applied from here — the deploy runbook owns
``alembic upgrade``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Callable

revision: str = "0010_p2p"
down_revision: str | None = "0009_donations_rating_writeside"
branch_labels = None
depends_on = None


def _create_orders_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("amount_com", sa.Integer(), nullable=False),
        sa.Column("remaining_com", sa.Integer(), nullable=False),
        sa.Column("price_per_com", sa.Float(), nullable=False),
        sa.Column("fiat_currency", sa.Text(), nullable=False),
        sa.Column("payment_methods", sa.Text(), nullable=True),
        sa.Column("min_amount", sa.Integer(), nullable=True),
        sa.Column("max_amount", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )


def _create_trades_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("buyer_id", sa.Integer(), nullable=False),
        sa.Column("amount_com", sa.Integer(), nullable=False),
        sa.Column("price_per_com", sa.Float(), nullable=False),
        sa.Column("total_fiat", sa.Float(), nullable=False),
        sa.Column("fiat_currency", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )


_ORDER_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("idx_p2p_sell_orders_user", ["user_id"]),
    ("idx_p2p_sell_orders_book", ["status", "fiat_currency", "price_per_com"]),
)

_TRADE_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("idx_p2p_trades_order", ["order_id"]),
    ("idx_p2p_trades_buyer", ["buyer_id"]),
    ("idx_p2p_trades_seller", ["seller_id"]),
    ("idx_p2p_trades_status", ["status"]),
)

# Legacy index names that must be dropped with the legacy tables so the
# rename can recreate ours without a name collision.
_LEGACY_ORDER_INDEXES = ("idx_p2p_sell_orders_user", "idx_p2p_sell_orders_status")
_LEGACY_TRADE_INDEXES = (
    "idx_p2p_trades_order",
    "idx_p2p_trades_seller",
    "idx_p2p_trades_buyer",
)


def _rebuild(
    name: str,
    create_table: Callable[[str], None],
    copy_select: str,
    legacy_indexes: tuple[str, ...],
    new_indexes: tuple[tuple[str, list[str]], ...],
    *,
    exists: bool,
) -> None:
    """Create ``name`` in the target shape, migrating legacy rows if any.

    ``copy_select`` lists the legacy columns in the new column order
    (including the renames) — executed only when the legacy table
    exists. Index drops precede the table drop because SQLite keeps
    index names global per database file.
    """
    if not exists:
        create_table(name)
        for index_name, cols in new_indexes:
            op.create_index(index_name, name, cols)
        return

    tmp = f"{name}__new"
    create_table(tmp)
    op.execute(f"INSERT INTO {tmp} SELECT {copy_select} FROM {name}")  # noqa: S608
    for index_name in legacy_indexes:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    op.drop_table(name)
    op.rename_table(tmp, name)
    for index_name, cols in new_indexes:
        op.create_index(index_name, name, cols)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    _rebuild(
        "p2p_sell_orders",
        _create_orders_table,
        "id, user_id, amount_com, remaining_com, price_per_com, fiat_currency,"
        " payment_methods, min_amount, max_amount, status, created_at",
        _LEGACY_ORDER_INDEXES,
        _ORDER_INDEXES,
        exists="p2p_sell_orders" in existing,
    )

    _rebuild(
        "p2p_trades",
        _create_trades_table,
        "id, order_id, seller_id, buyer_id, amount_com, price_per_com,"
        " total_fiat, fiat_currency, status, created_at, paid_at, confirmed_at,"
        " dispute_resolved_by, dispute_resolved_at",
        _LEGACY_TRADE_INDEXES,
        _TRADE_INDEXES,
        exists="p2p_trades" in existing,
    )

    if "p2p_seller_stats" not in existing:
        op.create_table(
            "p2p_seller_stats",
            sa.Column("user_id", sa.Integer(), primary_key=True),
            sa.Column(
                "successful_trades", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "total_sold_com", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "dispute_count", sa.Integer(), nullable=False, server_default="0"
            ),
        )
        # Seed from the legacy counters so prod sellers keep their
        # reputation. total_withdrawn_com was only ever written by the
        # P2P confirm paths (bot.py:20038/20287), so it IS total-sold.
        if "user_withdrawal_limits" in existing:
            op.execute(
                "INSERT INTO p2p_seller_stats"
                " (user_id, successful_trades, total_sold_com, dispute_count)"
                " SELECT user_id, COALESCE(successful_trades, 0),"
                "        COALESCE(total_withdrawn_com, 0), COALESCE(dispute_count, 0)"
                " FROM user_withdrawal_limits"
                " WHERE COALESCE(successful_trades, 0) != 0"
                "    OR COALESCE(total_withdrawn_com, 0) != 0"
                "    OR COALESCE(dispute_count, 0) != 0"
            )


def downgrade() -> None:
    """Restore the legacy table shapes (dead columns come back NULL/0).

    Lossy only for rows that used the NEW trade statuses
    (``dispute_returned_seller`` / ``cancelled_timeout``) — those status
    strings are kept verbatim (legacy ignores unknown statuses) so no
    row is dropped.
    """
    op.drop_table("p2p_seller_stats")

    # trades: rebuild back into the legacy shape.
    op.create_table(
        "p2p_trades__legacy",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("buyer_id", sa.Integer(), nullable=False),
        sa.Column("amount_com", sa.Integer(), nullable=False),
        sa.Column("price_per_com", sa.Float(), nullable=False),
        sa.Column("total_fiat", sa.Float(), nullable=False),
        sa.Column("fiat_currency", sa.Text(), nullable=False),
        sa.Column("payment_method", sa.Text(), nullable=True),
        sa.Column("payment_details", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True, server_default="pending"),
        sa.Column("created_at", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.Text(), nullable=True),
        sa.Column("dispute_reason", sa.Text(), nullable=True),
        sa.Column("dispute_resolved_by", sa.Integer(), nullable=True),
        sa.Column("dispute_resolved_at", sa.Text(), nullable=True),
        sa.Column("escrow_release_tx", sa.Text(), nullable=True),
    )
    op.execute(
        "INSERT INTO p2p_trades__legacy"
        " (id, order_id, seller_id, buyer_id, amount_com, price_per_com,"
        "  total_fiat, fiat_currency, status, created_at, paid_at, confirmed_at,"
        "  dispute_resolved_by, dispute_resolved_at)"
        " SELECT id, order_id, seller_id, buyer_id, amount_com, price_per_com,"
        "        total_fiat, fiat_currency, status, created_at, paid_at,"
        "        confirmed_at, resolved_by, resolved_at"
        " FROM p2p_trades"
    )
    for index_name, _cols in _TRADE_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    op.drop_table("p2p_trades")
    op.rename_table("p2p_trades__legacy", "p2p_trades")
    op.create_index("idx_p2p_trades_order", "p2p_trades", ["order_id"])
    op.create_index("idx_p2p_trades_seller", "p2p_trades", ["seller_id"])
    op.create_index("idx_p2p_trades_buyer", "p2p_trades", ["buyer_id"])

    # orders: rebuild back into the legacy shape.
    op.create_table(
        "p2p_sell_orders__legacy",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("amount_com", sa.Integer(), nullable=False),
        sa.Column("remaining_com", sa.Integer(), nullable=False),
        sa.Column("price_per_com", sa.Float(), nullable=False),
        sa.Column("fiat_currency", sa.Text(), nullable=False),
        sa.Column("payment_methods", sa.Text(), nullable=True),
        sa.Column("min_amount", sa.Integer(), nullable=True),
        sa.Column("max_amount", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True, server_default="active"),
        sa.Column("created_at", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column("completed_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("total_sold", sa.Integer(), nullable=True, server_default="0"),
    )
    op.execute(
        "INSERT INTO p2p_sell_orders__legacy"
        " (id, user_id, amount_com, remaining_com, price_per_com, fiat_currency,"
        "  payment_methods, min_amount, max_amount, status, created_at)"
        " SELECT id, user_id, amount_com, remaining_com, price_per_com,"
        "        fiat_currency, payment_methods, min_amount, max_amount, status,"
        "        created_at"
        " FROM p2p_sell_orders"
    )
    for index_name, _cols in _ORDER_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    op.drop_table("p2p_sell_orders")
    op.rename_table("p2p_sell_orders__legacy", "p2p_sell_orders")
    op.create_index("idx_p2p_sell_orders_user", "p2p_sell_orders", ["user_id"])
    op.create_index("idx_p2p_sell_orders_status", "p2p_sell_orders", ["status"])
