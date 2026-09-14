"""marriages.in_top — lift the legacy zeros onto the board (#2021)

Revision ID: 0012_marriages_in_top_backfill
Revises: 0011_voice_transcriptions_user_index
Create Date: 2026-09-11

``/marry_top_off`` has always written a 0 into ``marriages.in_top`` and
told the couple they were off ``/marriages`` — and nothing has ever read
the column back (``bot.py:22758`` writes it, ``bot.py:22991`` selects
without it). #2021 makes the leaderboard honour the flag, which turns
every stored 0 into a live exclusion for the first time.

That is the reason this backfill exists. Prod's column is
``in_top INTEGER DEFAULT 0`` (``docs/prod_schemas.sql:78``), so an
overwhelming majority of rows hold 0 for no reason but the default —
they are marriages nobody ever expressed a preference about, and they
are on the board today. Shipping the new predicate without this
revision would empty ``/marriages`` in one deploy.

The cost is stated plainly rather than engineered around: an untouched
0 and a deliberate ``/marry_top_off`` are the same zero, and no column
here distinguishes them. Lifting all of them to 1 restores today's
observable behaviour exactly and opts a handful of couples back onto a
board they had left; they can say ``/marry_top_off`` once more. The
opposite choice — reading 0 as "off" and leaving the rows alone —
would silently delete the whole leaderboard, so it is not a choice.

The column DEFAULT is deliberately NOT rebuilt. Doing it on SQLite
means ``batch_alter_table`` recreating ``marriages`` on prod, and the
only writer that would inherit ``DEFAULT 0`` is the legacy monolith,
which no longer runs; the new bot names ``in_top=1`` on every insert
(``repositories/bonds_repo.py`` ``accept_proposal``) and reads NULL as
on-the-board anyway. A table rebuild would be pure risk for no
behaviour.

Idempotent: re-running it finds nothing left to lift.

Heads chained: ``0011_voice_transcriptions_user_index`` -> ``0012_marriages_in_top_backfill``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_marriages_in_top_backfill"
down_revision: str | None = "0011_voice_transcriptions_user_index"
branch_labels = None
depends_on = None

_TABLE = "marriages"
_COLUMN = "in_top"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN not in {col["name"] for col in inspector.get_columns(_TABLE)}:
        return
    op.execute(
        sa.text('UPDATE "marriages" SET "in_top" = 1 WHERE "in_top" IS NULL OR "in_top" = 0')
    )


def downgrade() -> None:
    """Deliberately a no-op — see the module docstring.

    The inverse of this backfill does not exist. It wrote 1 over rows
    that held 0 for two different reasons and kept no record of which
    was which, so zeroing them again would opt every married couple in
    the database off the leaderboard on the strength of a default they
    never chose. Downgrading the schema must not do that; the flag is a
    user setting and stays where the users left it.
    """
