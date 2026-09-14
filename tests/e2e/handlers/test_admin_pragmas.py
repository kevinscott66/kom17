"""End-to-end ``/admin_pragmas``.

Pins:

* Non-developer → silent drop.
* Card lists every DB from :data:`ALL_DBS`.
* journal_mode ``wal`` on every file (legacy + new share each
  ``.db``; a non-WAL connection silently downgrades the file
  for everyone — this is the regression the card exists to
  catch).
* foreign_keys ``1`` on every file.
* synchronous label matches :func:`synchronous_level` per-DB
  (users/economy = FULL, rest = NORMAL).
* "All engines configured as expected" line on healthy state.
* One unreadable engine is reported as its exception class on its
  own row and does not take the other four readouts down (#1645);
  the drift footer and the unreadable footer are independent.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.db.pragma import synchronous_level
from telegram_invite_bot.handlers.admin.pragmas import (
    _PragmaSnapshot,
    _read,
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
        make_message_update("/admin_pragmas", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_lists_all_dbs_with_expected_pragmas(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_pragmas", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Engine PRAGMA readout" in text
    for db in ALL_DBS:
        # Header per DB.
        assert f"<b>{db.value}</b>" in text
        # WAL is the load-bearing pragma — file-level, sticky.
        # Asserting on the literal string would over-pin the renderer;
        # asserting on the value + flag-glyph proves it landed AND
        # was scored as OK.
        assert "journal_mode: <code>wal</code> ✅" in text
        # FK enforcement is silent if missing — explicit assertion.
        assert "foreign_keys: <code>1</code> ✅" in text
    # Per-DB synchronous parity with the documented tuning.
    for db in ALL_DBS:
        expected = synchronous_level(db)
        assert f"(expected <code>{expected}</code>) ✅" in text
    # Healthy footer — no drift.
    assert "All engines configured as expected" in text
    assert "⚠" not in text


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
            "/admin_pragmas",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


@pytest.mark.asyncio
async def test_read_captures_engine_failure_instead_of_raising() -> None:
    """#1645: a dead engine becomes a snapshot, not an exception."""

    class _BoomRegistry:
        def engine(self, db: DBName) -> Any:
            raise RuntimeError("engine gone")

    registry: Any = _BoomRegistry()
    snap = await _read(registry, DBName.USERS)
    assert snap.error == "RuntimeError"


def test_render_error_row_replaces_the_pragma_rows() -> None:
    """The exception class IS the readout for that engine.

    The placeholder pragma values on a failed snapshot must never
    reach the card — printing ``journal_mode: <code></code>`` would
    read as a measured value and send the operator after a WAL
    problem that was never measured (#1645)."""
    rendered = _render([_PragmaSnapshot.failed(db=DBName.USERS, error="OperationalError")])
    assert "<b>users</b> ⚠ <code>OperationalError</code>" in rendered
    assert "journal_mode" not in rendered
    assert "could not be read at all" in rendered
    assert "All engines configured as expected" not in rendered


@pytest.mark.asyncio
async def test_one_broken_engine_does_not_kill_the_card(
    make_wired: WiredFactory, capture_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1645: the other four readouts are still the diagnosis."""

    class _BrokenEngine:
        """Engine that cannot be opened at all."""

        def connect(self) -> Any:
            raise RuntimeError("engine gone")

        async def dispose(self) -> None:
            # Registry teardown disposes every engine; stay quiet.
            return None

    bot, dispatcher, registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    broken: Any = _BrokenEngine()
    monkeypatch.setitem(registry.engines, DBName.ECONOMY, broken)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_pragmas", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert f"<b>{DBName.ECONOMY.value}</b> ⚠ <code>RuntimeError</code>" in text
    for db in ALL_DBS:
        if db is DBName.ECONOMY:
            continue
        assert f"<b>{db.value}</b>" in text
    assert "could not be read at all" in text
    assert "All engines configured as expected" not in text
