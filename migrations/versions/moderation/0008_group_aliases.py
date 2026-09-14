"""moderation.db: add group_aliases table (L-60)

Revision ID: 0008_group_aliases
Revises: 0007_captcha_config
Create Date: 2026-06-11

Per-group dynamic command aliases (cluster H3). Legacy keeps these in
the singleton ``settings["command_aliases"]`` JSON dict (bot.py:42073),
owner-only and global; L-60 re-scopes them per group with group-admin
management, so they get a real table in ``moderation.db`` alongside the
other per-group admin config.

``unique(group_id, word)`` mirrors the legacy one-word-one-command dict
semantics (re-adding a word overwrites its mapping, bot.py:42112).

NOTE: chained to the current moderation head ``0007_captcha_config``;
if another revision also claims 0008, linearize before deploying.
WRITE-only — DO NOT apply manually.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_group_aliases"
down_revision: str | None = "0007_captcha_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_aliases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("word", sa.Text(), nullable=False),
        sa.Column("target_command", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("group_id", "word", name="uq_group_aliases_group_word"),
    )
    op.create_index(
        "idx_group_aliases_group", "group_aliases", ["group_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_group_aliases_group", table_name="group_aliases")
    op.drop_table("group_aliases")
