"""moderation.db: add antiflood columns to group_mod_config (L-56)

Revision ID: 0006_antiflood_config
Revises: 0005_welcome_config
Create Date: 2026-06-11

Per-group antiflood (message-burst limiter) configuration — four new
columns on the existing ``group_mod_config`` row (cluster F4):

* ``antiflood_enabled``   — 0 by default; legacy had no antiflood, so an
                            unconfigured group behaves exactly as before.
* ``flood_max_msgs``      — burst threshold (messages), default 5.
* ``flood_window_sec``    — sliding-window length (seconds), default 10.
* ``flood_mute_minutes``  — auto-mute duration on a tripped burst,
                            default 10.

Plain ``ADD COLUMN`` with server defaults (SQLite supports this without
a batch/table-rebuild). NOT NULL is safe because every column carries a
``server_default``.

NOTE: chained to the current moderation head ``0005_welcome_config``;
if another revision also claims 0006, linearize before deploying.
DO NOT apply manually.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_antiflood_config"
down_revision: str | None = "0005_welcome_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_mod_config",
        sa.Column(
            "antiflood_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "group_mod_config",
        sa.Column(
            "flood_max_msgs",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("5"),
        ),
    )
    op.add_column(
        "group_mod_config",
        sa.Column(
            "flood_window_sec",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
    )
    op.add_column(
        "group_mod_config",
        sa.Column(
            "flood_mute_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("group_mod_config") as batch:
        batch.drop_column("flood_mute_minutes")
        batch.drop_column("flood_window_sec")
        batch.drop_column("flood_max_msgs")
        batch.drop_column("antiflood_enabled")
