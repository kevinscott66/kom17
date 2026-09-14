"""Shared regression fixtures.

:func:`registered_commands` builds the production router tree once per
test and returns the flat set of slash tokens it registers. Three suites
need exactly this answer for different reasons:

* ``test_command_surface`` — "is every ported command still wired?"
* ``test_help_surface`` — "does ``/help`` advertise exactly what's wired?"
* ``test_command_rank_gate`` — "does the rank gate see the same command
  the router does?" (that one needs to know *which handler* owns a
  token, so it takes :func:`registered_command_handlers` instead).

:func:`production_dispatcher` hands out the same tree unreduced, for
``test_chat_scope_coverage``, which asks a question about *routers* —
which chat types each handler can match — that a flat token map has
thrown away by construction.

Keeping one implementation matters more than usual here: if they
drifted, the help card could be validated against a *different* notion
of "live" than the surface audit uses, and they would agree while both
were wrong.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware
from telegram_invite_bot.routers.main_router import build_main_router

if TYPE_CHECKING:
    from pathlib import Path


def _build_dispatcher(tmp_path: Path) -> Dispatcher:
    """The production router tree, assembled and nothing else.

    No outgoing requests, no DB calls — pure construction, so every
    suite that wants to *ask the tree a question* (which commands
    exist, which chat types they accept) starts from the same object
    rather than from its own idea of how the bot is wired.
    """
    settings = Settings(
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )
    registry = build_registry(settings)
    # No ``Bot`` instance built here — introspection touches only filter
    # callbacks on the static router tree, not the wire. Constructing a
    # Bot would open an httpx session that we'd then have to close
    # asynchronously, but this fixture is intentionally synchronous so
    # the parametrised cases share one router build.
    dispatcher = Dispatcher(storage=MemoryStorage())
    throttle = ThrottlingMiddleware(settings.throttling)
    dispatcher.include_router(
        build_main_router(
            registry,
            settings,
            throttle=throttle,
            get_dispatcher=lambda: dispatcher,
        )
    )
    # Best-effort cleanup: this helper is sync because pytest's
    # ``tmp_path`` fixture is sync — the bot's HTTP session never
    # actually opens during introspection, so closing it can be
    # delegated to GC on the throwaway path. ``registry.dispose`` is
    # similarly safe to skip: no connection ever opened.
    return dispatcher


def _build_command_map(tmp_path: Path) -> dict[str, set[str]]:
    """``{slash token: {"module.func", …}}`` for the production tree.

    Introspection (rather than a static scan of ``Command("…")``
    literals) is load-bearing: several handlers register via a starred
    tuple (``Command(*_PROFILE_ALIASES)``), which a regex-based audit
    reports as dead. The router tree is the only source that can't lie.

    A token maps to a *set* because several commands are deliberately
    split across two handlers (a group-only one and a private-only one,
    or a ``magic=F.args`` pair) — that is legitimate, so the value is
    never assumed to be a singleton.
    """
    dispatcher = _build_dispatcher(tmp_path)

    cmds: dict[str, set[str]] = {}

    def _walk(router: object) -> None:
        # Each Router exposes its handler lists per observer; we only
        # need ``message``-side ``Command`` filters here. Callback
        # routing is covered by per-handler e2e suites — the surface
        # this describes is the slash-command vocabulary the user types
        # directly.
        observer = getattr(router, "message", None)
        if observer is not None:
            for handler in observer.handlers:
                fn = handler.callback
                owner = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
                for flt in handler.filters or []:
                    cb = getattr(flt, "callback", None)
                    if isinstance(cb, Command):
                        for token in cb.commands:
                            cmds.setdefault(str(token), set()).add(owner)
        for sub in getattr(router, "sub_routers", []):
            _walk(sub)

    _walk(dispatcher)
    return cmds


@pytest.fixture
def production_dispatcher(tmp_path: Path) -> Dispatcher:
    """The assembled production tree, for suites that introspect routers
    rather than command tokens (see ``test_chat_scope_coverage``)."""
    return _build_dispatcher(tmp_path)


@pytest.fixture
def registered_commands(tmp_path: Path) -> set[str]:
    """Every :class:`Command` token registered on a message handler,
    flattened to a ``set[str]`` so each assertion stays a one-liner."""
    return set(_build_command_map(tmp_path))


@pytest.fixture
def registered_command_handlers(tmp_path: Path) -> dict[str, set[str]]:
    """Same walk as :func:`registered_commands`, but keeping the owning
    handler(s) of every token — "who actually runs when the user types
    this?" — which the rank-gate audit needs and a flat set can't say."""
    return _build_command_map(tmp_path)
