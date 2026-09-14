"""End-to-end ``/admin_tables``.

Pins:

* Non-developer → silent drop.
* Card lists every DB from :data:`ALL_DBS`, each with a table count
  header.
* SQLite-internal tables (sqlite_master, sqlite_sequence, etc.) are
  filtered out — the catalog must show the *application* schema,
  not SQLite's bookkeeping.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` for empty-schema branch (the explicit
  "no application tables" surface is what catches a missed alembic
  baseline) and the truncation tail at ``_MAX_TABLES_PER_DB``.
* One unreadable engine is reported as its exception class on its
  own row and does not take the other four readouts down (#1645).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.handlers.admin.tables import (
    _MAX_TABLES_PER_DB,
    _read_tables,
    _render,
    _TableRow,
    _TableSnapshot,
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
        make_message_update("/admin_tables", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_lists_every_db_with_application_tables(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_tables", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Tables per engine" in text
    for db in ALL_DBS:
        # Header per DB with the parenthesised count.
        assert f"<b>{db.value}</b> (" in text
    # SQLite-internal tables must NOT leak into the card. ``sqlite_master``
    # would be the regression: a future refactor that drops the
    # ``NOT LIKE 'sqlite_%'`` filter would render it as a real row.
    assert "sqlite_master" not in text
    assert "sqlite_sequence" not in text


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
            "/admin_tables",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_empty_schema_surfaces_explicitly() -> None:
    """An engine with zero application tables is itself a finding
    — usually means the alembic baseline didn't run there. The card
    must surface this explicitly rather than rendering an empty
    section that an operator might skim past."""
    rendered = _render([_TableSnapshot(db=DBName.USERS, rows=[])])
    assert "<b>users</b> (0 table(s))" in rendered
    assert "no application tables" in rendered


def test_render_truncates_long_table_lists() -> None:
    """A DB with more tables than the cap must surface the tail
    line so the operator knows the catalog was truncated. Rendering
    silently would hide the "your schema is unexpectedly large"
    signal."""
    many = [_TableRow(name=f"t_{i:03d}", row_count=i) for i in range(_MAX_TABLES_PER_DB + 5)]
    # Re-sort to match handler behaviour (rows sorted by count desc).
    many.sort(key=lambda r: (-r.row_count, r.name))
    rendered = _render([_TableSnapshot(db=DBName.USERS, rows=many)])
    # Tail line announces how many got dropped.
    assert "… and 5 more" in rendered
    # Highest-count tables render first; lowest are the ones dropped.
    # Rows are sorted by count desc, so t_000..t_004 (the five
    # lowest counts) are the dropped tail. Confirm none of them
    # render, and at least one high-count row does.
    for dropped in ("t_000", "t_001", "t_002", "t_003", "t_004"):
        assert dropped not in rendered
    assert "t_034" in rendered  # highest count, top of the list


@pytest.mark.asyncio
async def test_read_captures_engine_failure_instead_of_raising() -> None:
    """#1645: a dead engine becomes a snapshot, not an exception.

    The read is the only place that touches the engine, so this
    is where the containment has to live; the handler loops over
    :data:`ALL_DBS` and cannot recover on its own.
    """

    class _BoomRegistry:
        def engine(self, db: DBName) -> Any:
            raise RuntimeError("engine gone")

    registry: Any = _BoomRegistry()
    snap = await _read_tables(registry, DBName.USERS)
    assert snap.error == "RuntimeError"
    assert snap.rows == []


def test_render_draws_the_error_row_instead_of_a_count() -> None:
    """An unreadable engine must not be spelled like an empty one.

    "(0 table(s))" plus "no application tables" is a real finding
    — a missed alembic baseline. An engine that could not be
    opened says nothing at all about the schema, so it gets its
    own row shape (#1645). Conflating the two would turn a
    connection failure into a false schema alarm."""
    rendered = _render([_TableSnapshot(db=DBName.USERS, rows=[], error="OperationalError")])
    assert "<b>users</b> ⚠ <code>OperationalError</code>" in rendered
    assert "no application tables" not in rendered
    assert "0 table(s)" not in rendered


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
        make_message_update("/admin_tables", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert f"<b>{DBName.ECONOMY.value}</b> ⚠ <code>RuntimeError</code>" in text
    for db in ALL_DBS:
        if db is DBName.ECONOMY:
            continue
        assert f"<b>{db.value}</b> (" in text
