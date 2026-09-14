"""economy: runtime_secrets — runtime-settable provider credentials (T-027)

Revision ID: 0006_runtime_secrets
Revises: 0005_user_emoji_badge
Create Date: 2026-06-05

Adds the net-new ``runtime_secrets`` table: one row per key (e.g.
``CRYPTO_PAY_TOKEN``) holding a value a developer can set from the in-bot
admin panel (``/set_crypto_token``) without a redeploy. The payment code
resolves the effective token at call time — this row wins over the
``.env``-loaded ``PaymentsConfig`` value when present.

The table is absent from the prod dump (net-new), so this is a plain
CREATE TABLE with no data-migration caveat. ``key`` is the PRIMARY KEY —
at most one value per key. See ``db/models/economy.py:RuntimeSecret`` for
the ORM mapping ``create_all`` uses in tests.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_runtime_secrets"
down_revision: str | None = "0005_user_emoji_badge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_secrets",
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("runtime_secrets")
