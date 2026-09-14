"""economy: promo_codes + promo_redemptions — gift-code subsystem (L-96)

Revision ID: 0007_promo_codes
Revises: 0006_runtime_secrets
Create Date: 2026-06-10

Adds the two net-new tables that back the promo / gift-code feature
(L-96): ``promo_codes`` (one row per mintable code, with a flat
``reward_coins`` payout, a global ``max_uses`` cap, and a
``per_user_once`` toggle) and ``promo_redemptions`` (append-only, one row
per successful redemption).

Both tables are absent from the prod dump (net-new), so this is a plain
CREATE TABLE pair with no data-migration / dedup caveat. There is NO
``UNIQUE(code_id, user_id)`` constraint on ``promo_redemptions`` on
purpose — non-once codes legitimately let the same user redeem more than
once; the per-user-once guard is a conditional ``INSERT ... WHERE NOT
EXISTS`` in ``PromoRepo.redeem`` instead. The composite
``idx_promo_redemptions_code_user`` index backs that probe and the
per-user count.

See ``db/models/promo.py`` for the ORM mappings ``create_all`` uses in
tests. This migration is not applied to any DB from here — the deploy
runbook owns ``alembic upgrade``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_promo_codes"
down_revision: str | None = "0006_runtime_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("reward_coins", sa.Integer(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("per_user_once", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("code", name="uq_promo_codes_code"),
    )
    op.create_index("idx_promo_codes_code", "promo_codes", ["code"])

    op.create_table(
        "promo_redemptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("code_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reward_coins", sa.Integer(), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_promo_redemptions_code_user",
        "promo_redemptions",
        ["code_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_promo_redemptions_code_user", table_name="promo_redemptions")
    op.drop_table("promo_redemptions")
    op.drop_index("idx_promo_codes_code", table_name="promo_codes")
    op.drop_table("promo_codes")
