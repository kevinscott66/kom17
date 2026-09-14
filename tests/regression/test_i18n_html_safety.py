"""Every rendered i18n string is sent under ``parse_mode=HTML``.

The failure mode, the allowed-tag set and the validator itself live in
``tests/telegram_html.py`` — the ``/admin_*`` cards are checked against
the same rules by ``test_admin_card_html_safety.py``, and one copy of
the Bot API's tag list beats two that can drift.

What is specific to *this* surface: the YAML is byte-locked to legacy
``translations.py`` (see ``tests/unit/i18n/test_legacy_parity.py``) —
it was generated 1-for-1 from a bot that sent Markdown, where
``<param>`` was ordinary text. So the copy CANNOT be fixed in place,
and the fix lives where ``_md_to_html`` already lives: the render
path. This module therefore checks what :func:`t` returns, not what
the file says — the string that actually reaches Telegram.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.i18n import _load, available_languages, t
from tests.telegram_html import telegram_html_errors

pytestmark = pytest.mark.integration


def _every_key() -> list[str]:
    keys: set[str] = set()
    for lang in available_languages():
        keys |= set(_load(lang))
    return sorted(keys)


def test_every_rendered_string_is_valid_telegram_html() -> None:
    broken: list[str] = []
    for lang in available_languages():
        for key in _every_key():
            for problem in telegram_html_errors(t(key, lang)):
                broken.append(f"[{lang}] {key}: {problem}")
    assert not broken, (
        "these render into a message Telegram refuses to parse — the send "
        "400s and the user gets nothing:\n" + "\n".join(broken)
    )


def test_the_legacy_usage_strings_render_with_their_angles_escaped() -> None:
    """The concrete shape that motivated the guard.

    ``<параметр>`` is a placeholder a human is meant to read, not markup.
    Escaped it renders exactly as written; raw it kills the message.
    """
    rendered = t("modcfg_usage", "ru")
    assert "&lt;параметр&gt;" in rendered, rendered
    assert "<" not in rendered.replace("&lt;", ""), rendered


def test_a_real_tag_still_survives_the_escaping() -> None:
    """Escaping stray angles must not disarm the markup handlers rely on."""
    rendered = t("h_vip", "ru")
    assert "<b>" in rendered and "</b>" in rendered, rendered
    assert not telegram_html_errors(rendered)


def test_the_validator_rejects_what_telegram_rejects() -> None:
    """Guard the guard: an assertion that cannot fail is not a test.

    Each sample below is a real 400 from the Bot API, so the validator
    that greenlights 1400 keys has to flag every one of them.
    """
    samples = {
        "Usage: /modcfg <param>": "unsupported tag",
        "<script>alert(1)</script>": "unsupported tag",
        "<b>bold": "never closed",
        "plain </b>": "with nothing open",
        "<b><i>x</b></i>": "closes",
        "a < b": "unescaped",
    }
    for text, expected in samples.items():
        found = telegram_html_errors(text)
        assert any(expected in row for row in found), (text, found)

    assert not telegram_html_errors('<b>ok</b> <a href="x">y</a> &lt;esc&gt; 5 > 4')


def test_the_scan_covers_the_whole_catalogue() -> None:
    """Guard the guard: a loader that quietly returned ``{}`` would make
    every assertion above vacuous."""
    keys = _every_key()
    assert len(keys) >= 1_400, len(keys)
