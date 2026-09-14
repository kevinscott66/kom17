"""Achievement definitions + pure unlock rules (A-12).

Single source of truth for the 14 achievement definitions (id → localized
name / icon / unlock condition). Pure data + pure functions — NO I/O, no
DB, no aiogram — so both the awarding write path
(:meth:`telegram_invite_bot.repositories.economy_repo.EconomyRepo.award_achievements`)
and the read-only ``/achievements`` card import from here without a cycle.

Ported from legacy ``achievements`` / ``achievements_en`` dicts
(bot.py:44823-44984) + ``check_game_achievements`` (bot.py:15565).
Three deliberate divergences from legacy:

* **``>=`` not ``==``** thresholds. Legacy used exact equality
  (``total_played == 10``), so an achievement was missed whenever a stat
  jumped past the value (e.g. a PvP game increments both players' counts).
  Idempotent awarding (the ``(user_id, achievement_id)`` PK) makes ``>=``
  safe — re-evaluating an already-earned achievement just no-ops.
* **The "dead" achievements are now reachable.** Legacy ran the game/win
  checks (``check_game_achievements``, bot.py:15565) and a transfer check
  (``check_transfer_achievements``, bot.py:11566), leaving ``rich_*`` /
  ``streak_*`` defined-but-never awarded. Here every condition that maps
  to a tracked stat is evaluated, so those unlock too (after the user's
  next game or /daily claim).
* **``first_transfer`` is defined here, unlike legacy.** Legacy awarded
  the id (bot.py:11573) without ever putting it in its ``achievements``
  dict, so every render fell through to ``ach_data.get('name', ach)``
  (bot.py:17377, :21121) and printed the bare string ``first_transfer``
  at users, against a ``/13`` denominator that did not count it. Three
  production wallets hold that row. Defining it fixes both halves of
  that legacy bug; the count therefore reads ``/14``.

``first_message`` has no tracked stat (the new pipeline counts messages
per-chat for the economy, not a global lifetime counter), so it stays
unawardable — ``stat=None``. ``first_transfer`` is the same shape for a
different reason: only legacy ever awarded it, and the port deliberately
does not (see #473). Both are kept in the table so the denominator
counts what a user can actually be holding.

Coin ``reward`` is intentionally NOT modelled: legacy stored a reward
field but ``award_achievement`` never paid it out (bot.py:44648), so
honouring it would be a behavioural change, not parity.
"""

from __future__ import annotations

from typing import NamedTuple


class Achievement(NamedTuple):
    """One achievement definition.

    ``stat`` names the tracked metric the unlock is keyed on (one of the
    keys :func:`eligible_ids` accepts); ``None`` means "not awardable from
    a tracked stat" — ``first_message`` and ``first_transfer`` both, for
    the two different reasons the module docstring gives. ``threshold`` is
    the inclusive ``>=`` bound.
    """

    ru: str
    en: str
    icon: str
    stat: str | None
    threshold: int
    desc_ru: str
    desc_en: str


# Order mirrors legacy's listing; the card preserves DB ``earned_date``
# order, so this ordering is purely cosmetic for any full-table render.
# RR-1 #8: the ``desc_*`` columns restore the one-line "how to earn it"
# blurb the legacy /achievements list showed under each row.
# The table is deliberately one row per line so the columns line up;
# ``ruff format`` would explode each row into nine lines and lose that.
# The matching E501 waiver lives in ``pyproject.toml`` per-file-ignores.
# fmt: off
DEFINITIONS: dict[str, Achievement] = {
    "first_message": Achievement("Первое слово", "First word", "💬", None, 0, "Написать первое сообщение", "Send your first message"),
    "first_transfer": Achievement("Первый перевод", "First transfer", "💸", None, 0, "Перевести монеты другому игроку", "Send coins to another player"),
    "first_game": Achievement("Новичок", "First game", "🎮", "games_played", 1, "Сыграть первую игру", "Play your first game"),
    "first_win": Achievement("Первая победа", "First win", "🏆", "games_won", 1, "Одержать первую победу", "Win your first game"),
    "game_master": Achievement("Игрок", "Game master", "🎲", "games_played", 10, "Сыграть 10 игр", "Play 10 games"),
    "game_legend": Achievement("Легенда игр", "Game legend", "👑", "games_played", 100, "Сыграть 100 игр", "Play 100 games"),
    "win_master": Achievement("Победитель", "Win master", "🏅", "games_won", 10, "Выиграть 10 игр", "Win 10 games"),
    "win_legend": Achievement("Чемпион", "Champion", "🏆🏆", "games_won", 50, "Выиграть 50 игр", "Win 50 games"),
    "duel_winner": Achievement("Дуэлянт", "Duelist", "⚔️", "duel_wins", 1, "Победить в дуэли", "Win a duel"),
    "duel_master": Achievement("Мастер дуэлей", "Duel master", "⚔️🏆", "duel_wins", 10, "Победить в 10 дуэлях", "Win 10 duels"),
    "rich_1000": Achievement("Богач", "Rich", "💰", "balance", 1_000, "Накопить 1 000 монет", "Reach 1,000 coins"),
    "rich_10000": Achievement("Миллионер", "Millionaire", "💎", "balance", 10_000, "Накопить 10 000 монет", "Reach 10,000 coins"),
    "streak_7": Achievement("Недельный стрик", "7-day streak", "🔥", "daily_streak", 7, "Заходить 7 дней подряд", "Keep a 7-day daily streak"),
    "streak_30": Achievement("Месячный стрик", "30-day streak", "🔥🔥", "daily_streak", 30, "Заходить 30 дней подряд", "Keep a 30-day daily streak"),
}
# fmt: on

TOTAL = len(DEFINITIONS)


def eligible_ids(
    *,
    games_played: int,
    games_won: int,
    duel_wins: int,
    daily_streak: int,
    balance: int,
) -> set[str]:
    """Return the ids whose unlock condition the given stats satisfy.

    Pure: returns EVERY achievement the stats qualify for (already-earned
    filtering is the caller's job — done against the per-user ledger so
    the result is idempotent). The two ``stat=None`` rows —
    ``first_message`` and ``first_transfer`` — are never included.
    """
    stats = {
        "games_played": games_played,
        "games_won": games_won,
        "duel_wins": duel_wins,
        "daily_streak": daily_streak,
        "balance": balance,
    }
    return {
        aid for aid, a in DEFINITIONS.items() if a.stat is not None and stats[a.stat] >= a.threshold
    }


def name(achievement_id: str, lang: str) -> str:
    """Localized name for an id, falling back to the raw id if unknown."""
    definition = DEFINITIONS.get(achievement_id)
    if definition is None:
        return achievement_id
    return definition.ru if lang == "ru" else definition.en


def description(achievement_id: str, lang: str) -> str:
    """Localized one-line "how to earn it" blurb, or '' if unknown."""
    definition = DEFINITIONS.get(achievement_id)
    if definition is None:
        return ""
    return definition.desc_ru if lang == "ru" else definition.desc_en


def icon(achievement_id: str) -> str:
    """Display icon for an id, ``🏆`` for an unknown (legacy) id."""
    definition = DEFINITIONS.get(achievement_id)
    return definition.icon if definition is not None else "🏆"
