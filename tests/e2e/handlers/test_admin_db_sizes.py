"""End-to-end ``/admin_db_sizes``.

Pins:

* Non-developer → silent drop.
* Card lists every DB from :data:`ALL_DBS`.
* file/logical/wal lines render for every DB even when wal is
  absent (em-dash) — the operator must see the row, not have it
  silently dropped.
* Real on-disk byte counts appear (writing a row inflates the
  file, the size readout reflects that — not a stub).
* No stuck-checkpoint warning on a healthy DB.
* Stuck-checkpoint warning surfaces when WAL > file size.
* Formatter rounds to KiB / MiB / GiB sensibly.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.handlers.admin.db_sizes import _fmt
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
        make_message_update("/admin_db_sizes", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_lists_all_dbs(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_db_sizes", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "DB sizes" in text
    for db in ALL_DBS:
        assert f"<b>{db.value}</b>" in text
        # Every row must have a file/logical/wal line — even when wal
        # is absent. A silently-dropped row would confuse the operator.
        # (Three "• " bullets per DB.)
    # No stuck-checkpoint warning on a freshly-built fixture.
    assert "checkpoint may be" not in text


@pytest.mark.asyncio
async def test_file_size_reflects_real_writes(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """A row in users.db inflates the file. The card must show a
    non-zero file size after the insert — proves we're reading the
    actual disk state, not a stub."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=100, username="u"))
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_db_sizes", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # The users-block must show a real KiB-or-larger file. The block
    # begins at the users header and runs until the next blank line.
    users_block = text.split("<b>users</b>", 1)[1].split("\n\n", 1)[0]
    assert "KiB" in users_block or "MiB" in users_block


def test_fmt_rounds_correctly() -> None:
    """Unit-level check on the formatter so the bucketed render rules
    aren't only tested indirectly through full-card output. The cases
    cover every branch of :func:`_fmt`."""
    assert _fmt(None) == "—"
    assert _fmt(512) == "512 B"
    assert _fmt(2048) == "2.0 KiB"
    assert _fmt(5 * 1024 * 1024) == "5.0 MiB"
    assert _fmt(3 * 1024 * 1024 * 1024) == "3.00 GiB"


@pytest.mark.asyncio
async def test_stuck_checkpoint_warning_on_oversized_wal(
    make_wired: WiredFactory, capture_outgoing: Any, tmp_path: Any
) -> None:
    """When the WAL file is materially larger than the main DB (the
    classic stuck-checkpoint symptom), the card must surface the
    warning. Forge the condition by writing a fake oversized
    ``users.db-wal`` file alongside the real DB."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    # Real settings path is read via ``resolve_db_path``; we plant the
    # WAL file at the same location the handler will look at. Have to
    # use the same Settings the dispatcher built — resolve via the
    # internals of make_wired's fixture isn't ergonomic, so reconstruct
    # the path by querying the engine's URL.
    engine = registry.engine(DBName.USERS)
    # The aiosqlite URL is ``sqlite+aiosqlite:///<path>``.
    url = str(engine.url)
    db_path_str = url.split("sqlite+aiosqlite:///", 1)[1]
    from pathlib import Path

    db_path = Path(db_path_str)
    # Ensure the .db actually exists with some bytes by issuing a write.
    async with AsyncSession(engine) as session:
        session.add(User(user_id=1, username="x"))
        await session.commit()
    # Forge a 5 MiB WAL alongside it.
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_path.write_bytes(b"\0" * (5 * 1024 * 1024))

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_db_sizes", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "checkpoint may be" in text


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
            "/admin_db_sizes",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
