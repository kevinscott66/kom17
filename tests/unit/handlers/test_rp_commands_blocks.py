"""Unit tests for the ``/rp_commands`` help blocks (RR-5 #54/#58).

The point of the restored blocks is *discoverability*: the card must
print the words a user can actually TYPE, in both alphabets, and it must
never silently drop an action from the spec.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.rp_actions import (
    RP_ACTION_SPEC,
    RP_UNIVERSAL,
    TRIGGERS_BY_NAME,
)
from telegram_invite_bot.handlers.rp import (
    _OVERFLOW_MARKER,
    _build_level_blocks,
    _build_universal_block,
    _is_cyrillic,
    _join_capped,
    _open_action_names,
    _triggers_for,
)

_LANGS = ["ru", "en"]


# ---------------------------------------------------------------------------
# Inverse index
# ---------------------------------------------------------------------------


def test_inverse_index_covers_every_trigger_exactly_once() -> None:
    flattened = [trigger for triggers in TRIGGERS_BY_NAME.values() for trigger in triggers]
    assert sorted(flattened) == sorted(RP_ACTION_SPEC)


def test_inverse_index_keeps_spec_order() -> None:
    """Russian triggers come first — the spec lists them before the EN twins."""
    assert TRIGGERS_BY_NAME["hug"] == ("обнять", "hug")
    assert TRIGGERS_BY_NAME["sex"] == ("выебать", "трахнуть", "sex")
    assert TRIGGERS_BY_NAME["highfive"] == ("дать пять", "highfive", "high five")


def test_triggers_for_unknown_name_echoes_the_name() -> None:
    assert _triggers_for("nope") == ("nope",)


# ---------------------------------------------------------------------------
# Universal block
# ---------------------------------------------------------------------------


def test_is_cyrillic_splits_the_two_alphabets() -> None:
    assert _is_cyrillic("обнять")
    assert not _is_cyrillic("hug")
    assert not _is_cyrillic("high five")


@pytest.mark.parametrize("lang", _LANGS)
def test_universal_block_lists_both_alphabets(lang: str) -> None:
    lines = _build_universal_block(lang)
    body = "\n".join(lines)
    # Header + one RU line + one EN line.
    assert len(lines) == 3
    assert "обнять" not in body  # hug is level 2 — it belongs to a level block
    for trigger in ("пожать руку", "дать пять", "handshake", "kick"):
        assert trigger in body, trigger


@pytest.mark.parametrize("lang", _LANGS)
def test_universal_block_only_shows_level_one_actions(lang: str) -> None:
    body = "\n".join(_build_universal_block(lang))
    for name in RP_UNIVERSAL:
        min_level = next(spec[0] for spec in RP_ACTION_SPEC.values() if spec[2] == name)
        shown = any(trigger in body for trigger in TRIGGERS_BY_NAME[name])
        assert shown is (min_level <= 1), name


def test_open_action_names_are_all_universal() -> None:
    assert set(_open_action_names()) <= set(RP_UNIVERSAL)


# ---------------------------------------------------------------------------
# Level blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", _LANGS)
def test_every_non_open_action_appears_in_exactly_one_level_block(lang: str) -> None:
    body = "\n".join(_build_level_blocks(lang))
    open_names = set(_open_action_names())
    for name, triggers in TRIGGERS_BY_NAME.items():
        if name in open_names:
            continue
        # Every typeable synonym is printed, joined by " · ".
        assert " · ".join(triggers) in body, name


@pytest.mark.parametrize("lang", _LANGS)
def test_level_blocks_are_ordered_and_carry_xp(lang: str) -> None:
    lines = _build_level_blocks(lang)
    headers = [ln for ln in lines if not ln.startswith("•")]
    levels = [int("".join(c for c in ln.split("—")[0] if c.isdigit())) for ln in headers]
    assert levels == sorted(levels)
    assert levels[0] >= 2  # level 1 lives in the universal block
    assert all("XP" in ln for ln in lines if ln.startswith("•"))


@pytest.mark.parametrize("lang", _LANGS)
def test_no_action_is_listed_in_both_blocks(lang: str) -> None:
    universal = "\n".join(_build_universal_block(lang))
    levels = "\n".join(_build_level_blocks(lang))
    for name in _open_action_names():
        assert all(f"• {trigger}" not in levels for trigger in TRIGGERS_BY_NAME[name])
    assert "handshake" in universal


@pytest.mark.parametrize("lang", _LANGS)
def test_the_whole_card_fits_a_single_telegram_message(lang: str) -> None:
    body = "\n".join([*_build_universal_block(lang), *_build_level_blocks(lang)])
    assert len(body) < 4096


# ---------------------------------------------------------------------------
# Overflow backstop
# ---------------------------------------------------------------------------


def test_join_capped_marks_a_truncated_list() -> None:
    assert _join_capped(["a", "b", "c"], 5) == "a, b, c"
    assert _join_capped(["a", "b", "c"], 2) == "a, b" + _OVERFLOW_MARKER


@pytest.mark.parametrize("lang", _LANGS)
def test_current_spec_never_trips_the_overflow_marker(lang: str) -> None:
    """The caps are a message-length backstop, not a normal-path clip."""
    body = "\n".join([*_build_universal_block(lang), *_build_level_blocks(lang)])
    assert _OVERFLOW_MARKER not in body
