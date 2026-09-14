"""Shared base for per-DB session-injecting middlewares.

Every :class:`BaseSessionMiddleware` subclass runs the same lifecycle.
Four live in this package (:class:`SessionMiddleware` for ``users.db``,
:class:`EconomyMiddleware` for ``economy.db``,
:class:`MessageStatsMiddleware` for ``message_stats.db``,
:class:`ModerationMiddleware` for ``moderation.db``) and four more are
module-private inside handlers, all of them on ``moderation.db``
(``handlers/modcfg.py``, ``handlers/moderation.py``,
``handlers/group_aliases.py``, ``handlers/wordfilter.py``). Do not read
that as a closed list — subclasses are cheap by design, so count them
with a grep rather than trusting a number written here:

    open sessionmaker(DBName) → bind repos into handler ``data`` →
    run handler → commit on success / rollback on raise → close

Before this base existed each middleware open-coded the whole shape,
which meant (a) the commit/rollback contract was duplicated once per
middleware — a single forgotten ``rollback`` would cap a write to disk on
exception, silently committing half-baked state — and (b) adding a
fourth DB (or attaching a new repo to an existing DB) involved a
copy-paste of the lifecycle scaffold rather than a one-liner.

Subclasses only override :meth:`_bind`, which receives the open
session and the handler ``data`` dict and stamps the repos/services
the corresponding router needs. Nothing else is configurable —
deliberately. A subclass that wants different commit semantics (e.g.
read-only without commit) almost certainly wants a different
middleware altogether.

M-I-5: the session is opened eagerly (so subclass ``_bind`` can pass
the real ``AsyncSession`` straight into repos), but ``commit`` /
``rollback`` are skipped when the handler never executed a statement
— i.e. when :meth:`AsyncSession.in_transaction` is ``False`` at exit.
SQLAlchemy 2.x's autobegin means a session that nobody touched has
no active transaction, so the no-op commit was a wasted round-trip
on every channel-post / edited-message event no handler claimed.

Every session opened for an update is also registered with the
update's :class:`~telegram_invite_bot.db.session.Checkpoint`, which
lands in ``data["checkpoint"]``. Handlers that are about to wait on an
external service take it as an argument and await it, so the SQLite
write lock isn't held across somebody else's latency.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware

from telegram_invite_bot.db.session import Checkpoint
from telegram_invite_bot.db.session import session_was_touched as _session_was_touched

if TYPE_CHECKING:
    from aiogram.types import TelegramObject
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.db.names import DBName

# Handler-facing key for the per-update checkpoint. Handlers declare an
# argument of this name to receive it (aiogram injects by name).
CHECKPOINT_KEY = "checkpoint"


def _checkpoint_context(data: dict[str, Any]) -> str:
    """Label the checkpoint's partial-commit log with the update behind it.

    #1493. Both keys are aiogram's own, stamped into ``data`` before any
    middleware runs; read defensively all the same, because this is the
    hot path and a missing id must never be what turns a commit failure
    into a second failure.
    """
    update = data.get("event_update")
    user = data.get("event_from_user")
    return f"update_id={getattr(update, 'update_id', None)} user_id={getattr(user, 'id', None)}"


class BaseSessionMiddleware(BaseMiddleware):
    """Open one session per update; subclass binds the repos.

    Subclasses set :attr:`_db_name` (class attribute, set in __init__
    via super().__init__) and override :meth:`_bind`. The base owns
    the open/commit/rollback/close dance — and only that.
    """

    _db_name: DBName

    def __init__(self, registry: EngineRegistry, db_name: DBName) -> None:
        self._registry = registry
        self._db_name = db_name

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        """Stamp repos/services for this DB into ``data``.

        Called exactly once per update, inside the session-open
        context but before the handler runs. Subclasses populate
        ``data`` with the concrete repos / services that share this
        session.
        """
        raise NotImplementedError

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sessionmaker = self._registry.session(self._db_name)
        async with sessionmaker() as session:
            self._bind(session, data)
            # One checkpoint per update, shared by however many of these
            # middlewares are stacked — there is no fixed roster, every
            # ``BaseSessionMiddleware`` subclass lands here:
            # a handler about to call out to the network wants every DB
            # it has written released, not just its own.
            checkpoint = data.get(CHECKPOINT_KEY)
            if not isinstance(checkpoint, Checkpoint):
                checkpoint = Checkpoint(context=_checkpoint_context(data))
                data[CHECKPOINT_KEY] = checkpoint
            # #1493: hand over the DB name too. The checkpoint commits in
            # a fixed order rather than in mount order, and it can only
            # do that if it knows which file each session belongs to.
            checkpoint.track(session, self._db_name)
            try:
                result = await handler(event, data)
            except Exception:
                # Roll back BEFORE re-raising — leaving a half-committed
                # transaction on a raised handler is the silent-corrupt
                # case the lifecycle is designed to prevent. M-I-5 gate:
                # only rollback when the handler actually touched the
                # session (autobegin opened a tx). Without this, every
                # raise on a no-op handler still hit the rollback path.
                if _session_was_touched(session):
                    await session.rollback()
                raise
            else:
                # M-I-5: skip the empty commit on no-op paths. SQLAlchemy
                # 2.x autobegin means a session that nobody touched has
                # ``in_transaction() is False`` — committing it is a
                # wasted round-trip we paid for on every channel-post /
                # edited-message / service-message event no handler
                # claimed. In a busy group that was hundreds of empty
                # commits per minute.
                #
                # #1493: this exit is NOT ordered. Nested ``async with``
                # unwinds inside-out, so the inner router middleware
                # (economy, moderation) commits before the outer
                # dispatcher one (users) — fixed at startup, but decided
                # by mount order rather than on purpose. A handler that
                # wants the choice made deliberately awaits
                # ``checkpoint()`` before it does anything that can
                # fail; that path is ordered by
                # ``db.session._COMMIT_ORDER``.
                if _session_was_touched(session):
                    await session.commit()
            finally:
                # ``async with`` closes the session right after this; a
                # checkpoint holding on to it would commit a closed
                # session if the dict outlived the update.
                checkpoint.forget(session)
            return result
