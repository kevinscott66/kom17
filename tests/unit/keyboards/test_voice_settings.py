"""Unit tests for the voice-settings inline keyboards (L-71/L-72).

Asserts the CallbackData wire format (distinct ``vset_*`` prefixes that
cannot collide with each other or with the legacy ``voice_*`` literals)
and the markup shape of each menu/submenu/stats keyboard.

Most assertions are on the callback payloads and button counts, because
that is the part a rename silently breaks. Labels are checked where their
wording carries the meaning (on/off), plus one sweep proving no button
ships a bare ``h_vset_*`` key — ``t()`` echoes a key it cannot resolve,
so a missing translation would otherwise reach the user as a button.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.keyboards.builders.voice_settings import (
    VoiceSettingsBack,
    VoiceSettingsOpenLanguage,
    VoiceSettingsOpenTarget,
    VoiceSettingsPickLanguage,
    VoiceSettingsPickTarget,
    VoiceSettingsStats,
    VoiceSettingsToggle,
    VoiceSettingsToggleAutoDelete,
    VoiceSettingsToggleOnlyAdmins,
    build_language_markup,
    build_menu_markup,
    build_stats_markup,
    build_target_markup,
    is_known_language,
    is_known_target,
)


def _all_callback_data(markup: object) -> list[str]:
    """Flatten every button's ``callback_data`` string in a markup."""
    return [
        btn.callback_data
        for row in markup.inline_keyboard  # type: ignore[attr-defined]
        for btn in row
        if btn.callback_data is not None
    ]


def test_callback_prefixes_are_distinct() -> None:
    # Each class packs to its own unique prefix — no two share a leading
    # token, so aiogram's prefix router can never mis-dispatch.
    classes: list[type[CallbackData]] = [
        VoiceSettingsToggle,
        VoiceSettingsToggleAutoDelete,
        VoiceSettingsToggleOnlyAdmins,
        VoiceSettingsOpenTarget,
        VoiceSettingsPickTarget,
        VoiceSettingsOpenLanguage,
        VoiceSettingsPickLanguage,
        VoiceSettingsStats,
        VoiceSettingsBack,
    ]
    prefixes = [c.__prefix__ for c in classes]  # type: ignore[attr-defined]
    assert len(prefixes) == len(set(prefixes))
    # All carry the strangler-safe ``vset`` namespace, never the legacy
    # ``voice_`` literals owned by the live telebot process.
    assert all(p.startswith("vset") for p in prefixes)
    assert not any(p.startswith("voice_") for p in prefixes)


def test_pick_and_open_target_do_not_collide() -> None:
    # ``vset_tgt_open`` (open submenu) must not be matched by the
    # ``vset_tgt`` (pick) filter and vice-versa.
    assert VoiceSettingsPickTarget(target="chat").pack() == "vset_tgt:chat"
    assert VoiceSettingsOpenTarget().pack() == "vset_tgt_open"


def _menu(**overrides: object) -> object:
    """``build_menu_markup`` with everything off unless overridden."""
    kwargs: dict[str, object] = {
        "enabled": False,
        "auto_delete": False,
        "only_admins": False,
        "lang": "ru",
    }
    kwargs.update(overrides)
    return build_menu_markup(**kwargs)  # type: ignore[arg-type]


def test_menu_markup_toggle_label_reflects_state() -> None:
    # Off → "enable" label; on → "disable" label (rendered RU).
    off = _menu(enabled=False)
    on = _menu(enabled=True)
    off_labels = {b.text for row in off.inline_keyboard for b in row}  # type: ignore[attr-defined]
    on_labels = {b.text for row in on.inline_keyboard for b in row}  # type: ignore[attr-defined]
    assert "✅ Включить" in off_labels
    assert "❌ Выключить" in on_labels


def test_menu_markup_has_all_actions() -> None:
    data = _all_callback_data(_menu())
    assert VoiceSettingsToggle().pack() in data
    assert VoiceSettingsOpenTarget().pack() in data
    assert VoiceSettingsOpenLanguage().pack() in data
    assert VoiceSettingsToggleAutoDelete().pack() in data
    assert VoiceSettingsToggleOnlyAdmins().pack() in data
    assert VoiceSettingsStats().pack() in data


def test_switch_buttons_carry_their_own_state() -> None:
    # RR-6 #73: legacy labelled these with bare nouns, so the only way to
    # read a switch was to cross-reference the card text above. Each button
    # now shows its own ✅/❌ — assert both states, since a label that never
    # changes would still satisfy a one-sided check.
    def label(markup: object, needle: str) -> str:
        return next(
            b.text
            for row in markup.inline_keyboard  # type: ignore[attr-defined]
            for b in row
            if needle in b.text
        )

    off = _menu(auto_delete=False, only_admins=False)
    on = _menu(auto_delete=True, only_admins=True)
    assert label(off, "Удалять голосовое").startswith("❌")
    assert label(on, "Удалять голосовое").startswith("✅")
    assert label(off, "Только для админов").startswith("❌")
    assert label(on, "Только для админов").startswith("✅")


def test_menu_markup_omits_dead_whisper_knobs() -> None:
    # Model/device configured the monolith's local faster-whisper install;
    # this pipeline calls OpenAI whisper-1, where they mean nothing. A
    # button for them would be a lever wired to nothing.
    labels = " ".join(
        b.text
        for row in _menu().inline_keyboard  # type: ignore[attr-defined]
        for b in row
    ).lower()
    assert "whisper" not in labels
    assert "модель" not in labels


def test_target_markup_marks_current_and_lists_all() -> None:
    markup = build_target_markup(current="private", lang="ru")
    data = _all_callback_data(markup)
    for token in ("chat", "private", "admins", "log_chat"):
        assert VoiceSettingsPickTarget(target=token).pack() in data
    assert VoiceSettingsBack().pack() in data
    # Exactly one button is check-marked (the current target).
    checked = [b.text for row in markup.inline_keyboard for b in row if b.text.startswith("✅")]
    assert len(checked) == 1


def test_language_markup_marks_current_and_lists_all() -> None:
    markup = build_language_markup(current="en", lang="ru")
    data = _all_callback_data(markup)
    for code in ("ru", "en"):
        assert VoiceSettingsPickLanguage(language=code).pack() in data
    assert VoiceSettingsBack().pack() in data
    checked = [b.text for row in markup.inline_keyboard for b in row if b.text.startswith("✅")]
    assert len(checked) == 1


def test_stats_markup_is_back_only() -> None:
    markup = build_stats_markup(lang="ru")
    data = _all_callback_data(markup)
    assert data == [VoiceSettingsBack().pack()]


def test_no_button_ships_a_raw_i18n_key() -> None:
    """``t()`` echoes an unresolved key, so a gap renders as a button."""
    markups = [
        _menu(enabled=True),
        build_target_markup(current="private", lang="ru"),
        build_language_markup(current="ru", lang="ru"),
        build_stats_markup(lang="ru"),
    ]
    labels = [
        b.text
        for m in markups
        for row in m.inline_keyboard  # type: ignore[attr-defined]
        for b in row
    ]
    assert labels
    assert not [label for label in labels if "h_vset_" in label]


def test_known_token_helpers() -> None:
    assert is_known_target("chat")
    assert is_known_target("log_chat")
    assert not is_known_target("bogus")
    assert is_known_language("ru")
    assert is_known_language("en")
    assert not is_known_language("de")
