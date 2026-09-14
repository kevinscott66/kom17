"""End-to-end ``/admin_gc``.

Pins:

* Non-developer → silent drop.
* Card renders the three GC generations and the enabled flag — the
  load-bearing pair on which every other diagnostic interpretation
  rests.
* Disabled-GC branch surfaces a visible warning marker — a refactor
  that drops the marker would hide a real production regression
  mode (``gc.disable()`` left behind from a benchmarking branch).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.gc import _GCSnapshot, _render
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
        make_message_update("/admin_gc", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_three_generations(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_gc", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Garbage collector" in text
    # Three generations is a CPython invariant — all must render.
    assert "gen-0" in text
    assert "gen-1" in text
    assert "gen-2" in text
    # Enabled flag is the load-bearing top-line. In a healthy test
    # process gc is enabled by default.
    assert "Enabled" in text


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
            "/admin_gc",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_disabled_branch_surfaces_warning() -> None:
    """Disabled-GC is a real production regression mode and the card
    must visibly mark it — without the marker an operator could read
    "Enabled: no" as a routine value rather than the configuration
    bug it represents."""
    snap = _GCSnapshot(
        enabled=False,
        counts=(0, 0, 0),
        thresholds=(700, 10, 10),
        collections=(0, 0, 0),
    )
    rendered = _render(snap)
    assert "⚠" in rendered
    assert "cycles will accumulate" in rendered


def test_render_enabled_branch_no_warning() -> None:
    """Healthy state must NOT carry the warning glyph — otherwise the
    ⚠ becomes noise on every snapshot and operators learn to ignore
    it, defeating the disabled-branch signal."""
    snap = _GCSnapshot(
        enabled=True,
        counts=(123, 4, 0),
        thresholds=(700, 10, 10),
        collections=(50, 5, 1),
    )
    rendered = _render(snap)
    assert "⚠" not in rendered
    assert "Enabled" in rendered
