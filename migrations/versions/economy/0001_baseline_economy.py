"""baseline: economy.db

Revision ID: 0001_baseline_economy
Revises:
Create Date: 2026-05-14

Captures the prod schema as-of strangler Stage 2. See
``migrations/versions/users/0001_baseline_users.py`` for the rationale.
"""

from __future__ import annotations

revision: str = "0001_baseline_economy"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: baseline matches the existing schema on disk."""


def downgrade() -> None:
    """No-op: cannot un-baseline."""
