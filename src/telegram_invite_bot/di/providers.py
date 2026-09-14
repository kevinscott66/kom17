"""dishka providers for the new architecture.

Stage 1 wired ``Settings``, aiogram ``Bot``, ``Dispatcher`` (all APP scope).
Stage 2 adds the DB layer: a single :class:`EngineRegistry` lives at APP
scope; per-request ``AsyncSession`` instances are produced at REQUEST
scope, scoped to the DB the caller asks for.

Sessions are NOT injected through dishka today. Handlers get theirs
from ``middlewares/session.py``'s ``SessionMiddleware``, which owns the
commit/rollback boundary; ``DBSessionProvider`` below is scaffolding
nothing resolves yet — ``FromDishka`` appears nowhere else in ``src``.
An earlier version of this docstring described the opposite, and its
suggested annotation (``Annotated[AsyncSession, FromDishka[...]]``) is
not dishka syntax either: it is ``FromDishka[X]`` or
``Annotated[X, FromComponent("...")]``.

Each per-DB factory provides its own ``NewType`` rather than a bare
``AsyncSession`` because dishka keys factories by the type they
provide. Five factories all annotated ``AsyncIterator[AsyncSession]``
collapsed into ONE key and the last registration silently won, so a
resolved session was message_stats.db no matter which DB the caller
meant — and SQLite would only say so at runtime, as "no such table",
or not at all when the table names happen to match. The container is
built with ``STRICT_VALIDATION`` (``di/container.py``) so a future
duplicate key fails at import instead of eating four providers in
silence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import NewType

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from dishka import Provider, Scope, provide
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import Settings, get_settings
from telegram_invite_bot.db import EngineRegistry, build_registry
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.fsm.sqlite_storage import SQLiteStorage
from telegram_invite_bot.middlewares.api_length_guard import LengthGuardMiddleware
from telegram_invite_bot.middlewares.api_parse_mode_fallback import (
    ParseModeFallbackMiddleware,
)


class AppProvider(Provider):
    """App-scoped: process-lifetime singletons."""

    scope = Scope.APP

    @provide
    def settings(self) -> Settings:
        return get_settings()

    @provide
    async def bot(self, settings: Settings) -> Bot:
        bot = Bot(
            token=settings.bot.token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        # Telegram parses a message whole or not at all, so one stray
        # angle bracket anywhere in the body means the recipient gets
        # nothing. The fallback turns that total loss into a plain-text
        # delivery plus a loud ERROR naming the culprit — see the module
        # docstring; it is a net under the escaping, not a licence to
        # skip it.
        bot.session.middleware(ParseModeFallbackMiddleware())
        # The other way a card is lost whole: over 4096 parsed
        # characters, Telegram rejects it and the journal records a
        # generic 400 that names no renderer. /admin_help and
        # /admin_routes sat over the ceiling unnoticed for the entire
        # life of the deployment. This one measures before sending and
        # names the culprit; it deliberately does not truncate.
        bot.session.middleware(LengthGuardMiddleware())
        return bot

    @provide
    def fsm_storage(self, settings: Settings) -> BaseStorage:
        # Backend selection is opt-in via ``FSM_BACKEND`` (default
        # ``memory``). The ``SQLiteStorage`` path is what makes /cpc
        # flows survive a restart — but we don't switch a running
        # deploy without an operator flipping the env var, since the
        # SQLite schema starts empty and any user mid-flow at the
        # moment of cutover loses their state once exactly.
        #
        # The choice is logged because it is otherwise invisible in ops:
        # with ``memory`` every restart silently drops in-flight flows,
        # and the only way to tell the two apart on a live host was to
        # check whether the SQLite file exists. One startup line makes
        # "did the env var actually take?" answerable from the journal.
        if settings.fsm_storage.backend == "sqlite":
            logger.bind(component="bootstrap").info(
                "FSM storage: sqlite ({path})", path=settings.fsm_storage.sqlite_path
            )
            return SQLiteStorage(settings.fsm_storage.sqlite_path)
        logger.bind(component="bootstrap").info(
            "FSM storage: memory — in-flight flows are lost on restart"
        )
        return MemoryStorage()

    @provide
    def dispatcher(
        self, storage: BaseStorage, registry: EngineRegistry, settings: Settings
    ) -> Dispatcher:
        from telegram_invite_bot.middlewares.session import SessionMiddleware
        from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware
        from telegram_invite_bot.routers.main_router import build_main_router

        dispatcher = Dispatcher(storage=storage)
        # Throttling runs FIRST — before sessions open, before any
        # repo work, before any I/O. A flooded request that we'd drop
        # anyway should not have already opened a SQLite connection
        # and started a transaction. One shared instance across event
        # types so a single user's bucket spans both messages and
        # callbacks (they're the same human, and the rate limit is
        # protecting against the same outbound-API scarcity).
        throttle = ThrottlingMiddleware(settings.throttling)
        dispatcher.message.outer_middleware(throttle)
        dispatcher.callback_query.outer_middleware(throttle)
        # Session middleware fires after throttling, on ``message`` and
        # ``callback_query`` ONLY — the two lines below are the whole of
        # its coverage. ``pre_checkout_query``, ``my_chat_member`` and
        # ``chat_member`` get no session: their handlers take the
        # registry and open their own (``handlers/group_events.py:359``
        # in the ``my_chat_member`` handler, ``:1323`` on the
        # ``chat_member`` leave path). Do not write
        # ``async def h(event, session)`` for
        # those event types expecting DI to fill it — it raises at first
        # fire, in production, on update types that only arrive with real
        # group traffic. ``handlers/topup.py:1066`` had to hand-mount
        # ``LanguageMiddleware`` on ``pre_checkout_query`` for exactly
        # this reason. (An earlier version of this comment claimed
        # coverage "for every event type aiogram routes" and used
        # ``edited_message`` as its example — an update the process never
        # even receives, since ``webhook/lifespan.py:78`` derives
        # ``allowed_updates`` from the registered observers.)
        # Two instances (vs one shared) keeps
        # the per-event-type context isolated — message and
        # callback_query updates can interleave under the polling
        # loop, and we don't want a stray mutable middleware to bleed
        # state across them.
        dispatcher.message.outer_middleware(SessionMiddleware(registry))
        dispatcher.callback_query.outer_middleware(SessionMiddleware(registry))
        # ``/admin_middlewares`` reads the dispatcher's middleware
        # chains at message-handle time. We can't pass the Dispatcher
        # to ``build_main_router`` directly because the dispatcher's
        # own ``include_router`` happens *after* the call returns —
        # the router would freeze a half-wired snapshot. A getter
        # closure resolved at handle time always sees the fully-
        # wired instance. Same pattern as ``/admin_routes``'
        # ``get_root``.
        dispatcher.include_router(
            build_main_router(
                registry,
                settings,
                throttle=throttle,
                get_dispatcher=lambda: dispatcher,
            )
        )
        return dispatcher

    @provide
    def engine_registry(self, settings: Settings) -> EngineRegistry:
        # NOTE: returned synchronously despite holding async engines —
        # the engines are *lazy* (no connections opened until first use).
        # Disposal is handled in ``Application.close``.
        return build_registry(settings)


UsersSession = NewType("UsersSession", AsyncSession)
EconomySession = NewType("EconomySession", AsyncSession)
ActivitySession = NewType("ActivitySession", AsyncSession)
ModerationSession = NewType("ModerationSession", AsyncSession)
MessageStatsSession = NewType("MessageStatsSession", AsyncSession)


class DBSessionProvider(Provider):
    """Request-scoped: a fresh session per DB per handler invocation.

    One handler usually touches a single DB; if it needs two, it asks for
    both factories. We do NOT share a session across DBs — they're
    different files, different metadata, different commit semantics.

    Ask for the ``NewType`` (``UsersSession``, ``EconomySession``, …),
    never for a bare ``AsyncSession``: the alias IS the dishka key, and
    a bare annotation is what made all five factories one key. At
    runtime a ``NewType`` call is the identity function, so the value
    handed to the caller is the very session the registry opened.
    """

    scope = Scope.REQUEST

    @provide
    async def users_session(self, registry: EngineRegistry) -> AsyncIterator[UsersSession]:
        async with registry.session(DBName.USERS)() as session:
            yield UsersSession(session)

    @provide
    async def economy_session(self, registry: EngineRegistry) -> AsyncIterator[EconomySession]:
        async with registry.session(DBName.ECONOMY)() as session:
            yield EconomySession(session)

    @provide
    async def activity_session(self, registry: EngineRegistry) -> AsyncIterator[ActivitySession]:
        async with registry.session(DBName.ACTIVITY)() as session:
            yield ActivitySession(session)

    @provide
    async def moderation_session(
        self, registry: EngineRegistry
    ) -> AsyncIterator[ModerationSession]:
        async with registry.session(DBName.MODERATION)() as session:
            yield ModerationSession(session)

    @provide
    async def message_stats_session(
        self, registry: EngineRegistry
    ) -> AsyncIterator[MessageStatsSession]:
        async with registry.session(DBName.MESSAGE_STATS)() as session:
            yield MessageStatsSession(session)
