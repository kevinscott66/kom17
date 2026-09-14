"""#1975: the chat-scoped index ``message_counts`` never had.

``docs/prod_schemas.sql:767-768`` and ``db/models/message_stats.py``
agree on two indexes, ``idx_msg_counts_user(user_id, chat_id)`` and
``idx_msg_counts_date(date)``. Neither leads with ``chat_id``, and
``message_stats.db`` has only the no-op baseline revision — so the four
chat-scoped ``/chatstats`` reads were driven off the date index, which
ranges over the window across every chat and discards all but one
chat's rows.

The tests below run the real revision against the shape prod actually
carries (legacy DDL, legacy index names) rather than a ``create_all``
schema that already has the answer, and then ask SQLite itself what
plan each query gets. Reading the plan is the point: an index that
exists but which the planner declines to use would fix nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

if TYPE_CHECKING:
    from collections.abc import Iterator

_REVISION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "message_stats"
    / "0002_chat_scoped_index.py"
)

_NAME = "idx_msg_counts_chat_date"

# The legacy DDL, as ``docs/prod_schemas.sql:757-768`` carries it.
_LEGACY_DDL = (
    """
    CREATE TABLE message_counts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        count INTEGER DEFAULT 0,
        last_message TIMESTAMP,
        UNIQUE(user_id, chat_id, date)
    )
    """,
    "CREATE INDEX idx_msg_counts_user ON message_counts(user_id, chat_id)",
    "CREATE INDEX idx_msg_counts_date ON message_counts(date)",
)

# The four chat-scoped window reads, in the shape SQLAlchemy emits them.
# Keys name the repository method; see ``message_stats_repo.py``.
_WINDOW_QUERIES = {
    "chat_totals_by_date": (
        "SELECT date, sum(count) FROM message_counts "
        "WHERE chat_id = ? AND date >= ? AND date <= ? GROUP BY date ORDER BY date DESC"
    ),
    "chat_total_for_days": (
        "SELECT sum(count) FROM message_counts WHERE chat_id = ? AND date >= ? AND date <= ?"
    ),
    "active_user_count": (
        "SELECT count(DISTINCT user_id) FROM message_counts "
        "WHERE chat_id = ? AND date >= ? AND date <= ?"
    ),
    "top_users_by_messages": (
        "SELECT user_id, sum(count) AS t FROM message_counts "
        "WHERE chat_id = ? AND date >= ? AND date <= ? GROUP BY user_id ORDER BY t DESC LIMIT 10"
    ),
}


def _load_revision() -> Any:
    """Import the revision by path — its filename starts with a digit."""
    spec = importlib.util.spec_from_file_location("_rev_msgstats_0002", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'message_stats.db'}")
    with eng.begin() as conn:
        for statement in _LEGACY_DDL:
            conn.execute(sa.text(statement))
        # Enough rows across enough chats that the planner has a reason
        # to prefer a chat-scoped index over the date one. ANALYZE makes
        # the choice deliberate rather than a coin toss on an empty table.
        conn.execute(
            sa.text(
                "INSERT INTO message_counts (user_id, chat_id, date, count) "
                "VALUES (:user_id, :chat_id, :date, 1)"
            ),
            [
                {"user_id": user, "chat_id": chat, "date": f"2026-09-{day:02d}"}
                for user in range(40)
                for chat in (-1001, -1002, -1003, -1004, -1005)
                for day in range(1, 11)
            ],
        )
        conn.execute(sa.text("ANALYZE"))
    yield eng
    eng.dispose()


def _run(engine: sa.Engine, name: str) -> None:
    module = _load_revision()
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        getattr(module, name)()
    with engine.begin() as conn:
        conn.execute(sa.text("ANALYZE"))


def _plan(engine: sa.Engine, query: str) -> str:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "EXPLAIN QUERY PLAN " + query, (-1001, "2026-09-01", "2026-09-10")
        ).all()
    return "; ".join(str(row[3]) for row in rows)


def _indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {
        index["name"] or "": [name or "" for name in index.get("column_names") or []]
        for index in sa.inspect(engine).get_indexes("message_counts")
    }


def test_the_legacy_shape_starts_without_it(engine: sa.Engine) -> None:
    """The premise, stated as a test so it cannot rot silently."""
    assert _NAME not in _indexes(engine)


@pytest.mark.parametrize("method", sorted(_WINDOW_QUERIES))
def test_without_the_index_the_window_reads_range_over_every_chat(
    engine: sa.Engine, method: str
) -> None:
    """What the defect looks like from the planner's side."""
    plan = _plan(engine, _WINDOW_QUERIES[method])

    assert "idx_msg_counts_date" in plan, plan
    assert "chat_id=?" not in plan, plan


@pytest.mark.parametrize("method", sorted(_WINDOW_QUERIES))
def test_after_the_upgrade_each_window_read_is_chat_scoped(engine: sa.Engine, method: str) -> None:
    _run(engine, "upgrade")

    plan = _plan(engine, _WINDOW_QUERIES[method])

    assert _NAME in plan, plan
    assert "chat_id=?" in plan, plan


def test_upgrade_creates_it_on_the_legacy_shape(engine: sa.Engine) -> None:
    _run(engine, "upgrade")

    assert _indexes(engine)[_NAME] == ["chat_id", "date"]


def test_a_second_upgrade_is_a_no_op(engine: sa.Engine) -> None:
    """Creating an existing index is a hard error, which would abort the
    whole batch and strand every later revision — the failure mode
    ``economy/0012`` and ``moderation/0011`` were written against."""
    _run(engine, "upgrade")
    _run(engine, "upgrade")

    assert _indexes(engine)[_NAME] == ["chat_id", "date"]


def test_an_equivalent_index_under_another_name_is_left_alone(tmp_path: Path) -> None:
    """A second index over the same leading columns only costs writes."""
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'message_stats.db'}")
    try:
        with eng.begin() as conn:
            for statement in _LEGACY_DDL:
                conn.execute(sa.text(statement))
            conn.execute(
                sa.text("CREATE INDEX idx_msg_counts_chat ON message_counts(chat_id, date)")
            )

        _run(eng, "upgrade")

        names = _indexes(eng)
        assert "idx_msg_counts_chat" in names
        assert _NAME not in names
    finally:
        eng.dispose()


def test_downgrade_removes_only_what_it_named(engine: sa.Engine) -> None:
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    names = _indexes(engine)
    assert _NAME not in names
    assert "idx_msg_counts_user" in names
    assert "idx_msg_counts_date" in names


def test_downgrade_on_a_database_that_never_had_it(engine: sa.Engine) -> None:
    """``IF EXISTS``: upgrade is allowed to have skipped it."""
    _run(engine, "downgrade")

    assert "idx_msg_counts_user" in _indexes(engine)


def test_the_model_declares_the_same_index() -> None:
    """A fresh ``create_all`` database and a migrated one must match."""
    from telegram_invite_bot.db.models.message_stats import MessageCount

    table = MessageCount.__table__
    assert isinstance(table, sa.Table)
    declared = {
        str(index.name): [column.name for column in index.columns] for index in table.indexes
    }
    assert declared[_NAME] == ["chat_id", "date"]
