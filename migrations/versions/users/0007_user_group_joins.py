"""user_group_joins: per-(user, chat) membership record (RR-1 #3)

Revision ID: 0007_user_group_joins
Revises: 0006_rp_18_gate
Create Date: 2026-08-02

Registers the legacy ``user_group_joins`` table in the new pipeline's
Alembic chain. It backs the group-profile "in this group since" date and
the "messages since joining" counter — both of which need a *per-chat*
join date, which ``users.group_joined_date`` (one column, no chat) can
never provide.

PROD ALREADY HAS THIS TABLE. Legacy ``bot.py`` creates it at startup and
has been populating it since March 2026 (``source='observed_message'`` /
``'observed_private_check'``), so on the production DB this migration is a
no-op: ``upgrade`` inspects the live schema and creates only what is
absent. On a fresh test/dev DB it builds the legacy layout, indexes
included.

Deploy note: prod may use the STAMP-AFTER-VERIFY
path (confirm with ``.schema user_group_joins``, then ``alembic stamp
0007_user_group_joins``); the idempotent guard makes a real ``upgrade``
safe too, but the stamp keeps the revision pointer honest without
touching the populated table.

``downgrade`` drops the table. That is data loss on a DB where legacy
wrote the rows, which is exactly why the table is only ever created
here and never rebuilt — a downgrade is a deliberate act, not a retry.

Heads chained: ``0006_rp_18_gate`` -> ``0007_user_group_joins``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_user_group_joins"
down_revision: str | None = "0006_rp_18_gate"
branch_labels = None
depends_on = None


_TABLE = "user_group_joins"
_INDEXES = (
    ("idx_user_group_joins_chat", ["chat_id"]),
    ("idx_user_group_joins_active", ["is_active"]),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in set(inspector.get_table_names()):
        op.create_table(
            _TABLE,
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("chat_id", sa.Integer(), nullable=False),
            sa.Column("joined_at", sa.DateTime(), nullable=False),
            sa.Column("source", sa.Text(), nullable=True),
            sa.Column("group_title", sa.Text(), nullable=True),
            sa.Column("last_seen", sa.DateTime(), nullable=True),
            sa.Column("left_at", sa.DateTime(), nullable=True),
            sa.Column("is_active", sa.Integer(), nullable=True, server_default="1"),
            sa.PrimaryKeyConstraint("user_id", "chat_id"),
        )

    # Re-inspected: on the prod path the table already existed, and one
    # of its two indexes may still be missing (legacy added
    # ``idx_user_group_joins_active`` in a later release than the table).
    existing = {ix["name"] for ix in sa.inspect(bind).get_indexes(_TABLE)}
    for name, columns in _INDEXES:
        if name not in existing:
            op.create_index(name, _TABLE, columns)


def downgrade() -> None:
    """Deliberately a no-op.

    ``upgrade()`` did not create this table on prod — legacy did, in
    March 2026, and has been writing memberships into it ever since. A
    downgrade that dropped it would destroy months of data that no
    upgrade can rebuild, so a routine ``alembic downgrade -1`` to back
    out a bad deploy would be unrecoverable. Undoing an adoption means
    forgetting the table, not deleting it.

    On a fresh dev DB this leaves an orphan table behind. That is the
    cheaper mistake: it costs a stale table, not the data.
    """
