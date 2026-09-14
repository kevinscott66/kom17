"""End-to-end ``/admin_threads``.

Pins:

* Non-developer → silent drop.
* Card always lists the main thread tagged ``main`` — this is the
  load-bearing assertion: a future refactor that suppresses the
  main-thread row would leave the operator unable to anchor the
  list, so the tag must survive.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` empty-input branch — guards the
  "no threads enumerated" defensive copy from being deleted as
  dead code on a future cleanup pass.
* Unit pin on truncation tail — the cap is the protection against
  ``ThreadPoolExecutor`` blow-ups producing 5 KiB cards that
  Telegram rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.threads import _render, _ThreadRow
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
        make_message_update("/admin_threads", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_lists_main_thread(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """The main thread is the conceptual anchor of the card — every
    Python process has exactly one and the operator scans for it to
    verify the snapshot rendered. If a future refactor suppresses
    the ``main`` tag, this assertion fires."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_threads", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "OS threads" in text
    assert "Alive threads" in text
    # The main-thread tag is load-bearing: removing it would leave
    # operators unable to find the main thread amid worker rows.
    assert "main" in text


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
            "/admin_threads",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_empty_input_branch() -> None:
    """Defensive: the renderer must not assume non-empty input.
    ``threading.enumerate`` always returns at least the main thread in
    practice, but the defensive branch guards a future change that
    pre-filters the list — without this pin a cleanup pass could
    drop the branch as dead code."""
    rendered = _render([])
    assert "No threads enumerated" in rendered


def test_render_truncation_tail() -> None:
    """Over the sample cap, the renderer must surface "and N more" so
    a ``ThreadPoolExecutor`` blow-up is visible without overflowing
    Telegram's 4096-char message limit."""
    rows = [_ThreadRow(name=f"w-{i:03d}", daemon=True, is_main=False) for i in range(40)]
    # Replace one with the main thread so the sort puts it first.
    rows[0] = _ThreadRow(name="MainThread", daemon=False, is_main=True)
    rendered = _render(rows)
    assert "MainThread" in rendered
    assert "and 10 more" in rendered


def test_render_marks_non_daemon() -> None:
    """Non-daemon worker threads are the load-bearing signal during
    the legacy migration — they're the ones that block process
    shutdown. The renderer must distinguish them from daemon
    workers; otherwise the migration-audit use case in the module
    docstring quietly loses its signal."""
    rows = [
        _ThreadRow(name="MainThread", daemon=False, is_main=True),
        _ThreadRow(name="LegacyWorker", daemon=False, is_main=False),
    ]
    rendered = _render(rows)
    assert "non-daemon" in rendered


def test_render_escapes_thread_names() -> None:
    """A thread named with an angle bracket must not kill the card.

    Thread names are not ours to control — any library in the tree can
    name a worker whatever it likes, and ``<`` in one of them would
    otherwise take the whole card down with a Telegram 400.
    """
    from tests.telegram_html import telegram_html_errors

    rows = [
        _ThreadRow(name="MainThread", daemon=False, is_main=True),
        _ThreadRow(name="<unnamed 0x7f>", daemon=True, is_main=False),
    ]
    rendered = _render(rows)
    assert not telegram_html_errors(rendered), rendered
    assert "&lt;unnamed 0x7f&gt;" in rendered, rendered
