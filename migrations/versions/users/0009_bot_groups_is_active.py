"""bot_groups.is_active — the bot's own membership, kept after it leaves (#111)

Revision ID: 0009_bot_groups_is_active
Revises: 0008_user_city
Create Date: 2026-08-13

``bot_groups`` answers one question for the money path: which groups may
a user buy *for*, so that the 15% group cut has somewhere to go. Nothing
ever removed a row, so the answer stayed "yes" for groups the bot had
been kicked out of months earlier — the cut was credited to the
registrar of a chat the bot can no longer post to.

The obvious fix — delete the row when the bot leaves — trades that for a
worse one. A row that can be deleted can be recreated, and the recreated
row names whoever re-added the bot. Any co-admin could then kick the bot
and add it back to make themselves the payout target of a group they do
not own, quietly undoing a deliberate ``/transfer_rights``. So the row
stays and only its ``is_active`` flag moves: the attribution survives the
bot's absence, and a re-add restores exactly the owner the group had
before.

``NOT NULL DEFAULT 1``: every row that predates this column is a group
the bot was in as far as anyone knew, which is what ``1`` means. The
server default also keeps a plain ``INSERT (chat_id, added_by_user_id)``
— legacy's shape — valid.

Idempotent, like ``0008_user_city``: test databases come from
``create_all``, which already has the column off the model, so a blind
``add_column`` would abort there with "duplicate column name".

Heads chained: ``0008_user_city`` -> ``0009_bot_groups_is_active``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_bot_groups_is_active"
down_revision: str | None = "0008_user_city"
branch_labels = None
depends_on = None

_TABLE = "bot_groups"
_COLUMN = "is_active"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in existing:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(
                _COLUMN, sa.Integer(), nullable=False, server_default=sa.text("1")
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
