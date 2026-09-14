"""moderation: group_mod_config — per-group moderation config (L-43)

Revision ID: 0004_group_mod_config
Revises: 0003_word_filters
Create Date: 2026-06-10

Net-new table holding per-group moderation overrides. ``group_id`` is the
PRIMARY KEY (one config row per group). Every column ships with a server
default matching the moderation pipeline's hardcoded global values, so a
group that never ran ``/modcfg`` (and therefore has no row) is
indistinguishable from one whose row sits at the defaults:

    automod_enabled   DEFAULT 1   (legacy auto_moderate)
    profanity_enabled DEFAULT 1   (legacy profanity_enabled)
    max_warns         DEFAULT 3   (handlers.moderation.WARNING_THRESHOLD)
    mute_minutes      DEFAULT 1440 (legacy mute_duration 24h, in minutes)
    autoban_enabled   DEFAULT 1   (legacy auto_ban_on_max_warnings)

The table is absent from the prod dump, so this is a plain CREATE TABLE
with no data-migration caveat. See
``db/models/group_mod_config.py:GroupModConfig`` for the ORM mapping that
``create_all`` uses in tests.

Chained to the moderation head ``0002_modlog_user_id_nullable``. If
another revision also chains to 0002, the branch is linearised before
deploy — expected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_group_mod_config"
down_revision: str | None = "0003_word_filters"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_mod_config",
        sa.Column("group_id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column(
            "automod_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "profanity_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "max_warns",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column(
            "mute_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1440"),
        ),
        sa.Column(
            "autoban_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )


def downgrade() -> None:
    op.drop_table("group_mod_config")
