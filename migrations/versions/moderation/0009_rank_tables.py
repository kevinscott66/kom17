"""moderation.db: rank_permissions + command_rank_overrides (ranks epic R1)

Revision ID: 0009_rank_tables
Revises: 0008_group_aliases
Create Date: 2026-06-11

Delta-only override stores for the rank system (DESIGN_RANKS.md §2.1):

* ``rank_permissions(rank, permission, allowed)`` — operator-edited
  cells of the rank → permission matrix; unedited cells fall back to
  the in-code legacy default matrix (bot.py:2611-2712).
* ``command_rank_overrides(command_key, min_rank)`` — per-command
  minimum-rank overrides over the in-code COMMAND_CATALOG defaults
  (bot.py:42314-42410).

Both tables are seeded EMPTY on purpose — an empty table means
"pure legacy defaults", exactly like a virgin legacy settings.json.

NOTE: chained to the current moderation head ``0008_group_aliases``;
if another revision also claims 0009, linearize before deploying.
WRITE-only — DO NOT apply manually.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_rank_tables"
down_revision: str | None = "0008_group_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rank_permissions",
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("allowed", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("rank", "permission"),
    )
    op.create_table(
        "command_rank_overrides",
        sa.Column("command_key", sa.Text(), primary_key=True),
        sa.Column("min_rank", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("command_rank_overrides")
    op.drop_table("rank_permissions")
