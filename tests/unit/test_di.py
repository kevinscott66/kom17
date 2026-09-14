"""dishka container wiring — both AppProvider and DBSessionProvider.

The providers in ``di/providers.py`` are pure factory functions glued
together by dishka's decorators. They executed in CI only as a side
effect of the rare test that built a full Application, leaving the
module at 62%. A regression in any single factory — wrong scope,
missing dependency, wrong DB name on a session — would silently
break the corresponding handler at runtime without flagging at boot.

These tests exercise each factory by resolving from a real
``make_async_container(AppProvider(), DBSessionProvider())`` instead
of mocking. dishka is fast enough that this still runs as a unit
test (<1s), and going through the real container means we'd catch
provider misregistration that a hand-rolled factory call would miss.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from dishka import Provider, Scope, make_async_container, provide
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import Settings
from telegram_invite_bot.db import EngineRegistry
from telegram_invite_bot.di.container import make_container
from telegram_invite_bot.di.providers import (
    ActivitySession,
    AppProvider,
    DBSessionProvider,
    EconomySession,
    MessageStatsSession,
    ModerationSession,
    UsersSession,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``Settings`` is built from ``BOT_TOKEN`` (required) via
    ``get_settings()``. Inject a fake token and a writable tmp DB
    directory so the registry doesn't try to mkdir the repo root.
    """
    monkeypatch.setenv("BOT_TOKEN", "123:abcdefghijklmnopqrstuvwxyz0123456")
    monkeypatch.setenv("DATABASE_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("MESSAGE_STATS_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("LOGS_DIR", str(tmp_path / "logs"))
    # Cached singleton — ensure each test sees the env we just set.
    from telegram_invite_bot.config import settings as settings_module

    settings_module.get_settings.cache_clear()


# ── make_container ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_make_container_defaults_install_both_provider_groups() -> None:
    """The bootstrap factory must install both APP-scoped and
    REQUEST-scoped providers — a regression that forgot
    ``DBSessionProvider`` would resolve Settings/Bot fine and then
    deadlock the first handler call when it tries to pull an
    ``AsyncSession``. We verify by resolving one of each scope.
    """
    container = make_container()
    bot = None
    try:
        settings = await container.get(Settings)
        bot = await container.get(Bot)
        assert isinstance(settings, Settings)
        assert isinstance(bot, Bot)
    finally:
        await container.close()
        if bot is not None:
            await bot.session.close()


class _MarkerService:
    """Module-level marker — dishka resolves provider hints via
    ``get_type_hints``, which can't see names defined inside a test
    function. Lives here only for the extras-injection test below.
    """


class _MarkerProvider(Provider):
    """Wired in :func:`test_make_container_accepts_extra_providers`."""

    scope = Scope.APP

    @provide
    def marker(self) -> _MarkerService:
        return _MarkerService()


@pytest.mark.asyncio
async def test_make_container_accepts_extra_providers() -> None:
    """``extra_providers`` lets tests override individual factories
    (e.g. swap Bot for a stub). The branch is small but it's the seam
    every future integration test will use — locking it now means we
    catch a regression that silently drops the extras.
    """
    container = make_container(extra_providers=[_MarkerProvider()])
    bot = None
    try:
        bot = await container.get(Bot)  # base providers still wired
        marker = await container.get(_MarkerService)  # extras installed
        assert isinstance(marker, _MarkerService)
    finally:
        await container.close()
        if bot is not None:
            await bot.session.close()


# ── AppProvider ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_provider_resolves_settings_bot_dispatcher_registry() -> None:
    """One end-to-end resolution covering every APP-scoped factory.

    Together these four lines exercise: ``settings`` (reads env),
    ``bot`` (uses settings.bot.token), ``fsm_storage``, ``dispatcher``
    (depends on storage + registry + settings — the most-wired
    factory), and ``engine_registry`` (depends on settings).
    """
    container = make_async_container(AppProvider(), DBSessionProvider())
    try:
        settings = await container.get(Settings)
        bot = await container.get(Bot)
        dispatcher = await container.get(Dispatcher)
        registry = await container.get(EngineRegistry)
        # T-012: the storage factory now declares ``BaseStorage`` as
        # its return type (so it can swap to SQLite via FSM_BACKEND).
        # Resolving by the concrete class would only work for the
        # default backend; the abstract type is the stable contract.
        storage = await container.get(BaseStorage)
    finally:
        await container.close()
        # Bot owns its own aiohttp session; close it so we don't leak
        # event-loop-bound resources across tests.
        await bot.session.close()

    assert isinstance(settings, Settings)
    assert isinstance(bot, Bot)
    assert isinstance(dispatcher, Dispatcher)
    assert isinstance(registry, EngineRegistry)
    assert isinstance(storage, MemoryStorage)
    # ``dispatcher`` factory wires SessionMiddleware on message +
    # callback_query — the outer middleware lists must be non-empty.
    assert dispatcher.message.outer_middleware._middlewares
    assert dispatcher.callback_query.outer_middleware._middlewares


# ── DBSessionProvider ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_session_provider_factories_each_yield_a_session(
    tmp_path: Path,
) -> None:
    """Each of the five per-DB factories (``users_session``,
    ``economy_session``, …) returns its own async generator that
    opens a session bound to the right DB. We exercise each generator
    directly, bypassing the container, to lock the ``DBName.X``
    argument — a copy-paste typo (``DBName.USERS`` in the economy
    factory) would silently route economy writes to users.db, exactly
    the kind of cross-DB corruption WAL doesn't protect against.

    This test used to carry the note that dishka "can only expose one
    of them through ``container.get(AsyncSession)`` (all five share a
    return type), so the other four were uncovered". That was true and
    is the bug the aliases fixed; the through-the-container half is now
    covered by
    :func:`test_db_session_provider_yields_a_session_per_db`.
    """
    from telegram_invite_bot.config.settings import get_settings
    from telegram_invite_bot.db import build_registry

    get_settings.cache_clear()
    settings = get_settings()
    registry = build_registry(settings)
    provider = DBSessionProvider()

    try:
        for factory in (
            provider.users_session,
            provider.economy_session,
            provider.activity_session,
            provider.moderation_session,
            provider.message_stats_session,
        ):
            gen = factory(registry)
            session = await anext(gen)
            assert isinstance(session, AsyncSession)
            # Drain the generator so the ``async with`` exits cleanly.
            with pytest.raises(StopAsyncIteration):
                await anext(gen)
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_db_session_provider_yields_a_session_per_db() -> None:
    """Each REQUEST-scoped alias resolves to a session bound to its OWN
    DB, and asking for one never hands back another's.

    This is the regression test for the key collision. All five
    factories were annotated ``AsyncIterator[AsyncSession]``, and
    dishka keys a factory by the type it provides — so the five shared
    one key, the last registration (``message_stats_session``) answered
    every request, and the other four were unreachable dead code. A
    caller asking for the users DB got message_stats.db; SQLite would
    have said "no such table: users" at runtime in production, or
    nothing at all where the table names happened to match and the
    write landed in the wrong file.

    Comparing the bound filename per alias is what makes a silent
    relapse impossible: reverting any factory to a bare
    ``AsyncSession`` now fails ``make_container`` outright
    (STRICT_VALIDATION) and, failing that, fails here.
    """
    container = make_async_container(AppProvider(), DBSessionProvider())
    bot: Bot | None = None
    try:
        bot = await container.get(Bot)
        async with container({}, scope=Scope.REQUEST) as request_container:
            bound: set[str] = set()
            for alias, expected in (
                (UsersSession, "users"),
                (EconomySession, "economy"),
                (ActivitySession, "activity"),
                (ModerationSession, "moderation"),
                (MessageStatsSession, "message_stats"),
            ):
                session = await request_container.get(alias)
                assert isinstance(session, AsyncSession)
                bind = session.get_bind()
                assert isinstance(bind, Engine)
                database = bind.url.database
                assert database is not None
                assert Path(database).stem == expected
                bound.add(database)
            assert len(bound) == 5
    finally:
        await container.close()
        if bot is not None:
            await bot.session.close()


