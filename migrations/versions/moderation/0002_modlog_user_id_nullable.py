"""moderation.db: make moderation_log.user_id nullable (M-M-2)

Revision ID: 0002_modlog_user_id_nullable
Revises: 0001_baseline_moderation
Create Date: 2026-05-28

/pin and /unpin act on messages, not users; the previous ``NOT NULL``
constraint forced handlers to write ``user_id=0`` as a sentinel, which
collided with the ``/unpin`` sentinel and any future audit query that
filters on ``user_id=0``.  Making the column nullable lets the new
pipeline record ``NULL`` for target-less actions (M-M-2 fix).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_modlog_user_id_nullable"
down_revision: str | None = "0001_baseline_moderation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Relax ``moderation_log.user_id`` to nullable.

    SQLite's ``ALTER COLUMN`` is limited, so we use ``batch_alter_table``
    which transparently rebuilds the table with the new schema.
    """
    with op.batch_alter_table("moderation_log") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    """Restore ``NOT NULL`` (best-effort: any existing NULL rows would
    block this, but the prior baseline never produced NULL rows so the
    downgrade is safe in clean environments).
    """
    with op.batch_alter_table("moderation_log") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
