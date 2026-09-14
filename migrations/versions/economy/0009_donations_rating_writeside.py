"""economy: donations-rating write-side — in_rating flag + rating_history (L-38)

Revision ID: 0009_donations_rating_writeside
Revises: 0008_game_plays
Create Date: 2026-06-10

Adds the write-side schema the donations-rating admin toggles need:

* ``groups_donations.in_rating`` — NEW per-group flag (1 = ranked, the
  default; 0 = hidden from the leaderboard). Legacy had no such column —
  every group with ``group_xp > 0`` was always ranked — so the column is
  added with ``server_default = 1`` to keep every existing row included.
  ``in_rating != 0`` is also the read-side filter the leaderboard SELECT
  must adopt so an excluded group disappears from ``/rating``.
* ``rating_history`` — NEW table mirroring legacy's snapshot store
  (bot.py:5074). One row per ``(group_id, date)`` holds that day's
  ``group_xp`` (as ``total_donations``) and ``rating_position``; the
  recalc upserts on the composite PK so a re-run on the same day overwrites
  rather than duplicates. The ``idx_rating_history_date`` index backs the
  per-day history read legacy's group-stats card used.

``groups_donations`` already exists in the prod dump (A-04 reads it), so
the column is an ``ADD COLUMN`` with a server-default — safe on SQLite,
backfills existing rows to ``1`` automatically.

``rating_history`` is net-new to the MIGRATIONS but NOT to the database:
legacy's ``CREATE TABLE IF NOT EXISTS`` ran at startup, so the table and
``idx_rating_history_date`` are both in the prod dump already
(``docs/prod_schemas.sql:435-442``), with exactly the columns below. So
every object here is created only when absent, the way the other
revisions guard their DDL — an unconditional create aborted the revision
on any real database and stranded it at 0008, which meant every later
revision never ran either.

This migration is not applied from here — the deploy runbook owns
``alembic upgrade``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_donations_rating_writeside"
down_revision: str | None = "0008_game_plays"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # ``in_rating`` really is net-new (the dump's groups_donations ends at
    # group_xp), but guard it anyway so a partially-applied revision can be
    # replayed instead of dying on "duplicate column name".
    columns = {col["name"] for col in inspector.get_columns("groups_donations")}
    if "in_rating" not in columns:
        op.add_column(
            "groups_donations",
            sa.Column(
                "in_rating",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )

    # Legacy's startup DDL already created both of these — see the module
    # docstring. Creating them blindly aborts the revision.
    if "rating_history" not in set(inspector.get_table_names()):
        op.create_table(
            "rating_history",
            sa.Column("group_id", sa.Integer(), nullable=False),
            sa.Column("date", sa.Text(), nullable=False),
            sa.Column("total_donations", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=True),
            sa.PrimaryKeyConstraint("group_id", "date", name="pk_rating_history"),
        )
    # Fresh inspector: the one above cached its reflection before the
    # CREATE TABLE, so it would not see a table this call just made.
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("rating_history")}
    if "idx_rating_history_date" not in existing:
        op.create_index("idx_rating_history_date", "rating_history", ["date"])


def downgrade() -> None:
    """Reverses the column, never the adopted table.

    #1937: this revision is half net-new and half adoption, and the two
    halves need opposite treatment. ``groups_donations.in_rating`` is
    genuinely ours — ``upgrade`` adds it, so dropping it is a faithful
    inverse. ``rating_history`` is not: the module docstring above
    records that legacy's ``CREATE TABLE IF NOT EXISTS`` put it and
    ``idx_rating_history_date`` on prod long before this chain existed
    (``docs/prod_schemas.sql:435-442``), which is exactly why
    ``upgrade`` only creates it when absent.

    The old code inspected the table and then dropped it *because* it
    was there — a guard that guaranteed the destruction instead of
    preventing it. On prod that is unrecoverable history; on a fresh dev
    database keeping it costs one stale table.
    """
    inspector = sa.inspect(op.get_bind())
    if "in_rating" in {col["name"] for col in inspector.get_columns("groups_donations")}:
        op.drop_column("groups_donations", "in_rating")
