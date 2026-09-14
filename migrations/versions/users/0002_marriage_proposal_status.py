"""marriage_proposals: add status column (R-FIX-010)

Revision ID: 0002_marriage_proposal_status
Revises: 0001_baseline_users
Create Date: 2026-05-27

Adds a ``status`` column to ``marriage_proposals`` so the accept/decline
flow can claim a row atomically and prevent the double-accept race
(slash /marry_accept + inline button arriving in parallel).

SQLite requires :func:`op.batch_alter_table` for ``ALTER TABLE ... ADD
COLUMN`` with a ``DEFAULT`` clause; the batch helper rebuilds the table
under the hood. All existing rows are backfilled to ``'pending'`` via
``server_default``, which matches their semantics on disk today (any row
visible in the table is, by construction, an unresolved proposal —
legacy DELETEs the row on accept/decline).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_marriage_proposal_status"
down_revision: str | None = "0001_baseline_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("marriage_proposals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(),
                nullable=False,
                server_default="pending",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("marriage_proposals") as batch_op:
        batch_op.drop_column("status")
