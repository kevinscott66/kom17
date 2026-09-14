"""Regression: M-P-1 — per-user rate limit on /ai, /ask, /voice.

The audit (``audits/01_profile_stats_ai_vip.md``) flagged the AI and
TTS routers as having NO per-user rate limit. This test pins:

* :class:`AiRateLimitMiddleware` admits the first 10 requests in a
  minute and rejects the 11th with the localized cool-down reply.
* :class:`VoiceRateLimitMiddleware` admits the first 5 and rejects
  the 6th.
* The rejected reply is bilingual (EN copy when user.language_code
  is ``"en"``).
* #1537: the bare ``/voice`` form is free — it does not spend a
  token, does not create a bucket row, and does not refund one.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.filters.command import CommandObject
from aiogram.types import User

from telegram_invite_bot.middlewares.ai_rate_limit import (
    AiRateLimitMiddleware,
    VoiceRateLimitMiddleware,
)


class _MockEvent:
    def __init__(self, user_id: int, language_code: str = "ru") -> None:
        self.from_user = User(
            id=user_id,
            is_bot=False,
            first_name="Test",
            language_code=language_code,
        )
        self.answer = AsyncMock()


@pytest.fixture
def voice_bare_command() -> CommandObject:
    """What the ``Command`` filter hands a bare ``/voice`` (#1537)."""
    return CommandObject(prefix="/", command="voice", args=None)


@pytest.fixture
def voice_args_command() -> CommandObject:
    """What it hands ``/voice <text>`` — the form that pays."""
    return CommandObject(prefix="/", command="voice", args="привет мир")


@pytest.mark.asyncio
async def test_ai_rate_limit_admits_first_ten_within_a_minute() -> None:
    """Default AI bucket (capacity=10) admits the first 10 calls in
    rapid succession with no refill — frozen clock.
    """
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=42)

    for _ in range(10):
        assert await middleware(handler, event, {}) == "ok"
    assert handler.await_count == 10
    event.answer.assert_not_called()


@pytest.mark.asyncio
async def test_ai_rate_limit_rejects_eleventh_within_a_minute() -> None:
    """The 11th /ai within a minute is throttled with a localized
    cool-down reply, and the handler is NOT invoked.
    """
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=42, language_code="ru")

    for _ in range(10):
        await middleware(handler, event, {})

    # 11th call — bucket empty, frozen clock → reject.
    result = await middleware(handler, event, {})
    assert result is None
    # Handler was invoked exactly 10 times, never 11.
    assert handler.await_count == 10
    # Localized rejection rendered to the user.
    event.answer.assert_called_once()
    body = event.answer.call_args[0][0]
    assert "Слишком часто" in body
    assert "Подожди" in body


@pytest.mark.asyncio
async def test_ai_rate_limit_english_rejection() -> None:
    """EN language_code → English cool-down copy."""
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=42, language_code="en")
    for _ in range(10):
        await middleware(handler, event, {})

    assert await middleware(handler, event, {}) is None
    body = event.answer.call_args[0][0]
    assert "Too fast" in body
    assert "Wait" in body


@pytest.mark.asyncio
async def test_voice_rate_limit_admits_five_rejects_sixth() -> None:
    """Voice bucket (capacity=5) — 6th call rejected, handler unhit."""
    middleware = VoiceRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=42)

    for _ in range(5):
        assert await middleware(handler, event, {}) == "ok"

    result = await middleware(handler, event, {})
    assert result is None
    assert handler.await_count == 5
    event.answer.assert_called_once()
    body = event.answer.call_args[0][0]
    assert "Слишком часто" in body


@pytest.mark.asyncio
async def test_ai_router_attaches_rate_limit_middleware() -> None:
    """The ``ai`` router has :class:`AiRateLimitMiddleware` attached.

    Pinning the wiring keeps a future refactor that drops the
    middleware (e.g. "let's move it dispatcher-level") visible as a
    failing test instead of a silent regression — the audit fix
    depends on the middleware actually firing on every /ai update.
    """
    from telegram_invite_bot.config.settings import (
        AiConfig,
        AiQuotaSettings,
    )
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.handlers.ai import build_router

    # Build a router with stub configs / registry; we only inspect
    # the middleware chain, never feed updates through. The OpenAI
    # config arg was dropped when /ai went DeepSeek-only (dcb2e21).
    registry = EngineRegistry.__new__(EngineRegistry)
    router = build_router(registry, AiConfig(), AiQuotaSettings())
    middlewares = list(router.message.middleware)
    assert any(isinstance(m, AiRateLimitMiddleware) for m in middlewares), (
        f"AiRateLimitMiddleware missing from /ai router chain: {middlewares!r}"
    )


