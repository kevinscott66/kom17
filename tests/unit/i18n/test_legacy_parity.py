"""Parity check: the YAML extraction matches legacy ``translations.py``.

Stage 14 generated ``data/{ru,en}.yaml`` from the legacy
``TRANSLATIONS[...]`` dicts via a one-off script. Until ``legacy/bot.py``
is deleted (Stage 15) the legacy dict is the source-of-truth — any
edit by an operator to ``translations.py`` (e.g. patching a typo on a
hotfix branch) MUST also land in the YAML or both copies drift.

This test imports the legacy module at runtime and asserts every
``key → value`` pair matches the YAML. A drift is a CI failure with a
clear message pointing at the bad key — operators see exactly which
side they forgot to update.

Once ``translations.py`` is deleted in Stage 15, this test (and its
``importlib`` shim) goes with it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, cast

import pytest

# ``translations.py`` lives at the repo root, NOT under ``src/`` —
# loading it via a plain ``import`` would require the repo root on
# ``sys.path`` which would shadow other top-level files. Use a manual
# spec load so the import is local to this test module.
_LEGACY = Path(__file__).resolve().parents[3] / "translations.py"


def _load_legacy() -> dict[str, dict[str, str]]:
    if not _LEGACY.is_file():
        pytest.skip("legacy translations.py already removed — parity test obsolete")
    spec = importlib.util.spec_from_file_location("_legacy_translations", _LEGACY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_legacy_translations", module)
    spec.loader.exec_module(module)
    return cast("dict[str, dict[str, str]]", module.TRANSLATIONS)


@pytest.fixture(scope="module")
def _legacy() -> dict[str, dict[str, str]]:
    return _load_legacy()


def _yaml_for(lang: str) -> dict[str, str]:
    from telegram_invite_bot.i18n import _load

    _load.cache_clear()
    return dict(_load(lang))


# Namespaces the new pipeline owns outright, with no legacy
# counterpart. The parity check excludes them so new surfaces can land
# their strings in the YAML without forcing a matching no-op entry into
# translations.py.
#
# * ``h_`` — migrated handlers (Stage 20+); e.g. ``h_send_success``
#   renders HTML where the legacy ``send_*`` keys render Markdown.
# * ``site_`` — chrome for the public guide site (``/commands``), which
#   legacy never had: it served one hand-written Telegraph article and
#   no generated page.
_NEW_PIPELINE_PREFIXES = ("h_", "site_")

# Keys where the aiogram copy DELIBERATELY diverges from legacy, with
# the reason. These are not drift: the two bots behave differently, so
# forcing the strings to match would make one of them lie to its users.
#
# Everything here must be a *behavioural* divergence — a legacy value
# that is factually wrong about the new bot. A typo fix or a reworded
# sentence does NOT belong in this list; patch translations.py instead,
# which is what this test exists to enforce.
_INTENTIONAL_DIVERGENCE: dict[str, str] = {
    # T-020/R7: the stake games carry a ~5% house edge here
    # (DICE_MULTIPLIER 5.7, FLIP_MULTIPLIER 1.9). Legacy still computes
    # bet×6 / bet×2 at bot.py:2575/2578, so its copy is correct FOR
    # LEGACY and must not be "fixed" to match ours.
    "game_dice_desc": "stake multiplier differs (R7 house edge)",
    "game_flip_desc": "stake multiplier differs (R7 house edge)",
    "faq_part2": "quotes the dice multiplier in the games section (R7)",
    # #1347: legacy sends the help block with ``parse_mode="Markdown"``,
    # where a bare ``_`` opens italics — and the two bullets together
    # supply an opening AND a closing one, so legacy MUST escape them
    # as ``/pvp\_coin`` / ``/pvp\_dice``. We render HTML, where the
    # backslash survives verbatim into a command the reader can neither
    # tap nor copy. The escape is correct FOR LEGACY, so translations.py
    # keeps it and the YAML drops it.
    "help_bullet_pvp_coin": "legacy Markdown escape (\\_) is literal in HTML",
    "help_bullet_pvp_dice": "legacy Markdown escape (\\_) is literal in HTML",
}


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_yaml_has_same_keys_as_legacy(_legacy: dict[str, dict[str, str]], lang: str) -> None:
    """Set-level parity: no legacy key dropped, no non-handler key invented."""
    legacy_keys = set(_legacy[lang])
    yaml_keys = {k for k in _yaml_for(lang) if not k.startswith(_NEW_PIPELINE_PREFIXES)}
    missing = legacy_keys - yaml_keys
    extra = yaml_keys - legacy_keys
    assert not missing, f"{lang}: keys missing from YAML: {sorted(missing)[:10]}"
    assert not extra, f"{lang}: keys extra in YAML: {sorted(extra)[:10]}"


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_yaml_values_match_legacy(_legacy: dict[str, dict[str, str]], lang: str) -> None:
    """Value-level parity: every legacy value is byte-for-byte the YAML
    value. Catches whitespace munging or YAML-special-char escapes that
    a naive ``yaml.safe_dump`` could introduce.
    """
    yaml_map: Any = _yaml_for(lang)
    mismatches: list[tuple[str, str, str]] = []
    for key, legacy_val in _legacy[lang].items():
        if key in _INTENTIONAL_DIVERGENCE:
            continue
        yaml_val = yaml_map.get(key)
        if yaml_val != legacy_val:
            mismatches.append((key, legacy_val, yaml_val))
    assert not mismatches, (
        f"{lang}: {len(mismatches)} value mismatch(es) "
        f"(first: key={mismatches[0][0]!r} legacy={mismatches[0][1]!r} yaml={mismatches[0][2]!r})"
    )


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_divergence_allowlist_has_no_dead_entries(
    _legacy: dict[str, dict[str, str]], lang: str
) -> None:
    """Every allowlisted key must still actually diverge.

    Without this, an entry silently turns into a hole in the parity
    check the moment the two copies converge again — the next real
    drift on that key would pass unnoticed. An unknown key is flagged
    too: a rename that leaves its exemption behind is the same hole.
    """
    yaml_map: Any = _yaml_for(lang)
    for key, reason in _INTENTIONAL_DIVERGENCE.items():
        legacy_val = _legacy[lang].get(key)
        assert legacy_val is not None, f"{lang}: allowlisted key {key!r} is not a legacy key"
        assert yaml_map.get(key) != legacy_val, (
            f"{lang}: {key!r} no longer diverges ({reason}) — drop it from "
            f"_INTENTIONAL_DIVERGENCE so the parity check covers it again"
        )
