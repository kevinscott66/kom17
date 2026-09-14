"""End-to-end ``/achievements`` flow: dispatcher → Session+Economy → DB.

A-07 — read-only achievements card. Exercises the empty state (0/14 +
"no achievements" block), the earned state (rows render with icon +
name, progress counts, footer stats), the group-chat path (legacy
answers in groups too), and the graceful-degradation contract (an
unknown achievement_id from a legacy row must not crash the card).

Uses the shared ``make_wired`` / ``capture_outgoing`` fixtures. The
handler reads ``user_service`` (SessionMiddleware → users.db) AND
``achievements_repo`` (EconomyMiddleware → economy.db), so the wiring
needs both schemas plus ``session_middleware=True``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import (
    assert_unknown_form_hint,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


def _update(
    text: str,
    *,
    chat_type: str = "private",
    user_id: int = 7007,
    language_code: str = "ru",
) -> Update:
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Ace",
        language_code=language_code,
    )


async def _seed_economy_user(
    registry: EngineRegistry,
    *,
    user_id: int,
    balance: int = 0,
    games_played: int = 0,
    games_won: int = 0,
) -> None:
    from telegram_invite_bot.db.models.economy import EconomyUser

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            EconomyUser(
                user_id=user_id,
                balance=balance,
                games_played=games_played,
                games_won=games_won,
                language="ru",
            )
        )
        await session.commit()


async def _seed_achievement(
    registry: EngineRegistry,
    *,
    user_id: int,
    achievement_id: str,
    earned_date: datetime | None = None,
) -> None:
    from telegram_invite_bot.db.models.economy import UserAchievement

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            UserAchievement(
                user_id=user_id,
                achievement_id=achievement_id,
                earned_date=earned_date,
            )
        )
        await session.commit()


async def test_achievements_empty_shows_zero_and_none_block(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/achievements"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "ТВОИ ДОСТИЖЕНИЯ" in body
    assert "0/14" in body
    assert "Нет достижений" in body
    # Footer stats render even with no wallet row (collapse to zeros).
    assert "Баланс" in body
    assert "Побед" in body


async def test_achievements_earned_rows_render(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_economy_user(registry, user_id=7007, balance=1234, games_played=42, games_won=9)
    await _seed_achievement(
        registry,
        user_id=7007,
        achievement_id="first_win",
        earned_date=datetime(2025, 3, 14, 10, 0, 0),
    )
    await _seed_achievement(
        registry,
        user_id=7007,
        achievement_id="rich_1000",
        earned_date=datetime(2025, 1, 2, 8, 0, 0),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/achievements"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "2/14" in body
    assert "Получены" in body
    # Both earned definitions render with icon + RU name.
    assert "🏆" in body and "Первая победа" in body
    assert "💰" in body and "Богач" in body
    # Newest unlock first (earned_date DESC): first_win (Mar) before rich (Jan).
    assert body.index("Первая победа") < body.index("Богач")
    assert "2025-03-14" in body
    # Footer stats reflect the seeded wallet.
    assert "1 234" in body
    assert "42" in body
    assert "9" in body


async def test_achievements_alias_ach(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/ach"))
    assert result is not UNHANDLED
    assert "ТВОИ ДОСТИЖЕНИЯ" in sent[0]["text"]


async def test_achievements_renders_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_economy_user(registry, user_id=7007, games_played=3)
    await _seed_achievement(
        registry,
        user_id=7007,
        achievement_id="first_game",
        earned_date=datetime(2025, 5, 1),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/achievements", chat_type="supergroup"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "1/14" in body
    assert "Новичок" in body


async def test_achievements_en_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_achievement(
        registry,
        user_id=7008,
        achievement_id="first_win",
        earned_date=datetime(2025, 2, 2),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/achievements", user_id=7008, language_code="en")
    )
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "YOUR ACHIEVEMENTS" in body
    assert "First win" in body
    assert "1/14" in body


@pytest.mark.parametrize(
    ("achievement_id", "rendered", "absent"),
    [
        ("some_legacy_ghost", "some_legacy_ghost", None),
        ("<b>ghost</b>", "&lt;b&gt;ghost&lt;/b&gt;", "<b>ghost</b>"),
    ],
)
async def test_achievements_unknown_id_does_not_crash(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    achievement_id: str,
    rendered: str,
    absent: str | None,
) -> None:
    """A legacy row with an achievement_id we never modelled must render
    gracefully (raw id, fallback icon) rather than crashing the card.

    The second case is the same path with teeth: the fallback prints DB
    text into an HTML card, so an id carrying markup has to arrive
    escaped. Nothing writes such an id today — the legacy table is the
    only source and it is seeded by legacy code — but the fallback is
    the one place on this card where a stored string reaches the markup
    unmediated, and that is worth pinning rather than assuming."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_achievement(
        registry,
        user_id=7007,
        achievement_id=achievement_id,
        earned_date=datetime(2025, 4, 4),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/achievements"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    # Unknown id counts toward the earned total and renders its raw id.
    assert "1/14" in body
    assert rendered in body
    if absent is not None:
        assert absent not in body


async def test_achievements_with_args_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/achievements @other`` — a cross-user lookup is unported (#158).

    Unchanged: we do not render someone else's achievements. Changed:
    the user hears back. The old contract named legacy as the party who
    would answer this, and legacy no longer exists.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/achievements @someone"))

    assert result is not UNHANDLED
    assert_unknown_form_hint(sent, command="achievements")
