"""End-to-end ``/admin_fdlimit``.

Pins:

* Non-developer → silent drop.
* Card surfaces the NOFILE row (load-bearing — the AS/RSS rows
  are platform-conditional, but NOFILE is universally available
  on every POSIX host we run on).
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_fmt_limit` for the RLIM_INFINITY → "unlimited"
  collapse — surfacing the raw sentinel ``-1`` would actively
  mislead an operator scanning for "is this bounded?".
* Unit pin on :func:`_render` for the platform-absent rlimit
  branch — RLIMIT_AS doesn't exist on macOS, so the card must
  render "n/a on this platform" rather than skip the row.
"""

from __future__ import annotations

import resource
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.fdlimit import (
    _fmt_limit,
    _LimitRow,
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
        make_message_update("/admin_fdlimit", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_nofile_row(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """NOFILE is the universally-available rlimit — must always
    render. The AS/RSS rows are platform-conditional and tested
    separately at unit level."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_fdlimit", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Resource ceilings" in text
    assert "Open files (NOFILE)" in text
    assert "soft" in text
    assert "hard" in text


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
            "/admin_fdlimit",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_fmt_limit_collapses_rlim_infinity_to_unlimited() -> None:
    """Surfacing the raw -1 sentinel for unset rlimits would
    actively mislead an operator scanning for "is this bounded?".
    The collapse to "unlimited" is the load-bearing surface — if a
    future refactor drops the check, "Address space: hard -1"
    would render and an operator could waste minutes wondering
    about the negative number."""
    assert _fmt_limit(resource.RLIM_INFINITY) == "unlimited"
    # Finite values render with thousands separator for scanability.
    assert _fmt_limit(1024) == "1,024"
    assert _fmt_limit(65_536) == "65,536"


def test_render_surfaces_platform_absent_rows_explicitly() -> None:
    """RLIMIT_AS is undefined on macOS — the card must render an
    explicit "n/a on this platform" for those rows rather than
    skip them. Skipping would leave a macOS operator wondering
    whether the read failed silently; the explicit row teaches
    them this rlimit isn't available here."""
    rows = [
        _LimitRow(label="Open files (NOFILE)", soft=1024, hard=4096, available=True),
        _LimitRow(label="Address space (AS)", soft=0, hard=0, available=False),
    ]
    rendered = _render(rows)
    assert "Open files (NOFILE): soft <code>1,024</code>" in rendered
    # Load-bearing: absent row gets explicit surfacing.
    assert "Address space (AS): <code>n/a on this platform</code>" in rendered
