"""Parity check: the YAML registry matches legacy ``command_aliases.py``.

Stage 14b generated ``data/registry.yaml`` from
``command_aliases.COMMAND_REGISTRY``. Until ``legacy/bot.py`` and its
imports go away (Stage 15) the legacy dict is the source-of-truth —
any new alias added on a hotfix branch must land in both places. This
test pins parity in CI so drift fails loudly with a useful diff.

Deleted in Stage 15 with the legacy module.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, cast

import pytest

_LEGACY = Path(__file__).resolve().parents[3] / "command_aliases.py"


def _load_legacy() -> dict[str, dict[str, Any]]:
    if not _LEGACY.is_file():
        pytest.skip("legacy command_aliases.py already removed — parity test obsolete")
    spec = importlib.util.spec_from_file_location("_legacy_aliases", _LEGACY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_legacy_aliases", module)
    spec.loader.exec_module(module)
    return cast("dict[str, dict[str, Any]]", module.COMMAND_REGISTRY)


@pytest.fixture(scope="module")
def _legacy() -> dict[str, dict[str, Any]]:
    return _load_legacy()


def test_registry_has_same_canonicals_as_legacy(
    _legacy: dict[str, dict[str, Any]],
) -> None:
    from telegram_invite_bot.commands.registry import _load, iter_commands

    _load.cache_clear()
    new_canonicals = {c for c, _ in iter_commands()}
    legacy_canonicals = set(_legacy)
    missing = legacy_canonicals - new_canonicals
    extra = new_canonicals - legacy_canonicals
    assert not missing, f"commands missing from YAML: {sorted(missing)[:10]}"
    assert not extra, f"commands extra in YAML: {sorted(extra)[:10]}"


def test_registry_aliases_match_legacy(_legacy: dict[str, dict[str, Any]]) -> None:
    """Per-command alias parity. Catches a stray alias being added to
    the legacy dict on a hotfix that wasn't mirrored into YAML — the
    new dispatcher would otherwise silently stop recognising it.
    """
    from telegram_invite_bot.commands.registry import _load, get_spec

    _load.cache_clear()
    mismatches: list[tuple[str, set[str], set[str]]] = []
    for canon, data in _legacy.items():
        spec = get_spec(canon)
        assert spec is not None, f"missing canonical {canon!r}"
        legacy_aliases = {str(a).lower() for a in (data.get("aliases") or [])}
        new_aliases = set(spec.aliases)
        if legacy_aliases != new_aliases:
            mismatches.append((canon, legacy_aliases, new_aliases))
    assert not mismatches, (
        f"{len(mismatches)} alias mismatch(es) "
        f"(first: canon={mismatches[0][0]!r} legacy={sorted(mismatches[0][1])} "
        f"yaml={sorted(mismatches[0][2])})"
    )


def test_registry_handlers_match_legacy(_legacy: dict[str, dict[str, Any]]) -> None:
    """The ``handler`` slot maps the canonical to a function name in
    legacy ``bot.py``. The legacy dispatcher uses it to ``getattr`` the
    correct module function; a typo here turns a working command into
    a NotImplementedError at runtime.
    """
    from telegram_invite_bot.commands.registry import _load, get_spec

    _load.cache_clear()
    mismatches: list[tuple[str, str, str]] = []
    for canon, data in _legacy.items():
        spec = get_spec(canon)
        assert spec is not None
        legacy_handler = str(data.get("handler") or "")
        if legacy_handler != spec.handler:
            mismatches.append((canon, legacy_handler, spec.handler))
    assert not mismatches, (
        f"{len(mismatches)} handler mismatch(es) "
        f"(first: canon={mismatches[0][0]!r} legacy={mismatches[0][1]!r} "
        f"yaml={mismatches[0][2]!r})"
    )
