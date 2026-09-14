"""Unit tests for the pure parsing/normalisation helpers in handlers.modcfg."""

from __future__ import annotations

import inspect

import pytest

from telegram_invite_bot.handlers.modcfg import (
    _normalise_key,
    _parse_bool,
)
from telegram_invite_bot.utils.render import on_off_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("automod", "automod_enabled"),
        ("AutoMod", "automod_enabled"),
        ("автомод", "automod_enabled"),
        ("profanity", "profanity_enabled"),
        ("мат", "profanity_enabled"),
        ("warns", "max_warns"),
        ("mute", "mute_minutes"),
        ("мут", "mute_minutes"),
        ("autoban", "autoban_enabled"),
        ("автобан", "autoban_enabled"),
        ("coins", "coins_enabled"),
        ("Coins", "coins_enabled"),
        ("монеты", "coins_enabled"),
        ("nonsense", None),
        ("", None),
    ],
)
def test_normalise_key(raw: str, expected: str | None) -> None:
    assert _normalise_key(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("on", True),
        ("ON", True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("вкл", True),
        ("да", True),
        ("off", False),
        ("0", False),
        ("false", False),
        ("нет", False),
        ("выкл", False),
        ("maybe", None),
        ("", None),
    ],
)
def test_parse_bool(raw: str, expected: bool | None) -> None:
    assert _parse_bool(raw) == expected


def test_on_off_localised() -> None:
    assert on_off_text(True, "ru") == "вкл"
    assert on_off_text(False, "ru") == "выкл"
    assert on_off_text(True, "en") == "on"
    assert on_off_text(False, "en") == "off"


def test_warn_threshold_ceiling_fits_the_warnings_page() -> None:
    """``/warnings`` prints ``count = len(rows)`` over a LIMITed query.

    ``ModerationRepo.list_warnings`` caps at 20 rows, so that count is a
    page size pretending to be a total. Today it is honest only because
    ``/warn`` refuses past ``max_warns`` and ``max_warns`` itself is
    capped at 20 — the ceilings coincide by luck, not by construction,
    and nothing in either module mentions the other.

    Raising the ``/modcfg warns`` ceiling to, say, 50 would therefore not
    look like a display bug while writing it, but a group with 25 active
    warnings would show "25 warnings" as "20" — under-reporting a
    moderation figure admins act on. This asserts the coupling instead of
    leaving it implicit.
    """
    from telegram_invite_bot.handlers.modcfg import _MAX_WARNS_MAX
    from telegram_invite_bot.repositories.moderation_repo import ModerationRepo

    page_size = inspect.signature(ModerationRepo.list_warnings).parameters["limit"].default
    assert page_size >= _MAX_WARNS_MAX, (
        f"/warnings pages at {page_size} rows but /modcfg allows up to "
        f"{_MAX_WARNS_MAX} warnings — the printed count would understate. "
        "Raise the limit, or render an explicit 'showing N of M'."
    )
