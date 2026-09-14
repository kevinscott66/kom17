"""End-to-end ``/donaters`` (Stage 23).

Pins:

* Group call with donations → ranked list, top first.
* Group with no donations → invite-to-donate text (not silent).
* Per-group scoping — donations from another group don't leak.
* Names sourced from ``users.users`` first_name; missing rows fall
  back to ``"Пользователь <id>"`` so the leaderboard never renders
  an empty mention.
* HTML in user names is escaped (the bot-wide HTML parse_mode would
  otherwise mis-render or refuse a name like ``<b>spoof</b>``).
* Private DM gets the #123 group-only refusal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import GroupTopDonator
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import assert_chat_scope_refusal, make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_CHAT_ID = -100_777
_OTHER_CHAT_ID = -100_888


async def _seed(
    registry: EngineRegistry,
    *,
    donors: list[tuple[int, int]],
    users: dict[int, str | None] | None = None,
    foreign_donors: list[tuple[int, int]] | None = None,
) -> None:
    """Seed economy.group_top_donators (this chat + optional foreign chat)
    and users.users for display-name resolution.

    ``users=None`` means we deliberately omit ``users.users`` rows so
    the test asserts the missing-name fallback path.
    """
    econ_engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(econ_engine) as session:
        for uid, total in donors:
            session.add(GroupTopDonator(group_id=_CHAT_ID, user_id=uid, total_donated=total))
        for uid, total in foreign_donors or []:
            session.add(GroupTopDonator(group_id=_OTHER_CHAT_ID, user_id=uid, total_donated=total))
        await session.commit()
    if users is None:
        return
    users_engine = registry.engine(DBName.USERS)
    async with AsyncSession(users_engine) as session:
        for uid, name in users.items():
            session.add(User(user_id=uid, first_name=name))
        await session.commit()


def _group_update(
    text: str = "/donaters", *, user_id: int = 4242, language_code: str | None = None
) -> Any:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        language_code=language_code,
    )


@pytest.mark.asyncio
async def test_donaters_renders_ranked_list(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed(
        registry,
        donors=[(1, 500), (2, 1500), (3, 100)],
        users={1: "Alice", 2: "Bob", 3: "Carol"},
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _group_update())

    assert result is not UNHANDLED
    body = sent[-1]["text"]
    # Top → Bob (1500) → Alice (500) → Carol (100).
    assert body.index("Bob") < body.index("Alice") < body.index("Carol")
    assert "1500" in body
    assert "500" in body
    assert "100" in body
    # tg:// mention link is present for ranking purposes.
    assert 'href="tg://user?id=2"' in body


@pytest.mark.asyncio
async def test_donaters_query_breaks_ties_on_user_id(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Two donators on the same total must come back in a fixed order.

    Without a tiebreaker the storage engine returns equal rows in
    whatever order suits it, so the pair can swap between calls; at
    the ``LIMIT`` boundary that changes the SET of rows returned and a
    donator appears and disappears with nothing having changed.
    SQLite hides that at test scale — the sort is fed by the
    ``(group_id, user_id)`` primary-key index and is stable for a
    handful of rows — so the ordering is asserted on the statement,
    which is where the guarantee is actually made.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed(registry, donors=[(9, 500), (2, 500)], users={9: "Zoe", 2: "Bob"})
    statements: list[str] = []

    @event.listens_for(registry.engine(DBName.ECONOMY).sync_engine, "before_cursor_execute")
    def _record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        statements.append(statement)

    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _group_update())

    body = sent[-1]["text"]
    assert "Bob" in body
    assert "Zoe" in body
    selects = [s for s in statements if "group_top_donators" in s and "ORDER BY" in s]
    assert selects, statements
    order_by = selects[-1].split("ORDER BY", 1)[1]
    assert "total_donated DESC" in order_by
    assert "user_id ASC" in order_by


@pytest.mark.asyncio
async def test_donaters_empty_renders_invite(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(schemas=[UsersBase, EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    assert "пока нет донатов" in sent[-1]["text"]
    assert "/donate" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_donaters_does_not_leak_across_groups(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Critical: ``/donaters`` in chat A must not include donors who
    donated to chat B. A regression here would expose other groups'
    leaderboards on every call — privacy-sensitive.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed(
        registry,
        donors=[(1, 100)],
        foreign_donors=[(99, 999_999)],
        users={1: "Alice", 99: "Mallory"},
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    body = sent[-1]["text"]
    assert "Alice" in body
    assert "Mallory" not in body
    assert "999999" not in body


@pytest.mark.asyncio
async def test_donaters_falls_back_for_missing_name(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed(registry, donors=[(7777, 50)], users={})
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    body = sent[-1]["text"]
    # Fallback shape is "Пользователь <id>" — never empty, never blank.
    assert "Пользователь 7777" in body


@pytest.mark.asyncio
async def test_donaters_answers_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Title, row and the missing-name fallback were Russian literals.

    The fallback matters on its own: it is substituted per row, so a
    board that is otherwise translated can still hand an English user
    a Cyrillic name for every donor who never hit /start.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed(registry, donors=[(1, 500), (7777, 50)], users={1: "Alice"})
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update(language_code="en"))
    body = sent[-1]["text"]
    assert "Top group donators" in body
    assert "User 7777" in body
    assert not any("\u0400" <= ch <= "\u04ff" for ch in body), body


@pytest.mark.asyncio
async def test_donaters_empty_state_is_translated(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The empty board returns before the renderer runs — its own path."""
    bot, dispatcher, _registry = await make_wired(schemas=[UsersBase, EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update(language_code="en"))
    body = sent[-1]["text"]
    assert "No donations in this group yet" in body
    assert not any("\u0400" <= ch <= "\u04ff" for ch in body), body


@pytest.mark.asyncio
async def test_donaters_escapes_html_in_names(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed(
        registry,
        donors=[(1, 10)],
        users={1: "<b>spoof</b>"},
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    body = sent[-1]["text"]
    # Display name went through ``html.escape`` inside ``html_user_mention``.
    assert "<b>spoof</b>" not in body
    assert "&lt;b&gt;spoof&lt;/b&gt;" in body


@pytest.mark.asyncio
async def test_donaters_private_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A DM ``/donaters`` gets "group only", never a board (#123).

    The board is per-group, so there is nothing to render in a DM. The
    exact match on the refusal is the guard: a regression that let the
    private call reach the renderer would leak the seeded donor list.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed(registry, donors=[(1, 10)], users={1: "Alice"})
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("/donaters", user_id=1, chat_type="private")
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="group", command="donaters")
