"""Unit tests for the ``h_plural_*`` form picker (#207).

Two contracts, and they fail differently. The *rule* is pure arithmetic
and is pinned exhaustively over the ranges where Russian changes its
mind (1, 2-4, 5-20, and again at every hundred). The *catalogue* half is
the one that rots: a translator adding a language, or a merge dropping a
form, breaks the picker silently because :func:`plural` clamps rather
than raises. So the arity of every key is asserted here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from telegram_invite_bot.utils.plural import plural

_DATA = Path(__file__).resolve().parents[3] / "src/telegram_invite_bot/i18n/data"

_FAMILIES = ("games", "wins", "streak", "chars", "coins")


def _catalogue(lang: str) -> dict[str, str]:
    raw = yaml.safe_load((_DATA / f"{lang}.yaml").read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in raw.items()}


@pytest.mark.parametrize("family", _FAMILIES)
def test_russian_entry_carries_exactly_three_forms(family: str) -> None:
    value = _catalogue("ru")[f"h_plural_{family}"]
    assert len(value.split("|")) == 3, value


@pytest.mark.parametrize("family", _FAMILIES)
def test_english_entry_carries_exactly_two_forms(family: str) -> None:
    value = _catalogue("en")[f"h_plural_{family}"]
    assert len(value.split("|")) == 2, value


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "игр"),
        (1, "игра"),
        (2, "игры"),
        (4, "игры"),
        (5, "игр"),
        (10, "игр"),
        # The teens are the trap: they end in 1-4 but take ``many``.
        (11, "игр"),
        (12, "игр"),
        (14, "игр"),
        (15, "игр"),
        (20, "игр"),
        (21, "игра"),
        (22, "игры"),
        (25, "игр"),
        (100, "игр"),
        (101, "игра"),
        (102, "игры"),
        (111, "игр"),
        (112, "игр"),
        (121, "игра"),
        (1000, "игр"),
        (1001, "игра"),
    ],
)
def test_russian_rule(count: int, expected: str) -> None:
    assert plural(count, "h_plural_games", "ru") == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "games"), (1, "game"), (2, "games"), (11, "games"), (21, "games")],
)
def test_english_rule(count: int, expected: str) -> None:
    assert plural(count, "h_plural_games", "en") == expected


def test_sign_is_ignored() -> None:
    """A negative profit still declines like its magnitude."""
    assert plural(-1, "h_plural_coins", "ru") == "монета"
    assert plural(-2, "h_plural_coins", "ru") == "монеты"
    assert plural(-5, "h_plural_coins", "ru") == "монет"


def test_missing_key_degrades_to_the_key_name() -> None:
    """Same contract as ``t()``: visible in the bubble, never a crash."""
    assert plural(1, "h_plural_no_such_family", "ru") == "h_plural_no_such_family"


def test_unknown_language_follows_t_and_not_a_second_normalisation() -> None:
    """``lang="fr"`` falls back to RU inside ``t()``; the rule must agree."""
    assert plural(2, "h_plural_games", "fr") == "игры"
