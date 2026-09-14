"""Unit tests for the pure couple-activities catalog + gating logic."""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.couple_activities import (
    HISTORY_ONLY_ICONS,
    MARRIAGE_ACTIVITIES,
    MARRIAGE_BY_KEY,
    RELATIONSHIP_ACTIVITIES,
    RELATIONSHIP_BY_KEY,
    MarriageActivity,
    RelationshipActivity,
    effect_hours_split,
    effect_template_key,
    relationship_available,
)
from telegram_invite_bot.i18n import t

# Every catalog row, both kinds. Annotated because the two frozen
# dataclasses are unrelated types — mypy widens a bare splat to
# ``object`` and then every ``.key`` access is an error.
_EVERY_ACTIVITY: tuple[MarriageActivity | RelationshipActivity, ...] = (
    *MARRIAGE_ACTIVITIES,
    *RELATIONSHIP_ACTIVITIES,
)


def test_catalog_ported_verbatim() -> None:
    """A spot-check that the ported numbers match the legacy spec."""
    assert MARRIAGE_BY_KEY["dinner"].cost == 100
    assert MARRIAGE_BY_KEY["dinner"].xp == 15
    assert MARRIAGE_BY_KEY["trip_m"].cost == 2000
    assert MARRIAGE_BY_KEY["trip_m"].xp == 250

    big = RELATIONSHIP_BY_KEY["big_gift"]
    assert (big.cost, big.xp, big.min_level, big.effect_hours) == (1350, 3000, 7, 90)
    comp = RELATIONSHIP_BY_KEY["compliment"]
    assert (comp.cost, comp.xp, comp.min_level, comp.effect_hours) == (3, 5, 0, 4)
    assert len(MARRIAGE_ACTIVITIES) == 6
    assert len(RELATIONSHIP_ACTIVITIES) == 15


def test_lookup_maps_cover_every_row() -> None:
    assert set(MARRIAGE_BY_KEY) == {a.key for a in MARRIAGE_ACTIVITIES}
    assert set(RELATIONSHIP_BY_KEY) == {a.key for a in RELATIONSHIP_ACTIVITIES}


def test_every_row_carries_a_non_ascii_icon() -> None:
    """``icon`` is language-neutral DATA — it must exist and be an emoji.

    An empty or ASCII ``icon`` would render as a stray space (or a bare
    letter) in the catalog rows and buttons, which is exactly the mushy
    look RR-5 #49 set out to fix.
    """
    for act in _EVERY_ACTIVITY:
        assert act.icon, act.key
        assert not act.icon.isascii(), act.key


def test_history_only_icons_do_not_shadow_the_catalogs() -> None:
    """#232: three tables answer "what is this key", in that order.

    An overlap would make the answer depend on lookup order rather than
    on the data — and the catalog row, which carries the price and the
    XP, is the one that must win. Keeping the sets disjoint means the
    order in ``_activity_icon`` is a detail, not a rule.
    """
    catalog = set(MARRIAGE_BY_KEY) | set(RELATIONSHIP_BY_KEY)
    assert not catalog & set(HISTORY_ONLY_ICONS)
    # 26 RP actions + 7 retired catalog keys, ported verbatim.
    assert len(HISTORY_ONLY_ICONS) == 33
    for key, icon in HISTORY_ONLY_ICONS.items():
        assert icon, key
        assert not icon.isascii(), key


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_history_only_key_has_a_name(lang: str) -> None:
    """No story for these — they are never performed, only remembered."""
    for key in HISTORY_ONLY_ICONS:
        assert t(f"h_couple_act_name_{key}", lang) != f"h_couple_act_name_{key}", key


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_row_has_title_and_story_copy(lang: str) -> None:
    """Titles and stories are COPY — they live in YAML, one pair per row.

    ``t()`` echoes an unknown key back verbatim, so comparing against the
    key name is the reliable "translation missing" assertion.
    """
    for act in _EVERY_ACTIVITY:
        for prefix in ("h_couple_act_name_", "h_couple_act_done_"):
            key = f"{prefix}{act.key}"
            assert t(key, lang) != key, key


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_story_templates_carry_both_placeholders(lang: str) -> None:
    for act in _EVERY_ACTIVITY:
        template = t(f"h_couple_act_done_{act.key}", lang)
        assert "{actor}" in template, act.key
        assert "{partner}" in template, act.key


# ---------------------------------------------------------------------------
# Relationship availability: BOTH level AND coins gate.
# ---------------------------------------------------------------------------

_ACT = RelationshipActivity("x", icon="💕", cost=100, xp=10, min_level=3, effect_hours=40)


def test_available_when_level_and_coins_ok() -> None:
    assert relationship_available(_ACT, level=3, balance=100) is True
    assert relationship_available(_ACT, level=9, balance=999) is True


def test_locked_below_min_level_even_with_coins() -> None:
    assert relationship_available(_ACT, level=2, balance=10_000) is False


def test_locked_when_cant_afford_even_at_level() -> None:
    assert relationship_available(_ACT, level=5, balance=99) is False


def test_locked_when_both_fail() -> None:
    assert relationship_available(_ACT, level=0, balance=0) is False


def test_exact_boundaries_inclusive() -> None:
    # level == min_level and balance == cost both count as available.
    assert relationship_available(_ACT, level=3, balance=100) is True


# ---------------------------------------------------------------------------
# Effect-hours flavour selection (purely cosmetic).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected_split", "expected_key"),
    [
        (4, (0, 4), "rel_activity_done_effect_h"),
        (23, (0, 23), "rel_activity_done_effect_h"),
        (24, (1, 0), "rel_activity_done_effect_d"),
        (48, (2, 0), "rel_activity_done_effect_d"),
        (28, (1, 4), "rel_activity_done_effect_dh"),
        (90, (3, 18), "rel_activity_done_effect_dh"),
    ],
)
def test_effect_split_and_template(
    hours: int, expected_split: tuple[int, int], expected_key: str
) -> None:
    assert effect_hours_split(hours) == expected_split
    assert effect_template_key(hours) == expected_key


def test_every_relationship_effect_template_resolves() -> None:
    # No catalog row should fall outside the three template suffixes.
    for act in RELATIONSHIP_ACTIVITIES:
        key = effect_template_key(act.effect_hours)
        assert key in {
            "rel_activity_done_effect_d",
            "rel_activity_done_effect_dh",
            "rel_activity_done_effect_h",
        }
