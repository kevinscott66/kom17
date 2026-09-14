"""End-to-end ``/admin_tasks``.

Pins:

* Non-developer → silent drop.
* Card renders the live-task header and a count.
* Self-exclusion: the card must NOT contain its own coroutine name
  (without the filter, ``handle_admin_tasks`` would always appear).
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` empty-state surface — the explicit
  "No other tasks" line matters because rendering an empty bullet
  list would look like a UI bug rather than a meaningful state.
* Unit pin on the truncation tail at ``_MAX_SAMPLE``.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.tasks import (
    _MAX_SAMPLE,
    _render,
    _TaskRow,
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
        make_message_update("/admin_tasks", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_live_task_count_and_excludes_self(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The handler's own coroutine must NOT appear in the rendered
    card — without self-exclusion, every invocation would include a
    ``handle_admin_tasks`` row that an operator new to the codebase
    would have to learn to ignore."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_tasks", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Event-loop tasks" in text
    assert "Live tasks (excluding /admin_tasks itself)" in text
    # Load-bearing: ``handle_admin_tasks`` is the running task; the
    # card has to filter itself out.
    assert "handle_admin_tasks" not in text


@pytest.mark.asyncio
async def test_card_includes_background_task_when_present(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """If we spawn a task before invoking the card, that task's name
    must appear in the rendered sample. This is the regression-pin
    for the diagnostic itself: if ``_sample_tasks`` ever stopped
    enumerating, the card would silently show "no other tasks" on a
    busy loop and the operator would lose the leak-detection
    surface."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)

    # Park on an Event nobody sets, NOT on a timed sleep. ``asyncio.
    # all_tasks()`` only returns tasks that have not finished, so a
    # probe with a wall-clock lifetime is a race against dispatch
    # latency: on a loaded machine (full suite, cold DB fixtures) the
    # sleep expires before the card renders, the probe vanishes from
    # the snapshot and the assertion below fails for a reason that
    # has nothing to do with the code under test. An un-set Event
    # keeps the task pending for exactly as long as the test needs.
    never = asyncio.Event()

    async def _sleeper() -> None:
        await never.wait()

    task = asyncio.create_task(_sleeper(), name="probe-sleeper-xyz")
    try:
        await dispatcher.feed_update(
            bot,
            make_message_update("/admin_tasks", user_id=42, chat_type="private"),
        )
        text = sent[0]["text"]
        assert "probe-sleeper-xyz" in text
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
            "/admin_tasks",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_empty_state_renders_explicit_message() -> None:
    """Empty list is rare-but-real (an isolated test loop) — the
    card must surface it explicitly. Empty bullets would look like
    a UI bug, eroding operator trust in the diagnostic."""
    rendered = _render([])
    assert "Live tasks (excluding /admin_tasks itself): <b>0</b>" in rendered
    assert "No other tasks alive in the loop" in rendered


def test_render_truncates_long_sample() -> None:
    """An event loop with hundreds of live tasks is exactly the
    leak case this card exists to catch. Render the cap with a tail
    line so the operator knows the sample was clipped — and so the
    card stays under Telegram's 4096-char limit."""
    rows = [
        _TaskRow(name=f"t-{i:03d}", coro="some.handler", done=False) for i in range(_MAX_SAMPLE + 7)
    ]
    rendered = _render(rows)
    assert f"<b>{len(rows)}</b>" in rendered
    assert "… and 7 more" in rendered
    # First-cap row renders.
    assert "t-000" in rendered
    # Past-cap row does NOT render.
    assert f"t-{_MAX_SAMPLE + 1:03d}" not in rendered
