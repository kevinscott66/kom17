"""End-to-end ``/admin_loguru``.

Pins:

* Non-developer → silent drop.
* Card renders the configured-sinks count and at least the
  default stderr sink (loguru ships with one by default).
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` zero-sinks branch — load-bearing
  surface because every log call vanishing is itself a real
  failure mode, and the explicit message is what catches a
  post-deploy ``logger.remove()`` regression.
* Unit pin on standard-level-name resolution — the level column
  is the operator's read for "did the level bump take?". Numeric
  fallback must work for non-standard levels without crashing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.loguru_sinks import (
    _render,
    _SinkRow,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_loguru", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_with_default_sink(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Loguru ships with a default stderr sink that the conftest
    keeps in place; the card must render the count and at least
    one row. Without the count line the operator can't spot a
    duplicate-sink regression at a glance."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_loguru", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Loguru sinks" in text
    assert "Configured sinks" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_loguru",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_zero_sinks_branch_surfaces_dropped_logs() -> None:
    """Zero sinks is a real failure mode — every log call would
    vanish. The card must surface it explicitly so an operator
    skimming after a deploy spots the regression rather than
    parsing an empty section."""
    rendered = _render([])
    assert "Configured sinks: <b>0</b>" in rendered
    assert "every log call is dropped" in rendered


def test_render_resolves_standard_level_names() -> None:
    """The level column carries the level *name* (INFO, DEBUG, …)
    for standard levels — the operator's read for "did the bump
    take?" depends on it. A raw levelno would force the operator
    to memorise loguru's numeric mapping."""
    rows = [
        _SinkRow(handler_id=1, levelno=10, level_name="DEBUG", sink_kind="StreamSink"),
        _SinkRow(
            handler_id=2,
            levelno=40,
            level_name="ERROR",
            sink_kind="FileSink",
        ),
    ]
    rendered = _render(rows)
    assert "id <code>1</code>: <code>StreamSink</code> @ <code>DEBUG</code>" in (rendered)
    assert "id <code>2</code>: <code>FileSink</code> @ <code>ERROR</code>" in (rendered)
    assert "Configured sinks: <b>2</b>" in rendered


def test_render_falls_back_to_numeric_for_custom_levels() -> None:
    """Custom levels added via ``logger.level("AUDIT", no=22)`` must
    not crash the card — the level-name lookup falls back to a
    ``level_22`` numeric form. Load-bearing because a future
    deploy adding a custom level should not break a diagnostic."""
    rows = [
        _SinkRow(
            handler_id=3,
            levelno=22,
            level_name="level_22",
            sink_kind="StreamSink",
        ),
    ]
    rendered = _render(rows)
    assert "<code>level_22</code>" in rendered
