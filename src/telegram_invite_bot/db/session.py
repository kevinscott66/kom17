"""Request-scoped session helpers.

Stage 2 keeps this minimal: handlers in later stages will receive an
``AsyncSession`` for the DB they touch via dishka REQUEST scope. The
registry itself stays APP-scoped (one connection pool per process).

The :func:`session_for` helper is intentionally explicit about which DB
a caller wants — there is no implicit "default" session. Each repository
declares its DB via a class attribute, and the request-scope provider in
``di/providers.py`` resolves the right sessionmaker.

:class:`Checkpoint` is the escape hatch for the one shape the per-update
transaction handles badly: a handler that writes and then waits on the
network. See its docstring.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.webhook.metrics import CHECKPOINT_TEARS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db.engines import EngineRegistry


@asynccontextmanager
async def session_for(registry: EngineRegistry, db: DBName) -> AsyncIterator[AsyncSession]:
    """Open a session against a specific DB.

    Commits on clean exit, rolls back on exception. Use from background
    tasks / migrations / tests; handlers should depend on the injected
    ``AsyncSession`` instead.
    """
    sessionmaker = registry.session(db)
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def session_was_touched(session: AsyncSession) -> bool:
    """``True`` iff someone executed at least one statement on ``session``
    (i.e. SQLAlchemy autobegin opened a transaction).

    We probe :meth:`AsyncSession.in_transaction`, a cheap synchronous
    getter — no I/O, no awaiting.

    Test-double fallback: AsyncMock returns a coroutine from any method
    call, which would leak as "unraisable" if treated as a bool. When
    ``in_transaction()`` returns a non-bool, assume the session is mocked
    and report "touched" (the legacy commit/rollback semantics) — the
    lazy-skip optimisation is a real-session behaviour, not a contract.
    """
    in_tx = getattr(session, "in_transaction", None)
    if in_tx is None or not callable(in_tx):
        return True
    try:
        result = in_tx()
    except Exception:  # noqa: BLE001 — never let probe failure mask handler outcome
        return True
    if inspect.iscoroutine(result):
        # AsyncMock-shaped — close the coroutine to silence the
        # PytestUnraisableExceptionWarning, then fall back to "touched".
        result.close()
        return True
    return bool(result)


# #1493. The order :class:`Checkpoint` commits in. Two SQLite files
# cannot be committed atomically — there is no 2PC here and there is not
# going to be one — so the only thing left to choose is WHICH half is on
# disk when the other one loses. That choice used to be made by the
# order the middlewares happened to be mounted in, which is to say by
# nobody.
#
# Economy first, because it is the ledger of record: if the second
# commit dies, the worst outcome is "charged for something that did not
# happen", which the owner can see and refund. The reverse — moderation
# durable, economy rolled back — is "got the effect for free", which
# nobody sees at all and which repeats for as long as the user can
# provoke the failure (#1277). Bookkeeping (users, message_stats,
# activity) goes last: it is the half nobody has to be made whole for.
#
# A session tracked without a DB name sorts to the end, in tracking
# order — ``sorted`` is stable.
_COMMIT_ORDER: tuple[DBName, ...] = (
    DBName.ECONOMY,
    DBName.MODERATION,
    DBName.USERS,
    DBName.MESSAGE_STATS,
    DBName.ACTIVITY,
)


class Checkpoint:
    """Commit what the update has written so far, mid-handler.

    The session middlewares hold one transaction per update, and the
    engine opens it as ``BEGIN IMMEDIATE`` on the first write (see
    :mod:`db.engines`) — so from a handler's first write until the
    middleware commits, that DB has exactly one possible writer. For the
    ordinary handler (write, answer, done) the window is a Telegram
    round-trip and nobody notices. For a handler that writes and *then*
    waits on somebody else's server it is a different story: ``/ask``
    stamps ``users.last_seen`` and spends an AI quota slot, then waits up
    to half a minute for DeepSeek. Every other user's update wants
    ``users.db`` too, waits out ``busy_timeout`` (5 s) and gets
    ``database is locked``. One slow model call and the whole bot stops
    writing.

    So handlers that are about to make a slow external call await this
    first. It is only ever correct where what has been written already
    must stand no matter how the rest of the update goes — bookkeeping
    (``last_seen``) and deliberate pre-charges (the AI quota slot, which
    :class:`services.ai_quota_service.AiQuotaService` documents as spent
    even when the upstream call fails). Anything the handler writes
    afterwards still lives in a fresh transaction and is still rolled
    back by the middleware if the handler raises.

    Objects already loaded stay usable across the commit because the
    sessionmakers are built with ``expire_on_commit=False``
    (:mod:`db.engines`); without that, every attribute touched after a
    checkpoint would trigger a refresh — and a lazy refresh under
    asyncio is a ``MissingGreenlet``, not a slow query.

    Not for use inside ``session.begin_nested()``: committing out from
    under a savepoint is not what any caller means.
    """

    __slots__ = ("_context", "_sessions")

    def __init__(self, context: str = "") -> None:
        self._sessions: list[tuple[DBName | None, AsyncSession]] = []
        # Free-form label for the partial-commit log — the middleware
        # fills in update/user ids. Empty is fine: a checkpoint built by
        # a test or a background task has no update behind it.
        self._context = context

    def track(self, session: AsyncSession, db: DBName | None = None) -> None:
        """Put ``session`` under this checkpoint (called by the middleware).

        ``db`` is what gives :data:`_COMMIT_ORDER` something to sort on
        and the partial-commit log something to name. It stays optional
        so a caller that only has a session still works — such a session
        commits last, after every named one.
        """
        self._sessions.append((db, session))

    def forget(self, session: AsyncSession) -> None:
        """Drop ``session`` — its middleware is closing it."""
        for index, (_db, tracked) in enumerate(self._sessions):
            if tracked is session:
                del self._sessions[index]
                return

    def _ordered(self) -> list[tuple[DBName | None, AsyncSession]]:
        """Tracked sessions in :data:`_COMMIT_ORDER`, unknown DBs last."""

        def rank(item: tuple[DBName | None, AsyncSession]) -> int:
            db = item[0]
            if db is None or db not in _COMMIT_ORDER:
                return len(_COMMIT_ORDER)
            return _COMMIT_ORDER.index(db)

        return sorted(self._sessions, key=rank)

    async def __call__(self) -> None:
        """Commit every tracked session that has an open transaction.

        Sessions nobody ran a statement on are skipped: with nothing
        executed, autobegin never fired, there is no transaction to
        end, and committing would be a wasted round-trip on the hot
        path (same gate the middleware uses).

        #1652: note what that does NOT say. SQLAlchemy 2.x autobegins
        on any ``execute``, a SELECT included, so a database this
        update only READ is "touched" and does get committed — cheap,
        but not a no-op. ``users.db`` is the everyday case rather than
        the exotic one: eight repositories bind to it and the language
        lookup reads it on nearly every update. An earlier wording
        promised that a "read-only DB for this update" is skipped,
        which is true only of a session no handler opened at all.

        #1493: two things this does beyond looping.

        Order is :data:`_COMMIT_ORDER`, not the order the middlewares
        were mounted in — see there for which half is chosen to survive.

        A failing commit does not stop the others. Rolling back what is
        already on disk is not on the table, so a prefix that got there
        by accident is strictly worse than committing everything that
        still can be: "charged and applied but the stats are missing"
        beats "charged, and whether it applied depends on mount order".
        The first exception is still raised once the loop is done, so
        the handler and the middleware see the failure exactly as
        before — what changes is what is durable underneath it, and
        that a partial outcome now says so in the journal. Without that
        line a partial commit is indistinguishable from a clean
        rollback, and an investigation into a divergence has nowhere to
        start.

        #1989: and now it says so in a counter too
        (:data:`~telegram_invite_bot.webhook.metrics.CHECKPOINT_TEARS`),
        because a journal line is only found by someone who already
        suspects. The counter fires ONLY on a genuine tear — something
        committed AND something failed. A checkpoint where nothing
        committed is an ordinary failed commit that the middleware
        undoes whole, and putting those on the same series would mean
        every transient "database is locked" pages the owner, which is
        how an alert stops being read.
        """
        committed: list[str] = []
        failed: list[str] = []
        first_error: Exception | None = None
        for db, session in self._ordered():
            if not session_was_touched(session):
                continue
            label = db.value if db is not None else "unknown"
            try:
                await session.commit()
            except Exception as exc:  # noqa: BLE001 — re-raised below, after the rest
                failed.append(label)
                if first_error is None:
                    first_error = exc
            else:
                committed.append(label)
        if first_error is not None:
            if committed:
                # #1989: a genuine tear — one DB is durable, another is
                # not, and no rollback can reconcile them. Counted as
                # well as logged, labeled by the half that was LOST,
                # because the journal line has an audience of nobody.
                for label in failed:
                    CHECKPOINT_TEARS.labels(failed=label).inc()
                logger.error(
                    "checkpoint partial commit: committed={c} failed={f} {ctx}",
                    c=committed,
                    f=failed,
                    ctx=self._context or "context=-",
                )
            else:
                # Nothing reached disk: an ordinary failed commit, which
                # the middleware undoes whole. It still deserves the
                # ERROR line — the caller sees the exception, but not
                # which DB refused — and it deliberately does NOT touch
                # the tear counter, whose whole value is being rare.
                logger.error(
                    "checkpoint commit failed: failed={f} {ctx}",
                    f=failed,
                    ctx=self._context or "context=-",
                )
            raise first_error
