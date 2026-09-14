"""Pure unlock-rule logic for the achievements core (A-12)."""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.achievements import (
    DEFINITIONS,
    TOTAL,
    eligible_ids,
    icon,
    name,
)

_ZERO = {
    "games_played": 0,
    "games_won": 0,
    "duel_wins": 0,
    "daily_streak": 0,
    "balance": 0,
}


def test_total_is_fourteen() -> None:
    # 13 legacy definitions + ``first_transfer``, which legacy awarded
    # (bot.py:11573) without ever defining — see #473.
    assert TOTAL == 14


def test_first_transfer_never_awardable() -> None:
    # Legacy-only id: kept for rendering rows that already exist in
    # production, but ``stat=None`` so nothing here can award it.
    maxed = {k: 10**9 for k in _ZERO}
    assert "first_transfer" not in eligible_ids(**maxed)
    assert name("first_transfer", "ru") == "Первый перевод"


def test_no_stats_unlocks_nothing() -> None:
    assert eligible_ids(**_ZERO) == set()


def test_first_message_never_awardable() -> None:
    # stat=None → excluded even with maxed-out stats.
    maxed = {k: 10**9 for k in _ZERO}
    assert "first_message" not in eligible_ids(**maxed)
    assert DEFINITIONS["first_message"].stat is None


@pytest.mark.parametrize(
    ("stats", "expected_subset"),
    [
        ({**_ZERO, "games_played": 1}, {"first_game"}),
        ({**_ZERO, "games_played": 10}, {"first_game", "game_master"}),
        ({**_ZERO, "games_played": 100}, {"first_game", "game_master", "game_legend"}),
        ({**_ZERO, "games_won": 1}, {"first_win"}),
        ({**_ZERO, "games_won": 50}, {"first_win", "win_master", "win_legend"}),
        ({**_ZERO, "duel_wins": 1}, {"duel_winner"}),
        ({**_ZERO, "duel_wins": 10}, {"duel_winner", "duel_master"}),
        ({**_ZERO, "balance": 1_000}, {"rich_1000"}),
        ({**_ZERO, "balance": 10_000}, {"rich_1000", "rich_10000"}),
        ({**_ZERO, "daily_streak": 7}, {"streak_7"}),
        ({**_ZERO, "daily_streak": 30}, {"streak_7", "streak_30"}),
    ],
)
def test_thresholds_inclusive(stats: dict[str, int], expected_subset: set[str]) -> None:
    assert expected_subset <= eligible_ids(**stats)


def test_threshold_is_ge_not_eq() -> None:
    # 11 games (jumped past the ==10 legacy bug) still unlocks game_master.
    assert "game_master" in eligible_ids(**{**_ZERO, "games_played": 11})


def test_name_and_icon_fallback_for_unknown_id() -> None:
    assert name("not_a_real_id", "ru") == "not_a_real_id"
    assert icon("not_a_real_id") == "🏆"
    assert name("first_game", "ru") == "Новичок"
    assert name("first_game", "en") == "First game"
