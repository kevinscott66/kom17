"""Stage 14b: typed YAML-backed replacement for legacy ``command_aliases``.

These tests pin the contract every callback / message dispatcher
relies on. Special attention to:

* ``@botname`` stripping — the legacy ``get_canonical_command`` did
  NOT strip the ``@MyBot`` suffix that Telegram appends in groups, so
  ``/time@MyBot`` silently fell through. New code MUST strip it.
* Russian-alias parity — the original registry stored "погода" and
  the lookup must keep working with cyrillic + leading slash + case
  variation.
* Frozen dataclass — accidentally mutating the cached spec would
  poison every subsequent lookup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_invite_bot.commands.registry import (
    CommandSpec,
    _alias_index,
    _load,
    find_canonical,
    get_spec,
    iter_commands,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """The loader + alias index are ``@cache``-d; clear so any
    monkeypatch of the data file is picked up.
    """
    _load.cache_clear()
    _alias_index.cache_clear()


# ── basic lookup ─────────────────────────────────────────────────────────


def test_find_canonical_known_command() -> None:
    assert find_canonical("/start") == "start"


def test_find_canonical_alias() -> None:
    """The cyrillic alias must resolve back to the english canonical."""
    assert find_canonical("/погода") == "weather"


def test_find_canonical_case_insensitive() -> None:
    assert find_canonical("/WEATHER") == "weather"
    assert find_canonical("/Погода") == "weather"


def test_find_canonical_without_leading_slash() -> None:
    """Some text-only callsites pass the token without the leading
    ``/`` (e.g. button text); resolution still works.
    """
    assert find_canonical("weather") == "weather"


def test_find_canonical_strips_botname_suffix() -> None:
    """The legacy bug: ``/time@MyBot`` silently fell through. Pin the fix."""
    assert find_canonical("/time@MyBot") == "time"
    assert find_canonical("/time@MyBot extra args") == "time"


def test_find_canonical_returns_none_for_unknown() -> None:
    assert find_canonical("/definitely-not-a-command") is None


@pytest.mark.parametrize("inp", [None, "", "   ", "/", "/   ", "/@bot"])
def test_find_canonical_returns_none_for_empty(inp: str | None) -> None:
    """Empty / whitespace / lone slash inputs must not crash the
    dispatcher — Telegram occasionally delivers odd payloads.
    """
    assert find_canonical(inp) is None


# ── get_spec ─────────────────────────────────────────────────────────────


def test_get_spec_returns_typed_spec() -> None:
    spec = get_spec("weather")
    assert isinstance(spec, CommandSpec)
    assert spec.handler == "cmd_weather"
    assert "weather" in spec.aliases
    assert "погода" in spec.aliases


def test_get_spec_unknown_returns_none() -> None:
    assert get_spec("nope") is None


def test_command_spec_is_frozen() -> None:
    """Mutation of the cached spec would corrupt every later lookup."""
    spec = get_spec("weather")
    assert spec is not None
    with pytest.raises(AttributeError):
        spec.handler = "other"  # type: ignore[misc]


def test_command_spec_matches_helper() -> None:
    spec = get_spec("weather")
    assert spec is not None
    assert spec.matches("/Погода")
    assert spec.matches("weather")
    assert not spec.matches("not_an_alias")


# ── iteration ────────────────────────────────────────────────────────────


def test_iter_commands_yields_all() -> None:
    """Sanity: the legacy dump produced 122 commands. If the YAML
    diverges from that count silently we want CI to scream.
    """
    pairs = list(iter_commands())
    assert len(pairs) == 122
    canonicals = {c for c, _ in pairs}
    # Spot-check a few from each category that have shipped to prod.
    for k in ("start", "help", "weather", "balance", "marry", "support"):
        assert k in canonicals


# ── loader edge cases ────────────────────────────────────────────────────


def test_load_missing_file_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial-deploy must not crash on import."""
    from telegram_invite_bot.commands import registry

    monkeypatch.setattr(registry, "_DATA_FILE", tmp_path / "missing.yaml")
    registry._load.cache_clear()  # noqa: SLF001
    assert registry._load() == {}  # noqa: SLF001
    assert find_canonical("/start") is None


def test_load_malformed_yaml_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Top-level list instead of mapping → empty registry, no crash."""
    from telegram_invite_bot.commands import registry

    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_DATA_FILE", bad)
    registry._load.cache_clear()  # noqa: SLF001
    assert registry._load() == {}  # noqa: SLF001


def test_load_skips_non_dict_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed row (string value instead of dict) is skipped, not
    raised — keeps the rest of the registry usable.
    """
    from telegram_invite_bot.commands import registry

    bad = tmp_path / "mixed.yaml"
    bad.write_text(
        "good:\n  handler: h\n  aliases: [g]\n  contexts: [private]\nbroken: just_a_string\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_DATA_FILE", bad)
    registry._load.cache_clear()  # noqa: SLF001
    registry._alias_index.cache_clear()  # noqa: SLF001
    loaded = registry._load()  # noqa: SLF001
    assert "good" in loaded
    assert "broken" not in loaded
