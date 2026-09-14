"""End-to-end ``/admin_routes``.

Pins:

* Non-developer → silent drop.
* Card walks the live dispatcher tree — header counts match
  reality.
* Known commands surface in the output (``/start``, ``/admin_help``,
  ``/admin_routes`` itself — the load-bearing self-reference
  proves the closure resolves at message-time, not import-time).
* Empty-commands router (umbrella ``main``, ``errors``) renders
  with the "(no message commands)" placeholder rather than being
  silently skipped.
* Command aliases deduplicated and sorted within a row.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Router
from aiogram.filters import Command
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.routes import (
    _commands_for_router,
    _render_pages,
    _walk,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def test_commands_for_router_dedupes_and_sorts() -> None:
    """Pin the per-row aggregation: a Command filter with multiple
    aliases must surface as a single sorted list, not duplicates."""
    r = Router(name="t")

    async def _h(_: Any) -> None: ...

    r.message.register(_h, Command("foo", "bar", "baz"))
    assert _commands_for_router(r) == ["bar", "baz", "foo"]


def test_walk_is_depth_first_and_includes_root() -> None:
    """Operator's mental model is "I added these top-to-bottom";
    the walk must yield the root then each child in include-order."""
    root = Router(name="parent")
    child_a = Router(name="a")
    child_b = Router(name="b")
    grandchild = Router(name="a.deep")

    async def _h(_: Any) -> None: ...

    child_a.message.register(_h, Command("alpha"))
    grandchild.message.register(_h, Command("gamma"))
    child_b.message.register(_h, Command("beta"))
    child_a.include_router(grandchild)
    root.include_router(child_a)
    root.include_router(child_b)
    rows = _walk(root)
    names = [r[0] for r in rows]
    assert names == ["parent", "a", "a.deep", "b"]


def test_render_shows_empty_routers_with_placeholder() -> None:
    rows = [("main", []), ("start", ["start"])]
    out = "\n".join(_render_pages(rows))
    assert "(no message commands)" in out
    assert "/start" in out


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_routes", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_lists_live_routes_including_self(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Load-bearing: ``/admin_routes`` must list itself. Self-
    reference is what proves the ``get_root`` closure resolves at
    message-time — an early-binding bug would show every other
    route but not this one, which would be a silent regression
    for the operator's "is what I just added wired?" workflow."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_routes", user_id=42, chat_type="private"),
    )
    # The map does not fit in one message — the contract is "every
    # router is listed", not "in one bubble".
    assert len(sent) > 1
    text = "\n".join(m["text"] for m in sent)
    assert "Dispatcher route map" in sent[0]["text"]
    # Concrete commands known to be in the tree.
    assert "/start" in text
    assert "/admin_help" in text
    # Self-reference is the load-bearing proof of closure-resolution.
    assert "/admin_routes" in text
    # Header reports non-trivial counts (the tree has ~50 routers).
    assert " routers · " in sent[0]["text"]
    assert " bound commands" in sent[0]["text"]


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
            "/admin_routes",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


@pytest.mark.asyncio
async def test_every_page_fits_telegram_ceiling(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The card that could not be delivered at all until it was split.

    The live tree renders ~11 800 parsed characters — three times the
    ceiling — so this must be measured against the real dispatcher, not
    a synthetic two-row list that would pass no matter how large the
    real one grows. Also pins that the pager did not run out of pages:
    a truncated map is a map that quietly lies about what is wired.
    """
    from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_routes", user_id=42, chat_type="private"),
    )

    assert sent, "the map must render at least one page"
    for index, message in enumerate(sent):
        text = message["text"]
        assert parsed_length(text) <= TELEGRAM_TEXT_LIMIT, (
            f"page {index + 1}/{len(sent)} is {parsed_length(text)} chars"
        )

    joined = "\n".join(m["text"] for m in sent)
    assert "truncated" not in joined, "the pager ran out of pages — raise max_pages"


def test_long_command_row_wraps_below_the_ceiling() -> None:
    """A router binding hundreds of commands still renders deliverably.

    ``paginate_lines`` budgets whole lines and puts an over-budget line
    on a page of its own rather than splitting it, so before the row
    wrapped, the fallback router — which binds every command in the bot
    — produced a single page past Telegram's 4096-char limit. Seven
    restored legacy aliases (#501) were what finally tipped it over, but
    the row had been one alias away from undeliverable for a while.
    """
    from telegram_invite_bot.handlers.admin.routes import _render_pages
    from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

    cmds = [f"command_number_{i:04d}" for i in range(400)]
    pages = _render_pages([("unknown_form", cmds)])

    for index, page in enumerate(pages):
        assert parsed_length(page) <= TELEGRAM_TEXT_LIMIT, (
            f"page {index + 1}/{len(pages)} is {parsed_length(page)} chars"
        )
    joined = "\n".join(pages)
    assert "truncated" not in joined
    # The router name is printed once; the wrapped remainder is marked
    # as a continuation rather than repeating it.
    assert joined.count("<b>unknown_form</b>") == 1
    for cmd in cmds:
        assert f"/{cmd}" in joined
