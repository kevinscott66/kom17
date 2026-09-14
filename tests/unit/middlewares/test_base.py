"""Unit-scope tests for ``BaseSessionMiddleware``.

The three concrete subclasses (``SessionMiddleware``,
``EconomyMiddleware``, ``MessageStatsMiddleware``) all delegate the
open/commit/rollback/close lifecycle to the base. The end-to-end
suite (``test_support.py``, ``test_economy.py``) exercises this
*indirectly* via the public handler flow, but no test isolates the
contract that:

* on success → ``session.commit()`` runs, no ``rollback()``
* on raise  → ``session.rollback()`` runs, no ``commit()``, original
  exception propagates

A regression that flips the try/except → try/finally shape, or that
forgets the rollback before re-raising, would commit half-baked
state on every exception — exactly the silent-corruption case the
class docstring calls out. These tests lock both branches with
mock-only fixtures (no aiosqlite), so they run in milliseconds and
catch the contract violation directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class _RecordingMiddleware(BaseSessionMiddleware):
    """Minimal subclass: stamps a marker into ``data`` so we can prove
    ``_bind`` ran inside the session-open context.
    """

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        data["bound_session"] = session


def _fake_registry_with_session(session: AsyncMock) -> MagicMock:
    """Return a mock registry whose ``.session(db)`` yields ``session``.

    ``async with sessionmaker() as session`` requires the sessionmaker
    call to return an async-context-manager. We arrange that by making
    the mock's __aenter__ return the supplied AsyncSession-shaped mock.
    """
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    sessionmaker = MagicMock(return_value=session)
    registry = MagicMock()
    registry.session.return_value = sessionmaker
    return registry


@pytest.mark.asyncio
async def test_commits_on_handler_success() -> None:
    session = AsyncMock()
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.USERS)

    async def handler(_event: Any, data: dict[str, Any]) -> str:
        # Marker proves _bind ran before the handler.
        assert data["bound_session"] is session
        return "ok"

    result = await middleware(handler, MagicMock(), {})

    assert result == "ok"
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_rolls_back_and_reraises_on_handler_exception() -> None:
    session = AsyncMock()
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.ECONOMY)

    sentinel = RuntimeError("handler exploded")

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        raise sentinel

    with pytest.raises(RuntimeError) as excinfo:
        await middleware(handler, MagicMock(), {})

    # Same exception object propagates — base must not wrap or swallow.
    assert excinfo.value is sentinel
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_commit_or_rollback_when_session_untouched() -> None:
    """M-I-5: a handler that never executes a statement on the session
    must NOT trigger ``commit`` / ``rollback``. Previously every event
    paid for an empty commit even when no handler claimed it; in a busy
    group that was hundreds of wasted commits per minute.

    Detection uses ``session.in_transaction()`` — SQLAlchemy 2.x's
    autobegin only opens a tx on the first ``execute`` / ``add`` etc.,
    so an untouched session reports ``False`` here.
    """
    session = AsyncMock()
    # Override in_transaction to be a synchronous boolean (the real
    # AsyncSession.in_transaction is sync). False = untouched.
    session.in_transaction = MagicMock(return_value=False)
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.USERS)

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        # Deliberately do NOT touch the session.
        return "no-op"

    result = await middleware(handler, MagicMock(), {})

    assert result == "no-op"
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rollback_when_handler_raises_without_touching_session() -> None:
    """M-I-5: even on raise, an untouched session is left alone. The
    rollback exists to undo half-committed state; if no statements ran,
    there is nothing to roll back, and the empty rollback is the same
    wasted round-trip we want to avoid.
    """
    session = AsyncMock()
    session.in_transaction = MagicMock(return_value=False)
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.USERS)

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        raise RuntimeError("untouched")

    with pytest.raises(RuntimeError, match="untouched"):
        await middleware(handler, MagicMock(), {})

    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_handler_receives_a_checkpoint_that_commits_mid_update() -> None:
    """The per-update checkpoint reaches the handler and commits on call.

    Handlers that write and then wait on an external service (``/ask``
    → DeepSeek) use it to end the write transaction before the wait —
    otherwise the SQLite write lock is held for the whole round-trip
    and every other update on that DB waits out ``busy_timeout`` and
    fails with ``database is locked``.
    """
    session = AsyncMock()
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.USERS)

    async def handler(_event: Any, data: dict[str, Any]) -> str:
        await data["checkpoint"]()
        session.commit.assert_awaited_once()  # committed DURING the handler
        return "ok"

    assert await middleware(handler, MagicMock(), {}) == "ok"
    # Once for the checkpoint, once for the middleware's own close-out.
    assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_stacked_middlewares_share_one_checkpoint() -> None:
    """Two DBs, one checkpoint: a handler about to call out to the
    network wants every session it has written released, not just the
    innermost one.
    """
    outer_session, inner_session = AsyncMock(), AsyncMock()
    outer = _RecordingMiddleware(_fake_registry_with_session(outer_session), DBName.USERS)
    inner = _RecordingMiddleware(_fake_registry_with_session(inner_session), DBName.ECONOMY)

    async def handler(_event: Any, data: dict[str, Any]) -> None:
        await data["checkpoint"]()
        outer_session.commit.assert_awaited_once()
        inner_session.commit.assert_awaited_once()

    async def outer_handler(event: Any, data: dict[str, Any]) -> None:
        await inner(handler, event, data)

    await outer(outer_handler, MagicMock(), {})


@pytest.mark.asyncio
async def test_checkpoint_forgets_the_session_once_the_update_is_over() -> None:
    """A checkpoint that outlived its update must not commit a closed
    session — the middleware drops each session as it closes it.
    """
    session = AsyncMock()
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.USERS)
    data: dict[str, Any] = {}

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        return None

    await middleware(handler, MagicMock(), data)
    session.commit.reset_mock()

    await data["checkpoint"]()

    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkpoint_is_dropped_even_when_the_handler_raises() -> None:
    session = AsyncMock()
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.USERS)
    data: dict[str, Any] = {}

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await middleware(handler, MagicMock(), data)

    await data["checkpoint"]()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_uses_session_for_configured_db() -> None:
    """The base picks the sessionmaker keyed by ``_db_name``.

    Sanity check that the constructor's ``db_name`` argument is what
    gets passed to ``registry.session(...)`` — a typo in a concrete
    subclass would route writes to the wrong DB.
    """
    session = AsyncMock()
    registry = _fake_registry_with_session(session)
    middleware = _RecordingMiddleware(registry, DBName.MODERATION)

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        return None

    await middleware(handler, MagicMock(), {})

    registry.session.assert_called_once_with(DBName.MODERATION)


@pytest.mark.asyncio
async def test_the_checkpoint_commits_economy_first_whatever_the_mount_order() -> None:
    """#1493: the middleware hands its DB name to ``track``, and that is
    the whole reason the checkpoint can pick an order at all.

    ``users`` is the OUTER middleware here and ``economy`` the inner
    one, so tracking order is users-then-economy — and economy still
    commits first. Without the DB name the checkpoint has nothing to
    sort on and the ledger's fate is decided by however the routers
    happen to be wired.
    """
    order: list[str] = []
    users_session, economy_session = AsyncMock(), AsyncMock()
    users_session.commit.side_effect = lambda: order.append("users")
    economy_session.commit.side_effect = lambda: order.append("economy")
    outer = _RecordingMiddleware(_fake_registry_with_session(users_session), DBName.USERS)
    inner = _RecordingMiddleware(_fake_registry_with_session(economy_session), DBName.ECONOMY)

    async def handler(_event: Any, data: dict[str, Any]) -> None:
        await data["checkpoint"]()

    async def outer_handler(event: Any, data: dict[str, Any]) -> None:
        await inner(handler, event, data)

    await outer(outer_handler, MagicMock(), {})

    # The tail is each middleware's own close-out, which unwinds
    # inside-out and is NOT ordered — only the checkpoint is.
    assert order[:2] == ["economy", "users"]
