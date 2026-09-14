"""moderation.db: add coins_enabled column to group_mod_config (L-54)

Revision ID: 0010_group_coins_toggle
Revises: 0009_rank_tables
Create Date: 2026-06-12

Per-group economy earn toggle — one new column on the existing
``group_mod_config`` row (cluster T3):

* ``coins_enabled`` — 1 by default; legacy gated the passive
  per-message coin reward only by the GLOBAL ``coins_enabled``
  setting (``bot.py:43808``), so an unconfigured group must keep
  earning exactly as before.

Plain ``ADD COLUMN`` with a server default (SQLite supports this
without a batch/table-rebuild). NOT NULL is safe because the column
carries a ``server_default``.

NOTE: chained to the current moderation head ``0009_rank_tables``;
if another revision also claims 0010, linearize before deploying.
DO NOT apply manually.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_group_coins_toggle"
down_revision: str | None = "0009_rank_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_mod_config",
        sa.Column(
            "coins_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("group_mod_config") as batch:
        batch.drop_column("coins_enabled")
