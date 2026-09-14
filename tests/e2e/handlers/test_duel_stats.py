"""End-to-end ``/duel_stats`` (Stage 26).

Pins:

* No duels → static "empty" line (not a stat-card full of zeros).
* With duels → aggregates over the user's rows: count / wins /
  losses / sums / extremes.
* ``game != 'duel'`` rows DON'T leak in — other games live in the
  same table.
* Per-user scoping: another user's duels stay invisible.
* Worst-loss is rendered as ``abs(...)``; the 💔 emoji carries the
  sign so the number reads naturally.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import GameResult
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_USER_ID = 2222


@pytest.fixture(autouse=True)
def _clear_language_cache() -> None:
    """Language resolution caches per user id across tests.

    Start clean so the ``language_code``-driven tests below are not
    served a decision made for the same id by an earlier test.
    """
    from telegram_invite_bot.middlewares.language import clear_language_cache

    clear_language_cache()


async def _seed(
    registry: EngineRegistry,
    rows: list[tuple[int, str, int, bool, int]],  # (uid, game, bet, win, profit)
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for uid, game, bet, win, profit in rows:
            session.add(
                GameResult(
                    user_id=uid,
                    game=game,
                    bet=bet,
                    win=win,
                    profit=profit,
                    date=datetime(2026, 1, 1),
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_empty_renders_short_line(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/duel_stats", user_id=_USER_ID))
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "не участвовал" in body
    # Stat-card markers must not appear — otherwise the empty short-
    # circuit got bypassed and the user sees seven zeros.
    assert "Побед" not in body
    assert "Поражений" not in body


@pytest.mark.asyncio
async def test_aggregates_render(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    # 3 duels: 2 wins (+100 each), 1 loss (-50). Total profit 150,
    # avg bet 200/3 → 66 with integer division, win-rate 66%.
    await _seed(
        registry,
        rows=[
            (_USER_ID, "duel", 100, True, 100),
            (_USER_ID, "duel", 100, True, 100),
            (_USER_ID, "duel", 50, False, -50),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/duel_stats", user_id=_USER_ID))
    body = sent[0]["text"]
    assert "<code>3</code>" in body  # total
    assert "<code>2</code>" in body  # wins
    assert "<code>1</code>" in body  # losses
    assert "<code>66%</code>" in body  # integer win-rate
    assert "<code>150</code>" in body  # total profit
    assert "<code>100</code>" in body  # max_win
    # max_loss is rendered as abs(-50) = 50.
    assert "<code>50</code>" in body


@pytest.mark.asyncio
async def test_other_games_excluded(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A roulette row in the same table must not inflate the duel
    aggregate — both legacy and the new port filter on ``game='duel'``.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        rows=[
            (_USER_ID, "duel", 100, True, 100),
            (_USER_ID, "roulette", 9999, True, 99_999),  # noise — must be ignored
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/duel_stats", user_id=_USER_ID))
    body = sent[0]["text"]
    assert "<code>1</code>" in body  # total duels = 1, not 2
    assert "99999" not in body
    assert "100" in body


@pytest.mark.asyncio
async def test_other_users_excluded(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Critical privacy/correctness pin — another user's duels must not
    surface in the caller's card.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        rows=[
            (_USER_ID, "duel", 10, True, 10),
            (9999, "duel", 999_999, True, 999_999),  # someone else
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/duel_stats", user_id=_USER_ID))
    body = sent[0]["text"]
    assert "999999" not in body
    assert "10" in body


@pytest.mark.asyncio
async def test_lang_en_renders_english_card(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """L-20: the card renders through ``t(key, lang)`` with the injected
    effective language — an English-locale caller sees the EN template,
    zero Cyrillic."""
    en_user = 3333  # distinct id — LanguageMiddleware caches per user
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(registry, rows=[(en_user, "duel", 100, True, 100)])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/duel_stats", user_id=en_user, language_code="en")
    )
    body = sent[0]["text"]
    assert "Your duel stats" in body
    assert "Win rate" in body
    assert "Побед" not in body


@pytest.mark.asyncio
async def test_empty_en_renders_english_line(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    en_user = 4444
    bot, dispatcher, _registry = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/duel_stats", user_id=en_user, language_code="en")
    )
    assert "have not taken part" in sent[0]["text"]
