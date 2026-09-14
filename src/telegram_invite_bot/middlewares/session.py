"""Inject request-scoped repos backed by a shared ``users.db`` session.

Currently exposes (added in order of stage):

* ``users_repo`` / ``user_service`` — Stage 4 (``/start`` etc).
* ``support_tickets_repo`` — Stage 14 (``/feedback``).
* ``marriages_repo`` / ``relationships_repo`` — Stage 19 (group
  leaderboards ``/marriages`` and ``/relations``).
* ``user_settings_repo`` — Stage 26 (``/lang``); also threaded into
  ``user_service`` so every ``touch`` call carries the override.
* ``bonds_write_repo`` — T-019 (marriage / divorce / breakup writes).
* ``nicknames_repo`` — Stage 33 (``/nick``).
* ``ai_quota_repo`` — M-P-2 (per-user daily AI counter).

The last three are the WRITING repos, and they are the reason the list
has to stay honest: someone who does not find them here reaches for a
second users-DB session, which is exactly what the paragraph below
says not to do (#1455). :meth:`_bind` is the source of truth.

Every repo above shares ONE :class:`AsyncSession` so the per-update
transaction is a single commit / rollback boundary, not N parallel
ones. New users-DB repos get added here as handlers migrate — the
alternative (one middleware per repo) would fragment the boundary
and let half-saved state leak between writes.

Lifecycle (open → bind → handler → commit/rollback → close) lives in
:class:`BaseSessionMiddleware`; this subclass only wires the binding
step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.repositories.ai_quota_repo import AiQuotaRepo
from telegram_invite_bot.repositories.bonds_repo import (
    BondsWriteRepo,
    MarriagesRepo,
    RelationshipsRepo,
)
from telegram_invite_bot.repositories.nicknames_repo import NicknamesRepo
from telegram_invite_bot.repositories.support_tickets_repo import SupportTicketsRepo
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services.user_service import UserService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry


class SessionMiddleware(BaseSessionMiddleware):
    """Open one ``users`` session per update; expose every users-DB repo.

    Wired against both ``dispatcher.message`` and
    ``dispatcher.callback_query`` from Stage 26 onward — the ``/lang``
    inline-keyboard callback needs the same ``user_service`` /
    ``user_settings_repo`` shape that message handlers consume. The
    outer-middleware-on-each-event approach keeps the session lazily
    opened (cost is zero for updates no handler claims) and avoids
    sharing a session across two concurrent event types.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        super().__init__(registry, DBName.USERS)

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        repo = UsersRepo(session)
        settings_repo = UserSettingsRepo(session)
        data["users_repo"] = repo
        data["user_settings_repo"] = settings_repo
        # Pass the settings repo into the service so ``touch`` returns
        # entities with ``language_override`` already set. Handlers
        # that only need ``user_service`` don't have to know
        # ``user_settings`` exists.
        data["user_service"] = UserService(repo, settings_repo)
        # Same session — support tickets live in users.db too, so
        # ticket inserts share the per-update transaction boundary
        # (rolled back together if the handler raises).
        data["support_tickets_repo"] = SupportTicketsRepo(session)
        # Read-only leaderboards — same users-DB session keeps the
        # JOIN to ``users.first_name`` consistent with any concurrent
        # users-table writes in the same update.
        data["marriages_repo"] = MarriagesRepo(session)
        data["relationships_repo"] = RelationshipsRepo(session)
        # T-019: marriage/divorce/breakup write repo.
        data["bonds_write_repo"] = BondsWriteRepo(session)
        # Stage 33: ``/nick`` writes ``user_group_nicknames``. Shares the
        # users-DB session so a touch + nick-write commits atomically.
        data["nicknames_repo"] = NicknamesRepo(session)
        # M-P-2: per-user daily AI quota counter (``ai_daily_requests``).
        # Lives in users.db (matches legacy's write site,
        # bot.py:38488-38493; the read is bot.py:38479)
        # so the increment commits in the same transaction as any
        # ``users.last_seen`` touch — no cross-DB race possible.
        data["ai_quota_repo"] = AiQuotaRepo(session)
