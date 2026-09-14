"""Real-SQLite tests for :mod:`services.effect_gates` (cluster T3).

Covers:

* :class:`GroupCoinsGate` — default ON for an unconfigured group, OFF
  after a ``coins_enabled=False`` override, TTL caching (a flip is not
  observed until the TTL lapses), and fail-open (True) when the
  moderation DB is unusable.
* :func:`has_mute_protection` — False with no grant, True with an
  active grant, False once the grant has expired, and fail-closed
  (False) when the economy DB is unusable.
* #1603: the severity of the two fail-safe log lines. They differ on
  purpose — one voids something the user paid for, the other only
  restores a default — and nothing else in the suite would notice if
  they were levelled out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase

# Imports register the tables on their metadata for create_all.
from telegram_invite_bot.db.models.economy import UserPrivilege  # noqa: F401
from telegram_invite_bot.db.models.group_mod_config import GroupModConfig  # noqa: F401
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.services.effect_gates import GroupCoinsGate, has_mute_protection

_GROUP = -1001234567890
_USER = 4242


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engines = {}
    sessions = {}
    for db, base in ((DBName.ECONOMY, EconomyBase), (DBName.MODERATION, ModerationBase)):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{db.value}.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        engines[db] = engine
        sessions[db] = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(engines=engines, sessions=sessions)
    try:
        yield reg
    finally:
        await reg.dispose()


@pytest.fixture
async def broken_registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    """Engines pointed at DBs with NO tables — every query raises."""
    engines = {}
    sessions = {}
    for db in (DBName.ECONOMY, DBName.MODERATION):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'broken_{db.value}.db'}")
        engines[db] = engine
        sessions[db] = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(engines=engines, sessions=sessions)
    try:
        yield reg
    finally:
        await reg.dispose()


# ---------------------------------------------------------------------------
# GroupCoinsGate
# ---------------------------------------------------------------------------


async def _set_coins(registry: EngineRegistry, group_id: int, *, enabled: bool) -> None:
    async with registry.session(DBName.MODERATION)() as session:
        await GroupModConfigRepo(session).set_field(
            group_id=group_id, field="coins_enabled", value=enabled
        )
        await session.commit()


async def test_coins_gate_default_on(registry: EngineRegistry) -> None:
    gate = GroupCoinsGate(registry)
    assert await gate.coins_enabled(_GROUP) is True


async def test_coins_gate_respects_override(registry: EngineRegistry) -> None:
    await _set_coins(registry, _GROUP, enabled=False)
    gate = GroupCoinsGate(registry)
    assert await gate.coins_enabled(_GROUP) is False
    # Other groups unaffected.
    assert await gate.coins_enabled(_GROUP - 1) is True


async def test_coins_gate_caches_within_ttl(registry: EngineRegistry) -> None:
    gate = GroupCoinsGate(registry)
    assert await gate.coins_enabled(_GROUP) is True
    # Flip in the DB; the cached True must still be served until TTL.
    await _set_coins(registry, _GROUP, enabled=False)
    assert await gate.coins_enabled(_GROUP) is True
    # Force expiry without sleeping.
    gate._cache.clear()  # noqa: SLF001 — deterministic TTL expiry in test
    assert await gate.coins_enabled(_GROUP) is False


async def test_coins_gate_fails_open(broken_registry: EngineRegistry) -> None:
    gate = GroupCoinsGate(broken_registry)
    assert await gate.coins_enabled(_GROUP) is True


# ---------------------------------------------------------------------------
# has_mute_protection
# ---------------------------------------------------------------------------


async def _grant_protection(registry: EngineRegistry, *, hours: int) -> None:
    async with registry.session(DBName.ECONOMY)() as session:
        await PrivilegesRepo(session).grant_with_value(
            user_id=_USER,
            privilege_type="mute_protection",
            value="{}",
            now=datetime.now(UTC),
            duration=timedelta(hours=hours),
        )
        await session.commit()


async def test_mute_protection_absent(registry: EngineRegistry) -> None:
    assert await has_mute_protection(registry, _USER) is False


async def test_mute_protection_active(registry: EngineRegistry) -> None:
    await _grant_protection(registry, hours=24)
    assert await has_mute_protection(registry, _USER) is True
    # A different user is not protected.
    assert await has_mute_protection(registry, _USER + 1) is False


async def test_mute_protection_expired(registry: EngineRegistry) -> None:
    await _grant_protection(registry, hours=-1)
    assert await has_mute_protection(registry, _USER) is False


async def test_mute_protection_fails_closed(broken_registry: EngineRegistry) -> None:
    assert await has_mute_protection(broken_registry, _USER) is False


# ---------------------------------------------------------------------------
# #1603: how loud each fail-safe is
# ---------------------------------------------------------------------------


def _capture() -> tuple[list[dict[str, Any]], int]:
    """Attach a loguru sink recording each line's level and bound fields."""
    seen: list[dict[str, Any]] = []

    def sink(message: Any) -> None:  # noqa: ANN401 — loguru hands us its Message
        record = message.record
        seen.append({"level": record["level"].name, **record["extra"]})

    return seen, logger.add(sink, level="DEBUG")


def _gate_lines(seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in seen if r.get("component") == "services.effect_gates"]


async def test_mute_protection_failure_is_logged_at_error(
    broken_registry: EngineRegistry,
) -> None:
    """A protection that was paid for and silently voided is an ERROR.

    On the error path the gate returns False, and from every surface a
    human can see that is indistinguishable from "never bought" or
    "already expired": the mute simply lands. Neither the target nor
    the muting admin is told anything. The log line is the only trace
    the owner will ever get, so it has to be findable in the error
    stream, and it has to name the target — the chat comes from the
    restriction's own success line, emitted moments later for the same
    user (handlers/moderation.py).
    """
    seen, handler_id = _capture()
    try:
        assert await has_mute_protection(broken_registry, _USER) is False
    finally:
        logger.remove(handler_id)

    entries = _gate_lines(seen)
    assert entries, "the failed read left no trace at all"
    assert [r["level"] for r in entries] == ["ERROR"]
    assert entries[0]["uid"] == _USER
    assert entries[0]["error"], "the exception text is what makes it diagnosable"


async def test_coins_gate_failure_stays_a_warning(
    broken_registry: EngineRegistry,
) -> None:
    """The sibling gate keeps WARNING, and the gap is the whole point.

    Falling back to "earning enabled" restores the behaviour legacy had
    before the per-group toggle existed: nobody loses anything they
    bought. Levelling this up to ERROR would bury the line above it
    under one entry per chat message during an outage.
    """
    seen, handler_id = _capture()
    try:
        assert await GroupCoinsGate(broken_registry).coins_enabled(_GROUP) is True
    finally:
        logger.remove(handler_id)

    entries = _gate_lines(seen)
    assert [r["level"] for r in entries] == ["WARNING"]
    assert entries[0]["group"] == _GROUP
