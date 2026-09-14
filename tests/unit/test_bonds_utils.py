"""Pure-function tests for ``utils.bonds`` (Stage 19).

Pinning the arithmetic in isolation keeps the integration tests focused
on JOINs / ordering / wiring. If a legacy bucket threshold ever changes
in :mod:`bot`, the diff lands here first — easier to read than a
multi-line leaderboard output.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from telegram_invite_bot.utils.bonds import (
    format_db_date,
    format_duration,
    marriage_category,
    marriage_level_name,
    marriage_xp_to_level,
    relationship_xp_to_level,
)

# --- Marriage XP → level (bot.py:21636 — 100 XP/level, cap at 5) ----------


@pytest.mark.parametrize(
    ("xp", "expected_level"),
    [
        (0, 1),
        (50, 1),
        (99, 1),
        (100, 2),
        (250, 3),
        (399, 4),
        (400, 5),
        # Past the name table — legacy keeps counting (no clamp); the
        # name lookup falls back to "Супруги" via marriage_level_name.
        (10_000, 101),
    ],
)
def test_marriage_xp_to_level(xp: int, expected_level: int) -> None:
    assert marriage_xp_to_level(xp) == expected_level


def test_marriage_level_name_ru() -> None:
    assert marriage_level_name(1) == "Новобрачные"
    assert marriage_level_name(5) == "Неразлучны"
    # Unknown levels fall back to legacy's "Супруги" (bot.py:21641).
    assert marriage_level_name(99) == "Супруги"


def test_marriage_level_name_en() -> None:
    """``lang="en"`` returns the English mirror, Cyrillic-free."""
    assert marriage_level_name(1, "en") == "Newlyweds"
    assert marriage_level_name(3, "en") == "Family"
    assert marriage_level_name(5, "en") == "Inseparable"
    # Unknown levels fall back to the EN default.
    assert marriage_level_name(99, "en") == "Spouses"


# --- Relationship XP → level (bot.py:22197, non-linear table) -------------


@pytest.mark.parametrize(
    ("xp", "expected_level"),
    [
        (0, 0),  # no XP yet → no relationship level
        (149, 0),
        (150, 1),  # first threshold (RELATIONSHIP_LEVEL_XP[1])
        (1499, 1),
        (1500, 2),
        (10_000_000, 11),  # top of the table
        (50_000_000, 11),  # cap holds for very high XP
    ],
)
def test_relationship_xp_to_level(xp: int, expected_level: int) -> None:
    assert relationship_xp_to_level(xp) == expected_level


# --- Date formatter --------------------------------------------------------


def test_format_db_date_datetime() -> None:
    assert format_db_date(datetime(2024, 3, 15, 12, 30)) == "2024-03-15"


def test_format_db_date_string_isoformat() -> None:
    """Legacy stores TIMESTAMP as a string in some prod rows
    (PARSE_DECLTYPES turned off on certain code paths). Repo can't
    guarantee a ``datetime`` — accept the string too.
    """
    assert format_db_date("2024-03-15 12:30:00") == "2024-03-15"


def test_format_db_date_none_renders_em_dash() -> None:
    assert format_db_date(None) == "—"


# --- Duration buckets (bot.py:21686) --------------------------------------


@pytest.mark.parametrize(
    ("delta_days", "expected"),
    [
        (0, "0 дн."),
        (1, "1 дн."),
        (30, "30 дн."),
        (31, "1 мес."),
        (90, "3 мес."),
        (364, "12 мес."),
        (365, "1 лет"),
        (730, "2 лет"),
    ],
)
def test_format_duration_buckets(delta_days: int, expected: str) -> None:
    now = datetime(2024, 6, 1, 12, 0)
    created = now - timedelta(days=delta_days)
    assert format_duration(created, now=now) == expected


def test_format_duration_none() -> None:
    assert format_duration(None) == "—"


@pytest.mark.parametrize(
    ("delta_days", "expected"),
    [
        (0, "0 d"),
        (1, "1 d"),
        (30, "30 d"),
        (31, "1 mo"),
        (90, "3 mo"),
        (364, "12 mo"),
        (365, "1 y"),
        (730, "2 y"),
    ],
)
def test_format_duration_buckets_en(delta_days: int, expected: str) -> None:
    """``lang="en"`` returns concise English duration forms, no Cyrillic."""
    now = datetime(2024, 6, 1, 12, 0)
    created = now - timedelta(days=delta_days)
    assert format_duration(created, now=now, lang="en") == expected


# --- Marriage category (bot.py:21724) -------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (0, "Молодожёны"),
        (30, "Молодожёны"),
        (31, "Опытные"),
        (31 * 6 - 1, "Опытные"),
        (31 * 6, "Семейные"),
        (364, "Семейные"),
        (365, "Ветераны"),
        (5000, "Ветераны"),
    ],
)
def test_marriage_category_buckets(days: int, expected: str) -> None:
    now = datetime(2024, 6, 1, 12, 0)
    created = now - timedelta(days=days)
    assert marriage_category(created, now=now) == expected


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (0, "Newlyweds"),
        (30, "Newlyweds"),
        (31, "Experienced"),
        (31 * 6 - 1, "Experienced"),
        (31 * 6, "Settled"),
        (364, "Settled"),
        (365, "Veterans"),
        (5000, "Veterans"),
    ],
)
def test_marriage_category_buckets_en(days: int, expected: str) -> None:
    """``lang="en"`` returns English tier names, Cyrillic-free."""
    now = datetime(2024, 6, 1, 12, 0)
    created = now - timedelta(days=days)
    assert marriage_category(created, now=now, lang="en") == expected


def test_marriage_category_includes_extra_days() -> None:
    """``/marry_extend`` lets users buy additional days. The
    leaderboard must reflect the bumped category so a couple who
    spent 1000 coins to "feel" more veteran doesn't see their
    purchase ignored. Same arithmetic as bot.py:21734.
    """
    now = datetime(2024, 6, 1)
    created = now - timedelta(days=10)  # natural: Молодожёны
    # 10 natural + 360 bought = 370 days → past the 365-day Ветераны cut.
    assert marriage_category(created, extra_days=360, now=now) == "Ветераны"


# --- Defensive parsing paths ----------------------------------------------
#
# bonds.* takes ``created_at`` from the DB, which has historically been
# a mix of: aware datetimes (new code), naive datetimes (older rows),
# ISO strings (legacy SQLite TEXT columns), and outright garbage from
# the manual-edit days. The branches below lock the unhappy paths so
# a regression in _coerce_dt doesn't surface as a 500 in profile /
# /top render.


def test_format_duration_falls_back_to_em_dash_on_unparseable_string() -> None:
    """A ``created_at`` value that isn't a datetime and doesn't parse
    as ISO-8601 must surface as the em-dash placeholder, not crash
    the renderer. Legacy DB rows occasionally hold strings like
    ``"unknown"`` from the manual-edit era.
    """
    assert format_duration("not-a-date") == "—"


def test_marriage_category_uses_extra_days_when_created_is_garbage() -> None:
    """If ``created_at`` doesn't parse, ``marriage_category`` falls
    back to ``extra_days`` alone — a paid couple keeps their bought
    tier even when the natural-age column is corrupted.

    Without this branch a bad row would always render as Молодожёны
    (0 days), silently downgrading users who actually paid.
    """
    # 400 days of paid time, garbage natural date → Ветераны (≥365).
    assert marriage_category("unparseable", extra_days=400) == "Ветераны"


def test_format_duration_strips_tzinfo_from_aware_iso_strings() -> None:
    """ISO strings with a ``Z`` suffix or explicit offset must parse,
    then have their tzinfo stripped so arithmetic against a naive
    ``now`` works. The legacy column mixes both styles per-row, so
    every renderer goes through this branch in production.
    """
    now = datetime(2024, 6, 1, 12, 0)
    # 10 days ago, written as a Z-suffix ISO string the way SQLite's
    # default datetime adapter sometimes emits.
    assert format_duration("2024-05-22T12:00:00Z", now=now) == "10 дн."


def test_marriage_category_clamps_negative_extra_days_to_zero() -> None:
    """``extra_days`` is user-provided via a paid extension and the
    repo *should* never store a negative, but defensive clamp here
    means a future bug in the purchase flow can't roll users *back*
    to a younger tier.
    """
    assert marriage_category(None, extra_days=-1000) == "Молодожёны"
