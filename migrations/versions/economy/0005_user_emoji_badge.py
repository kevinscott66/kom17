"""economy: user_emoji_badge — VIP cosmetic emoji badge (#25)

Revision ID: 0005_user_emoji_badge
Revises: 0004_check_claims_unique
Create Date: 2026-06-05

The #25 "custom emoji" feature is a cosmetic badge the bot stores and
renders next to a VIP's display name (NOT a Telegram premium
``custom_emoji`` entity — see ``docs/CUSTOM_EMOJI_VIP_SPEC.md``). This
adds the net-new ``user_emoji_badge`` table: one row per user holding
their currently-equipped badge (``emoji``) and when they set it
(``set_at``). ``user_id`` is the PRIMARY KEY — a user has at most one
equipped badge.

The table is absent from the prod dump (it is net-new), so this is a
plain CREATE TABLE with no data-migration / dedup caveat. The
integration harness (``tests/integration/test_alembic_cli.py``)
exercises it on a fresh sqlite file. See
``db/models/economy.py:UserEmojiBadge`` for the ORM mapping that
``create_all`` uses in tests.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_user_emoji_badge"
down_revision: str | None = "0004_check_claims_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_emoji_badge",
        sa.Column("user_id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("emoji", sa.String(), nullable=False),
        sa.Column("set_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("user_emoji_badge")
