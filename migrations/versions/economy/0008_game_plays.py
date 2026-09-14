"""economy: game_plays — persistent game anti-abuse counter (L-25)

Revision ID: 0008_game_plays
Revises: 0007_promo_codes
Create Date: 2026-06-10

Adds the net-new ``game_plays`` table backing the per-user game
anti-abuse caps (cooldown 180s + 8/hour + 25/day). Legacy enforced these
by ``COUNT(*) FROM games`` (bot.py:14438); the new pipeline had them only
in-memory (resets on redeploy, per-process). Persisting one append-only
stamp per completed play makes the caps survive restarts and shared
across workers.

Absent from the prod dump (net-new), so this is a plain CREATE TABLE with
no data-migration / dedup caveat. See ``db/models/game_limits.py`` for the
ORM mapping ``create_all`` uses in tests. This migration is not applied
to any DB from here — the deploy runbook owns ``alembic upgrade``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_game_plays"
down_revision: str | None = "0007_promo_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "game_plays",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("game", sa.String(), nullable=False),
        sa.Column("played_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_game_plays_user_played", "game_plays", ["user_id", "played_at"])
    op.create_index("idx_game_plays_played", "game_plays", ["played_at"])


def downgrade() -> None:
    op.drop_index("idx_game_plays_played", table_name="game_plays")
    op.drop_index("idx_game_plays_user_played", table_name="game_plays")
    op.drop_table("game_plays")