@pytest.mark.asyncio
async def test_quotes_router_attaches_rate_limit_middleware() -> None:
    """The ``quotes`` router carries the same gate as ``/ai`` (#499).

    ``/quote`` reaches the owner's paid DeepSeek account, and the daily
    quota it also rides exempts developer ids outright — so before this
    wiring existed one exempt user could spend without any ceiling at
    all. Pinning the chain keeps that hole from re-opening quietly.
    """
    from telegram_invite_bot.config.settings import (
        AiConfig,
        AiQuotaSettings,
    )
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.handlers.quotes import build_router

    registry = EngineRegistry.__new__(EngineRegistry)
    router = build_router(registry, AiConfig(), AiQuotaSettings())
    middlewares = list(router.message.middleware)
    assert any(isinstance(m, AiRateLimitMiddleware) for m in middlewares), (
        f"AiRateLimitMiddleware missing from /quote router chain: {middlewares!r}"
    )


@pytest.mark.asyncio
async def test_voice_router_attaches_rate_limit_middleware() -> None:
    """The ``vip_emoji_voice`` router has
    :class:`VoiceRateLimitMiddleware` attached.
    """
    from telegram_invite_bot.config.settings import (
        FeatureFlags,
        OpenAiConfig,
        TtsConfig,
    )
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.handlers.vip_emoji_voice import build_router

    registry = EngineRegistry.__new__(EngineRegistry)
    router = build_router(registry, OpenAiConfig(), TtsConfig(), FeatureFlags())
    middlewares = list(router.message.middleware)
    assert any(isinstance(m, VoiceRateLimitMiddleware) for m in middlewares), (
        f"VoiceRateLimitMiddleware missing from /voice router chain: {middlewares!r}"
    )


@pytest.mark.asyncio
async def test_bucket_table_is_lru_capped() -> None:
    """The per-user bucket table must not grow without bound.

    Each of these middlewares is built once at router-wiring time and
    lives for the whole process, so an uncapped dict would keep one
    entry per user_id that ever ran the command — forever. Nine
    instances are wired today. ``ThrottlingMiddleware`` already caps
    its own table for exactly this reason; this pins the same
    behaviour for the bucket base.
    """
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]
    middleware._MAX_TRACKED_USERS = 3  # type: ignore[misc]

    handler = AsyncMock(return_value="ok")
    for uid in range(1, 11):
        await middleware(handler, _MockEvent(user_id=uid), {})

    assert len(middleware._buckets) == 3
    # The survivors are the most recently seen, not the first ones in.
    assert set(middleware._buckets) == {8, 9, 10}


@pytest.mark.asyncio
async def test_throttled_user_is_not_evicted_by_their_own_flood() -> None:
    """A rejected call must still refresh the LRU position.

    A flooder's last *admitted* call keeps aging while they keep
    knocking, so if only admitted calls touched the order, a stream of
    newcomers would age them out of the table — and an evicted bucket
    is re-created full. The cap would then be a rate-limit bypass
    rather than a memory bound. Here the flooder knocks between every
    newcomer, so the only thing keeping them in the table is the
    touch on the rejected call.
    """
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]
    middleware._MAX_TRACKED_USERS = 3  # type: ignore[misc]

    handler = AsyncMock(return_value="ok")
    flooder = _MockEvent(user_id=777)
    for _ in range(10):  # drain the flooder's bucket
        await middleware(handler, flooder, {})
    assert await middleware(handler, flooder, {}) is None

    for uid in (1, 2, 3, 4, 5):
        await middleware(handler, _MockEvent(user_id=uid), {})
        # Still empty — the reject also keeps the slot warm.
        assert await middleware(handler, flooder, {}) is None

    assert 777 in middleware._buckets
    assert len(middleware._buckets) == 3
    assert handler.await_count == 15  # 10 flooder + 5 newcomers, nothing more


