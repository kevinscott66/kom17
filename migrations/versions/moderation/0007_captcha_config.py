"""moderation.db: add captcha columns to group_mod_config (L-55)

Revision ID: 0007_captcha_config
Revises: 0006_antiflood_config
Create Date: 2026-06-11

Per-group join-captcha configuration — two new columns on the existing
``group_mod_config`` row (cluster G2):

* ``captcha_enabled``     — 0 by default; captcha has no legacy
                            counterpart at all, so an unconfigured group
                            behaves exactly as before (no captcha).
* ``captcha_timeout_sec`` — how long a newcomer has to press the
                            "I'm not a bot" button before being
                            kicked, default 120 seconds.

Plain ``ADD COLUMN`` with server defaults (SQLite supports this without
a batch/table-rebuild). NOT NULL is safe because both columns carry a
``server_default``.

NOTE: chained to the current moderation head ``0006_antiflood_config``;
if another revision also claims 0007, linearize before deploying.
DO NOT apply manually.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_captcha_config"
down_revision: str | None = "0006_antiflood_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_mod_config",
        sa.Column(
            "captcha_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "group_mod_config",
        sa.Column(
            "captcha_timeout_sec",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("120"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("group_mod_config") as batch:
        batch.drop_column("captcha_timeout_sec")
        batch.drop_column("captcha_enabled")
