"""End-to-end ``/admin_test_log``.

Pins:

* Non-developer → silent drop.
* Developer in private → reply contains timestamp + Sentry state +
  always-on stderr/loguru marker.
* Sentry "on" iff DSN is configured to a non-empty string.
* A real ERROR-level loguru event is emitted (sinks asserted via a
  loguru handler installed inside the test).
* Group invocation falls through to legacy (router-level private
  filter).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig, ObservabilityConfig
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_test_log", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_with_sentry_off(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_test_log", user_id=555, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Test log emitted" in text
    assert "Sentry: off" in text
    # stderr/loguru always-on marker — operator must always know they
    # CAN find the line, even with Sentry off.
    assert "stderr/loguru" in text


@pytest.mark.asyncio
async def test_renders_with_sentry_on(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DSN configured (non-empty string) → card reports Sentry on.

    Settings is built inside the conftest from explicit kwargs, so
    monkeypatching the env var doesn't reach it. We override the
    observability config directly via the factory's bot_config-only
    knob? The conftest doesn't expose observability as a kwarg, so
    instead we patch the live settings.observability on the dispatched
    handler. The cleanest pin is via the factory; absent that, we
    construct the Settings.observability ourselves below.
    """
    # Pass an ObservabilityConfig with a fake DSN through the factory
    # by overriding the conftest's default via the bot_config knob is
    # not enough — observability is its own pydantic block. The
    # conftest hardcodes ObservabilityConfig() defaults, so the only
    # currently-exposed handle is monkeypatching the environment that
    # ObservabilityConfig reads from. The env-var name is
    # ``SENTRY_DSN``.
    monkeypatch.setenv("SENTRY_DSN", "https://x@y.ingest.sentry.io/123")
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=777),
    )
    # The conftest builds Settings before our monkeypatch can win
    # (factory body runs at await-time, but ObservabilityConfig() is
    # called inside). To force the on-branch deterministically, patch
    # the live settings object on the router's closure directly. The
    # admin.test_log router stores ``settings`` in its callback's
    # closure, identical to admin.status — we walk to it.
    for router in dispatcher.sub_routers:
        if router.name == "main":
            for sub in router.sub_routers:
                if sub.name == "admin.test_log":
                    cb = sub.message.handlers[0].callback
                    assert cb.__closure__ is not None
                    for cell in cb.__closure__:
                        candidate = cell.cell_contents
                        if hasattr(candidate, "observability"):
                            candidate.observability = ObservabilityConfig(
                                SENTRY_DSN=SecretStr("https://x@y.ingest.sentry.io/123")
                            )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_test_log", user_id=777, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Sentry: on" in text


@pytest.mark.asyncio
async def test_emits_real_error_event(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The handler's whole point is that an ERROR-level event reaches
    the log pipeline. Hook a loguru sink and assert one ERROR record
    with our component name fires per invocation."""
    captured: list[dict[str, Any]] = []

    def _sink(record: Any) -> None:
        rec = record.record
        if rec["level"].name == "ERROR" and rec["extra"].get("component") == (
            "handlers.admin.test_log"
        ):
            captured.append(rec)

    handler_id = logger.add(_sink, level="ERROR")
    try:
        bot, dispatcher, _ = await make_wired(
            bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
        )
        _ = capture_outgoing(bot)
        await dispatcher.feed_update(
            bot, make_message_update("/admin_test_log", user_id=42, chat_type="private")
        )
    finally:
        logger.remove(handler_id)

    assert len(captured) == 1
    # The bound extras must include the caller's id and the timestamp
    # so an operator-side grep on user_id finds the record.
    assert captured[0]["extra"]["triggered_by"] == 42
    assert "timestamp" in captured[0]["extra"]


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_test_log", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []
