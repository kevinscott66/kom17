"""Stage 14: YAML-backed ``t()`` replacement for legacy translations.py.

These tests pin the contract that the rest of the bot relies on:
- A known key resolves to its RU and EN templates.
- Missing-in-target falls back to the other language, not a blank.
- Missing-everywhere returns the raw key (so the gap is visible in UI).
- Placeholder substitution uses ``str.format_map`` semantics with a
  safe-default for missing kwargs (the literal ``{key}`` stays, no
  KeyError reaches the handler).
- ``_normalise`` accepts the long-form spellings legacy callsites use.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from telegram_invite_bot import i18n
from telegram_invite_bot.i18n import (
    _escape_stray_angles,
    _load,
    _md_to_html,
    _normalise,
    _SafeFormat,
    available_languages,
    t,
)


@pytest.fixture(autouse=True)
def _clear_load_cache() -> Iterator[None]:
    """The loader is ``@lru_cache``-d; clear it around every test so any
    monkeypatched data dir is re-read instead of returning a stale
    earlier-test result.

    Clearing on BOTH setup and teardown is load-bearing: the tests that
    point ``_DATA_DIR`` at a nonexistent / malformed dir cache an empty
    ``{}`` for ``ru`` / ``en``. ``monkeypatch`` restores ``_DATA_DIR``
    after the test, but the poisoned cache entry would survive into the
    next *file* (the autouse fixture is module-scoped), making every
    downstream ``t(...)`` lookup render the key literally. The teardown
    clear contains the pollution to this file.
    """
    _load.cache_clear()
    yield
    _load.cache_clear()


# ── basic lookup ─────────────────────────────────────────────────────────


def test_known_key_resolves_ru() -> None:
    """``access_denied`` is one of the keys ported from the legacy
    RU dict; treat it as a canary that the YAML file loaded.
    """
    assert "🚫" in t("access_denied", "ru")
    assert "ограничен" in t("access_denied", "ru")


def test_known_key_resolves_en() -> None:
    assert "restricted" in t("access_denied", "en")


def test_default_lang_is_russian() -> None:
    """Legacy default was RU and the bot's user base is overwhelmingly
    Russian-speaking; passing ``None`` must keep that behaviour or
    every untyped call-site would silently switch language.
    """
    assert t("access_denied") == t("access_denied", "ru")


# ── fallback chain ───────────────────────────────────────────────────────


def test_missing_in_target_falls_back_to_other_lang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key only present in one language must still render in the other
    — fallback prevents blank lines on partially-translated keys (the
    common case during migration).

    Both maps are stubbed rather than sampled from the shipped YAMLs.
    The shipped files are full mirrors, so they contain no half-translated
    key to sample in the first place; the previous version of this test
    picked an arbitrary common key out of a ``set`` intersection, which
    (a) never exercised the fallback branch at all and (b) was ordered by
    Python's per-run hash seed, so it failed on roughly one run in two
    hundred — whenever the draw landed on one of the 14 keys containing
    a literal ``<placeholder>``, which ``t()`` escapes and the oracle
    used here did not.
    """
    stub = {"en": {"only_english": "EN only"}, "ru": {"only_russian": "Только RU"}}
    monkeypatch.setattr(i18n, "_load", lambda lang: stub[lang])

    assert t("only_english", "ru") == "EN only"
    assert t("only_russian", "en") == "Только RU"


def test_target_language_wins_over_the_fallback() -> None:
    """When a key exists in both, the requested language is served.

    The flip side of the fallback: a working fallback chain that always
    returned the same language would satisfy the test above and still be
    completely broken.
    """
    en_map = _load("en")
    ru_map = _load("ru")
    assert t("access_denied", "en") == _md_to_html(_escape_stray_angles(en_map["access_denied"]))
    assert t("access_denied", "ru") == _md_to_html(_escape_stray_angles(ru_map["access_denied"]))
    assert t("access_denied", "en") != t("access_denied", "ru")


def test_missing_everywhere_returns_raw_key() -> None:
    """A typo in the call-site should be visible in the UI, not
    rendered as a blank line. Returning the raw key is the same
    behaviour the legacy ``t()`` settled on.
    """
    assert t("definitely_does_not_exist_xyz", "ru") == "definitely_does_not_exist_xyz"
    assert t("definitely_does_not_exist_xyz", "en") == "definitely_does_not_exist_xyz"


# ── placeholder substitution ─────────────────────────────────────────────


def test_placeholder_substitution_via_format_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """We use ``str.format_map``; ensure a key with a ``{name}``
    placeholder takes the kwarg.
    """
    # Patch the loader cache so we can introduce a synthetic template
    # without touching the real YAML files.
    monkeypatch.setattr(
        "telegram_invite_bot.i18n._load",
        lambda lang: {"_test_greet": "Hi, {name}!"} if lang == "en" else {},
    )
    assert t("_test_greet", "en", name="Alice") == "Hi, Alice!"


def test_missing_placeholder_renders_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing kwarg must NOT raise — it renders as ``{name}`` so a
    bug is visible during dev but harmless in prod.
    """
    monkeypatch.setattr(
        "telegram_invite_bot.i18n._load",
        lambda lang: {"_test_greet": "Hi, {name}!"} if lang == "en" else {},
    )
    assert t("_test_greet", "en") == "Hi, {name}!"


def test_safe_format_returns_brace_form_for_missing_key() -> None:
    """Unit-level check on the ``_SafeFormat`` helper itself."""
    fmt = _SafeFormat({"present": "yes"})
    assert fmt["present"] == "yes"
    assert fmt["absent"] == "{absent}"


# ── _normalise ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        (None, "ru"),
        ("", "ru"),
        ("ru", "ru"),
        ("RU", "ru"),
        ("Russian", "ru"),
        ("русский", "ru"),
        ("en", "en"),
        ("English", "en"),
        ("garbage", "ru"),  # unrecognised → default
        ("fr", "ru"),  # unsupported lang → default (silent, not raise)
    ],
)
def test_normalise(inp: str | None, expected: str) -> None:
    assert _normalise(inp) == expected


# ── misc ─────────────────────────────────────────────────────────────────


def test_available_languages() -> None:
    assert available_languages() == ("ru", "en")


def test_load_missing_file_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deploy that ships partial language data must still boot —
    the loader returns ``{}`` for a missing file rather than raising.
    """
    from telegram_invite_bot import i18n

    monkeypatch.setattr(i18n, "_DATA_DIR", tmp_dir := i18n._DATA_DIR.parent / "nonexistent")  # noqa: SLF001
    assert not tmp_dir.exists()
    i18n._load.cache_clear()  # noqa: SLF001
    assert i18n._load("ru") == {}


def test_load_malformed_yaml_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If somebody ships a YAML list by accident (top-level ``- foo``),
    the loader degrades to empty rather than crashing every render.
    """
    from telegram_invite_bot import i18n

    (tmp_path / "ru.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "_DATA_DIR", tmp_path)
    i18n._load.cache_clear()  # noqa: SLF001
    assert i18n._load("ru") == {}
