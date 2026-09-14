"""Shared scaffolding for ``tests/integration/repositories``.

Every repo test in this directory was open-coding the same six-line
``create_async_engine → create_all → sessionmaker → yield → dispose``
dance: nine files, nine identical lifecycles, drift waiting to happen.
A single forgotten ``expire_on_commit=False`` in one fixture and a
test would silently start seeing detached-instance errors that don't
reproduce elsewhere.

:func:`build_session` is an :class:`asynccontextmanager` rather than a
fixture so the test files keep full control of the *yield shape*. Some
tests want a bare :class:`AsyncSession`; others want
``(Repo(session), session)``. A factory-fixture would have forced one
shape on every caller; the context manager keeps both shapes a 4-line
``async with`` wrapper.

``engine.dispose()`` runs in the ``finally`` so a raised
``conn.run_sync(metadata.create_all)`` doesn't leak the connection
pool across tests.

The engine also carries the real
:mod:`telegram_invite_bot.db.safety` listener pinned to
``AppEnv.PROD``. Without it these fixtures happily executed
statements production refuses outright — an unbounded
``DELETE FROM command_rank_overrides`` shipped green for exactly
that reason. Pinning PROD (rather than reading the ambient
``APP_ENV``) is deliberate: the strictest behaviour is the one worth
asserting against, and a dev-mode warning would leave the same hole
open. A test that genuinely needs a table wipe writes the ``WHERE``
out or wraps the call in ``allow_unbounded_writes()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import AppEnv
from telegram_invite_bot.db.safety import install as install_safety

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import DeclarativeBase


@asynccontextmanager
async def build_session(
    tmp_path: Path,
    base: type[DeclarativeBase],
    db_filename: str,
) -> AsyncIterator[AsyncSession]:
    """Open one session against a freshly-created throwaway SQLite file.

    Mirrors the production lifecycle (engine → metadata create_all →
    sessionmaker → session) but scoped to a single test's ``tmp_path``.
    ``expire_on_commit=False`` is preserved because every repo test
    holds onto entity attributes after ``flush`` / ``commit``.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / db_filename}")
    event.listens_for(engine.sync_engine, "before_cursor_execute")(install_safety(AppEnv.PROD))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            yield session
    finally:
        await engine.dispose()
