"""SQLite PRAGMA configuration applied on every new connection.

Mirrors what legacy ``bot.database_connection`` already does (audit:
``bot.py:4486``), so the new SQLAlchemy code can safely coexist with the
old ``sqlite3`` callers on the same files:

* ``journal_mode=WAL`` — readers don't block writers; required for two
  concurrent code-paths (legacy sync + new async) to share the file.
* ``foreign_keys=ON`` — SQLite default is OFF; we rely on FKs.
* ``synchronous`` — ``FULL`` for ``users``/``economy`` (money + identity);
  ``NORMAL`` for activity/moderation/message_stats where a crash-loss of
  the last few writes is acceptable. Matches legacy tuning exactly.
* ``cache_size=-20000`` (20 MB) — same as legacy.
* ``temp_store=MEMORY`` — same as legacy.
* ``busy_timeout=5000`` ms — legacy relied on ``connect(timeout=30)`` at
  the Python level; aiosqlite uses the pragma. 5s is plenty for our
  single-process workload and surfaces deadlocks faster than 30s.

On pragma order: flipping a rollback-journal file into WAL takes a brief
exclusive lock, so on a *fresh* file that pragma can fail outright with
``database is locked`` if the connection has no busy timeout. That cannot
bite here — both drivers install one at ``connect()`` time, before any
pragma runs (pysqlite and aiosqlite both default to ``timeout=5.0``, and
``db/engines.py`` passes no ``connect_args`` overriding it) — and
re-issuing ``journal_mode=WAL`` on an already-WAL file is a lock-free
no-op, because WAL is a persistent property of the file. ``busy_timeout``
is nonetheless stated first so the guarantee stays explicit should those
connect arguments ever change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from sqlite3 import Connection as SQLiteConnection


# Per-DB synchronous level. Legacy `bot.py` sets FULL for users/economy,
# NORMAL for the rest — keep parity so durability semantics don't change
# when a handler migrates.
_SYNCHRONOUS: dict[DBName, str] = {
    DBName.USERS: "FULL",
    DBName.ECONOMY: "FULL",
    DBName.ACTIVITY: "NORMAL",
    DBName.MODERATION: "NORMAL",
    DBName.MESSAGE_STATS: "NORMAL",
}


def apply_pragmas(connection: SQLiteConnection, db: DBName) -> None:
    """Apply the pragma set to a raw DBAPI connection.

    Wired into SQLAlchemy via ``event.listens_for(engine.sync_engine, "connect")``
    in :mod:`telegram_invite_bot.db.engines` — runs once per physical
    connection (aiosqlite pools them).
    """
    cursor = connection.cursor()
    try:
        # Stated before journal_mode on purpose — see the module docstring.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA synchronous={_SYNCHRONOUS[db]}")
        cursor.execute("PRAGMA cache_size=-20000")
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


def synchronous_level(db: DBName) -> str:
    """Public accessor — used by tests to assert per-DB tuning."""
    return _SYNCHRONOUS[db]
