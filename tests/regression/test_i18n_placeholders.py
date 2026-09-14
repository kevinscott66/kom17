"""Every literal ``t()`` call site must agree with both catalogues.

:mod:`telegram_invite_bot.i18n` is deliberately crash-proof: an unknown
key falls back to the raw key, and ``_SafeFormat.__missing__`` renders a
placeholder the call site forgot as a literal ``{name}``. Nothing
raises, nothing is logged — the defect exists only in the chat bubble
the user is looking at.

That trade is the right one for a running bot and the wrong one for a
test suite, so the checks the runtime declines to make live here:

* the key exists at all (else the user reads ``h_p2p_order_detail``);
* it exists in BOTH languages (else half the audience reads it);
* every ``{placeholder}`` in either template is supplied (else the user
  reads ``Ордер #{order_id}`` — which is how this file came to exist).

Scope: call sites with a literal key and no ``**`` unpacking, which is
1500 of them. A dynamic key or a splatted mapping is invisible to a
static scan; those are covered by the e2e tests that render the card.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.integration

_SRC = Path(__file__).resolve().parents[2] / "src/telegram_invite_bot"
_DATA = _SRC / "i18n/data"

# ``{name}``, but not ``{{name}}`` — the escaped form is a literal brace
# to ``str.format_map`` and needs no kwarg.
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Keys whose braces are the SUBJECT of the sentence rather than a slot
# to fill: the /setwelcome help text teaches the admin which
# placeholders their welcome template may contain, so substituting them
# would delete the very thing it is explaining.
#
# Adding a key here is a claim that its braces must reach the user
# verbatim. If that is not true, fix the call site instead.
_LITERAL_BRACE_KEYS = frozenset({"h_welcome_set_usage"})


def _catalogue(lang: str) -> dict[str, str]:
    raw = yaml.safe_load((_DATA / f"{lang}.yaml").read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in raw.items()}


def _call_sites() -> list[tuple[str, str, set[str] | None]]:
    """``(where, key, supplied_kwargs)``; ``None`` = ``**`` unpacking."""
    sites: list[tuple[str, str, set[str] | None]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "t" or not node.args:
                continue
            key_node = node.args[0]
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            where = f"{path.relative_to(_SRC)}:{node.lineno}"
            if any(kw.arg is None for kw in node.keywords):
                sites.append((where, key_node.value, None))
                continue
            sites.append((where, key_node.value, {kw.arg for kw in node.keywords if kw.arg}))
    return sites


@pytest.fixture(scope="module")
def sites() -> list[tuple[str, str, set[str] | None]]:
    return _call_sites()


def test_every_key_exists_in_both_languages(
    sites: list[tuple[str, str, set[str] | None]],
) -> None:
    ru, en = _catalogue("ru"), _catalogue("en")
    gaps = [
        f"{where} {key} (ru={key in ru} en={key in en})"
        for where, key, _ in sites
        if not (key in ru and key in en)
    ]
    assert not gaps, (
        "these t() keys are missing from a catalogue; the user reads the "
        "raw key instead of a sentence:\n" + "\n".join(sorted(set(gaps)))
    )


def test_every_placeholder_is_supplied(
    sites: list[tuple[str, str, set[str] | None]],
) -> None:
    ru, en = _catalogue("ru"), _catalogue("en")
    gaps: list[str] = []
    for where, key, supplied in sites:
        if supplied is None or key in _LITERAL_BRACE_KEYS:
            continue
        needed = set(_PLACEHOLDER.findall(ru.get(key, ""))) | set(
            _PLACEHOLDER.findall(en.get(key, ""))
        )
        missing = needed - supplied
        if missing:
            gaps.append(f"{where} {key} -> {sorted(missing)}")
    assert not gaps, (
        "these t() call sites leave a placeholder unfilled; _SafeFormat "
        "renders it literally into the user's message:\n" + "\n".join(gaps)
    )


def test_the_scan_actually_finds_the_call_sites(
    sites: list[tuple[str, str, set[str] | None]],
) -> None:
    """Guard the guard: a scan that matches nothing asserts nothing.

    Both checks above are "assert no offenders". If a refactor renamed
    ``t`` or moved it behind a wrapper, they would keep passing over an
    empty list forever. Pin a floor, and pin that at least some sites
    carry kwargs — a scan that found only bare calls would silently stop
    exercising the placeholder half.
    """
    assert len(sites) >= 1_000, len(sites)
    with_kwargs = [s for s in sites if s[2]]
    assert len(with_kwargs) >= 200, len(with_kwargs)


def test_literal_brace_allowlist_is_not_stale(
    sites: list[tuple[str, str, set[str] | None]],
) -> None:
    """An allowlist entry that no longer needs the exemption is a hole
    kept open for nothing — the next call site to reuse that key would
    inherit the pass."""
    ru, en = _catalogue("ru"), _catalogue("en")
    unneeded: list[str] = []
    for key in _LITERAL_BRACE_KEYS:
        templates: list[str] = [ru.get(key, ""), en.get(key, "")]
        if not any(_PLACEHOLDER.search(tpl) for tpl in templates):
            unneeded.append(key)
    assert not unneeded, (
        "these keys are exempted from the placeholder check but no longer "
        f"contain any placeholder: {sorted(unneeded)}"
    )


def test_allowlisted_keys_are_never_given_kwargs(
    sites: list[tuple[str, str, set[str] | None]],
) -> None:
    """The exemption says "these braces are literal text". A call site
    that passes kwargs for them means the opposite, and one of the two
    is wrong."""
    contradictions = [
        f"{where} {key} <- {sorted(supplied)}"
        for where, key, supplied in sites
        if key in _LITERAL_BRACE_KEYS and supplied
    ]
    assert not contradictions, (
        "allowlisted-as-literal keys called WITH substitutions:\n" + "\n".join(contradictions)
    )


def test_no_stray_format_call_on_a_translation() -> None:
    """``t(...).format(...)`` is a placeholder filled the long way round.

    It works — ``_SafeFormat`` leaves the brace intact for ``.format``
    to consume — right up until a translator adds a SECOND placeholder
    to that key, at which point ``str.format`` raises ``KeyError`` and
    the handler dies where ``t(..., **kwargs)`` would have degraded to
    a visible brace. The four ``/filter_add`` sites that did this are
    what this test was written from.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
            ):
                continue
            inner: Any = node.func.value
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "t"
            ):
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert not offenders, (
        "pass the substitutions to t() itself instead of .format()-ing "
        "its result:\n" + "\n".join(offenders)
    )
