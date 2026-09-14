"""``EconomyCleanupSweeper.sweep_once`` end-to-end (L-26).

Builds a one-DB :class:`EngineRegistry` over a throwaway economy file,
seeds expired + live inventory and stale + fresh game-play stamps, then
asserts one sweep reaps exactly the dead rows and commits.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import InventoryItem, UserPrivilege
from telegram_invite_bot.db.models.game_limits import GamePlay
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.scheduler.economy_cleanup import (
    EconomyCleanupReport,
    EconomyCleanupSweeper,
)

_NOW = datetime(2026, 6, 10, 12, 0, 0)


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(
        engines={DBName.ECONOMY: engine},
        sessions={DBName.ECONOMY: sessionmaker},
    )
    try:
        yield reg
    finally:
        await engine.dispose()


async def test_sweep_reaps_expired_and_stale(registry: EngineRegistry) -> None:
    # Seed via a direct session.
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(
            InventoryItem(
                user_id=1,
                item_id=1,
                purchase_date=_NOW - timedelta(days=5),
                expires=_NOW - timedelta(hours=1),  # expired → reaped
            )
        )
        s.add(
            InventoryItem(
                user_id=1,
                item_id=2,
                purchase_date=_NOW - timedelta(days=5),
                expires=_NOW + timedelta(days=1),  # live → kept
            )
        )
        s.add(GamePlay(user_id=1, game="roulette", played_at=_NOW - timedelta(days=3)))  # stale
        s.add(GamePlay(user_id=1, game="roulette", played_at=_NOW - timedelta(hours=1)))  # fresh

    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)
    report = await sweeper.sweep_once()

    assert report.inventory_deleted == 1
    assert report.game_plays_deleted == 1

    async with session_for(registry, DBName.ECONOMY) as s:
        inv = int((await s.execute(select(func.count()).select_from(InventoryItem))).scalar_one())
        plays = int((await s.execute(select(func.count()).select_from(GamePlay))).scalar_one())
    assert inv == 1
    assert plays == 1


async def test_sweep_noop_when_nothing_due(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(
            InventoryItem(
                user_id=1, item_id=1, purchase_date=_NOW - timedelta(days=5), expires=None
            )
        )
        s.add(GamePlay(user_id=1, game="roulette", played_at=_NOW - timedelta(hours=1)))

    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)
    report = await sweeper.sweep_once()

    assert report.inventory_deleted == 0
    assert report.game_plays_deleted == 0


async def test_sweep_prunes_expired_privileges_and_keeps_the_rest(
    registry: EngineRegistry,
) -> None:
    """M-1: the hourly hygiene pass is what calls ``delete_expired``.

    ``PrivilegesRepo.delete_expired`` documented itself as scheduled work
    long before anything scheduled it, so expired grants accrued forever.
    The frame matters as much as the wiring: ``expires_at`` is a legacy
    ``time.time()`` REAL and the sweeper's clock is naive LOCAL, so the
    repo's ``.timestamp()`` is what makes the two comparable — seeding
    from the same ``_NOW`` the sweeper is given proves the boundary lands
    where a reader would put it, not an offset away.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(
            UserPrivilege(
                user_id=1,
                privilege_type="double_daily",
                group_id=0,
                expires_at=(_NOW - timedelta(hours=1)).timestamp(),  # expired → pruned
            )
        )
        s.add(
            UserPrivilege(
                user_id=2,
                privilege_type="double_daily",
                group_id=0,
                expires_at=(_NOW + timedelta(days=1)).timestamp(),  # live → kept
            )
        )
        s.add(
            UserPrivilege(
                user_id=3,
                privilege_type="legend",
                group_id=0,
                expires_at=0.0,  # never expires → kept
            )
        )

    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)
    report = await sweeper.sweep_once()

    assert report.privileges_deleted == 1

    async with session_for(registry, DBName.ECONOMY) as s:
        survivors = sorted(
            int(row) for row in (await s.execute(select(UserPrivilege.user_id))).scalars()
        )
    assert survivors == [2, 3]


def _run_source() -> ast.AsyncFunctionDef:
    """The ``EconomyCleanupSweeper.run`` node, parsed from its own source."""
    import telegram_invite_bot.scheduler.economy_cleanup as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    runs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    ]
    assert len(runs) == 1, "exactly one run() is expected in this module"
    return runs[0]


def test_every_counter_reaches_both_the_pass_gate_and_the_pass_log() -> None:
    """#1034 (and #268 before it): a counter added to the report and to
    nothing else is invisible.

    ``run`` decides whether a pass is worth logging from a hand-written
    ``or`` chain, and then names the counters in a hand-written
    ``log.bind``. Both were written out field by field, so each new
    counter had to be added in three places and twice it was added in
    only one: #268 lost ``pvp_offers_expired`` and #1034 lost
    ``privileges_deleted``. A pass whose sole work was pruning expired
    privilege rows logged nothing at all, and a pass that logged for
    other reasons never said how many it had pruned — the sweeper's only
    output is that line, so the work was unobservable either way.

    Asserting against ``dataclasses.fields`` rather than a hard-coded
    list is deliberate: the next counter added to the report fails this
    test until it is wired into both places, which is the only thing
    that stops the pattern from recurring a third time.
    """
    run = _run_source()
    expected = {field.name for field in dataclasses.fields(EconomyCleanupReport)}

    gates = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
    ]
    assert len(gates) == 1, "the pass gate is expected to be the only boolean chain in run()"
    gate = gates[0].test
    assert isinstance(gate, ast.BoolOp)
    assert {value.attr for value in gate.values if isinstance(value, ast.Attribute)} == expected

    binds = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "bind"
    ]
    assert len(binds) == 1, "the pass log is expected to be the only bind in run()"
    assert {keyword.arg for keyword in binds[0].keywords} == expected
