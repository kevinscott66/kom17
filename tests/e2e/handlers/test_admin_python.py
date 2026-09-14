"""End-to-end ``/admin_python``.

Pins:

* Non-developer → silent drop.
* Card surfaces all five interpreter identity fields — Version,
  Implementation, Executable, Prefix, Platform. These are the
  diagnostic surface the handler exists for; dropping any one
  silently is the regression.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` first-line-only trim of
  ``sys.version`` — the compiler banner is multi-line and
  printing all of it would push the card past 4096 chars on a
  verbose build string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.python import _PythonSnapshot, _render
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
        make_message_update("/admin_python", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_all_identity_surfaces(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_python", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Python interpreter" in text
    assert "Version" in text
    assert "Implementation" in text
    assert "Executable" in text
    assert "Prefix" in text
    assert "Platform" in text


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
            "/admin_python",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_trims_multiline_version_to_first_line() -> None:
    """``sys.version`` on a real interpreter is ``"3.12.3 (main, …)\\n[GCC …]"``
    — multi-line. The card must render only the first line; rendering
    the compiler banner inline would push the card past Telegram's
    4096-char limit on hosts with long build strings and would noise
    the surface an operator scans for X.Y.Z."""
    snap = _PythonSnapshot(
        version="3.12.3 (main, Apr 9 2024, 10:11:12) [GCC 11.4.0]\n"
        "[banner-line-that-should-not-render]",
        impl_name="CPython",
        executable="/opt/venv/bin/python",
        prefix="/opt/venv",
        platform_str="Linux-6.1.0-x86_64-with-glibc2.36",
    )
    rendered = _render(snap)
    assert "3.12.3 (main, Apr 9 2024" in rendered
    # Load-bearing: the second line must not render — operators
    # rely on the card staying inside the 4096 budget.
    assert "banner-line-that-should-not-render" not in rendered
    # Other identity fields render verbatim.
    assert "/opt/venv/bin/python" in rendered
    assert "/opt/venv" in rendered
    assert "Linux-6.1.0-x86_64" in rendered
    assert "CPython" in rendered
