"""End-to-end ``/admin_uptime``.

Pins:

* Non-developer → silent drop.
* Card shows ``booted at`` with UTC timestamp.
* Card shows ``elapsed`` formatted by :func:`_format_elapsed`.
* Elapsed is computed from monotonic, not wall-clock — pinned by
  unit-testing the formatter at boundary durations.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.uptime import _format_elapsed
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def test_format_elapsed_units() -> None:
    """Pin the two-unit rendering at every branch boundary. The
    operator value of this handler is "is this fresh or stale?";
    if the formatter renders ``5h`` for both 5h2m and 5h59m,
    that question becomes harder to answer at a glance, so the
    contract is that *both* surfaced units are visible."""
    assert _format_elapsed(0) == "0s"
    assert _format_elapsed(45) == "45s"
    assert _format_elapsed(125) == "2m 5s"
    assert _format_elapsed(3 * 3600 + 17 * 60 + 5) == "3h 17m"
    assert _format_elapsed(2 * 86_400 + 5 * 3600) == "2d 5h"
    # Negative is clamped to zero — guards against monotonic skew
    # in tests where boot time was monkey-patched to a future value.
    assert _format_elapsed(-10) == "0s"


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_uptime", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_boot_time_and_elapsed(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_uptime", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Process uptime" in text
    assert "booted at:" in text
    assert "UTC" in text
    assert "elapsed:" in text
    # NTP-safety note appears (load-bearing per the docstring — if
    # someone removes it, the operator loses the cue that wall-clock
    # subtraction would be wrong).
    assert "monotonic" in text


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
            "/admin_uptime",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