@pytest.mark.asyncio
async def test_cooldown_follows_the_stored_language_not_the_telegram_locale() -> None:
    """``data["lang"]`` wins over ``user.language_code``.

    ``LanguageMiddleware`` is an OUTER middleware on the root router and
    every bucket gate is an INNER one on a child router, so the resolved
    language is already in ``data`` when the gate runs. Reading
    ``language_code`` instead meant a user on an English Telegram client
    who had chosen ``/lang ru`` got the whole bot in Russian and this one
    line in English — at the exact moment they were being refused.
    """
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=99, language_code="en")
    # ``_MockEvent`` is not a ``TelegramObject``; the alias keeps the
    # stand-in out of mypy's way without loosening the real signature.
    target: Any = event

    for _ in range(10):
        await middleware(handler, target, {"lang": "ru"})
    assert await middleware(handler, target, {"lang": "ru"}) is None

    body = event.answer.call_args[0][0]
    assert "Слишком часто" in body, body


@pytest.mark.asyncio
async def test_cooldown_falls_back_to_the_telegram_locale_without_data_lang() -> None:
    """No ``lang`` in ``data`` — the Telegram locale is still the best
    signal available, so the fallback must survive (a gate wired outside
    the root chain, and every unit test above, relies on it)."""
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=98, language_code="en")
    target: Any = event

    for _ in range(10):
        await middleware(handler, target, {})
    assert await middleware(handler, target, {}) is None

    body = event.answer.call_args[0][0]
    assert "Too fast" in body, body


@pytest.mark.asyncio
async def test_bare_voice_command_is_free(voice_bare_command: CommandObject) -> None:
    """#1537: ``/voice`` with no text never spends a token.

    The no-argument form answers a static usage hint — no OpenAI call,
    no audio upload, no wallet write — so charging it let five typos
    lock the paid form out for the rest of the window. Twenty bare
    calls against a capacity-5 bucket must all pass, and the bucket
    table must stay EMPTY: the free path returns before the lookup, so
    no row is created and none is touched.
    """
    middleware = VoiceRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=1537)
    target: Any = event
    data = {"command": voice_bare_command}

    for _ in range(20):
        assert await middleware(handler, target, data) == "ok"
    assert handler.await_count == 20
    event.answer.assert_not_called()
    assert middleware._buckets == {}


@pytest.mark.asyncio
async def test_bare_voice_does_not_refund_the_paid_form(
    voice_bare_command: CommandObject,
    voice_args_command: CommandObject,
) -> None:
    """The free form must not top the bucket back up either.

    Five paid calls drain a capacity-5 bucket; bare calls in between
    change nothing, so the sixth paid call is still rejected.
    """
    middleware = VoiceRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=1538)
    target: Any = event

    for _ in range(5):
        assert await middleware(handler, target, {"command": voice_args_command}) == "ok"
        assert await middleware(handler, target, {"command": voice_bare_command}) == "ok"

    assert await middleware(handler, target, {"command": voice_args_command}) is None
    assert "Слишком часто" in event.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_non_command_data_still_pays(voice_args_command: CommandObject) -> None:
    """A gate reached without a parsed command charges as before.

    ``_is_free`` keys on the :class:`CommandObject` the ``Command``
    filter produced; anything else — a future non-command registration,
    or a bare ``data`` mapping — must fall through to the bucket rather
    than opening a hole in the gate.
    """
    middleware = VoiceRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=1539)
    target: Any = event

    for _ in range(5):
        assert await middleware(handler, target, {}) == "ok"
    assert await middleware(handler, target, {}) is None
    assert await middleware(handler, target, {"command": voice_args_command}) is None


@pytest.mark.asyncio
async def test_ai_gate_ignores_the_argument_shape() -> None:
    """``_is_free`` is a VOICE override, not base-class behaviour.

    ``/ai`` with no arguments still reaches DeepSeek through the
    handler's own prompt flow, so the base class must keep charging it.
    """
    middleware = AiRateLimitMiddleware()
    middleware._now = lambda: 100.0  # type: ignore[method-assign]

    handler = AsyncMock(return_value="ok")
    event = _MockEvent(user_id=1540)
    target: Any = event
    data = {"command": CommandObject(prefix="/", command="ai", args=None)}

    for _ in range(10):
        assert await middleware(handler, target, data) == "ok"
    assert await middleware(handler, target, data) is None
