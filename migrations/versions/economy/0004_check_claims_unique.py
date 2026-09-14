"""economy: check_claims UNIQUE(check_id, user_id) — double-claim guard (#26)

Revision ID: 0004_check_claims_unique
Revises: 0003_withdrawal_amount_int
Create Date: 2026-06-05

The #26 "checks" (coin-code voucher) claim path relies on a
``UNIQUE(check_id, user_id)`` constraint on ``check_claims`` as its
ATOMIC double-claim guard: two concurrent claims from the same user
collide on the INSERT, and the loser's IntegrityError rolls its whole
transaction back (including the wallet decrement), so a check can never
pay the same user twice. See
``services/check_service.py:claim_check`` and
``db/models/economy.py:CheckClaim``.

The prod dump (``docs/prod_schemas.sql:497``) ships ``check_claims``
WITHOUT this constraint. This migration adds it as a UNIQUE INDEX
(``uq_check_claims_check_user``). The lookup index
(``idx_check_claims_check``) is already there — legacy's own DDL creates
it (dump line 502) — so it is created only when absent, the way the
other revisions guard their DDL. Creating it unconditionally aborted the
revision on any real database and left it stranded at 0003.

⚠️  NEEDS HUMAN CONFIRMATION BEFORE ``alembic upgrade`` ON PROD ⚠️
------------------------------------------------------------------
Creating a UNIQUE index FAILS if ``check_claims`` already contains
duplicate ``(check_id, user_id)`` rows — and legacy's racy
``_activate_check`` (``bot.py:10081``) could in principle have written
some. BEFORE running this upgrade on the production ``economy.db`` a
human MUST:

  1. Audit for duplicates::

         SELECT check_id, user_id, COUNT(*) c
         FROM check_claims
         GROUP BY check_id, user_id
         HAVING c > 1;

  2. If any rows are returned, DEDUP them (keep the earliest claim per
     pair; the duplicates represent double-credits that should also be
     reconciled against the wallet ledger — flag for finance review,
     do NOT just delete silently).

  3. Only once the audit returns zero rows, run the upgrade.

This is schema-only and the integration harness
(``tests/integration/test_alembic_cli.py``) exercises it on a FRESH
sqlite file (no duplicates possible), so CI is green regardless — the
dedup caveat is a PROD-ONLY operational gate, not a test concern.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_check_claims_unique"
down_revision: str | None = "0003_withdrawal_amount_int"
branch_labels = None
depends_on = None

_TABLE = "check_claims"
_INDEXES: tuple[tuple[str, list[str], bool], ...] = (
    # UNIQUE index = the double-claim guard. See module docstring for
    # the MANDATORY prod dedup pre-check before this runs against live
    # data — a duplicate (check_id, user_id) row makes this fail.
    ("uq_check_claims_check_user", ["check_id", "user_id"], True),
    # Lookup index for the per-check claim scans (has_claimed / counts).
    ("idx_check_claims_check", ["check_id"], False),
)

# Names legacy created, which ``upgrade`` only adopts and ``downgrade``
# must therefore leave alone (#1974).
_ADOPTED_INDEXES = frozenset({"idx_check_claims_check"})


def upgrade() -> None:
    # Only the UNIQUE index is net-new: legacy's own DDL already ships
    # the lookup index, so it is present in every DB this migration
    # actually runs against (``docs/prod_schemas.sql:502``). Creating it
    # blindly aborts the whole revision with "index already exists",
    # which strands the database at 0003 — every later revision then
    # never runs. Same existence check the other revisions use.
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    for name, columns, unique in _INDEXES:
        if name not in existing:
            op.create_index(name, _TABLE, columns, unique=unique)


def downgrade() -> None:
    """Drops only the index this revision owns.

    #1974: the old body dropped whatever it found, so on prod it also
    removed ``idx_check_claims_check`` — legacy's own lookup index
    (``docs/prod_schemas.sql:502``), the one ``upgrade`` skips precisely
    because legacy ships it. The same inverted guard as
    ``economy/0013``: the existence check read as protection and acted
    as permission.

    The damage is milder than a dropped column — an index holds no rows
    and the next ``upgrade`` recreates it — but between the two the LIVE
    telebot scans ``check_claims`` unindexed on every claim, and nothing
    tells the operator that a foreign object went missing.
    """
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    for name, _columns, _unique in reversed(_INDEXES):
        if name in _ADOPTED_INDEXES or name not in existing:
            continue
        op.drop_index(name, table_name=_TABLE)
