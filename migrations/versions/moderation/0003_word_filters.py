"""moderation.db: add word_filters table (L-52)

Revision ID: 0003_word_filters
Revises: 0002_modlog_user_id_nullable
Create Date: 2026-06-10

Per-group banned-word filter for the new automod (L-52). Legacy keeps
this table in ``users.db`` with extra ``action``/``severity`` columns;
the new pipeline's automod is delete-only and gated on live Telegram
admin status, so only the columns it reads/writes are created here, in
``moderation.db`` (alongside warnings + the audit log).

``unique(group_id, word)`` makes "add the same word twice" a clean
no-op the repo detects before insert.

NOTE: chained to the moderation head ``0002_modlog_user_id_nullable``.
If this is linearised against another revision that also chains to
0002, that is expected — just re-point ``down_revision`` to the new
head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_word_filters"
down_revision: str | None = "0002_modlog_user_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "word_filters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("word", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("group_id", "word", name="uq_word_filters_group_word"),
    )
    op.create_index(
        "idx_word_filters_group", "word_filters", ["group_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_word_filters_group", table_name="word_filters")
    op.drop_table("word_filters")
