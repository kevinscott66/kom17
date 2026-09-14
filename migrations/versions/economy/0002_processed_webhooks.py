"""economy: processed_webhooks idempotency table (T-025)

Revision ID: 0002_processed_webhooks
Revises: 0001_baseline_economy
Create Date: 2026-05-26

Adds the table that gates payment-webhook credit on idempotency. See
``src/telegram_invite_bot/db/models/economy.py:ProcessedWebhook`` for
the full rationale (one row per (provider, external_id) we've already
credited; duplicate deliveries find the row and skip the credit).

The legacy ``bot.py`` does not own this table — it derives idempotency
from ``economy.transactions`` (``is_payment_transaction_processed``).
The new port owns the table outright; legacy continues to run its own
deduplication against the ledger during the strangler window. Both
layers coexist safely: legacy duplicate-detection still works for any
webhook legacy itself ever processed, and the new layer is the source
of truth for everything the FastAPI router credits going forward.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_processed_webhooks"
down_revision: str | None = "0001_baseline_economy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processed_webhooks",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("credited_amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "external_id"),
    )
    op.create_index(
        "idx_processed_webhooks_user", "processed_webhooks", ["user_id"]
    )
    op.create_index(
        "idx_processed_webhooks_processed_at",
        "processed_webhooks",
        ["processed_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_processed_webhooks_processed_at", table_name="processed_webhooks")
    op.drop_index("idx_processed_webhooks_user", table_name="processed_webhooks")
    op.drop_table("processed_webhooks")
