"""economy: PvP escrow stake games — pvp_offers + pvp_escrow (AUD-2)

Revision ID: 0012_pvp_stake_games
Revises: 0011_vip_expiry_notice
Create Date: 2026-06-15

AUD-2 found the legacy PvP escrow stake games ``/pvp_coin`` and
``/pvp_dice`` (bot.py:20906 / bot.py:20867) shipped but never ported to
the strangler pipeline. Porting them needs two durable tables — an
*offer* (the published challenge) and the per-seat *escrow hold*.

Both tables already exist on prod ``economy.db`` — legacy's own DDL
created them — and they hold REAL history: 80 rows in ``pvp_offers`` and
93 in ``pvp_escrow``, written by three players between 2026-03-17 and
2026-06-15. Nothing is stranded (``SUM(amount) WHERE status='held'`` is
0), but those rows are financial history. So this revision has nothing to
migrate (unlike ``0010_p2p``), only a shape to guarantee, and every
object below is created ONLY WHEN ABSENT. Creating them unconditionally
aborted the revision on any real database and stranded it at 0011, so no
later revision ran either. ``downgrade`` is a no-op for the same reason —
see its own docstring.

Schema per ``db/models/pvp.py``:

* ``pvp_offers`` — one row per published challenge. ``game`` is
  ``coin`` | ``dice``; ``status`` walks ``pending`` → ``active`` (an
  opponent accepted, resolution in flight) → ``finished`` |
  ``cancelled`` | ``expired``. ``params_json`` carries the coin side
  for /pvp_coin (``{"side": "heads"}``); for /pvp_dice prod holds two
  shapes — ``{"mode": "higher"}`` from legacy (60 rows) and ``{}`` from
  the new pipeline (1 row), so readers must tolerate both. The race
  guard that lets only one accept win is a status-guarded UPDATE on
  this table (``PvpRepo.claim_for_accept``), so the indispensable bit
  is ``status`` being indexed for the expiry scan.

* ``pvp_escrow`` — one row per held stake (creator at create-time,
  opponent at accept-time). ``status`` is ``held`` → ``released`` |
  ``refunded``. The wallet debit that funds it is a separate atomic
  ``EconomyRepo.debit`` in the SAME transaction, so this row existing
  with ``status='held'`` ⇔ the wallet paid for it (the escrow
  invariant, enforced by both landing in one commit — same posture as
  ``p2p_sell_orders.remaining_com``).

Not applied from here — the deploy runbook owns ``alembic upgrade``.
This revision is written by the change that needs it, never run by it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_pvp_stake_games"
down_revision: str | None = "0011_vip_expiry_notice"
branch_labels = None
depends_on = None


def _covered(inspector: sa.Inspector, table: str, name: str, columns: list[str]) -> bool:
    """Is ``name``/``columns`` already indexed on ``table``?

    Two ways it can be: the same index NAME exists (creating it again is a
    hard error), or legacy shipped an equivalent under its OWN name — it
    indexes ``player1_id`` as ``idx_pvp_offers_p1`` — in which case a
    second index over the same leading column would only cost writes.
    """
    for index in inspector.get_indexes(table):
        if index["name"] == name:
            return True
        existing = list(index.get("column_names") or [])
        if existing[: len(columns)] == columns:
            return True
    return False


def upgrade() -> None:
    # Column names match the LEGACY prod schema (type/player1_id/player2_id;
    # pvp_escrow keyed by (offer_id, user_id), no surrogate id) — prod
    # already has these tables from the old telebot, so every CREATE below
    # is skipped there and only really runs for fresh deploys / CI, where it
    # must produce the shape the ORM models map to.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "pvp_offers" not in tables:
        op.create_table(
            "pvp_offers",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("chat_id", sa.Integer(), nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=True),
            sa.Column("player1_id", sa.Integer(), nullable=False),
            sa.Column("player2_id", sa.Integer(), nullable=True),
            sa.Column("bet", sa.Integer(), nullable=False),
            sa.Column("params_json", sa.Text(), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("accepted_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )

    # Fresh inspector — the one above cached its reflection before the
    # CREATE TABLE, so it cannot see a table this call just made.
    inspector = sa.inspect(op.get_bind())
    if not _covered(inspector, "pvp_offers", "idx_pvp_offers_player1", ["player1_id"]):
        op.create_index("idx_pvp_offers_player1", "pvp_offers", ["player1_id"])
    # Backs the expiry scan: WHERE status='pending' AND created_at < cutoff.
    # On a legacy DB the name is taken by a status-only index, which still
    # serves the equality half of that predicate — not worth a rebuild.
    if not _covered(
        inspector, "pvp_offers", "idx_pvp_offers_status", ["status", "created_at"]
    ):
        op.create_index("idx_pvp_offers_status", "pvp_offers", ["status", "created_at"])

    if "pvp_escrow" not in tables:
        op.create_table(
            "pvp_escrow",
            sa.Column("offer_id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), primary_key=True),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="held"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    """Deliberately a no-op.

    ``upgrade()`` did not create these tables on prod — legacy did, and
    they carry real history: 80 offers and 93 escrow holds from three
    players spanning 2026-03-17..2026-06-15. Dropping them to back out a
    bad deploy would destroy months of financial history that no upgrade
    can rebuild, which makes a routine ``alembic downgrade -1``
    unrecoverable. Undoing an adoption means forgetting the tables, not
    deleting them.

    On a fresh dev DB this leaves two orphan tables behind. That is the
    cheaper mistake: it costs a stale table, not the data.
    """
