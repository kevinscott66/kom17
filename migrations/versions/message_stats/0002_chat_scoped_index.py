"""message_stats.db: idx_msg_counts_chat_date on message_counts (#1975)

Revision ID: 0002_chat_scoped_index
Revises: 0001_baseline_message_stats
Create Date: 2026-09-10

``message_counts`` carries exactly two indexes on prod
(``docs/prod_schemas.sql:767-768``) and in the model
(``db/models/message_stats.py:29-30``): ``idx_msg_counts_user(user_id,
chat_id)`` and ``idx_msg_counts_date(date)``. Neither leads with
``chat_id``, so neither can drive a ``WHERE chat_id = ?`` predicate, and
``message_stats.db`` has only the no-op baseline revision — the gap is
identical on prod, in tests and on a fresh deploy.

Four ``/chatstats`` reads are chat-scoped over a date window:
``chat_totals_by_date``, ``chat_total_for_days``, ``active_user_count``
and ``top_users_by_messages`` (``repositories/message_stats_repo.py``
:236, :258, :274, :353, plus ``handlers/mygroups.py:211`` and
``handlers/ads.py:232`` at ``days=30``). SQLite drives all four off
``idx_msg_counts_date``, which ranges over the window across **every
chat** and then discards all but one chat's rows — so the work grows
with the number of groups the bot serves, not with the group being
rendered. With ``(chat_id, date)`` the same reads become
``SEARCH ... (chat_id=? AND date>? AND date<?)``.

The table is the fastest-growing one in the system: one row per
``(user_id, chat_id, day)``, written on every group message
(``MessageStatsRepo.increment``), and nothing in ``scheduler/`` prunes
it. That is also why this index stays two columns wide. A covering
``(chat_id, date, user_id, count)`` does turn all four into index-only
scans — measured — but every extra column is paid on the write path of
the hottest table in the deployment, and the defect being fixed is the
missing chat predicate, not the table lookup.

Deliberately NOT addressed here: ``newcomer_count``
(``message_stats_repo.py:317``) has no date predicate at all by design,
and SQLite already serves it from the UNIQUE constraint's autoindex as
``COVERING INDEX sqlite_autoindex_message_counts_1 (ANY(user_id) AND
chat_id=?)`` — a skip-scan, not a full table scan. This index does not
change that plan and is not meant to. A ``(chat_id, user_id, date)``
index would serve it properly, but it would *not* serve the four window
queries above, so it is a separate call and a separate revision.

The CREATE is guarded exactly as ``moderation/0011`` guards its own:
skip when the name is taken (a second CREATE is a hard error that would
abort the batch and strand every later revision), and skip when an
existing index already leads with the same columns under another name.

Not applied from here — the deploy runbook owns ``alembic upgrade``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_chat_scoped_index"
down_revision: str | None = "0001_baseline_message_stats"
branch_labels = None
depends_on = None

_TABLE = "message_counts"
_NAME = "idx_msg_counts_chat_date"
_COLUMNS = ["chat_id", "date"]


def _covered(inspector: sa.Inspector) -> bool:
    """Is the name taken, or the prefix already indexed under another name?

    Same helper and the same two cases as
    ``migrations/versions/moderation/0011_chat_scoped_indexes.py``.
    """
    for index in inspector.get_indexes(_TABLE):
        if index["name"] == _NAME:
            return True
        existing = list(index.get("column_names") or [])
        if existing[: len(_COLUMNS)] == _COLUMNS:
            return True
    return False


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        # Defensive: the table predates this revision everywhere it will
        # actually run; on a database whose create_all has not happened
        # there is nothing to index.
        return
    if not _covered(inspector):
        op.create_index(_NAME, _TABLE, _COLUMNS)


def downgrade() -> None:
    """Drop the index by name.

    An index carries no rows, so unlike an adopting revision that must
    never drop a legacy TABLE (#1937, #1974) this is a faithful inverse:
    the worst a wrong drop costs is a rebuild from the table it was
    derived from. The name is ours — legacy never created it — and
    ``IF EXISTS`` because ``upgrade`` is allowed to have skipped it.
    """
    op.execute(f"DROP INDEX IF EXISTS {_NAME}")
