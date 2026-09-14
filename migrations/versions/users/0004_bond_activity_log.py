"""bond activity-log tables (L-34/L-35 history views)

Revision ID: 0004_bond_activity_log
Revises: 0003_relationship_proposal_status
Create Date: 2026-06-10

Registers ``marriage_activity_log`` + ``relationship_activity_log`` in the
new pipeline's Alembic chain. Both tables ALREADY exist in prod (legacy
``bot.py:5568``/``:5583`` created them at startup; see
``docs/prod_schemas.sql:107``/``:119``) but were never captured by the new
baseline ``0001_baseline_users``. The couple-activity card history views
(L-34/L-35) read them and ``handlers/couple_activities`` now writes them.

Idempotent on purpose: on a real prod DB the tables are present, so
``upgrade`` skips creation when the inspector already sees them; on a
fresh test/dev DB it creates them with the legacy column + index layout.
This is the standard "adopt an existing legacy table into Alembic"
shape — create-if-absent, never drop on a populated prod table.

NOT APPLIED by the change set that adds it: the deploy step decides when
to stamp or upgrade. Heads chained:
``0003_relationship_proposal_status`` -> ``0004_bond_activity_log``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_bond_activity_log"
down_revision: str | None = "0003_relationship_proposal_status"
branch_labels = None
depends_on = None


_TABLES = ("marriage_activity_log", "relationship_activity_log")


def _create_log_table(name: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("user1_id", sa.Integer(), nullable=False),
        sa.Column("user2_id", sa.Integer(), nullable=False),
        sa.Column("activity_key", sa.Text(), nullable=False),
        sa.Column("xp_gained", sa.Integer(), nullable=False),
        sa.Column("paid_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index(f"idx_{name}_chat", name, ["chat_id"])
    op.create_index(
        f"idx_{name}_pair", name, ["chat_id", "user1_id", "user2_id"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for name in _TABLES:
        if name not in existing:
            _create_log_table(name)


def downgrade() -> None:
    """Deliberately a no-op.

    #1937: ``upgrade`` above only ADOPTS these two tables. Legacy
    ``bot.py:5568``/``:5583`` created them years ago and prod carries
    them populated (``docs/prod_schemas.sql:107``/``:119``), so the
    create-if-absent branch never fires there. Dropping them on the way
    back down would destroy history this revision never wrote and no
    ``upgrade`` can rebuild — backing a bad deploy out with
    ``alembic downgrade -1`` would be unrecoverable. The module
    docstring above already stated the rule ("create-if-absent, never
    drop on a populated prod table"); the code did not follow it.

    On a fresh dev database this leaves two orphan tables behind, which
    is the cheaper mistake: a stale table, not the rows.
    """
