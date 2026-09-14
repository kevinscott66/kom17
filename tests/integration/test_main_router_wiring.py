"""Whole-tree dispatcher smoke: every ported command must route, not UNHANDLED.

Per-handler e2e tests already cover behaviour. The gap they leave is
**wiring**: a forgotten ``root.include_router(...)`` in
``main_router.py`` would still let every isolated test pass while the
production dispatcher silently drops the command into the legacy
fallback (or, with the feature flag on but the router unwired, into
the void). This single integration test instantiates the FULL main
router exactly like ``di/providers.py`` does and asserts that each
ported command reaches a handler.

Add a new line per port. If a future stage forgets to wire its router,
the relevant assertion flips to ``UNHANDLED`` and CI catches it before
the silent regression ships.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Dice, Message, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    EconomyConfig,
    FeatureFlags,
    HelpConfig,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    StatsConfig,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import (
    EconomyBase,
    MessageStatsBase,
    UsersBase,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.group_pay import build_router as build_group_pay_router
from telegram_invite_bot.middlewares.ai_rate_limit import AiRateLimitMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware
from telegram_invite_bot.routers import main_router
from telegram_invite_bot.routers.main_router import build_main_router
from telegram_invite_bot.services.weather_service import WeatherService


@pytest.fixture
async def full_dispatcher(
    tmp_path: Path,
) -> AsyncIterator[tuple[Bot, Dispatcher]]:
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        help=HelpConfig(),
        stats=StatsConfig(),
        features=FeatureFlags(),
    )
    registry = build_registry(settings)
    # Create every schema a ported handler can touch — missing tables
    # would mask wiring bugs as plain runtime errors.
    for base, db in (
        (UsersBase, DBName.USERS),
        (EconomyBase, DBName.ECONOMY),
        (MessageStatsBase, DBName.MESSAGE_STATS),
    ):
        engine = registry.engine(db)
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)

    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    # Same wiring as di/providers.py — if THAT drifts, this test should
    # be updated in the same PR, because the production dispatcher is
    # what users actually hit.
    dispatcher.message.outer_middleware(SessionMiddleware(registry))
    throttle = ThrottlingMiddleware(settings.throttling)
    dispatcher.include_router(
        build_main_router(
            registry,
            settings,
            throttle=throttle,
            get_dispatcher=lambda: dispatcher,
        )
    )

    try:
        yield bot, dispatcher
    finally:
        await bot.session.close()
        await registry.dispose()


def _capture(bot: Bot, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sink: list[dict[str, Any]] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendMessage":
            sink.append({"kind": "text", "text": method.text})
            return Message(
                message_id=1,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        if name == "SendPhoto":
            sink.append({"kind": "photo", "caption": getattr(method, "caption", None)})
            return Message(
                message_id=1,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=getattr(method, "caption", None) or "ok",
            )
        if name == "SendDice":
            sink.append({"kind": "dice", "emoji": method.emoji})
            return Message(
                message_id=1,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                dice=Dice(emoji=method.emoji or "🎲", value=4),
            )
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return sink


def _msg(text: str, *, chat_type: str = "private", chat_id: int = 7777) -> Update:
    chat: dict[str, Any] = {"id": chat_id, "type": chat_type}
    if chat_type != "private":
        chat["title"] = "T"
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": chat,
                "from": {
                    "id": 7777,
                    "is_bot": False,
                    "first_name": "T",
                    "language_code": "ru",
                },
                "text": text,
            },
        }
    )


# Each row: (command_text, chat_type) → must NOT be UNHANDLED.
# When a new command lands, append a row here. When a command intentionally
# falls through to legacy in some scope, document it in this same table.
_PORTED_PRIVATE = [
    "/start",
    "/profile",
    "/help",
    "/balance",
    "/weather Москва",
    # /ai with no args → static help card. /ask without prompt → warning copy
    # (no API call; no key needed in fixture). Both must reach the new router.
    "/ai",
    "/ask",
    # Stage 14: support module. /feedback without text → usage warning
    # (no DB write); /faq and /check → static text.
    "/feedback",
    "/faq",
    "/check",
    # Stage 15: VIP-stub commands (static text, six handlers,
    # multiple aliases each — every alias gets covered by the
    # per-handler e2e test; here we just verify wiring).
    "/vip",
    "/vip_shop",
    "/emojis",
    "/voice_settings",
    "/voice_stats",
    # Stage 16: read-only shop. Empty catalog still produces a reply
    # ("shop is empty"); /inventory similarly replies with an empty
    # state. Both private-only — group fallthrough is asserted in
    # the per-handler e2e suite.
    "/shop",
    "/магазин",
    "/kom_shop",
    "/inventory",
    "/inv",
    "/инвентарь",
    # Stage 17: /buy with no arg renders a usage hint (no DB hit).
    # The transactional path is covered in the per-handler e2e suite;
    # here we just need to confirm the wiring catches the bare command.
    "/buy",
    "/купить",
    # Stage 18: vanity guess variants of /roll and /flip. Bare /roll,
    # invalid arg, and the bet form (/roll 100 4) intentionally fall
    # through — only the exact "1 arg in 1..6" / "1 arg in heads-or-
    # tails" forms are owned by the new pipeline. Tested in the
    # per-handler e2e suite.
    "/roll 4",
    "/flip орёл",
    # Stage 21: heartbeat pair. /ping renders three metrics, /botcheck
    # is a static "alive" reply. Aliases handled in the per-handler
    # e2e; here we just confirm the wiring catches the canonical form.
    "/ping",
    "/botcheck",
    # Stage 22: /whoami /me /kom_whoami are folded into the profile
    # handler as legacy self-profile aliases (bot.py:41258). Private-
    # only — group calls still fall through to legacy.
    "/whoami",
    "/me",
    "/kom_whoami",
    # Stage 23: /joke18 local adult-jokes pool (private only — group
    # path still gated by the un-migrated "ai" feature flag in legacy).
    "/joke18",
    "/шутка18",
    "/анекдот18",
    "/kom_joke18",
    # Stage 26: /lang language switch (private only — group path needs
    # the un-migrated is_chat_owner infra). Stub from Stage 15 vip
    # router has been removed; this entry now exercises the real
    # handler in ``handlers.language`` that persists to
    # ``user_settings.language``.
    "/lang",
    "/language",
    "/язык",
    "/kom_lang",
    # Stage 30: /settings + /настройки fold into the language router —
    # legacy cmd_settings (bot.py:24771) is just the language picker
    # with the same callback_data. Private only (matches /lang).
    "/settings",
    "/настройки",
    # Stage 27: /timezone preference (private only — same scope as /lang).
    # Bare form shows the current value or help; no DB hit if unset
    # (the user has no row in user_settings) but ``touch`` still
    # populates ``users.user_id`` first so the wiring test sees a reply.
    "/timezone",
    "/часовой_пояс",
    "/tz",
    "/kom_timezone",
]
# Stage 28: bare /time renders MSK + optional user-tz block. Group calls
# are also accepted (read-only, no economy / admin checks needed).
# The arg-form (/time London) intentionally falls through to legacy
# where the geocoder still lives; the wiring assertion below covers
# the bare-command path only.
_PORTED_ANY_CHAT_TIME = [
    "/time",
    "/время",
    "/time_msk",
    "/kom_time",
]
_PORTED_ANY_CHAT = [
    # /stats is the only command that's also valid in groups.
    ("/stats", "private"),
    ("/stats", "supergroup"),
    # /dice is group-only in legacy modular handler, but the new port
    # accepts it in private chats too (no FSM, no economy, harmless).
    ("/dice", "private"),
    ("/dice", "supergroup"),
    # Stage 19: read-only bond leaderboards. Group-only by design;
    # private chats fall through to legacy ("только в группе").
    ("/marriages", "supergroup"),
    ("/relations", "supergroup"),
    # Stage 20: /top messages-mode leaderboard. Group-only; private
    # /top defaults to the balance ladder in legacy and falls through
    # here (covered by the per-handler suite).
    ("/top", "supergroup"),
    ("/top messages 7", "supergroup"),
    # #2007: /donate funds the current group's rating out of the
    # caller's wallet, so it only makes sense where there is a group to
    # fund; in private it answers the chat-scope refusal. With no
    # argument it prints the usage card, which is a read of the wallet
    # and no write.
    ("/donate", "supergroup"),
    # Stage 28: /time both in private and groups (read-only).
    *[(cmd, "private") for cmd in _PORTED_ANY_CHAT_TIME],
    *[(cmd, "supergroup") for cmd in _PORTED_ANY_CHAT_TIME],
]


@pytest.mark.parametrize("command", _PORTED_PRIVATE)
async def test_private_ported_commands_route(
    full_dispatcher: tuple[Bot, Dispatcher],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    bot, dispatcher = full_dispatcher
    sink = _capture(bot, monkeypatch)

    # ``/weather Москва`` is the only ported private command that
    # reaches an *external* HTTP service (Open-Meteo geocoding +
    # forecast) on the happy path. The per-handler e2e suite mocks
    # ``httpx.MockTransport``; here we only need to prove the wiring
    # reaches the handler, so we short-circuit the service to a
    # cheap exception that still produces a user-facing reply. Before
    # this hook the wiring test took 8.5s on a cold geocoder call —
    # 40× slower than the next case — and would flake whenever
    # CI lost outbound network.
    if command.startswith("/weather"):
        from telegram_invite_bot.services import weather_service as ws

        async def fake_lookup(_self: Any, _query: str) -> Any:
            raise ws.CityNotFoundError("stub")

        monkeypatch.setattr(ws.WeatherService, "lookup", fake_lookup)

    result = await dispatcher.feed_update(bot, _msg(command))
    assert result is not UNHANDLED, f"{command} fell through to legacy"
    assert sink, f"{command} routed but produced no reply"


@pytest.mark.parametrize(("command", "chat_type"), _PORTED_ANY_CHAT)
async def test_chat_scoped_ported_commands_route(
    full_dispatcher: tuple[Bot, Dispatcher],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    chat_type: str,
) -> None:
    bot, dispatcher = full_dispatcher
    sink = _capture(bot, monkeypatch)
    result = await dispatcher.feed_update(
        bot, _msg(command, chat_type=chat_type, chat_id=-100 if chat_type != "private" else 7777)
    )
    assert result is not UNHANDLED, f"{command} ({chat_type}) fell through"
    assert sink, f"{command} ({chat_type}) routed but produced no reply"


async def test_unrouted_command_still_falls_through(
    full_dispatcher: tuple[Bot, Dispatcher],
) -> None:
    """The escape hatch: commands the new pipeline doesn't own MUST UNHANDLED
    so the legacy bridge gets a chance to run them.
    """
    _, dispatcher = full_dispatcher
    # No request capture — if the dispatcher tried to reply, the bot's
    # session would 401 on a real outbound call, which would be a
    # different failure mode. UNHANDLED is the only acceptable result.
    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        result = await dispatcher.feed_update(bot, _msg("/notported"))
        assert result is UNHANDLED
    finally:
        await bot.session.close()


async def test_resolved_update_types_match_the_handlers_we_register(
    full_dispatcher: tuple[Bot, Dispatcher],
) -> None:
    """The webhook subscribes to exactly this set (``webhook/lifespan.py``).

    ``setup_webhook`` passes ``resolve_used_update_types()`` to Telegram
    as ``allowed_updates``, so this list *is* the production
    subscription — anything missing from it is an update type Telegram
    will never deliver, and the handler for it would sit dead with no
    error anywhere.

    Pinning it makes that consequence visible: registering the first
    ``chat_member`` handler (a type Telegram withholds by default) flips
    this assertion, and whoever adds it confirms the widened
    subscription instead of discovering the silence in prod.

    That is exactly what happened — #245(d) registered one on
    ``handlers/group_events`` to catch the invite-link and approved-
    request joins, which produce no ``new_chat_members`` service
    message. The widened subscription is confirmed here: ``chat_member``
    is now part of what the webhook asks Telegram for. Note it is only
    *delivered* while the bot is an administrator in the chat, which the
    captcha needs anyway.
    """
    _, dispatcher = full_dispatcher
    assert sorted(dispatcher.resolve_used_update_types()) == [
        "callback_query",
        "chat_member",
        "message",
        "my_chat_member",
        "pre_checkout_query",
    ]


async def test_weather_service_is_built_once_for_the_whole_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#262(d): /weather, /city, /time and the AI router share ONE instance.

    ``WeatherService`` carries a TTL cache and a ``KeyedLocks`` map, so a
    second instance means a second cold cache and a second lock map: two
    routers can then issue duplicate concurrent upstream calls for the
    same city and neither warms the other's cache.

    ``handlers/ai.build_router`` constructs its own fallback instance when
    the caller passes none, which is exactly the omission this test
    guards. Counting constructions during ``build_main_router`` catches a
    re-introduced omission (count 2) and any future router that forgets
    to take the shared instance (count 3+).
    """
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        help=HelpConfig(),
        stats=StatsConfig(),
        features=FeatureFlags(),
    )
    registry = build_registry(settings)
    built: list[WeatherService] = []
    original_init = WeatherService.__init__

    def counting_init(self: WeatherService, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(WeatherService, "__init__", counting_init)
    dispatcher = Dispatcher(storage=MemoryStorage())
    try:
        build_main_router(
            registry,
            settings,
            throttle=ThrottlingMiddleware(settings.throttling),
            get_dispatcher=lambda: dispatcher,
        )
    finally:
        await registry.dispose()

    assert len(built) == 1


async def test_ai_rate_limit_is_built_once_for_the_whole_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1103: /ai, /ask and /quote share ONE per-minute token bucket.

    The bucket table lives on the middleware instance, so a second
    instance is a second full allowance: one user could spend the
    per-minute budget on /ai and then spend it again on /quote, against
    the same paid DeepSeek key. The quota ledger does not close the gap
    either — it counts daily requests, not the burst rate this bucket
    exists to cap.

    ``handlers/ai.build_router`` and ``handlers/quotes.build_router``
    each construct a fallback instance when the caller passes none,
    which keeps them usable standalone in tests and is exactly the
    omission this test guards. Counting constructions during
    ``build_main_router`` catches a re-introduced omission (count 2) and
    any future AI-facing router that forgets to take the shared
    instance (count 3+).
    """
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        help=HelpConfig(),
        stats=StatsConfig(),
        features=FeatureFlags(),
    )
    registry = build_registry(settings)
    built: list[AiRateLimitMiddleware] = []
    original_init = AiRateLimitMiddleware.__init__

    def counting_init(self: AiRateLimitMiddleware, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(AiRateLimitMiddleware, "__init__", counting_init)
    dispatcher = Dispatcher(storage=MemoryStorage())
    try:
        build_main_router(
            registry,
            settings,
            throttle=ThrottlingMiddleware(settings.throttling),
            get_dispatcher=lambda: dispatcher,
        )
    finally:
        await registry.dispose()

    assert len(built) == 1


async def test_group_pay_min_withdrawal_is_wired_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#957: the operator's GROUP_TREASURY_MIN_WITHDRAWAL must reach gate 5.

    The settings field always loaded; what was missing was the keyword at
    this call site, so ``/group_pay`` compared the requested amount
    against the hard-coded default and silently ignored an operator who
    had raised the floor to protect a group treasury.

    The per-handler tests cannot catch that: they build the router
    themselves and pass ``min_withdrawal`` by hand, so they stay green
    with the wiring removed. The assertion belongs here, with the wiring.
    """
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        help=HelpConfig(),
        stats=StatsConfig(),
        features=FeatureFlags(),
        economy=EconomyConfig(GROUP_TREASURY_MIN_WITHDRAWAL=4321),
    )
    # Deliberately not the DEFAULT_MIN_WITHDRAWAL of 1000: a test that
    # passes the default would pass with the wiring removed.
    assert settings.economy.group_treasury_min_withdrawal == 4321
    registry = build_registry(settings)
    seen: list[int] = []
    # Same object main_router imported, taken from its home module:
    # reading it off main_router is an implicit re-export mypy rejects.
    original = build_group_pay_router

    def spy(*args: Any, **kwargs: Any) -> Router:
        seen.append(kwargs["min_withdrawal"])
        return original(*args, **kwargs)

    monkeypatch.setattr(main_router, "build_group_pay_router", spy)
    dispatcher = Dispatcher(storage=MemoryStorage())
    try:
        build_main_router(
            registry,
            settings,
            throttle=ThrottlingMiddleware(settings.throttling),
            get_dispatcher=lambda: dispatcher,
        )
    finally:
        await registry.dispose()

    assert seen == [4321]
