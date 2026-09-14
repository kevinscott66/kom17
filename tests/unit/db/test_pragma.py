"""Per-DB pragma tuning matches the legacy ``bot.py`` defaults."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.db.pragma import apply_pragmas, synchronous_level


@pytest.mark.parametrize(
    ("db", "expected"),
    [
        (DBName.USERS, "FULL"),
        (DBName.ECONOMY, "FULL"),
        (DBName.ACTIVITY, "NORMAL"),
        (DBName.MODERATION, "NORMAL"),
        (DBName.MESSAGE_STATS, "NORMAL"),
    ],
)
def test_synchronous_level_matches_legacy(db: DBName, expected: str) -> None:
    assert synchronous_level(db) == expected


def test_all_dbs_have_synchronous_setting() -> None:
    for db in ALL_DBS:
        # Should not raise KeyError for any known DB.
        assert synchronous_level(db) in {"FULL", "NORMAL"}


def test_apply_pragmas_sets_wal_and_foreign_keys(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "x.db")
    try:
        apply_pragmas(conn, DBName.USERS)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # busy_timeout is in milliseconds.
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()
