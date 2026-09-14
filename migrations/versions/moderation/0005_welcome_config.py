"""moderation.db: add welcome_config table (L-57)

Revision ID: 0005_welcome_config
Revises: 0004_group_mod_config
Create Date: 2026-06-10

Per-group custom welcome template + on/off toggle (L-57, extends
FEAT-WELCOME). One row per group:

* ``group_id`` — PK (the group chat id; BigInteger since supergroup ids
  exceed 32-bit range).
* ``template`` — nullable admin-supplied template text (``{user}`` /
  ``{chat}`` placeholders); ``NULL`` falls back to the default card.
* ``enabled`` — 1 by default; ``/welcome_off`` flips it to 0.

NOTE: the chain is linearised if several revisions hang off the
``0002_modlog_user_id_nullable`` head — this migration only declares
``down_revision = "0002_modlog_user_id_nullable"`` and expects that.
DO NOT apply manually.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_welcome_config"
down_revision: str | None = "0004_group_mod_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "welcome_config",
        sa.Column("group_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("template", sa.Text(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    op.drop_table("welcome_config")
