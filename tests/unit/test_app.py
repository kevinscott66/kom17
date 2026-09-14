"""Bootstrap path: ``build_app`` wires container → settings → bot/Dispatcher.

``build_app`` is the one function every entry point (polling runner,
webhook runner, ``python -m telegram_invite_bot``) goes through, and
yet it had no direct test — coverage on ``app.py`` sat at 71% because
the only path that runs ``build_app`` in CI is the real runtime.

A regression in this function — wrong resolution order, swallowed
exception around ``get_me``, missing ``configure_logging`` call —
would break every entry point at once, so a thin unit test that
patches the container and the Telegram round-trip earns its keep.

``close()`` is pinned here for the same reason: it is the only
teardown both runners go through, and #1445 showed that a background
task nobody was watching could take the rest of it down with it.

We don't try to drive the real dishka container here: that's covered
indirectly by every test that builds an ``Application`` via fixtures.
The point is to lock the *bootstrap sequence* on ``app.py`` itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from telegram_invite_bot import app as app_module
from telegram_invite_bot.app import build_app
from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db import EngineRegistry
from telegram_invite_bot.handlers import broadcast as bc_mod


def _settings() -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(),
        paths=PathsConfig(),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


@pytest.mark.asyncio
async def test_build_app_resolves_in_correct_order_and_returns_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the contract: ``container.get`` must be called for
    Settings, Bot, Dispatcher, and EngineRegistry, and the result is
    a fully-populated ``Application`` dataclass.

    The order matters because ``configure_logging`` depends on
    Settings being resolved first — flipping the order would log the
    pre-config "bot authenticated" line through stdlib logging
    instead of loguru, dropping it from any structured-log sink.
    """
    settings = _settings()
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="testbot", id=42))
    dispatcher = MagicMock()
    engines = MagicMock(spec=EngineRegistry)

    resolution_log: list[Any] = []

    async def fake_get(cls: Any) -> Any:
        resolution_log.append(cls)
        return {
            Settings: settings,
            type(bot): bot,
            type(dispatcher): dispatcher,
            EngineRegistry: engines,
        }.get(cls, MagicMock())

    container = MagicMock()
    # ``Bot`` and ``Dispatcher`` are real types in app.py, not MagicMocks —
    # we route ``container.get`` by identity, so register them explicitly.
    from aiogram import Bot, Dispatcher

    async def routed_get(cls: Any) -> Any:
        resolution_log.append(cls)
        if cls is Settings:
            return settings
        if cls is Bot:
            return bot
        if cls is Dispatcher:
            return dispatcher
        if cls is EngineRegistry:
            return engines
        raise AssertionError(f"unexpected container.get({cls!r})")

    container.get = routed_get

    monkeypatch.setattr(app_module, "make_container", lambda: container)

    configure_called: list[Settings] = []
    monkeypatch.setattr(app_module, "configure_logging", configure_called.append)

    application = await build_app()

    assert application.settings is settings
    assert application.bot is bot
    assert application.dispatcher is dispatcher
    assert application.engines is engines
    assert application.container is container

    # configure_logging must run AFTER Settings resolves but BEFORE
    # anything else gets pulled out of the container.
    assert configure_called == [settings]
    assert resolution_log[0] is Settings  # Settings first
    assert resolution_log.index(Bot) > resolution_log.index(Settings)

    # The bootstrap log line is the one observable side-effect of
    # ``get_me`` succeeding — it has to be awaited.
    bot.get_me.assert_awaited_once()


def _recording_application(order: list[str]) -> app_module.Application:
    """An :class:`Application` whose four teardown steps log their name."""
    bot = MagicMock()
    bot.session = MagicMock()
    bot.session.close = AsyncMock(side_effect=lambda: order.append("bot.session.close"))
    engines = MagicMock(spec=EngineRegistry)
    engines.dispose = AsyncMock(side_effect=lambda: order.append("engines.dispose"))
    container = MagicMock()
    container.close = AsyncMock(side_effect=lambda: order.append("container.close"))
    dispatcher = MagicMock()
    dispatcher.storage = MagicMock()
    dispatcher.storage.close = AsyncMock(side_effect=lambda: order.append("storage.close"))

    return app_module.Application(
        container=container,
        settings=_settings(),
        bot=bot,
        dispatcher=dispatcher,
        engines=engines,
    )


@pytest.mark.asyncio
async def test_application_close_disposes_engines_before_container() -> None:
    """The dataclass docstring calls out the ordering: engines must be
    disposed BEFORE the container, because the container doesn't
    await ``dispose`` on APP-scoped non-context-managed resources.
    Flipping the order would leak the SQLite file handles on every
    shutdown.

    ``storage.close`` leads: the FSM connection must go before the bot
    session, and it is the step whose omission left ``fsm.db-wal``
    behind on every restart (#279). It sits inside a
    ``suppress(Exception)``, which is exactly why it needs pinning —
    a storage backend that started raising would otherwise be silently
    skipped with nothing to notice.
    """
    order: list[str] = []
    application = _recording_application(order)

    await application.close()

    assert order == [
        "storage.close",
        "bot.session.close",
        "engines.dispose",
        "container.close",
    ]


