"""Unit tests for :mod:`telegram_invite_bot.core.ai_modes` (Cluster J L-62)."""

from __future__ import annotations

import pytest

from telegram_invite_bot.core import ai_modes
from telegram_invite_bot.core.ai_modes import (
    DEFAULT_MODE,
    MODE_HINTS,
    MODE_HINTS_EN,
    MODE_TITLES,
    SYSTEM_PROMPTS,
    SYSTEM_PROMPTS_EN,
    AiModeStore,
    is_valid_mode,
    mode_hint,
    resolve_mode_token,
    system_prompt_for,
)


def test_seven_modes_present() -> None:
    assert set(SYSTEM_PROMPTS) == {
        "default",
        "chat",
        "party",
        "help",
        "creative",
        "code",
        "expert",
    }
    # Every mode has a user-facing title.
    assert set(MODE_TITLES) == set(SYSTEM_PROMPTS)


def test_is_valid_mode() -> None:
    assert is_valid_mode("expert")
    assert not is_valid_mode("nope")


def test_mode_hint_defaults() -> None:
    assert mode_hint("party") == "тусовка"
    assert mode_hint("unknown") == "обычный"


def test_store_defaults_to_default() -> None:
    store = AiModeStore()
    assert store.get(1) == DEFAULT_MODE


def test_store_set_and_get() -> None:
    store = AiModeStore()
    assert store.set(1, "expert") is True
    assert store.get(1) == "expert"


def test_store_rejects_unknown_mode() -> None:
    store = AiModeStore()
    assert store.set(1, "bogus") is False
    assert store.get(1) == DEFAULT_MODE


def test_resolve_pins_non_vip_to_default() -> None:
    store = AiModeStore()
    store.set(7, "party")
    # Non-VIP collapses to default regardless of stored choice (legacy).
    assert store.resolve(7, is_vip=False) == "default"
    # VIP keeps the selection.
    assert store.resolve(7, is_vip=True) == "party"


def test_system_prompt_follows_resolution() -> None:
    store = AiModeStore()
    store.set(9, "expert")
    assert store.system_prompt(9, is_vip=True) == SYSTEM_PROMPTS["expert"]
    assert store.system_prompt(9, is_vip=False) == SYSTEM_PROMPTS["default"]


# --- Issue 4: /mode accepts localized display names AND English keys ---


def test_resolve_mode_token_localized_expert() -> None:
    # The live bug: "/mode эксперт" answered "Unknown style" because only
    # the English key "expert" was accepted. The Russian label shown in
    # the /mode list must now resolve to the expert mode.
    assert resolve_mode_token("эксперт") == "expert"


@pytest.mark.parametrize(
    ("token", "canonical"),
    [
        # English canonical keys still resolve.
        ("expert", "expert"),
        ("EXPERT", "expert"),
        ("creative", "creative"),
        # Russian display names from MODE_TITLES (emoji/case-insensitive).
        ("эксперт", "expert"),
        ("Эксперт", "expert"),
        ("📚 Эксперт", "expert"),
        ("креативный", "creative"),
        ("тусовка", "party"),
        ("обычный", "default"),
        ("дружеский", "chat"),
        ("помощник", "help"),
        ("код", "code"),
    ],
)
def test_resolve_mode_token_aliases(token: str, canonical: str) -> None:
    assert resolve_mode_token(token) == canonical


def test_resolve_mode_token_unknown_returns_none() -> None:
    assert resolve_mode_token("нетакого") is None
    assert resolve_mode_token("") is None
    # Resolved tokens are always real modes.
    for canonical in (resolve_mode_token(label) for label in MODE_TITLES.values()):
        assert canonical is None or is_valid_mode(canonical)


# ── The mode map stays bounded ───────────────────────────────────────────────


def test_mode_store_is_lru_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The map used to be an unbounded ``dict``.

    One entry per user who ever switched mode, never removed, in a
    process that runs for months — the same monotone shape as the
    middleware caches that were capped earlier. Eviction is safe here
    because a lost selection reads back as ``default``, which is exactly
    what a restart (and the non-VIP gate) already produces.
    """
    monkeypatch.setattr(ai_modes, "_MAX_TRACKED_USERS", 3)
    store = AiModeStore()

    for uid in range(1, 6):
        assert store.set(uid, "expert")

    assert store.get(1) == DEFAULT_MODE  # evicted
    assert store.get(2) == DEFAULT_MODE  # evicted
    assert store.get(5) == "expert"


def test_mode_store_reads_count_as_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user who keeps *using* their mode must outlive a one-off switcher.

    ``resolve`` (hence ``get``) runs on every AI reply while ``set`` runs
    once per switch, so eviction keyed on writes alone would drop the
    active user and keep the dormant one — backwards.
    """
    monkeypatch.setattr(ai_modes, "_MAX_TRACKED_USERS", 2)
    store = AiModeStore()
    store.set(1, "expert")
    store.set(2, "code")

    # User 1 keeps talking to Kom; user 2 went quiet.
    assert store.get(1) == "expert"

    store.set(3, "party")

    assert store.get(1) == "expert"  # active — survived
    assert store.get(2) == DEFAULT_MODE  # dormant — evicted