@pytest.mark.asyncio
async def test_bot_factory_installs_the_parse_mode_fallback() -> None:
    """The markup net must survive refactors of the Bot factory.

    Without it a single unescaped angle bracket anywhere in the tree
    means the recipient gets no message at all — a failure mode with
    no user-visible symptom other than silence. It is one line in the
    factory and therefore exactly the kind of line a future edit drops
    without noticing.
    """
    from telegram_invite_bot.middlewares.api_parse_mode_fallback import (
        ParseModeFallbackMiddleware,
    )

    container = make_async_container(AppProvider(), DBSessionProvider())
    try:
        bot = await container.get(Bot)
    finally:
        await container.close()
        await bot.session.close()

    assert any(
        isinstance(mw, ParseModeFallbackMiddleware)
        for mw in bot.session.middleware._middlewares  # noqa: SLF001
    ), "Bot factory no longer installs ParseModeFallbackMiddleware"


@pytest.mark.asyncio
async def test_bot_factory_installs_the_length_guard() -> None:
    """The over-length detector must survive refactors of the factory.

    Same reasoning as the markup net above: one line in the factory,
    and the symptom of losing it is not a failure but a quieter log —
    a 400 that no longer says which card produced it.
    """
    from telegram_invite_bot.middlewares.api_length_guard import LengthGuardMiddleware

    container = make_async_container(AppProvider(), DBSessionProvider())
    try:
        bot = await container.get(Bot)
    finally:
        await container.close()
        await bot.session.close()

    assert any(
        isinstance(mw, LengthGuardMiddleware)
        for mw in bot.session.middleware._middlewares  # noqa: SLF001
    ), "Bot factory no longer installs LengthGuardMiddleware"
