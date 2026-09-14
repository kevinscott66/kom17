"""Unit tests for the Kom control keyboards — RR-6 #64/#65.

Three properties this file is here to pin:

* **Wire format.** Every ``CallbackData`` class packs to its own ``aik_*``
  prefix, disjoint from the legacy ``ai_*`` literals still owned by the
  live telebot process. A collision there would let one process swallow
  the other's taps.
* **Gating.** The persona row exists only for users who may switch;
  the VIP upsell exists only for users who aren't VIP already.
* **Localisation.** Labels come from yaml in both locales — the whole
  reason ``mode_title`` exists is that ``core.ai_modes.MODE_TITLES`` is
  a Russian-only legacy port.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.ai_modes import DEFAULT_MODE, SYSTEM_PROMPTS
from telegram_invite_bot.keyboards.builders.ai_controls import (
    MODE_ORDER,
    AiContextClear,
    AiExport,
    AiKomEnter,
    AiKomExit,
    AiModePick,
    build_controls_markup,
    build_quota_limit_markup,
    is_known_mode,
    mode_title,
)

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup


def _payloads(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_callback_prefixes_are_distinct_and_strangler_safe() -> None:
    classes: list[type[CallbackData]] = [
        AiKomEnter,
        AiKomExit,
        AiContextClear,
        AiExport,
        AiModePick,
    ]
    prefixes = [c.__prefix__ for c in classes]  # type: ignore[attr-defined]
    assert len(prefixes) == len(set(prefixes))
    # ``aik_`` namespace only: the legacy process owns ``ai_enter`` /
    # ``ai_exit`` / ``ai_reset`` / ``ai_export`` / ``ai_mode_*``, so a
    # bare ``ai_`` prefix here would be routable by both bots.
    assert all(p.startswith("aik_") for p in prefixes), prefixes


def test_mode_pick_packs_the_mode_token() -> None:
    assert AiModePick(mode="expert").pack() == "aik_mode:expert"


def test_mode_order_covers_every_persona() -> None:
    # The button list and the value layer must not drift: a persona with
    # no button is unreachable, a button with no prompt is a dead tap.
    assert set(MODE_ORDER) == set(SYSTEM_PROMPTS)
    assert MODE_ORDER[0] == DEFAULT_MODE


def test_is_known_mode_accepts_personas_and_rejects_forgeries() -> None:
    assert is_known_mode("expert")
    assert not is_known_mode("root")
    assert not is_known_mode("")


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_mode_titles_resolve_in_both_locales(lang: str) -> None:
    for mode in MODE_ORDER:
        label = mode_title(mode, lang)
        # ``t`` echoes the key when the translation is missing; the
        # fallback then hands back the bare mode token. Either means a
        # hole in yaml.
        assert label != f"h_ai_mode_title_{mode}"
        assert label != mode


def test_english_mode_titles_carry_no_cyrillic() -> None:
    # The bug this whole helper exists for: ``MODE_TITLES`` is RU-only,
    # so an English user's card used to answer in Cyrillic.
    for mode in MODE_ORDER:
        label = mode_title(mode, "en")
        assert not any("Ѐ" <= ch <= "ӿ" for ch in label), (mode, label)


def test_controls_hide_the_persona_row_for_non_vip() -> None:
    markup = build_controls_markup(lang="ru", session_active=False, can_change_mode=False)
    payloads = _payloads(markup)
    assert not any(p.startswith("aik_mode:") for p in payloads), payloads
    # The free controls stay: the gate is on switching persona, not on
    # entering the chat, clearing it or taking the transcript home.
    assert "aik_in" in payloads
    assert "aik_clr" in payloads
    assert "aik_exp" in payloads


def test_controls_show_every_persona_for_vip() -> None:
    markup = build_controls_markup(lang="ru", session_active=False, can_change_mode=True)
    payloads = _payloads(markup)
    assert [p for p in payloads if p.startswith("aik_mode:")] == [
        f"aik_mode:{mode}" for mode in MODE_ORDER
    ]


def test_current_persona_is_check_marked_and_only_it() -> None:
    markup = build_controls_markup(
        lang="ru", session_active=False, can_change_mode=True, current_mode="expert"
    )
    checked = [label for label in _labels(markup) if label.startswith("✅ ")]
    assert checked == [f"✅ {mode_title('expert', 'ru')}"]


def test_toggle_label_points_at_the_next_action() -> None:
    idle = build_controls_markup(lang="ru", session_active=False, can_change_mode=False)
    live = build_controls_markup(lang="ru", session_active=True, can_change_mode=False)
    assert "aik_in" in _payloads(idle)
    assert "aik_out" not in _payloads(idle)
    assert "aik_out" in _payloads(live)
    assert "aik_in" not in _payloads(live)


def test_tail_controls_are_one_per_row() -> None:
    # A mis-tap between "clear my context" and a persona switch is the
    # kind of thing users report as data loss, so the destructive-ish
    # controls never share a row.
    markup = build_controls_markup(lang="ru", session_active=False, can_change_mode=True)
    tail = markup.inline_keyboard[-3:]
    assert [len(row) for row in tail] == [1, 1, 1]
    # …and the persona rows above pack two-up, as legacy did.
    persona_rows = markup.inline_keyboard[:-3]
    assert [len(row) for row in persona_rows] == [2, 2, 2, 1]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_no_control_label_is_a_raw_translation_key(lang: str) -> None:
    markup = build_controls_markup(lang=lang, session_active=True, can_change_mode=True)
    assert not any(label.startswith("h_") for label in _labels(markup))


def test_quota_keyboard_offers_vip_and_the_way_back() -> None:
    markup = build_quota_limit_markup("ru", is_vip=False)
    payloads = _payloads(markup)
    assert payloads == ["menu:shop", "menu:home"]


def test_quota_keyboard_drops_the_upsell_for_a_vip() -> None:
    # A VIP who burned the VIP ceiling must not be sold VIP. Legacy showed
    # the button unconditionally because it only ever refused non-VIPs.
    markup = build_quota_limit_markup("ru", is_vip=True)
    assert _payloads(markup) == ["menu:home"]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_quota_keyboard_labels_resolve(lang: str) -> None:
    markup = build_quota_limit_markup(lang, is_vip=False)
    assert not any(label.startswith("h_") for label in _labels(markup))
