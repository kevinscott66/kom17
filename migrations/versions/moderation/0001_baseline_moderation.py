"""baseline: moderation.db

Revision ID: 0001_baseline_moderation
Revises:
Create Date: 2026-05-14
"""

from __future__ import annotations

revision: str = "0001_baseline_moderation"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: baseline matches the existing schema on disk."""


def downgrade() -> None:
    """No-op: cannot un-baseline."""
