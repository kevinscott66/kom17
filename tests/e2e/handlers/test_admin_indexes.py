"""End-to-end ``/admin_indexes``.

Pins:

* Non-developer → silent drop.
* Card lists every DB from :data:`ALL_DBS` with a header.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` zero-indexes branch (the explicit
  "no indexes at all" surface is what catches a missed baseline
  migration).
* Unit pin on the implicit-vs-explicit split — ``sqlite_autoindex_*``
  rows must NOT render by name but MUST contribute to the implicit
  count, because the names are opaque and listing them would
  blow past the 4096-char limit on schemas with many UNIQUE
  constraints.
* Unit pin on per-table and per-engine truncation tails.
* One unreadable engine is reported as its exception class on its
  own row and does not take the other four readouts down (#1645).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.handlers.admin.indexes import (
    _MAX_INDEXES_PER_TABLE,
    _MAX_TABLES_PER_DB,
    _IndexRow,
    _IndexSnapshot,
    _read_indexes,
    _render,
    _TableIndexes,
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
        make_message_update("/admin_indexes", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_lists_every_db(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_indexes", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Indexes per engine" in text
    for db in ALL_DBS:
        assert f"<b>{db.value}</b>" in text


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
            "/admin_indexes",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_zero_indexes_branch_surfaces_explicitly() -> None:
    """An engine with zero indexes anywhere is a strong smell —
    even a fresh schema usually has PK auto-indexes. The card must
    say so explicitly so an operator skimming doesn't conflate it
    with "no information rendered yet"."""
    rendered = _render([_IndexSnapshot(db=DBName.USERS, tables=[])])
    assert "<b>users</b> (0 table(s) with indexes)" in rendered
    assert "no indexes at all" in rendered


def test_render_splits_implicit_and_explicit_counts() -> None:
    """``sqlite_autoindex_*`` rows must contribute to the implicit
    count and NOT render by name — the names are opaque and a
    schema with many UNIQUE columns would otherwise blow past the
    4096-char limit. The dedicated count tells the operator
    something exists without enumerating it."""
    tbl = _TableIndexes(
        tbl_name="users",
        explicit=[
            _IndexRow(name="ix_users_telegram_id", tbl_name="users"),
            _IndexRow(name="ix_users_last_seen", tbl_name="users"),
        ],
        implicit_count=3,
    )
    rendered = _render([_IndexSnapshot(db=DBName.USERS, tables=[tbl])])
    # The explicit names render in full.
    assert "ix_users_telegram_id" in rendered
    assert "ix_users_last_seen" in rendered
    # The header shows both counts.
    assert "<code>5</code>:" in rendered
    assert "<code>2</code> explicit" in rendered
    assert "<code>3</code> implicit" in rendered
    # Auto-index names must NOT leak — load-bearing: even one
    # ``sqlite_autoindex_users_1`` rendered would mean a single
    # heavily-constrained table eats the entire engine's budget.
    # The footer legend mentions ``sqlite_autoindex_*`` as a glossary
    # note, so check that no concrete numbered auto-index name (the
    # ``_users_1`` shape SQLite generates) appears as a list entry.
    assert "sqlite_autoindex_users" not in rendered


def test_render_truncates_per_table_explicit_list() -> None:
    """A table with more than the cap of explicit indexes must
    surface a tail line — silent truncation would hide the "this
    table is unexpectedly over-indexed" signal which is itself a
    finding worth investigating."""
    explicit = [
        _IndexRow(name=f"ix_t_{i:02d}", tbl_name="t") for i in range(_MAX_INDEXES_PER_TABLE + 4)
    ]
    tbl = _TableIndexes(tbl_name="t", explicit=explicit, implicit_count=0)
    rendered = _render([_IndexSnapshot(db=DBName.USERS, tables=[tbl])])
    assert "… and 4 more explicit" in rendered
    # The first cap-many render.
    assert f"ix_t_{_MAX_INDEXES_PER_TABLE - 1:02d}" in rendered
    # Past-cap entries do not.
    assert f"ix_t_{_MAX_INDEXES_PER_TABLE:02d}" not in rendered


def test_render_truncates_per_engine_table_list() -> None:
    """A DB with more tables-having-indexes than the per-engine cap
    must surface the engine-level tail — again, silent truncation
    would hide an unexpectedly broad schema from an operator
    skimming the card."""
    tables = [
        _TableIndexes(
            tbl_name=f"t_{i:03d}",
            explicit=[_IndexRow(name=f"ix_{i}", tbl_name=f"t_{i:03d}")],
            implicit_count=0,
        )
        for i in range(_MAX_TABLES_PER_DB + 3)
    ]
    # Re-sort to handler behaviour (most-indexed first; here all
    # totals are equal, so secondary sort by tbl_name kicks in).
    tables.sort(key=lambda t: (-t.total, t.tbl_name))
    rendered = _render([_IndexSnapshot(db=DBName.USERS, tables=tables)])
    assert "… and 3 more table(s)" in rendered


@pytest.mark.asyncio
async def test_read_captures_engine_failure_instead_of_raising() -> None:
    """#1645: a dead engine becomes a snapshot, not an exception."""

    class _BoomRegistry:
        def engine(self, db: DBName) -> Any:
            raise RuntimeError("engine gone")

    registry: Any = _BoomRegistry()
    snap = await _read_indexes(registry, DBName.USERS)
    assert snap.error == "RuntimeError"
    assert snap.tables == []


def test_render_draws_the_error_row_instead_of_the_zero_branch() -> None:
    """An unreadable engine must not be spelled like an empty one.

    "no indexes at all" is a real finding — a missed baseline
    migration. An engine that could not be opened says nothing
    at all about its indexes, and the two carry opposite next
    steps, so they get different row shapes (#1645)."""
    rendered = _render([_IndexSnapshot(db=DBName.USERS, tables=[], error="OperationalError")])
    assert "<b>users</b> ⚠ <code>OperationalError</code>" in rendered
    assert "no indexes at all" not in rendered
    assert "0 table(s) with indexes" not in rendered


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
        make_message_update("/admin_indexes", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert f"<b>{DBName.ECONOMY.value}</b> ⚠ <code>RuntimeError</code>" in text
    for db in ALL_DBS:
        if db is DBName.ECONOMY:
            continue
        assert f"<b>{db.value}</b> (" in text
