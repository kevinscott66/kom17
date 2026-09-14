"""Direct probe of ``check_databases``.

The integration-level ``/healthz`` test in ``tests/integration/webhook``
covers the *route*, but it monkeypatches ``check_databases`` itself to
simulate a broken DB — so the actual ``try/except`` inside the probe
(lines 21-26 of ``health.py``) never runs in CI. That ``except`` is the
one piece of code that has to never raise, no matter what the engine
does: a probe that bubbles its own exception would 500 instead of
returning 503, and the orchestrator's degraded-vs-down distinction
collapses.

These mock-only tests exercise the real loop against a fake registry,
so a regression that swaps the broad ``except`` for a narrower one (or
removes it entirely) gets caught here in milliseconds.

Since #682 they also pin the *content* of the probe: an engine that
connects and answers, but from a file with no tables, must read as not
ready. ``tests/integration/webhook/test_server.py`` covers the same
contract end-to-end against real SQLite files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_invite_bot.db.names import ALL_DBS
from telegram_invite_bot.webhook.health import check_databases

if TYPE_CHECKING:
    from telegram_invite_bot.db.names import DBName


def _fake_registry(
    *, fail_for: set[DBName] | None = None, empty_for: set[DBName] | None = None
) -> MagicMock:
    """Return a registry stand-in whose ``engine(db)`` yields an engine
    that answers the schema probe with a table count, answers it with
    ``0``, or raises on ``connect``.

    ``async with engine.connect() as conn: await conn.execute(...)`` —
    we satisfy that with AsyncMock for the connection context manager.
    The ``.scalar()`` return value is set explicitly rather than left to
    ``MagicMock``'s auto-attribute, because an auto-attribute is truthy
    and would make every DB look healthy no matter what the probe asks
    (#682: that is precisely the failure mode this file now guards).
    """
    failed = fail_for or set()
    empty = empty_for or set()

    def _engine_for(db: DBName) -> Any:
        engine = MagicMock()
        if db in failed:
            engine.connect.side_effect = RuntimeError("simulated DB down")
            return engine
        result = MagicMock()
        result.scalar.return_value = 0 if db in empty else 7
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=result)
        engine.connect.return_value.__aenter__.return_value = conn
        engine.connect.return_value.__aexit__.return_value = None
        return engine

    registry = MagicMock()
    registry.engine.side_effect = _engine_for
    return registry


@pytest.mark.asyncio
async def test_all_dbs_healthy_returns_true_for_every_name() -> None:
    results = await check_databases(_fake_registry())
    assert set(results.keys()) == set(ALL_DBS)
    assert all(results.values())


@pytest.mark.asyncio
async def test_failing_engine_yields_false_without_raising() -> None:
    """The exact contract the production triage path depends on: a
    single dead engine surfaces as ``False`` for that key while every
    other key stays ``True``. The probe must NOT raise — letting the
    exception escape would 500 the route and the orchestrator would
    see "unknown" instead of "degraded".
    """
    from telegram_invite_bot.db.names import DBName

    results = await check_databases(_fake_registry(fail_for={DBName.ECONOMY}))
    assert results[DBName.ECONOMY] is False
    assert results[DBName.USERS] is True
    # Sanity: the rest also stay healthy.
    assert sum(1 for v in results.values() if v is False) == 1


@pytest.mark.asyncio
async def test_connectable_but_schemaless_db_is_not_ready() -> None:
    """#682: connecting is not the same as being a database.

    SQLite creates the file on connect, so a wrong ``DATABASE_DIR`` or
    an unmounted volume answers ``SELECT 1`` from an empty file and the
    old probe reported green — the orchestrator kept the process in
    rotation until the first real query died with ``no such table``.
    The engine here connects and answers; it just has no tables.
    """
    from telegram_invite_bot.db.names import DBName

    results = await check_databases(_fake_registry(empty_for={DBName.ACTIVITY}))
    assert results[DBName.ACTIVITY] is False
    assert results[DBName.USERS] is True
    assert sum(1 for v in results.values() if v is False) == 1
