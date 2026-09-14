"""End-to-end ``/admin_proc``.

Pins:

* Non-developer → silent drop.
* Card surfaces PID, peak RSS, user CPU, system CPU — the four
  diagnostic surfaces the handler exists for. Dropping any one
  silently is the regression.
* Group invocation → router-level private filter rejects.
* Unit pin on platform-aware :func:`_maxrss_to_bytes` — load-bearing
  because macOS reports ``ru_maxrss`` in bytes and Linux in KiB.
  Without the per-platform normaliser an operator switching dev/
  prod would see a 1024× difference and chase a phantom leak.
* Unit pin on :func:`_render` MiB formatting — operator's leak
  detection workflow depends on consistent units between snapshots.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.proc import (
    _maxrss_to_bytes,
    _ProcSnapshot,
    _render,
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
        make_message_update("/admin_proc", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_all_resource_surfaces(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_proc", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Process resources" in text
    assert "PID" in text
    assert "Peak RSS" in text
    assert "User CPU" in text
    assert "System CPU" in text
    # The MiB unit must appear — operators rely on consistent
    # units across snapshots.
    assert "MiB" in text


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
            "/admin_proc",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_maxrss_normaliser_macos_passes_bytes_through() -> None:
    """macOS reports ``ru_maxrss`` already in bytes — the normaliser
    must NOT multiply by 1024 on darwin, or every macOS dev would
    see a 1024× phantom RSS and the cross-platform comparison this
    card exists to support would break."""
    with patch.object(sys, "platform", "darwin"):
        assert _maxrss_to_bytes(14_663_680) == 14_663_680


def test_maxrss_normaliser_linux_multiplies_kib_to_bytes() -> None:
    """Linux reports ``ru_maxrss`` in KiB — the normaliser must
    multiply by 1024 so the renderer's MiB math sees the same
    underlying bytes a macOS read would yield. Load-bearing: an
    operator switching between staging (Linux) and dev (macOS)
    snapshots reads identical numbers only because of this
    normalisation."""
    with patch.object(sys, "platform", "linux"):
        # 14_320 KiB ≈ 14 MiB — picked to be obviously different
        # from the macOS bytes value above.
        assert _maxrss_to_bytes(14_320) == 14_320 * 1024


def test_render_formats_rss_as_mib_with_two_decimals() -> None:
    """The card renders MiB with two-decimal precision so the
    leak-detection workflow (compare RSS across two snapshots) can
    spot sub-MiB drift without rendering noise. KiB-resolution would
    flap on every alloc; GiB-resolution would hide 100MiB leaks."""
    snap = _ProcSnapshot(
        pid=12345,
        max_rss_bytes=104_857_600,  # exactly 100 MiB
        user_cpu_s=1.234,
        system_cpu_s=0.567,
    )
    rendered = _render(snap)
    assert "<code>12345</code>" in rendered
    assert "100.00 MiB" in rendered
    assert "1.234s" in rendered
    assert "0.567s" in rendered
