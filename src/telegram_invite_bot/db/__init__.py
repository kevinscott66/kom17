"""DB layer: 5 async engines + per-DB declarative bases.

Public API:

* :class:`DBName` — canonical identifier for each SQLite file
* :class:`EngineRegistry` — holds all five engines/sessionmakers
* :func:`build_registry` — factory; called once from the DI container
* :func:`session_for` — explicit context-managed session (non-handler code)
* :class:`Checkpoint` — mid-handler commit, for handlers that write and
  then wait on an external service

Pragmas (:mod:`.pragma`) and destructive-write guards (:mod:`.safety`)
are applied automatically on engine construction.
"""

from __future__ import annotations

from telegram_invite_bot.db.engines import EngineRegistry, build_registry
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.db.session import Checkpoint, session_for

__all__ = [
    "ALL_DBS",
    "Checkpoint",
    "DBName",
    "EngineRegistry",
    "build_registry",
    "session_for",
]
