"""End-to-end ``/admin_bot_session``.

Pins:

* Non-developer → silent drop.
* Card surfaces all five session fields (session class, API base,
  local/remote badge, HTTP timeout, default parse_mode) — the
  diagnostic surfaces the card exists for, dropping any one
  silently is the regression.
* Group invocation → router-level private filter rejects.
* Unit pin on the local-API badge — load-bearing because a deploy
  switching to a self-hosted Bot API server has no other Telegram-
  visible verification path.
* Unit pin on the parse_mode-unset ⚠ marker. Every admin card in
  this codebase renders HTML; a drift to None would turn ``<b>``
  into literal angle brackets and the warning must stand out.
* Confirms the rendered URL still carries the ``{token}`` placeholder,
  not the real token (aiogram does the substitution at call time).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.bot_session import (
    _render,
    _SessionSnapshot,
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
        make_message_update("/admin_bot_session", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_session_surfaces_for_default_remote_api(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Default aiogram Bot points at api.telegram.org and ships
    AiohttpSession with a 60s timeout. The DI fixture builds the
    Bot with default HTML parse_mode (matches the app.py shape)."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_bot_session", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Bot session" in text
    assert "Session class" in text
    assert "API base" in text
    assert "HTTP timeout" in text
    assert "Default parse_mode" in text
    # Load-bearing: the {token} placeholder must NOT have been
    # substituted — leaking the real bot token into a rendered
    # admin card would be a security regression.
    assert "{token}" in text


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
            "/admin_bot_session",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_local_api_badge_surfaces_self_hosted_deploy() -> None:
    """A deploy switching to a self-hosted Bot API server must
    render the ``local`` badge — this is the only Telegram-visible
    verification path for the switch. The remote default is
    asserted in the e2e test above; here we pin the local branch."""
    snap = _SessionSnapshot(
        session_kind="AiohttpSession",
        base_url="http://internal-botapi:8081/bot{token}/{method}",
        is_local_api=True,
        timeout_s=60.0,
        parse_mode="HTML",
    )
    rendered = _render(snap)
    assert "<i>local</i>" in rendered
    assert "internal-botapi:8081" in rendered


def test_render_remote_branch_omits_local_badge() -> None:
    """Conversely, an api.telegram.org session must NOT show
    ``local`` — false-positive on the badge would tell an operator
    the deploy switched when it didn't, the worst kind of
    diagnostic lie."""
    snap = _SessionSnapshot(
        session_kind="AiohttpSession",
        base_url="https://api.telegram.org/bot{token}/{method}",
        is_local_api=False,
        timeout_s=60.0,
        parse_mode="HTML",
    )
    rendered = _render(snap)
    assert "<i>remote</i>" in rendered
    assert "<i>local</i>" not in rendered


def test_render_parse_mode_unset_carries_warning_marker() -> None:
    """parse_mode=None would turn every ``<b>`` in every admin card
    into literal angle brackets. The ⚠ marker is what an operator
    scanning the card uses to spot the regression — a bare
    "Default parse_mode: <unset>" line would not stand out."""
    snap = _SessionSnapshot(
        session_kind="AiohttpSession",
        base_url="https://api.telegram.org/bot{token}/{method}",
        is_local_api=False,
        timeout_s=60.0,
        parse_mode=None,
    )
    rendered = _render(snap)
    assert "&lt;unset&gt;" in rendered
    assert "⚠" in rendered
