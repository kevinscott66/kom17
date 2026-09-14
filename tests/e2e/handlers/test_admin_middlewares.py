"""End-to-end ``/admin_middlewares``.

Pins:

* Non-developer → silent drop.
* Card renders the three observers we surface (update, message,
  callback_query) and includes the load-bearing outer-middleware
  classes the deploy wires up: ``ThrottlingMiddleware`` and
  ``SessionMiddleware``. Asserting them by class name is the
  regression-pin for "throttle deployed but not actually firing"
  — a future refactor that drops the wiring would surface here
  before any user notices.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` empty-chain branch — explicit "none"
  rather than rendering an empty bullet sub-list, so the operator
  sees the absence rather than skipping a malformed row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.middlewares import (
    _ObserverRow,
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
        make_message_update("/admin_middlewares", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_surfaces_throttle_and_session_outer(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The card must list ``ThrottlingMiddleware`` on the message +
    callback_query outer chains — that's the wiring shape
    ``di/providers.py`` produces, and the regression-pin for "the
    rate limiter is silently not wired" lives here."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
        session_middleware=True,
        throttle_middleware=True,
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_middlewares", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Dispatcher middlewares" in text
    # Each observer column rendered.
    assert "<b>update</b>" in text
    assert "<b>message</b>" in text
    assert "<b>callback_query</b>" in text
    # Load-bearing: outer chain on message must include the
    # throttle middleware. If a future deploy drops the
    # registration call, this assertion fires first.
    assert "ThrottlingMiddleware" in text
    assert "SessionMiddleware" in text


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
            "/admin_middlewares",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_empty_chain_branch_surfaces_explicit_none() -> None:
    """An observer with no inner middlewares is normal (this codebase
    attaches everything outer); the card must surface that as
    "inner: none" rather than rendering an empty sub-list. An empty
    sub-list reads as a UI bug, not as "yes, there are zero here"."""
    rows = [
        _ObserverRow(name="message", outer=["ThrottlingMiddleware"], inner=[]),
    ]
    rendered = _render(rows)
    assert "ThrottlingMiddleware" in rendered
    assert "inner: none" in rendered


def test_render_observer_with_both_chains_empty() -> None:
    """An observer with neither outer nor inner middlewares is the
    other empty branch — both labels must render explicitly so the
    operator sees the observer was checked and the chain genuinely
    is bare."""
    rows = [
        _ObserverRow(name="callback_query", outer=[], inner=[]),
    ]
    rendered = _render(rows)
    assert "outer: none" in rendered
    assert "inner: none" in rendered
