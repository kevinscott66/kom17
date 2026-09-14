"""End-to-end ``/admin_locale``.

Pins:

* Non-developer → silent drop.
* Card renders LC_ALL, LANG env, and the four encoding rows
  (preferred, filesystem, stdout, stderr). Any row going missing
  would silently degrade the "where does Cyrillic break?" triage
  story.
* Non-UTF-8 encoding renders ⚠ — load-bearing: removing the
  marker would hide the C-locale-container failure mode the
  module docstring documents.
* UTF-8 encoding does NOT render ⚠ — guards against a future
  change that always-warns (cry-wolf trains operators to ignore).
* Per-stream markers are independent — stdout broken with stderr
  fine must render exactly one ⚠, not two.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.locale_info import (
    _LocaleSnapshot,
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
        make_message_update("/admin_locale", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_locale_rows(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_locale", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Locale" in text
    for label in ("LC_ALL:", "LANG env:", "preferred:", "filesystem:", "stdout:", "stderr:"):
        assert label in text, f"{label} missing from /admin_locale output"


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
            "/admin_locale",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_c_locale_surfaces_warnings_on_all_rows() -> None:
    """The C-locale-container failure mode: every encoding row is
    ASCII. All four ⚠ markers must fire — this is the canonical
    incident shape and the card has to make it loud."""
    snap = _LocaleSnapshot(
        lc_all="C",
        preferred="ANSI_X3.4-1968",
        fs_encoding="ascii",
        stdout_enc="ascii",
        stderr_enc="ascii",
        lang_env="C",
    )
    rendered = _render(snap)
    # Four rows × ⚠ → at least four occurrences of the glyph.
    assert rendered.count("⚠") == 4


def test_render_utf8_locale_no_warnings() -> None:
    """Healthy state: every encoding row is utf-8. The card must
    carry zero ⚠ markers — otherwise operators learn to ignore the
    glyph and the C-locale-container signal is lost."""
    snap = _LocaleSnapshot(
        lc_all="en_US.UTF-8",
        preferred="UTF-8",
        fs_encoding="utf-8",
        stdout_enc="utf-8",
        stderr_enc="utf-8",
        lang_env="en_US.UTF-8",
    )
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_per_stream_markers_independent() -> None:
    """The triage story in the module docstring ("stdout broken,
    stderr fine") depends on each row computing its marker
    independently. A future refactor that uses a single global
    "is everything utf-8?" check would lose that signal — pinning
    the exact ⚠ count guards the per-row evaluation."""
    snap = _LocaleSnapshot(
        lc_all="en_US.UTF-8",
        preferred="UTF-8",
        fs_encoding="utf-8",
        stdout_enc="ascii",  # only stdout broken
        stderr_enc="utf-8",
        lang_env="en_US.UTF-8",
    )
    rendered = _render(snap)
    assert rendered.count("⚠") == 1


def test_render_case_insensitive_utf8_match() -> None:
    """The encoding ecosystem spells UTF-8 inconsistently:
    ``UTF-8``, ``utf-8``, ``utf8``, ``UTF8``. The renderer's
    substring match must accept all of them, or operators will
    chase phantom regressions on a healthy host whose Python
    just reports a slightly-different spelling."""
    for spelling in ("UTF-8", "utf-8", "utf8", "UTF8", "Utf-8"):
        snap = _LocaleSnapshot(
            lc_all="en_US.UTF-8",
            preferred=spelling,
            fs_encoding=spelling,
            stdout_enc=spelling,
            stderr_enc=spelling,
            lang_env="en_US.UTF-8",
        )
        rendered = _render(snap)
        assert "⚠" not in rendered, f"spelling {spelling!r} false-positived"
