"""Inject request-scoped :class:`ModerationRepo` backed by ``moderation.db``.

T-020: the moderation handler is the first to write to ``moderation.db``
via the new pipeline.  The wiring follows the same pattern as
:class:`EconomyMiddleware` — one session per update, one commit at the
end of the handler call, rolled back on exception.

:class:`EconomyMiddleware` is also attached to the moderation router for
the /fine command (the only one of the nine that touches economy.db).
Both are INNER middlewares (``router.message.middleware``, not
``outer_middleware``), so an update this router's filters reject never
opens a session at all.

They hold two independent sessions, and /fine really does write across
both files: the debit and the ledger row on ``economy.db``, the audit
row on ``moderation.db``, committed one after the other by the shared
checkpoint that ``handle_fine`` calls once both writes are staged
(``handlers/moderation.py``). #1651: that used to carry a line number,
which had drifted by two dozen lines before anyone noticed; the symbol
cannot drift. SQLite serialises writes per FILE, which is exactly why
that is NOT one atomic act — a crash
between the two commits leaves coins taken with no audit row. The
window is accepted (the alternative is a distributed transaction across
two SQLite files), but it exists. An earlier version of this docstring
said there was "no cross-DB atomicity concern", which read as a
guarantee nothing here provides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry


class ModerationMiddleware(BaseSessionMiddleware):
    """Open one ``moderation`` session per update; expose :class:`ModerationRepo`."""

    def __init__(self, registry: EngineRegistry) -> None:
        super().__init__(registry, DBName.MODERATION)

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        data["moderation_repo"] = ModerationRepo(session)
