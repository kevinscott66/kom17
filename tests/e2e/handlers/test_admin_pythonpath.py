"""End-to-end ``/admin_pythonpath``.

Pins:

* Non-developer → silent drop.
* Card renders the entry count + PYTHONPATH env + per-entry kind
  tags. Any rendering branch going missing would silently degrade
  the shadowing-audit story in the module docstring.
* ``<cwd>`` kind surfaces ⚠ "shadowing vector" — load-bearing
  signal for the accidental-shadow case (an aborted rsync leaving
  a stale ``./telegram_invite_bot/`` in cwd silently wins over the
  site-packages install).
* ``missing`` kind surfaces ⚠ "silently skipped on import" —
  CPython doesn't tell you the path entry was bad, only that the
  import failed.
* Healthy kinds (``site-packages``, ``dir``) render bare — without
  this pin, a future refactor that always-warns would defeat the
  scan-for-markers triage flow.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.pythonpath import _PathEntry, _render
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
        make_message_update("/admin_pythonpath", user_id=42, chat_type="private"),
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
        make_message_update("/admin_pythonpath", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "sys.path" in text
    assert "PYTHONPATH env" in text
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
            "/admin_pythonpath",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_cwd_entry_surfaces_warning() -> None:
    """``<cwd>`` is the implicit-current-directory entry that makes
    accidental shadowing possible. The ⚠ must fire so an operator
    chasing "the deploy ran but behaviour didn't update" can spot
    it without learning the CPython path-bootstrap rules."""
    entries = [_PathEntry(index=0, path="", kind="<cwd>")]
    rendered = _render(entries, "<unset>")
    assert "⚠" in rendered
    assert "shadowing vector" in rendered


def test_render_missing_entry_surfaces_warning() -> None:
    """A PYTHONPATH entry that doesn't exist on disk is silently
    skipped by CPython on import — the symptom is "package not
    found" with no hint about the path entry. The marker surfaces
    the root cause."""
    entries = [
        _PathEntry(index=2, path="/home/dev/leaked", kind="missing"),
    ]
    rendered = _render(entries, "/home/dev/leaked")
    assert "⚠" in rendered
    assert "silently skipped" in rendered


def test_render_site_packages_no_warning() -> None:
    """Healthy state: the venv's site-packages entry. Must NOT
    carry the marker — otherwise operators learn to ignore the
    glyph and lose the shadowing-vector signal."""
    entries = [
        _PathEntry(
            index=0,
            path="/opt/venv/lib/python3.14/site-packages",
            kind="site-packages",
        ),
    ]
    rendered = _render(entries, "<unset>")
    assert "⚠" not in rendered
    assert "site-packages" in rendered


def test_render_dir_entry_no_warning() -> None:
    """An existing directory entry (stdlib, e.g. ``/usr/lib/python3.14``)
    is healthy and must render bare."""
    entries = [
        _PathEntry(index=1, path="/usr/lib/python3.14", kind="dir"),
    ]
    rendered = _render(entries, "<unset>")
    assert "⚠" not in rendered


def test_render_preserves_index_order() -> None:
    """sys.path resolution order is what makes shadowing happen.
    Sorting the entries would erase the entire diagnostic value
    of the card — the indices must render in input order."""
    entries = [
        _PathEntry(index=0, path="", kind="<cwd>"),
        _PathEntry(index=1, path="/opt/venv/lib/python3.14/site-packages", kind="site-packages"),
    ]
    rendered = _render(entries, "<unset>")
    # Entry 0 must appear before entry 1 in the rendered text.
    pos_zero = rendered.find("0.")
    pos_one = rendered.find("1.")
    assert 0 <= pos_zero < pos_one, "sys.path index order must be preserved"
