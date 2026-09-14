"""Unit tests for the L-70 transcript-quote formatter.

Covers the pure ``_format_quote`` helper (HTML port of the legacy
Markdown ``_format_transcription_as_quote``). Dispatcher wiring is
covered by the e2e voice suite; here we pin the HTML-escaping +
truncation contract.

``h_vtr_more_chars`` now lives in both catalogues, so the truncation
test asserts the rendered Russian string rather than the raw key name
the note here used to describe. Its noun is declined via
``h_plural_chars`` (#207), which is why the assertion pins a specific
count: 100 and 1 take different forms.
"""

from __future__ import annotations

from telegram_invite_bot.handlers.voice_transcribe import _format_quote


def test_short_text_is_plain_blockquote() -> None:
    out = _format_quote("hello world", "ru")
    assert out == "<blockquote>hello world</blockquote>"


def test_html_is_escaped() -> None:
    out = _format_quote("a < b & c > d", "ru")
    assert "&lt;" in out
    assert "&amp;" in out
    assert "&gt;" in out
    # No raw angle brackets leak inside the quote body.
    assert "a < b" not in out


def test_long_text_is_truncated_with_more_chars_note() -> None:
    text = "x" * 700
    out = _format_quote(text, "ru", max_chars=600)
    assert out.startswith("<blockquote>")
    # Truncated body holds exactly the cap (600 x's).
    assert "x" * 600 in out
    assert "x" * 601 not in out
    # Rendered RU "… ещё {count} {noun}" with count=100 → "символов".
    assert "ещё 100 символов" in out


def test_truncation_tail_declines_the_noun_for_a_single_character() -> None:
    """#207: 601 chars past a 600 cap leaves 1, and 1 is not "символов"."""
    out = _format_quote("y" * 601, "ru", max_chars=600)
    assert "ещё 1 символ<" in out or "ещё 1 символ</i>" in out
    assert "символов" not in out
