"""End-to-end ``/admin_modules``.

Pins:

* Non-developer → silent drop.
* Card lists every curated package from :data:`_PACKAGES`.
* The package's own ``__version__`` surfaces (the row that
  matters most after a deploy).
* Found packages render with ✅ + version glyph.
* A missing package (forced via monkeypatch on ``version()``)
  renders with ⚠ + em-dash — the diagnostic row, not a crash.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot import __version__
from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin import modules as modules_handler
from telegram_invite_bot.handlers.admin.modules import _PACKAGES
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
        make_message_update("/admin_modules", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_lists_curated_packages_with_versions(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_modules", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Installed modules" in text
    # Own package version is the post-deploy primary signal.
    assert __version__ in text
    for pkg in _PACKAGES:
        # Curated list must each have a row.
        assert pkg in text
    # Healthy deploy: every curated package is installed (CI / dev env).
    # No ⚠ should appear.
    assert "⚠" not in text


@pytest.mark.asyncio
async def test_missing_package_renders_as_warning(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force a curated package to be undiscoverable. The card must
    surface the ⚠ glyph and em-dash, not crash. ``version()`` raises
    ``PackageNotFoundError`` for unknown packages — the exact failure
    path the handler swallows."""

    real_version = modules_handler.version

    def fake_version(pkg: str) -> str:
        # Pin the failure to one package so the rest still render
        # normally — confirms the per-row error boundary is intact.
        if pkg == "aiogram":
            raise PackageNotFoundError(pkg)
        return real_version(pkg)

    monkeypatch.setattr(modules_handler, "version", fake_version)

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_modules", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "aiogram: <code>—</code> ⚠" in text
    # Other packages still render fine (one bad row doesn't pollute the
    # rest — important because the card's value is the side-by-side scan).
    assert "sqlalchemy: <code>" in text and "</code> ✅" in text


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
            "/admin_modules",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
