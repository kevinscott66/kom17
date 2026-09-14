"""#1949: the moderation indexes the code claims prod has.

``db/models/moderation.py`` declares ``idx_warnings_chat`` and
``idx_modlog_chat_date``, and ``moderation_repo.py:475`` says outright
that ``recent_chat_actions`` is "Served by ``idx_modlog_chat_date``".
Prod has neither: ``moderation.db`` there was created by the legacy
telebot's own statements, the moderation baseline revision is a
documented no-op, and nothing since creates them — so they exist only
where ``create_all`` built the schema, which is every environment
EXCEPT the one the claim was about.

``0011_chat_scoped_indexes`` closes that. The revision runs against a
database it did not create, so the tests below give it the shape prod
actually carries — legacy index names and all — rather than a
``create_all`` schema that already has the answer.
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
    / "moderation"
    / "0011_chat_scoped_indexes.py"
)

# The legacy DDL, as read off prod's ``moderation.db``: the tables plus
# six indexes, none of them keyed on ``chat_id`` alone.
_LEGACY_DDL = (
    """
    CREATE TABLE warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        reason TEXT NOT NULL,
        admin_id INTEGER NOT NULL,
        date TIMESTAMP NOT NULL,
        expires TIMESTAMP,
        active BOOLEAN DEFAULT 1
    )
    """,
    """
    CREATE TABLE moderation_log (
        id INTEGER PRIMARY KEY,
        action TEXT NOT NULL,
        user_id INTEGER,
        admin_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        reason TEXT,
        details TEXT,
        date TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX idx_warnings_user ON warnings(user_id, chat_id)",
    "CREATE INDEX idx_warnings_date ON warnings(date)",
    "CREATE INDEX idx_warnings_admin ON warnings(admin_id)",
    "CREATE INDEX idx_mod_log_user ON moderation_log (user_id)",
    "CREATE INDEX idx_mod_log_date ON moderation_log (date)",
    "CREATE INDEX idx_mod_log_admin ON moderation_log (admin_id)",
)


def _load_revision() -> Any:
    """Import the revision by path — its filename starts with a digit."""
    spec = importlib.util.spec_from_file_location("_rev_mod_0011", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'moderation.db'}")
    with eng.begin() as conn:
        for statement in _LEGACY_DDL:
            conn.execute(sa.text(statement))
    yield eng
    eng.dispose()


def _run(engine: sa.Engine, name: str) -> None:
    module = _load_revision()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, name)()


def _indexes(engine: sa.Engine, table: str) -> dict[str, list[str]]:
    inspector = sa.inspect(engine)
    return {
        index["name"] or "": list(index.get("column_names") or [])
        for index in inspector.get_indexes(table)
    }


def test_the_legacy_shape_starts_without_them(engine: sa.Engine) -> None:
    """The premise, stated as a test so it cannot rot silently."""
    assert "idx_warnings_chat" not in _indexes(engine, "warnings")
    assert "idx_modlog_chat_date" not in _indexes(engine, "moderation_log")


def test_upgrade_creates_both_on_the_legacy_shape(engine: sa.Engine) -> None:
    _run(engine, "upgrade")

    assert _indexes(engine, "warnings")["idx_warnings_chat"] == ["chat_id"]
    assert _indexes(engine, "moderation_log")["idx_modlog_chat_date"] == ["chat_id", "date"]


def test_a_second_upgrade_is_a_no_op(engine: sa.Engine) -> None:
    """Creating an existing index is a hard error, which would abort the
    whole batch and strand every later revision — the failure mode
    ``economy/0012`` was written against."""
    _run(engine, "upgrade")
    _run(engine, "upgrade")

    assert _indexes(engine, "warnings")["idx_warnings_chat"] == ["chat_id"]


def test_an_equivalent_legacy_index_is_left_alone(tmp_path: Path) -> None:
    """A second index over the same leading column only costs writes.

    ``_covered`` accepts a legacy NAME with matching leading columns —
    which is how the models' ``idx_warnings_user_chat`` is already
    served by prod's ``idx_warnings_user(user_id, chat_id)``, and why
    this revision does not create that one.
    """
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'moderation.db'}")
    try:
        with eng.begin() as conn:
            for statement in _LEGACY_DDL:
                conn.execute(sa.text(statement))
            conn.execute(sa.text("CREATE INDEX idx_mod_log_chat ON moderation_log (chat_id, date)"))

        _run(eng, "upgrade")

        names = _indexes(eng, "moderation_log")
        assert "idx_mod_log_chat" in names
        assert "idx_modlog_chat_date" not in names
    finally:
        eng.dispose()


def test_downgrade_removes_only_what_it_named(engine: sa.Engine) -> None:
    """An index carries no rows, so dropping ours is a faithful inverse
    — but the six legacy indexes must survive it."""
    _run(engine, "upgrade")
    _run(engine, "downgrade")

    warnings = _indexes(engine, "warnings")
    modlog = _indexes(engine, "moderation_log")
    assert "idx_warnings_chat" not in warnings
    assert "idx_modlog_chat_date" not in modlog
    assert set(warnings) == {"idx_warnings_user", "idx_warnings_date", "idx_warnings_admin"}
    assert set(modlog) == {"idx_mod_log_user", "idx_mod_log_date", "idx_mod_log_admin"}


def test_downgrade_on_a_database_that_never_had_them(engine: sa.Engine) -> None:
    """``IF EXISTS``: upgrade is allowed to have skipped either one."""
    _run(engine, "downgrade")

    assert set(_indexes(engine, "warnings")) == {
        "idx_warnings_user",
        "idx_warnings_date",
        "idx_warnings_admin",
    }