def test_mode_store_rejected_mode_does_not_grow_the_map() -> None:
    """An unknown mode is refused *before* the write.

    Otherwise ``/ai нетакого`` would be a free way to occupy a slot in a
    capped table — a cheap eviction lever against real users.
    """
    store = AiModeStore()
    assert store.set(1, "нетакого") is False
    assert store.get(1) == DEFAULT_MODE
    assert len(store._modes) == 0


def test_every_mode_hint_is_already_an_alias() -> None:
    """#1074: the ``MODE_HINTS`` fallback in ``resolve_mode_token`` is dead.

    Every hint word the bot advertises must round-trip back to its own
    mode, and it must do so through the alias table — the fallback loop
    below it is a backstop, not a path. The comment that used to justify
    that loop argued from a typo, comparing "дружеский" to "дружеский"
    and calling them different; this pins the truth instead of restating
    it in prose.

    If a hint is ever added without a matching alias, this test names it
    before the loop can quietly paper over it.
    """
    for mode, hint in MODE_HINTS.items():
        assert resolve_mode_token(hint) == mode, (mode, hint)


def test_mode_hint_round_trips_through_resolve() -> None:
    """The public pair ``mode_hint`` / ``resolve_mode_token`` is closed.

    ``mode_hint`` is what the prompt shows the user; whatever it shows,
    the user must be able to type back (#1074).
    """
    for mode in MODE_TITLES:
        hint = mode_hint(mode)
        if hint:
            assert resolve_mode_token(hint) == mode, (mode, hint)


def test_en_prompts_cover_every_mode() -> None:
    """#1345: the two persona tables must stay keyed alike.

    ``system_prompt_for`` falls back to ``DEFAULT_MODE`` inside whichever
    table it picked, so a mode present in one table and missing from the
    other degrades silently to the wrong persona instead of raising. This
    is the guard the source comment points at by name.
    """
    assert set(SYSTEM_PROMPTS_EN) == set(SYSTEM_PROMPTS)


def test_en_prompts_have_no_cyrillic() -> None:
    """An English reader must not be briefed in Russian (#1345)."""
    for mode, prompt in SYSTEM_PROMPTS_EN.items():
        assert not any("\u0400" <= ch <= "\u04ff" for ch in prompt), (mode, prompt)


def test_system_prompt_for_picks_the_language() -> None:
    assert system_prompt_for("expert", "ru") == SYSTEM_PROMPTS["expert"]
    assert system_prompt_for("expert", "en") == SYSTEM_PROMPTS_EN["expert"]


def test_system_prompt_for_unknown_mode_falls_back_in_language() -> None:
    """A stale stored key must degrade to the plain persona, not raise.

    The fallback stays inside the language the caller asked for: falling
    back across tables would hand an English reader a Russian preamble,
    which is the exact defect #1345 removes.
    """
    assert system_prompt_for("nope", "ru") == SYSTEM_PROMPTS[DEFAULT_MODE]
    assert system_prompt_for("nope", "en") == SYSTEM_PROMPTS_EN[DEFAULT_MODE]


def test_unknown_language_is_treated_as_english() -> None:
    """Only ``ru`` selects the Russian table; everything else is English.

    ``lang`` reaches here from ``LanguageMiddleware``, which only ever
    yields ``ru`` or ``en`` today. Pinning the else-branch keeps a future
    third locale degrading to English rather than to Russian.
    """
    assert system_prompt_for("chat", "de") == SYSTEM_PROMPTS_EN["chat"]
    assert mode_hint("party", "de") == MODE_HINTS_EN["party"]


def test_en_mode_hints_round_trip_through_resolve() -> None:
    """The #1074 contract, closed in English too.

    Whatever ``mode_hint`` shows an English user, that user must be able
    to type back after ``/mode``.
    """
    for mode, hint in MODE_HINTS_EN.items():
        assert resolve_mode_token(hint) == mode, (mode, hint)


def test_en_mode_hint_fallback_is_english() -> None:
    assert mode_hint("party", "en") == "party"
    assert mode_hint("unknown", "en") == "default"
    # The Russian default is untouched by #1345.
    assert mode_hint("unknown") == "обычный"


def test_store_system_prompt_honours_lang() -> None:
    store = AiModeStore()
    store.set(7, "creative")
    # ``is_vip=True`` because the VIP gate in ``resolve`` collapses a
    # non-VIP selection back to ``DEFAULT_MODE`` before the language is
    # ever consulted, which would hide the thing under test.
    assert store.system_prompt(7, is_vip=True, lang="en") == SYSTEM_PROMPTS_EN["creative"]
    assert store.system_prompt(7, is_vip=True) == SYSTEM_PROMPTS["creative"]
    # A non-VIP user still gets the default persona — in their language.
    assert store.system_prompt(7, is_vip=False, lang="en") == SYSTEM_PROMPTS_EN[DEFAULT_MODE]
