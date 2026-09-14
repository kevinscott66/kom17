"""relationship_proposals: add status column (A-02 unbrick)

Revision ID: 0003_relationship_proposal_status
Revises: 0002_marriage_proposal_status
Create Date: 2026-06-06

Adds a ``status`` column to ``relationship_proposals`` so the new
aiogram accept/decline flow can claim a row atomically and prevent a
double-accept race (two taps on the inline ✅ button arriving in
parallel). Mirrors ``0002_marriage_proposal_status`` exactly.

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

revision: str = "0003_relationship_proposal_status"
down_revision: str | None = "0002_marriage_proposal_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("relationship_proposals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(),
                nullable=False,
                server_default="pending",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("relationship_proposals") as batch_op:
        batch_op.drop_column("status")
