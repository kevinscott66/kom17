"""moderation.db: chat-scoped indexes the models declare (#1949)

Revision ID: 0011_chat_scoped_indexes
Revises: 0010_group_coins_toggle
Create Date: 2026-09-09

``db/models/moderation.py`` declares ``idx_warnings_chat`` (:54) and
``idx_modlog_chat_date`` (:91), and ``moderation_repo.py:475`` states
outright that ``recent_chat_actions`` is "Served by
``idx_modlog_chat_date``". Neither index exists on prod. The moderation
baseline revision is a documented no-op — ``moderation.db`` was built by
the legacy telebot's own statements — and no revision since creates
them, so they exist only where ``create_all`` built the database: tests
and fresh deploys. Read off the live database, prod carries
``idx_warnings_user(user_id, chat_id)``, ``idx_warnings_date``,
``idx_warnings_admin``, ``idx_mod_log_user(user_id)``,
``idx_mod_log_date``, ``idx_mod_log_admin`` — and nothing keyed on
``chat_id`` alone.

Three chat-scoped queries scan the table without them:
``count_active_warnings_in_chat`` (the /groupadmin header),
``recent_chat_actions`` and ``count_chat_actions`` (the stats page).
Both tables are small on prod today (26 warnings, 39 log rows), so this
is about the shape matching what the code claims, not about a query that
is slow right now — ``moderation_log`` is append-only and never pruned.

Every CREATE is guarded the same way ``economy/0012`` guards its own:
skip when the NAME is taken (creating it again is a hard error), and
skip when an existing index already leads with the same columns under a
legacy name (a second index over the same prefix would only cost
writes). ``idx_warnings_user`` is exactly that case for the models'
OTHER warnings index, which is why this revision does not create it.

Not applied from here — the deploy runbook owns ``alembic upgrade``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_chat_scoped_indexes"
down_revision: str | None = "0010_group_coins_toggle"
branch_labels = None
depends_on = None


def _covered(inspector: sa.Inspector, table: str, name: str, columns: list[str]) -> bool:
    """Is ``name``/``columns`` already indexed on ``table``?

    Same helper, same two cases as
    ``migrations/versions/economy/0012_pvp_stake_games.py``: the name is
    taken, or legacy shipped an equivalent under its own name.
    """
    for index in inspector.get_indexes(table):
        if index["name"] == name:
            return True
        existing = list(index.get("column_names") or [])
        if existing[: len(columns)] == columns:
            return True
    return False


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # Both tables predate this revision everywhere it will ever run; the
    # guard is for the theoretical fresh database whose ``create_all``
    # has not happened yet, where there is nothing to index.
    if "warnings" in tables and not _covered(
        inspector, "warnings", "idx_warnings_chat", ["chat_id"]
    ):
        op.create_index("idx_warnings_chat", "warnings", ["chat_id"])

    if "moderation_log" in tables and not _covered(
        inspector, "moderation_log", "idx_modlog_chat_date", ["chat_id", "date"]
    ):
        op.create_index("idx_modlog_chat_date", "moderation_log", ["chat_id", "date"])


def downgrade() -> None:
    """Drop both indexes by name.

    Unlike an adopting revision that must never drop a legacy TABLE, an
    index carries no rows: the worst a wrong drop costs is a rebuild
    from the table it was derived from. ``IF EXISTS`` because
    ``upgrade`` is allowed to have skipped either one.
    """
    op.execute("DROP INDEX IF EXISTS idx_modlog_chat_date")
    op.execute("DROP INDEX IF EXISTS idx_warnings_chat")
