"""End-to-end ``/admin_signals``.

Pins:

* Non-developer → silent drop.
* Card renders every signal in :data:`_SIGNALS_OF_INTEREST` — drift
  between that constant and the rendered set would silently drop a
  signal from the operator's audit surface.
* SIG_DFL on SIGTERM/SIGINT marks ⚠ ("no graceful shutdown wired")
  — load-bearing: removing the marker hides a real regression mode
  on rolling restarts.
* SIG_IGN marks ⚠ ("un-killable; requires SIGKILL") — the
  unambiguously-bad case; without the marker an operator might miss
  the corruption-on-restart risk.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.signals import (
    _SIGNALS_OF_INTEREST,
    _render,
    _SignalRow,
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
        make_message_update("/admin_signals", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_lists_every_signal_of_interest(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Drift between :data:`_SIGNALS_OF_INTEREST` and the rendered
    output would silently drop a signal from the operator's audit
    surface — pin every entry."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_signals", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Signal handlers" in text
    for name in _SIGNALS_OF_INTEREST:
        assert name in text, f"{name} missing from /admin_signals output"


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
            "/admin_signals",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_default_sigterm_surfaces_warning() -> None:
    """SIG_DFL on SIGTERM means a rolling restart will kill the bot
    mid-write. The ⚠ marker is the load-bearing signal — a future
    cleanup that drops the per-signal marker logic would hide a
    real regression mode."""
    rows = [
        _SignalRow(
            name="SIGTERM",
            handler_desc="default (process death)",
            is_default=True,
            is_ignored=False,
        ),
    ]
    rendered = _render(rows)
    assert "⚠" in rendered
    assert "no graceful shutdown wired" in rendered


def test_render_ignored_signal_surfaces_warning() -> None:
    """SIG_IGN on any signal is the un-killable case. The marker
    must visibly mark the corruption-on-restart risk."""
    rows = [
        _SignalRow(
            name="SIGTERM",
            handler_desc="ignored",
            is_default=False,
            is_ignored=True,
        ),
    ]
    rendered = _render(rows)
    assert "⚠" in rendered
    assert "un-killable" in rendered


def test_render_python_handler_no_warning() -> None:
    """A Python-installed handler on SIGTERM is the healthy state —
    must NOT carry the warning glyph, or operators learn to ignore
    it and the SIG_DFL/SIG_IGN signals lose their alarm value."""
    rows = [
        _SignalRow(
            name="SIGTERM",
            handler_desc="telegram_invite_bot.app.Application._shutdown",
            is_default=False,
            is_ignored=False,
        ),
    ]
    rendered = _render(rows)
    assert "⚠" not in rendered
    assert "Application._shutdown" in rendered
