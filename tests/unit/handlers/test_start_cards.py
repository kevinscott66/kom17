"""Unit coverage for the ``/start`` card renderers (RR-6 #60/#61).

The renderers are pure ``(data) -> str``, so the *shape* of the highest-
traffic surface in the bot is pinned here — in both locales at once —
instead of only through the slower e2e path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import User as TgUser

from telegram_invite_bot.handlers.start_cards import (
    ROLE_DEVELOPER,
    ROLE_MEMBER,
    ROLE_OWNER,
    display_name,
    greeting,
    render_group_welcome,
    render_welcome_back,
    render_welcome_new,
    role_icon,
)
from telegram_invite_bot.utils.time import day_period, local_now

LANGS = ["ru", "en"]


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 6, hour, 30, tzinfo=UTC)


# --- time-of-day bucketing --------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, "night"),
        (4, "night"),
        (5, "morning"),  # boundary: morning opens at 05
        (11, "morning"),
        (12, "day"),  # boundary: day opens at 12
        (17, "day"),
        (18, "evening"),  # boundary: evening opens at 18
        (22, "evening"),
        (23, "night"),  # boundary: night reclaims 23
    ],
)
def test_day_period_boundaries(hour: int, expected: str) -> None:
    assert day_period(_at(hour)) == expected


def test_local_now_uses_user_timezone() -> None:
    """The salute must follow the *user's* clock, not the server's —
    legacy used the server hour, so a user abroad got "good morning" at
    midnight."""
    tokyo = local_now("Asia/Tokyo")
    utc = local_now(None)
    # Same instant, different wall clock (Tokyo is UTC+9, no DST).
    assert (tokyo.utcoffset() or timedelta(0)) == timedelta(hours=9)
    assert abs((tokyo - utc).total_seconds()) < 5


@pytest.mark.parametrize("tz", [None, "", "Not/AZone", "!!!"])
def test_local_now_falls_back_to_utc(tz: str | None) -> None:
    """An unset or corrupt ``/timezone`` must degrade to UTC, never
    raise — this runs on every ``/start``."""
    assert local_now(tz).utcoffset() == timedelta(0)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("hour", [8, 14, 20, 2])
def test_greeting_returns_icon_and_translated_salute(lang: str, hour: int) -> None:
    icon, salute = greeting(lang, _at(hour))
    assert icon  # every bucket has a glyph
    # A missing i18n key renders as the bare key name.
    assert not salute.startswith("h_start_greet_")


# --- role glyph -------------------------------------------------------------


def test_role_icon_developer_wins_over_owner() -> None:
    """Legacy precedence: a developer who also owns groups is still 💻."""
    assert role_icon(is_developer=True, owns_groups=True) == ROLE_DEVELOPER
    assert role_icon(is_developer=True, owns_groups=False) == ROLE_DEVELOPER


def test_role_icon_owner_and_member() -> None:
    assert role_icon(is_developer=False, owns_groups=True) == ROLE_OWNER
    assert role_icon(is_developer=False, owns_groups=False) == ROLE_MEMBER


# --- display name -----------------------------------------------------------


def _tg(first_name: str | None = None, username: str | None = None) -> TgUser:
    return TgUser(id=1, is_bot=False, first_name=first_name or "", username=username)


def test_display_name_prefers_first_name() -> None:
    assert display_name(_tg("Alice", "alice_x"), "ru") == "Alice"


def test_display_name_falls_back_to_username() -> None:
    assert display_name(_tg(None, "alice_x"), "ru") == "alice_x"


@pytest.mark.parametrize(("lang", "expected"), [("ru", "друг"), ("en", "friend")])
def test_display_name_anonymous_fallback_is_localised(lang: str, expected: str) -> None:
    assert display_name(_tg(None, None), lang) == expected


def test_display_name_unknown_lang_uses_ru_default() -> None:
    assert display_name(_tg(None, None), "de") == "друг"


def test_display_name_escapes_html() -> None:
    """SEC: cards go out with ``parse_mode=HTML``; a name carrying markup
    must arrive inert."""
    out = display_name(_tg("<b>x</b><a href='http://e.vil'>y</a>"), "ru")
    assert "<b>" not in out
    assert "<a href" not in out
    assert "&lt;b&gt;x&lt;/b&gt;" in out


# --- renderers --------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_render_welcome_new_carries_name_and_bonus(lang: str) -> None:
    out = render_welcome_new(lang, name="Alice", bonus=100)
    assert "Alice" in out
    assert "100" in out
    assert "{" not in out  # every placeholder substituted
    assert "<b>" in out  # emoji-led bold headers survived


@pytest.mark.parametrize("lang", LANGS)
def test_render_welcome_back_shape(lang: str) -> None:
    out = render_welcome_back(
        lang,
        name="Alice",
        role=ROLE_MEMBER,
        now=_at(9),
        balance=1234,
        games_played=7,
        streak=0,
        cooldown=timedelta(0),
    )
    assert "Alice" in out
    assert ROLE_MEMBER in out
    assert "{" not in out
    # Thousands separator comes from ``format_number``, not raw str().
    assert "1234" not in out


def test_render_welcome_back_streak_line_only_when_positive() -> None:
    kwargs = {
        "name": "Alice",
        "role": ROLE_MEMBER,
        "now": _at(9),
        "balance": 10,
        "games_played": 0,
        "cooldown": timedelta(0),
    }
    without = render_welcome_back("ru", streak=0, **kwargs)  # type: ignore[arg-type]
    with_streak = render_welcome_back("ru", streak=4, **kwargs)  # type: ignore[arg-type]
    assert "🔥" not in without  # an empty "0 days" row is noise
    assert "🔥" in with_streak
    assert with_streak.count("\n") == without.count("\n") + 1


@pytest.mark.parametrize("lang", LANGS)
def test_render_welcome_back_daily_branches(lang: str) -> None:
    kwargs = {
        "name": "Alice",
        "role": ROLE_MEMBER,
        "now": _at(9),
        "balance": 10,
        "games_played": 0,
        "streak": 0,
    }
    ready = render_welcome_back(lang, cooldown=timedelta(0), **kwargs)  # type: ignore[arg-type]
    waiting = render_welcome_back(  # type: ignore[arg-type]
        lang, cooldown=timedelta(hours=3, minutes=5), **kwargs
    )
    assert ready != waiting
    assert "/daily" in ready  # ready state names the command to run
    assert "3" in waiting  # concrete wait, not a boolean "tomorrow"
    assert "{" not in waiting


def test_render_welcome_back_negative_cooldown_reads_as_ready() -> None:
    """``daily_cooldown_remaining`` can go negative once the window has
    passed; that must land in the ready branch, not print "-2h"."""
    out = render_welcome_back(
        "ru",
        name="Alice",
        role=ROLE_MEMBER,
        now=_at(9),
        balance=10,
        games_played=0,
        streak=0,
        cooldown=timedelta(hours=-2),
    )
    assert "/daily" in out
    assert "-" not in out


@pytest.mark.parametrize("lang", LANGS)
def test_render_group_welcome_shape(lang: str) -> None:
    out = render_group_welcome(lang, name="Alice")
    assert "Alice" in out
    assert "{" not in out
    assert "/help" in out  # the "full list" pointer is the card's payoff


def test_group_welcome_only_advertises_live_commands() -> None:
    """Every slash command the card names must exist in the new pipeline
    — legacy aliases like ``/kom_balance`` would be a dead advert."""
    out = render_group_welcome("ru", name="A")
    for cmd in ("/ai", "/games", "/balance", "/shop", "/help"):
        assert cmd in out
    assert "/kom_" not in out
