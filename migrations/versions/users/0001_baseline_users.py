"""baseline: users.db

Revision ID: 0001_baseline_users
Revises:
Create Date: 2026-05-14

Captures the prod schema as-of strangler Stage 2. No upgrade body — the
schema already exists on disk (see ``docs/prod_schemas.sql``). Use
``alembic -x db=users stamp head`` to mark a fresh DB as up-to-date.

When the first ORM-mapped table lands (Stage 4, ``users`` table), a real
``0002_*.py`` revision adds any drift between the legacy DDL and the new
SQLAlchemy model.
"""

from __future__ import annotations

revision: str = "0001_baseline_users"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: baseline matches the existing schema on disk."""


def downgrade() -> None:
    """No-op: cannot un-baseline."""
