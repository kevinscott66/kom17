"""End-to-end ``/groupstats`` (Stage 27).

Pins:

* Group with donations → aggregate header + top-N donator list,
  sorted by total DESC.
* Group with no aggregate row → "no donations yet" localised line
  (legacy parity for a never-donated group).
* Private DM → UNHANDLED (falls through to legacy rating page).
* HTML in group_name / first_name is escaped.
* Missing display name → "Группа <id>" fallback (legacy used
  ``f"User {uid}"`` — port uses the localised group_default_name
  noun, matching the rest of the new pipeline).
* Top list scoped to the current chat — donations to another
  group don't surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import GroupDonationsAggregate, GroupTopDonator
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.groupstats import GroupStatsBoost
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_CHAT_ID = -100777
_OTHER_CHAT_ID = -100888


async def _seed_group(
    registry: EngineRegistry,
    *,
    chat_id: int = _CHAT_ID,
    group_name: str | None = "Альфа-группа",
    total_donations: int = 500,
    group_xp: int = 750,
    rating_position: int | None = 3,
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        session.add(
            GroupDonationsAggregate(
                group_id=chat_id,
                group_name=group_name,
                total_donations=total_donations,
                group_xp=group_xp,
                rating_position=rating_position,
            )
        )
        await session.commit()


async def _seed_top(
    registry: EngineRegistry,
    rows: list[tuple[int, int, int]],  # (group_id, user_id, total)
    names: dict[int, str] | None = None,
) -> None:
    economy = registry.engine(DBName.ECONOMY)
    async with AsyncSession(economy) as session:
        for gid, uid, total in rows:
            session.add(GroupTopDonator(group_id=gid, user_id=uid, total_donated=total))
        await session.commit()
    users_engine = registry.engine(DBName.USERS)
    async with AsyncSession(users_engine) as session:
        for uid, name in (names or {}).items():
            session.add(User(user_id=uid, first_name=name))
        await session.commit()


def _group_update(text: str = "/groupstats", *, user_id: int = 42) -> Any:
    return make_message_update(text, user_id=user_id, chat_id=_CHAT_ID, chat_type="supergroup")


@pytest.mark.asyncio
async def test_renders_aggregate_and_top(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry)
    await _seed_top(
        registry,
        rows=[
            (_CHAT_ID, 10, 300),
            (_CHAT_ID, 20, 200),
        ],
        names={10: "Алиса", 20: "Боб"},
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _group_update())
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    # Header
    assert "Альфа-группа" in body
    assert "<b>750</b>" in body  # XP
    assert "3" in body  # rating position
    # Top list: top donator first.
    assert body.index("Алиса") < body.index("Боб")
    assert "300" in body
    assert "200" in body


@pytest.mark.asyncio
async def test_top_query_breaks_ties_on_user_id(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Two donators on the same total must come back in a fixed order.

    Same guarantee as ``/donaters`` — without the tiebreaker the pair
    can swap between calls and, at the ``LIMIT`` boundary, a donator
    appears and disappears with nothing having changed. SQLite hides
    that at test scale (the sort is fed by the ``(group_id, user_id)``
    primary-key index and is stable for a handful of rows), so the
    ordering is asserted on the statement itself.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry)
    await _seed_top(
        registry,
        rows=[(_CHAT_ID, 9, 500), (_CHAT_ID, 2, 500)],
        names={9: "Зоя", 2: "Боб"},
    )
    statements: list[str] = []

    @event.listens_for(registry.engine(DBName.ECONOMY).sync_engine, "before_cursor_execute")
    def _record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        statements.append(statement)

    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _group_update())

    body = sent[-1]["text"]
    assert "Боб" in body
    assert "Зоя" in body
    selects = [s for s in statements if "group_top_donators" in s and "ORDER BY" in s]
    assert selects, statements
    order_by = selects[-1].split("ORDER BY", 1)[1]
    assert "total_donated DESC" in order_by
    assert "user_id ASC" in order_by


@pytest.mark.asyncio
async def test_boost_button_pops_hint(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
) -> None:
    """#7: tapping the 💝 Boost button answers with the localized how-to
    hint (no card edit — a read-only public alert)."""
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_callback_update(GroupStatsBoost().pack(), user_id=42, language_code="ru"),
    )
    assert result is not UNHANDLED
    answers = [e for e in sent if e["kind"] == "callback_answer"]
    assert len(answers) == 1
    assert "рейтинг" in answers[0]["text"].lower()
    # A hint, not a card mutation.
    assert not any(e["kind"] == "edit" for e in sent)


@pytest.mark.asyncio
async def test_no_aggregate_row_renders_refusal(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    body = sent[-1]["text"]
    assert t("no_donates_in_group", "ru") in body
    # The full stats card MUST NOT render — distinct from "zero stats".
    assert "Статистика группы" not in body


@pytest.mark.asyncio
async def test_private_routes_to_rating(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """In a private DM the groupstats spelling routes to the rating
    leaderboard (A-04 owns that branch), not this group-only handler.

    Before A-04 the router-level group filter left private ``/groupstats``
    UNHANDLED → a silent dead-end once the legacy bridge was deleted.
    With no groups in the empty test DB, the rating handler answers the
    localised "no groups with donations yet" line — proving the branch
    is claimed rather than dropped.
    """
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("/groupstats", user_id=42, chat_type="private")
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert sent[0]["text"] == t("no_groups_with_donates", "ru")


@pytest.mark.asyncio
async def test_escapes_html(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry, group_name="<b>spoof</b>")
    await _seed_top(
        registry,
        rows=[(_CHAT_ID, 10, 100)],
        names={10: "<i>name</i>"},
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    body = sent[-1]["text"]
    assert "<b>spoof</b>" not in body
    assert "&lt;b&gt;spoof&lt;/b&gt;" in body
    assert "<i>name</i>" not in body
    assert "&lt;i&gt;name&lt;/i&gt;" in body


@pytest.mark.asyncio
async def test_top_scoped_to_current_group(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A user with a huge donation total in ANOTHER group must not
    surface on this group's leaderboard. Bug regression here would
    cross-leak per-group standings.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry)
    await _seed_top(
        registry,
        rows=[
            (_CHAT_ID, 10, 50),
            (_OTHER_CHAT_ID, 20, 999_999),  # different group — must be excluded
        ],
        names={10: "Local", 20: "Outsider"},
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    body = sent[-1]["text"]
    assert "Local" in body
    assert "Outsider" not in body
    assert "999999" not in body


@pytest.mark.asyncio
async def test_missing_first_name_falls_back(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Top donator with no users.users row → render the localised
    group_default_name + id fallback. Legacy used 'User <id>'; the
    port uses the localised noun to stay consistent with the rest of
    the new pipeline.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry)
    await _seed_top(registry, rows=[(_CHAT_ID, 555, 100)], names={})
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update())
    body = sent[-1]["text"]
    assert "555" in body
    # The default noun ("Группа") doubles as the fallback prefix here.
    assert "Группа 555" in body


@pytest.mark.asyncio
async def test_zero_xp_is_not_replaced_by_the_treasury(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Regression (#479): no ``total_donations`` fallback for ``group_xp``.

    Legacy's ``g.get('group_xp', g['total_donations'])`` (bot.py:25005)
    reads like a fallback but never takes one — both keys come from the
    same ``COALESCE(group_xp, 0)`` expression (bot.py:10812) and the key
    is always present (bot.py:10839). The port's version substituted the
    *withdrawable treasury* for a zero XP score, printing a number the
    rating line right beside it contradicts.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry, total_donations=500, group_xp=0, rating_position=None)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _group_update())
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "<b>0</b>" in body
    assert "<b>500</b>" not in body


@pytest.mark.asyncio
async def test_zero_rating_position_renders_the_dash(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Regression (#485): falsy check, matching legacy and /chatstats.

    Legacy renders ``g['rating_position'] or '—'`` (bot.py:25006).
    ``recalc_positions`` only ever writes NULL or 1..N, so a stored 0 can
    only come from a legacy row — exactly the case the dash is for.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry, rating_position=0)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _group_update())
    assert result is not UNHANDLED
    assert "—" in sent[-1]["text"]
