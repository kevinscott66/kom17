"""End-to-end ``/admin_integrity``.

Pins:

* Non-developer → silent drop.
* Healthy state: every DB renders as ``ok ✅`` and the footer says
  "All engines clean".
* Group invocation → router-level private filter rejects.
* Unit pins on :func:`_render`: integrity issues + FK orphans
  surface as separate sections (they fail orthogonally — pinning
  both in one render is what makes the card useful).
* Unit pin on issue-row truncation at ``_MAX_ISSUES_PER_DB`` —
  rendering 100 issue rows from a single dirty DB would push the
  card past Telegram's 4096-char limit.
* Per-engine isolation (#1590): an engine that cannot be read at
  all is captured as its exception class rather than raised, so
  one broken engine still leaves the other four readouts on the
  card. Pinned at three levels — :func:`_read` (does not raise),
  :func:`_render` (draws the class) and end-to-end through the
  handler with one engine replaced by a broken stand-in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.handlers.admin.integrity import (
    _MAX_ISSUES_PER_DB,
    _IntegritySnapshot,
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
        make_message_update("/admin_integrity", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_healthy_state_renders_ok_per_db(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """On a clean test fixture every DB should pass both pragmas —
    fresh schema, no orphaned rows, no on-disk corruption.

    This is the load-bearing happy-path pin: the card has to be
    *quiet* when nothing is wrong, otherwise operators learn to
    ignore the ⚠ glyph that's the whole point of the diagnostic."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_integrity", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Engine integrity check" in text
    for db in ALL_DBS:
        assert f"<b>{db.value}</b> — <code>ok</code> ✅" in text
    assert "All engines clean" in text
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
            "/admin_integrity",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_surfaces_integrity_and_fk_orphans_orthogonally() -> None:
    """Both failure classes have to be visible at once — they fail
    independently and an operator looking at the card has to be able
    to tell *which* class fired.

    Hand-built snapshots so we don't have to corrupt a real test DB
    to exercise the dirty branch (no portable way to fake bit-rot at
    SQLite level in a temp file)."""
    snaps = [
        _IntegritySnapshot(
            db=DBName.USERS,
            integrity_issues=["row 7 missing from index idx_users_chat"],
            fk_orphans=[],
        ),
        _IntegritySnapshot(
            db=DBName.ECONOMY,
            integrity_issues=[],
            fk_orphans=[("transactions", 42, "users", 0)],
        ),
        _IntegritySnapshot(
            db=DBName.ACTIVITY,
            integrity_issues=[],
            fk_orphans=[],
        ),
    ]
    rendered = _render(snaps)
    # USERS has integrity issue but no FK orphans — and vice versa
    # for ECONOMY. The card has to surface BOTH classes.
    assert "row 7 missing from index idx_users_chat" in rendered
    assert "integrity_check: <code>1</code> issue(s)" in rendered
    assert "foreign_key_check: <code>1</code> orphan(s)" in rendered
    assert "<code>transactions</code> rowid=<code>42</code>" in rendered
    # ACTIVITY is healthy — must still render its ok line so the
    # operator can confirm the check ran for every DB.
    assert f"<b>{DBName.ACTIVITY.value}</b> — <code>ok</code> ✅" in rendered
    # Footer flips to warning.
    assert "At least one engine is unhealthy" in rendered


def test_render_truncates_long_issue_lists() -> None:
    """``integrity_check`` caps at 100 rows; if even one DB returns
    that many we'd blow past Telegram's 4096-char message limit.
    The card truncates to the first three with a "… and N more" tail
    so the card stays sendable even on a catastrophically dirty
    engine."""
    many_issues = [f"problem #{i}" for i in range(10)]
    snaps = [
        _IntegritySnapshot(
            db=DBName.USERS,
            integrity_issues=many_issues,
            fk_orphans=[],
        ),
    ]
    rendered = _render(snaps)
    # First three render verbatim.
    for i in range(_MAX_ISSUES_PER_DB):
        assert f"problem #{i}" in rendered
    # Fourth and beyond collapse to the tail line.
    assert "problem #3" not in rendered
    remaining = len(many_issues) - _MAX_ISSUES_PER_DB
    assert f"… and {remaining} more" in rendered


@pytest.mark.asyncio
async def test_read_captures_engine_failure_instead_of_raising() -> None:
    """The caller is a plain list comprehension over ``ALL_DBS``
    (integrity.py:handle_admin_integrity), so an exception escaping
    ``_read`` blanks the whole card. This is the load-bearing half
    of #1590: the operator opens this card when something is
    already wrong, and that is the worst moment to show nothing."""

    class _BoomRegistry:
        def engine(self, db: DBName) -> Any:
            raise RuntimeError("engine gone")

    registry: Any = _BoomRegistry()
    snap = await _read(registry, DBName.USERS)
    assert snap.error == "RuntimeError"
    assert snap.healthy is False
    # Neither list means anything when the check never ran.
    assert snap.integrity_issues == []
    assert snap.fk_orphans == []


def test_render_surfaces_engine_failure_class() -> None:
    """A failed engine renders its exception class on the row and
    draws no per-class bullets — there is nothing to report about
    integrity_check or foreign_key_check when neither pragma ran.
    Same row shape as /admin_dbprobe."""
    snaps = [
        _IntegritySnapshot(
            db=DBName.USERS,
            integrity_issues=[],
            fk_orphans=[],
            error="OperationalError",
        ),
        _IntegritySnapshot(
            db=DBName.ACTIVITY,
            integrity_issues=[],
            fk_orphans=[],
        ),
    ]
    rendered = _render(snaps)
    assert f"<b>{DBName.USERS.value}</b> ⚠ <code>OperationalError</code>" in rendered
    assert "integrity_check:" not in rendered
    assert "foreign_key_check:" not in rendered
    # The healthy sibling still renders — that is the whole point.
    assert f"<b>{DBName.ACTIVITY.value}</b> — <code>ok</code> ✅" in rendered
    assert "At least one engine is unhealthy" in rendered


@pytest.mark.asyncio
async def test_one_broken_engine_does_not_kill_the_card(
    make_wired: WiredFactory, capture_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proof of #1590: replace one engine with a stand-in
    that refuses to connect and require the card to still arrive with
    the other four readouts intact."""

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
        make_message_update("/admin_integrity", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert f"<b>{DBName.ECONOMY.value}</b> ⚠ <code>RuntimeError</code>" in text
    for db in ALL_DBS:
        if db is DBName.ECONOMY:
            continue
        assert f"<b>{db.value}</b> — <code>ok</code> ✅" in text
    assert "At least one engine is unhealthy" in text
