"""ru/en interface convergence invariants (institutionalizes the
'both languages must converge; the English UI must contain no Cyrillic'
rule).

Distinct from ``test_legacy_parity`` (which checks the YAML against the
frozen legacy ``translations.py``): this guards the live ru↔en symmetry
for EVERY key — including the new ``h_``-prefixed handler keys the legacy
check ignores — and forbids stray Cyrillic in the English file (a
copy-paste-from-Russian regression), and pins that the two files agree
on the *placeholders* inside those values, not just on the key names.
"""

from __future__ import annotations

import re
import string

import pytest
import yaml

from telegram_invite_bot.i18n import _DATA_DIR, _load

#: #1349: the whole Cyrillic block, not just the Russian alphabet. The
#: narrower ``[А-Яа-яЁё]`` let Ukrainian/Serbian/Bulgarian letters
#: (і, ї, є, ђ, ћ, ў) through, and those are exactly what a half-done
#: copy-paste leaves behind. Same range as
#: ``cms/guide_site/command_index.py::_CYRILLIC``, so the site filter and
#: this guard agree on what "Cyrillic" means.
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")


@pytest.fixture
def ru() -> dict[str, str]:
    _load.cache_clear()
    return dict(_load("ru"))


@pytest.fixture
def en() -> dict[str, str]:
    _load.cache_clear()
    return dict(_load("en"))


def test_ru_en_key_sets_are_equal(ru: dict[str, str], en: dict[str, str]) -> None:
    """Every key must exist in BOTH languages — a key present in one but
    not the other means one locale silently falls back / KeyErrors."""
    only_ru = sorted(set(ru) - set(en))
    only_en = sorted(set(en) - set(ru))
    assert not only_ru, f"keys in ru.yaml missing from en.yaml: {only_ru[:20]}"
    assert not only_en, f"keys in en.yaml missing from ru.yaml: {only_en[:20]}"


def test_english_yaml_has_no_cyrillic(en: dict[str, str]) -> None:
    """The English UI must never contain Cyrillic — an EN value with
    Russian letters is an untranslated copy-paste leaking to en users."""
    offenders = {k: v for k, v in en.items() if isinstance(v, str) and _CYRILLIC.search(v)}
    assert not offenders, (
        f"en.yaml values contain Cyrillic (untranslated): {sorted(offenders)[:20]}"
    )


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_yaml_defines_every_key_once(lang: str) -> None:
    """A duplicate key is silently survivable — and that is the problem.

    ``yaml.safe_load`` keeps the LAST definition of a repeated key and
    says nothing, so a second ``h_foo:`` further down the file quietly
    wins and the first wording becomes dead text. Legacy shipped exactly
    that bug: 1463 unique keys x 2 languages against 1470 written
    entries, i.e. seven keys defined twice per language.

    ``yaml.compose`` returns the raw node with EVERY entry, duplicates
    included, so comparing its length against the loaded mapping's is a
    direct duplicate count.

    Deliberately not a hardcoded key-count assertion: the sibling
    ``test_ru_en_key_sets_are_equal`` already catches an asymmetric loss,
    and a literal total would have to be bumped on every legitimate
    addition — which trains people to edit the test reflexively.
    """
    text = (_DATA_DIR / f"{lang}.yaml").read_text(encoding="utf-8")
    written = len(yaml.compose(text).value)
    loaded = len(yaml.safe_load(text))
    assert written == loaded, f"{lang}.yaml defines {written - loaded} key(s) twice"


def _placeholders(template: str) -> set[str]:
    """Field names ``str.format_map`` would substitute in ``template``.

    ``string.Formatter().parse`` rather than a regexp because it is the
    same parser ``format_map`` uses: it honours ``{{`` escapes, accepts
    a conversion/format spec after the name, and — as the ``ValueError``
    below relies on — refuses a malformed template instead of quietly
    matching part of it.

    A regexp for ``{[a-z_]+}`` also misses a Cyrillic-named field, and
    the one real mismatch this guard was written for
    (``rel_activity_footer_hint``, which documented a chat-code syntax
    that no handler implements) was spelled ``{код}``.

    ``is not None`` and not a truth test: ``{}`` parses to an empty field
    name, and an auto-numbered field is a genuine divergence — it would
    raise ``IndexError`` against the ``dict`` ``format_map`` receives.
    """
    return {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}


def test_templates_are_parseable(ru: dict[str, str], en: dict[str, str]) -> None:
    """An unbalanced brace is a crash waiting for its first kwarg.

    ``t()`` only calls ``format_map`` when the caller passes kwargs, so a
    malformed template sits harmless until someone adds a placeholder to
    that call site — and then raises ``ValueError`` inside the handler.
    """
    broken = []
    for lang, table in (("ru", ru), ("en", en)):
        for key, value in table.items():
            try:
                # ``parse`` is a generator: it must be drained to raise.
                list(string.Formatter().parse(value))
            except ValueError as exc:  # noqa: PERF203 — one report per key
                broken.append(f"{lang}:{key} ({exc})")
    assert not broken, f"unparseable format templates: {broken}"


def test_placeholders_match_across_languages(ru: dict[str, str], en: dict[str, str]) -> None:
    """The same key must take the same substitutions in both languages.

    Neither direction of a mismatch raises — ``_SafeFormat`` renders a
    missing field as ``{name}`` and an unused kwarg is simply dropped —
    which is exactly why this needs a test: the failure is a user
    reading a literal ``{amount}``, or an English sentence that quietly
    lost the number the Russian one states.
    """
    offenders = []
    for key in sorted(set(ru) & set(en)):
        in_ru, in_en = _placeholders(ru[key]), _placeholders(en[key])
        if in_ru != in_en:
            offenders.append(
                f"{key}: ru-only={sorted(in_ru - in_en)} en-only={sorted(in_en - in_ru)}"
            )
    assert not offenders, f"placeholder sets diverge between ru.yaml and en.yaml: {offenders}"