@pytest.mark.asyncio
async def test_application_close_is_idempotent() -> None:
    """Since #279 the graph has two teardown callers in webhook mode.

    The ASGI lifespan owns the real shutdown (it is the only hook that
    still runs under SIGTERM) and ``runner/webhook.py`` keeps its
    ``finally`` for the no-signal failure paths. Whichever gets there
    first has to make the other a no-op — ``container.close()`` in
    particular is not safe to call twice.
    """
    order: list[str] = []
    application = _recording_application(order)

    await application.close()
    await application.close()

    assert order == [
        "storage.close",
        "bot.session.close",
        "engines.dispose",
        "container.close",
    ]
    assert application._closed is True


async def _dies_at_once() -> None:
    """A background task that raises on its first pass, like #1445's."""
    msg = "the sweeper hit a broken sink"
    raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_close_finishes_teardown_when_a_background_task_already_died() -> None:
    """#1445 — a task that died hours ago must not abort the shutdown.

    ``cancel()`` on a finished task is a no-op, and awaiting that task
    re-raises whatever killed it. Before the fix that exception left
    ``close()`` at the first drain loop, past ``storage.close``,
    ``bot.session.close``, ``engines.dispose`` and ``container.close``
    — with ``_closed`` already latched, so the second teardown caller
    (#279) returned at the guard and the pools were never disposed at
    all. Asserting the full order rather than "did not raise": the
    point is that every later step still ran.
    """
    order: list[str] = []
    application = _recording_application(order)

    task: asyncio.Task[None] = asyncio.create_task(_dies_at_once())
    application._track_background(task)  # noqa: SLF001 — the unit under test
    with contextlib.suppress(RuntimeError):
        await task

    await application.close()

    assert order == [
        "storage.close",
        "bot.session.close",
        "engines.dispose",
        "container.close",
    ]


@pytest.mark.asyncio
async def test_a_background_task_that_dies_says_so_immediately() -> None:
    """#1445 — the strong reference is what makes the death silent.

    ``asyncio`` prints "Task exception was never retrieved" from
    ``__del__``, and ``_background_tasks`` keeps the task from ever
    being collected, so nothing was ever printed. The sweeper could be
    dead for hours and the first symptom would be FSM states that
    quietly stopped expiring. The task name is asserted because a log
    line that does not say *which* task died sends the operator to the
    wrong sweeper.
    """
    order: list[str] = []
    application = _recording_application(order)
    records: list[str] = []

    handler_id = logger.add(records.append, level="ERROR", format="{message}")
    try:
        task: asyncio.Task[None] = asyncio.create_task(_dies_at_once(), name="fsm_timeout_sweeper")
        application._track_background(task)  # noqa: SLF001 — the unit under test
        with contextlib.suppress(RuntimeError):
            await task
        # ``add_done_callback`` runs through ``call_soon``.
        await asyncio.sleep(0)
    finally:
        logger.remove(handler_id)

    assert any("fsm_timeout_sweeper" in line and "died" in line for line in records), records


@pytest.mark.asyncio
async def test_close_cancels_an_inflight_broadcast_before_the_session_closes() -> None:
    """#1815 — the fan-out task does not live in ``_background_tasks``.

    ``/broadcast`` keeps its send loop in a module-global set of its own
    (it doubles as the single-flight marker, #1496), so the drain loop
    above never touched it. The loop then met a closed aiohttp session
    instead of a ``CancelledError``: ``RuntimeError`` is not a
    ``TelegramAPIError``, so it escaped the per-recipient handler into
    the outer one AND escaped the report send's own suppression — the
    operator got nothing at all, and their last signal was a progress
    edit at some multiple of 100. The abort report exists and is tested
    (#888); it simply never fired.

    Both halves are asserted: the task is actually cancelled, and it is
    already finished by the time ``bot.session.close`` runs, because a
    report sent over a closed session is the same silence as no report.
    """
    order: list[str] = []
    application = _recording_application(order)

    started = asyncio.Event()

    async def _forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task: asyncio.Task[None] = asyncio.create_task(_forever())
    # Standing in for the confirm handler's own two lines.
    bc_mod._BACKGROUND_TASKS.add(task)
    task.add_done_callback(bc_mod._BACKGROUND_TASKS.discard)
    await started.wait()

    pending_at_session_close: list[bool] = []

    async def _closing_session() -> None:
        order.append("bot.session.close")
        pending_at_session_close.append(not task.done())

    # Through an ``Any`` alias: the field is a ``MagicMock`` at runtime,
    # but ``Application.bot`` is typed as ``Bot`` and mypy reads the
    # rebind as ``method-assign``.
    recording_bot: Any = application.bot
    recording_bot.session.close = AsyncMock(side_effect=_closing_session)

    try:
        await application.close()
    finally:
        task.cancel()
        bc_mod._BACKGROUND_TASKS.discard(task)

    assert pending_at_session_close == [False]
    assert task.cancelled()
    assert not bc_mod._BACKGROUND_TASKS
