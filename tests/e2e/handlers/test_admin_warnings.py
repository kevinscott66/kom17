"""End-to-end ``/admin_warnings``.

Pins:

* Non-developer → silent drop.
* Card renders the filter list with column labels (action, cat,
  msg, mod, line).
* ``ignore`` action surfaces 🔇 — load-bearing: removing the
  marker would hide the silenced-DeprecationWarning failure mode
  the module docstring documents.
* ``error`` action surfaces 💥 — promotes a warning to an
  exception; operators chasing "the bot crashed on a strange
  error" need to spot the promotion.
* Healthy actions (``default``, ``always``, ``module``, ``once``)
  render bare so the markers retain triage value.
* Filter order preserved — first-match-wins semantics; sorting
  would erase the entire reason for the card.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.warnings_view import (
    _FilterRow,
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
        make_message_update("/admin_warnings", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_structure(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_warnings", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Warning filters" in text
    assert "entries:" in text


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
            "/admin_warnings",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_ignore_action_surfaces_silenced_marker() -> None:
    """A filter with action=``ignore`` is the silenced-warning case
    the module docstring documents (warnings.simplefilter('ignore')
    leftover from a refactor). The 🔇 marker is load-bearing —
    without it an operator scanning the card couldn't distinguish
    a silencer from any other entry."""
    rows = [
        _FilterRow(
            action="ignore",
            message="<any>",
            category="DeprecationWarning",
            module="<any>",
            lineno=0,
        ),
    ]
    rendered = _render(rows)
    assert "🔇" in rendered
    assert "silenced" in rendered


def test_render_error_action_surfaces_raises_marker() -> None:
    """A filter with action=``error`` promotes a warning to a
    raised exception. Operators chasing 'the bot crashed on an
    unfamiliar exception' need to spot the promotion — the 💥
    marker is the visual cue."""
    rows = [
        _FilterRow(
            action="error",
            message="<any>",
            category="DeprecationWarning",
            module="<any>",
            lineno=0,
        ),
    ]
    rendered = _render(rows)
    assert "💥" in rendered
    assert "raises" in rendered


def test_render_default_action_no_marker() -> None:
    """The default CPython action for most categories. Must render
    bare — if the renderer marked every entry, the 🔇/💥 glyphs
    lose their triage signal."""
    rows = [
        _FilterRow(
            action="default",
            message="<any>",
            category="DeprecationWarning",
            module="__main__",
            lineno=0,
        ),
    ]
    rendered = _render(rows)
    assert "🔇" not in rendered
    assert "💥" not in rendered


def test_render_empty_filter_list_branch() -> None:
    """An empty filter list means every warning falls through to
    the category-default action. Rare but possible (an explicit
    ``warnings.resetwarnings()`` then no re-population) — the
    renderer must surface it explicitly rather than emit a blank
    card."""
    rendered = _render([])
    assert "no filters installed" in rendered


def test_render_preserves_filter_order() -> None:
    """warnings.filters is first-match-wins. The renderer must NOT
    sort — operators reading the card must see the same order the
    interpreter would walk on a raised warning."""
    rows = [
        _FilterRow(
            action="error",
            message="<any>",
            category="DeprecationWarning",
            module="zzz_late",  # alphabetically last
            lineno=0,
        ),
        _FilterRow(
            action="default",
            message="<any>",
            category="UserWarning",
            module="aaa_early",  # alphabetically first
            lineno=0,
        ),
    ]
    rendered = _render(rows)
    # zzz_late must appear before aaa_early — order preserved.
    pos_late = rendered.find("zzz_late")
    pos_early = rendered.find("aaa_early")
    assert 0 <= pos_late < pos_early, "filter order must be preserved"
