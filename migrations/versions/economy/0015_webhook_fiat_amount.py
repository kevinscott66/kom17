"""Record what the customer was actually charged, in the provider's currency.

Revision ID: 0015_webhook_fiat_amount
Revises: 0014_webhook_reversal

#239. Every money column in this schema is denominated in coins. That
is correct for the ledger and useless for reconciliation: when a
RollyPay settlement report lists a rouble figure for a date, there is
no column anywhere on our side to compare it against. The roubles were
known only inside the adapter, for the length of one function call,
and the USD/RUB rate that priced them moves daily — so they cannot be
recovered after the fact either.

Three nullable TEXT columns on the row that already records "this
payment credited these coins to this user". TEXT and not REAL because
these are decimal money and a float column would reintroduce exactly
the drift ``withdrawal_requests.amount_fiat`` was moved off floats to
escape; the adapters carry ``Decimal`` end to end and TEXT is the only
SQLite affinity that preserves it.

Nullable because this is an audit trail bolted onto a table that has
been credited against for months. Rows written before this revision
have nothing to say and are deliberately not backfilled: inventing a
rouble figure from a coin count and today's rate would produce a
number that looks authoritative and is not.

Idempotent by the same rules as every migration here: inspect first,
return early when the table is absent or the columns already exist.
``batch_alter_table`` because SQLite rewrites the table for ADD COLUMN.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0015_webhook_fiat_amount"
down_revision: str | None = "0014_webhook_reversal"
branch_labels = None
depends_on = None

_TABLE = "processed_webhooks"
# ``Any`` for the same reason as 0014: ``TypeEngine`` is invariant, so a
# tuple of columns has no common parameterisation even when — as here —
# every member happens to be a ``String``.
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[Any]], ...] = (
    ("fiat_amount", sa.String()),
    ("fiat_currency", sa.String()),
    ("fx_rate", sa.String()),
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
    """Drop the audit columns again, if this revision is the one that added them.

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
