"""Mark a credited webhook as reversed (chargeback / refund).

Revision ID: 0014_webhook_reversal
Revises: 0013_display_currency

#174. ``processed_webhooks`` already maps (provider, external_id) to
the user we credited and the coins we minted. A reversal webhook
carries that same provider payment id for RollyPay and YooKassa, so
two nullable columns turn the existing row into the durable record of
"this top-up was taken back" — the half of the reversal alert that
currently dies with the process.

Idempotent by the same rules as every migration here: inspect first,
return early when the table is absent (a fresh economy DB that has not
reached T-025 yet) or the columns already exist. ``batch_alter_table``
because SQLite rewrites the table for ADD COLUMN.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0014_webhook_reversal"
down_revision: str | None = "0013_display_currency"
branch_labels = None
depends_on = None

_TABLE = "processed_webhooks"
# ``Any`` and not ``object``: ``TypeEngine`` is invariant, so a tuple
# mixing ``DateTime`` and ``String`` has no common parameterisation.
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[Any]], ...] = (
    ("reversed_at", sa.DateTime()),
    ("reversed_event", sa.String()),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    missing = [(name, type_) for name, type_ in _COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        for name, type_ in missing:
            batch_op.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    """Drop the markers again, if this revision is the one that added them.

    A no-op when the table is gone or the columns were never created —
    downgrading past a revision that already found its work done must
    not fail the chain.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    present = [name for name, _ in _COLUMNS if name in existing]
    if not present:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        for name in present:
            batch_op.drop_column(name)
