"""Internationalisation layer (Stage 14).

Replaces the 3415-line ``translations.py`` (1463 unique keys × 2 langs;
1470 written entries — seven keys are defined twice per language and the
later definition silently wins) with a thin YAML-backed lookup. The file
sits at the repository root: there is no ``legacy/`` package. The data lives in ``data/ru.yaml`` and
``data/en.yaml`` — flat ``key: template`` maps generated 1-for-1 from
the legacy ``RU`` / ``EN`` dicts so call-sites can migrate piecemeal.

Why a fresh implementation instead of importing the legacy module:

* The legacy ``t()`` is wrapped in ~50 lines of cyrillic-detection /
  raw-key-suppression / sub-fallback paranoia accreted over years of
  edge-case patching. We don't want to carry that into the new code;
  every new handler should see a predictable API.

* ``str.format_map`` with ``_SafeFormat`` makes a missing placeholder
  render as ``{key}`` instead of raising — the legacy code silently
  returned the raw template on KeyError, hiding the bug. Showing the
  literal placeholder surfaces it during dev without crashing prod.

* ``@lru_cache`` on the YAML loader amortises parse cost over the
  process lifetime. The files are static at runtime; reloading would
  just burn CPU on every callback.

Fallback chain: ``target_lang → other_supported_lang → raw key``. The
raw key is returned (not an empty string) so a missing translation is
visible in the UI rather than rendering as a blank line.

This is the OPPOSITE of the trade legacy made, and an earlier version of
this docstring claimed it was the same one (#712). Legacy's ``t()``
ends with an explicit "Never show raw key to user" block
(``translations.py:3396-3414``) that substitutes ``unknown_label`` or a
bare em-dash, and it even sniffs for key-*shaped* strings
(``:3409-3411``) to catch keys that leaked through another path. We
diverge deliberately: a key on screen is an ugly bug report from the
user, an em-dash is a bug nobody reports. Keep that reasoning in mind
before "fixing" it back.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml

_DATA_DIR = Path(__file__).parent / "data"
_SUPPORTED: tuple[str, ...] = ("ru", "en")
_DEFAULT_LANG = "ru"


class _SafeFormat(dict[str, Any]):
    """``str.format_map`` companion: missing keys render literally.

    A template ``"hello {name}"`` called with no ``name`` kwarg yields
    ``"hello {name}"`` rather than raising ``KeyError``. That keeps a
    misspelled placeholder visible to the developer (and harmless to
    the user) instead of crashing the handler.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@cache
def _load(lang: str) -> Mapping[str, str]:
    """Load and cache one language file. Empty mapping if missing.

    Missing-file is not an error: a deploy that ships only ``ru.yaml``
    should boot and serve Russian, falling back to raw keys for any
    English caller — the chain in :func:`t` handles that gracefully.
    """
    path = _DATA_DIR / f"{lang}.yaml"
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _normalise(lang: str | None) -> str:
    """Map a user-supplied language hint to one of :data:`_SUPPORTED`.

    Accepts the long-form spellings the legacy callsites use
    (``"english"``, ``"русский"``) so the migration is drop-in.
    Anything unrecognised falls back to :data:`_DEFAULT_LANG` — silent
    by design; we don't want a stray ``lang="fr"`` from a database row
    to crash a render.
    """
    if not lang:
        return _DEFAULT_LANG
    s = str(lang).strip().lower()
    if s in {"en", "english"}:
        return "en"
    if s in {"ru", "russian", "rus", "русский"}:
        return "ru"
    return _DEFAULT_LANG


# Legacy ``translations.py`` (and thus the 1-for-1 YAML) marks emphasis
# with Markdown — ``**bold**`` and `` `code` `` — but the whole bot sends
# ``parse_mode=HTML``, so those markers rendered as LITERAL asterisks /
# backticks to users (a faithfully-ported legacy bug). We normalise them
# to HTML at render time, on the TEMPLATE only (before placeholder
# substitution) so a user-supplied ``**`` inside a ``{name}`` value can't
# inject ``<b>``. The YAML is byte-identical to legacy apart from the
# three keys ``tests/unit/i18n/test_legacy_parity.py`` lists in
# ``_INTENTIONAL_DIVERGENCE`` (``game_dice_desc``, ``game_flip_desc``,
# ``faq_part2``), so the ``translations.py`` parity guard keeps passing —
# the fix lives purely in the render path. Handler-owned ``h_`` keys already use ``<b>``/
# ``<code>`` directly and contain no ``**``, so they pass through inert.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_CODE = re.compile(r"`([^`\n]+?)`")


def _md_to_html(template: str) -> str:
    """Convert leftover Markdown emphasis in a template to HTML."""
    template = _MD_BOLD.sub(r"<b>\1</b>", template)
    return _MD_CODE.sub(r"<code>\1</code>", template)


# The same legacy inheritance bites a second time, and harder. Under
# Markdown ``Использование: /modcfg <параметр> <значение>`` was ordinary
# text; under HTML it is a tag Telegram has never heard of, and Telegram
# parses a message whole or not at all — ``sendMessage`` answers
# ``400 Bad Request: can't parse entities: Unsupported start tag``. Not a
# mangled bubble: NO bubble. The command replies nothing and the only
# trace is a traceback.
#
# The copy can't be fixed where it is written: the YAML is byte-locked to
# ``translations.py`` (tests/unit/i18n/test_legacy_parity.py) until the
# legacy bot is deleted. So the repair lives here, next to
# ``_md_to_html``, for the same reason.
#
# ``&`` is deliberately left alone. Telegram accepts a bare one (``👥
# Roles & permissions`` ships today), much of that copy is button text
# that is never parsed at all, and escaping ``&`` would corrupt every
# ``&lt;`` already in the catalogue.
_ALLOWED_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "code",
        "del",
        "em",
        "i",
        "ins",
        "pre",
        "s",
        "span",
        "strike",
        "strong",
        "tg-emoji",
        "tg-spoiler",
        "u",
    }
)
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^<>]*)?)>")


def _escape_stray_angles(template: str) -> str:
    """Escape every ``<``/``>`` that is not part of a tag Telegram knows.

    Runs on the TEMPLATE only, before placeholder substitution — the same
    boundary ``_md_to_html`` respects, so a ``{name}`` value carrying an
    angle bracket is still the caller's job to escape (``utils.html``).
    """
    out: list[str] = []
    pos = 0
    for match in _TAG.finditer(template):
        if match.group(2).lower() not in _ALLOWED_TAGS:
            continue  # not a tag — its angles get escaped with the rest
        out.append(_escape(template[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_escape(template[pos:]))
    return "".join(out)


def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def t(key: str, lang: str | None = None, /, **kwargs: Any) -> str:
    """Look up ``key`` in ``lang``, falling back to the other language.

    Positional-only ``lang`` keeps the call-site readable
    (``t("greeting", "en", name="Alice")``) and prevents a typo from
    accidentally binding a placeholder to the ``lang`` slot.
    """
    target = _normalise(lang)
    fallback = "en" if target == "ru" else "ru"
    raw = _load(target).get(key) or _load(fallback).get(key) or key
    template = _md_to_html(_escape_stray_angles(raw))
    if kwargs:
        return template.format_map(_SafeFormat(kwargs))
    return template


def available_languages() -> tuple[str, ...]:
    """Public accessor for the supported-language tuple."""
    return _SUPPORTED


__all__ = ("available_languages", "t")
