"""economy: withdrawal_requests.amount_fiat → integer minor units (M-E-6)

Revision ID: 0003_withdrawal_amount_int
Revises: 0002_processed_webhooks
Create Date: 2026-05-27

Legacy stored ``amount_fiat`` as REAL (Float). Float money math
silently drifts on admin sum aggregates (``SELECT SUM(amount_fiat)
WHERE status='pending'`` accumulates 0.1+0.2 != 0.3 error across many
rows). The fix is to store the amount in *minor units* (cents for
USD/EUR, kopecks for RUB) as an Integer column and convert back to
the major-unit display string at render time via
``utils.economy.format_fiat_amount``.

The migration uses ``batch_alter_table`` so it works on SQLite
(which has no real ALTER COLUMN TYPE — alembic rebuilds the table
under the hood). The data conversion runs *inside* the batch via a
plain ``UPDATE`` against the old column before the type change lands;
``CAST(ROUND(amount_fiat * 100) AS INTEGER)`` is the exact same
form legacy renderers would produce when reading the float back and
multiplying — round-trip is lossless for the 2dp values legacy ever
wrote.

Downgrade reverses by dividing back to a float, accepting that the
inverse round-trip is now exact only for values that fit in the
2dp envelope (which is every legacy value).

Both directions are guarded and re-runnable (#1746). The data
statements are bare arithmetic that carries no marker of having run, so
a replay would multiply every stored payout by a hundred a second time
— and a replay is ordinary: alembic aborts a revision on the first
error and leaves ``alembic_version`` at the PREVIOUS one, a
disaster-recovery restore can stamp a stale version, and an operator
testing a rollback downgrades and upgrades again by design. The guard
reads the live column type and returns when the conversion has already
landed, which is the same inspect-then-return shape every sibling
economy revision uses (``0004``, ``0009``, ``0010``, ``0012``…
``0016``). ``tests/regression/test_migration_idempotency.py`` runs both
functions twice against a real sqlite file and asserts the row is
unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_withdrawal_amount_int"
down_revision: str | None = "0002_processed_webhooks"
branch_labels = None
depends_on = None

_TABLE = "withdrawal_requests"
_COLUMN = "amount_fiat"


def _amount_column(bind: sa.Connection) -> sa.types.TypeEngine[object] | None:
    """The live type of ``withdrawal_requests.amount_fiat``, or None.

    None means there is nothing to convert: either the legacy table was
    never created on this database (a fresh disaster-recovery file), or
    the column is gone. Returning instead of raising keeps a replay from
    stranding ``alembic_version`` at the previous revision, which would
    silently skip every later revision too.
    """
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return None
    for column in inspector.get_columns(_TABLE):
        if column["name"] == _COLUMN:
            return column["type"]
    return None


def upgrade() -> None:
    current = _amount_column(op.get_bind())
    if current is None or isinstance(current, sa.Integer):
        # Already minor units — running the UPDATE again would multiply
        # every stored payout by a hundred a second time.
        return
    # Data conversion runs BEFORE the type change so the existing
    # Float values are read with their original semantics (major
    # units). Rounding to the nearest cent matches what every
    # legacy renderer already did via ``{:.2f}`` truncation.
    op.execute(
        "UPDATE withdrawal_requests "
        "SET amount_fiat = CAST(ROUND(amount_fiat * 100) AS INTEGER) "
        "WHERE amount_fiat IS NOT NULL"
    )
    with op.batch_alter_table("withdrawal_requests") as batch:
        batch.alter_column(
            "amount_fiat",
            existing_type=sa.Float(),
            type_=sa.Integer(),
            existing_nullable=True,
        )


def downgrade() -> None:
    current = _amount_column(op.get_bind())
    if current is None or not isinstance(current, sa.Integer):
        # Already major units — dividing again would turn 1 234.56 into
        # 12.3456. Same reasoning as ``upgrade``, mirrored.
        return
    with op.batch_alter_table("withdrawal_requests") as batch:
        batch.alter_column(
            "amount_fiat",
            existing_type=sa.Integer(),
            type_=sa.Float(),
            existing_nullable=True,
        )
    op.execute(
        "UPDATE withdrawal_requests "
        "SET amount_fiat = amount_fiat / 100.0 "
        "WHERE amount_fiat IS NOT NULL"
    )
