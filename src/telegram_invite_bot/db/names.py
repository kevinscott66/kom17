"""Canonical identifiers for the five SQLite databases.

The legacy monolith (``bot.py``, ``bot/config.py``, ``bot/database.py``)
opens five independent files. New code addresses each by ``DBName`` and
never hardcodes paths — :mod:`telegram_invite_bot.db.engines` resolves
the actual file via :class:`PathsConfig`.

NOTE — split rationale (kept intentionally): the legacy code applies
``PRAGMA synchronous=FULL`` to ``users`` and ``economy`` (balance/identity
consistency) and ``NORMAL`` elsewhere. Per-DB tuning lives in
:mod:`telegram_invite_bot.db.pragma`, keyed by these names.
"""

from __future__ import annotations

from enum import StrEnum


class DBName(StrEnum):
    USERS = "users"
    ECONOMY = "economy"
    ACTIVITY = "activity"
    MODERATION = "moderation"
    MESSAGE_STATS = "message_stats"


ALL_DBS: tuple[DBName, ...] = tuple(DBName)
