"""End-to-end ``/admin_loop``.

Pins:

* Non-developer → silent drop.
* Card surfaces all four loop bits (impl, debug, slow-callback,
  running/closed) — they're the diagnostic surface the handler
  exists for, and dropping any one silently is the regression.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` debug-on branch — load-bearing
  because the ⚠ marker is the visual signal an operator scans
  for during a "post-restart slowness" investigation.
* Unit pin on the debug-off branch — must NOT carry the warning
  marker; a false ⚠ erodes operator trust in the card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.loop import _LoopSnapshot, _render
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
        make_message_update("/admin_loop", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_loop_surfaces(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """Inside an aiogram handler there IS a running loop, so the
    capture path is exercised end-to-end here. Pins that all four
    documented surfaces render — Implementation, Debug, Slow-
    callback threshold, and Running/closed."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_loop", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Event loop" in text
    assert "Implementation" in text
    assert "Debug" in text
    assert "Slow-callback threshold" in text
    assert "Running" in text
    # Inside a test, the loop is running and not closed.
    assert "Running: <code>yes</code>" in text
    assert "closed: <code>no</code>" in text


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
            "/admin_loop",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_debug_on_carries_warning_marker() -> None:
    """Debug-on must surface a ⚠ marker — it's the visual cue an
    operator scans for during a "post-restart slowness" investigation.
    A bare ``Debug: on`` line would not stand out in a card with
    five other lines."""
    snap = _LoopSnapshot(
        impl_class="_UnixSelectorEventLoop",
        debug=True,
        slow_callback_s=0.1,
        is_running=True,
        is_closed=False,
        python_impl="cpython",
    )
    rendered = _render(snap)
    assert "Debug: <code>on</code> ⚠" in rendered
    assert "slows every coroutine" in rendered


def test_render_debug_off_has_no_warning_marker() -> None:
    """Debug-off must NOT carry the ⚠ marker — a false warning
    on the healthy state erodes operator trust in the diagnostic.
    Also pins the absence of the cost-warning sentence on the
    healthy branch."""
    snap = _LoopSnapshot(
        impl_class="_UnixSelectorEventLoop",
        debug=False,
        slow_callback_s=0.1,
        is_running=True,
        is_closed=False,
        python_impl="cpython",
    )
    rendered = _render(snap)
    assert "Debug: <code>off</code>" in rendered
    # Load-bearing: the warning marker must not appear when off.
    # The "slows every coroutine" wording lives only on the on-branch
    # line; the footer is generic legend (uses different phrasing).
    assert "<code>on</code> ⚠" not in rendered
    assert "slows every coroutine" not in rendered
