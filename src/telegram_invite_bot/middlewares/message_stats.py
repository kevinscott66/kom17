"""Inject a request-scoped ``MessageStatsRepo`` for stats handlers.

Attached to the stats router specifically so updates that don't touch
``/stats`` don't pay for a ``message_stats.db`` session per dispatch.

The session is opened read-only in spirit (Stage 11 only exposes
read methods), but we still commit on success / rollback on raise to
match the established middleware lifecycle (inherited from
:class:`BaseSessionMiddleware`) and to keep SQLAlchemy from emitting
"Session was not closed" warnings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry


class MessageStatsMiddleware(BaseSessionMiddleware):
    """Open one ``message_stats`` session per update; expose ``MessageStatsRepo``."""

    def __init__(self, registry: EngineRegistry) -> None:
        super().__init__(registry, DBName.MESSAGE_STATS)

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        data["message_stats_repo"] = MessageStatsRepo(session)
