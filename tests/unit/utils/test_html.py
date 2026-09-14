"""Unit tests for :mod:`telegram_invite_bot.utils.html`.

``plain_text`` exists for one specific surface: ``answerCallbackQuery``
takes no ``parse_mode``, so a toast/alert shows its text verbatim. Card
copy reused as a popup therefore has to be stripped, or the user reads
``<b>500.00 RUB</b>`` instead of a bold number.

``visible_len`` is the older caller (it counts the *rendered* length so
``/top`` and ``/help`` split on the boundary Telegram measures). Since
#187 it delegates to :func:`telegram_invite_bot.utils.render.parsed_length`,
which strips and un-escapes exactly like ``plain_text`` and then counts
UTF-16 code units instead of characters — so the strip round-trip is
still pinned here, and the counting unit is pinned separately below.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.utils.html import (
    TELEGRAM_TEXT_LIMIT,
    html_user_mention,
    legacy_md_to_html,
    plain_text,
    visible_len,
)
from telegram_invite_bot.utils.render import utf16_length


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        # Nothing to strip — the common case must be a no-op.
        ("plain sentence", "plain sentence"),
        # Inline tags: the popup wants the words, not the markup.
        ("<b>500.00 RUB</b>", "500.00 RUB"),
        (
            "❌ up to <b>{fiat}</b> — at most <b>1 000</b> COM.",
            "❌ up to {fiat} — at most 1 000 COM.",
        ),
        # Attributes must not survive the strip.
        ('<a href="tg://user?id=7">Имя</a>', "Имя"),
        # Entities are markup for the HTML surface; the popup wants the
        # character. ``&lt;`` is what the i18n YAML stores for a literal
        # "<" (see h_daily_wait_lt_minute).
        ("&lt; 1 мин", "< 1 мин"),
        ("A &amp; B", "A & B"),
    ],
)
def test_plain_text_renders_what_a_popup_shows(markup: str, expected: str) -> None:
    assert plain_text(markup) == expected


def test_plain_text_is_idempotent() -> None:
    """Double-stripping a popup string must not eat more of it — handlers
    may pass copy that was already plain.
    """
    once = plain_text("<i>&lt;1 min</i>")
    assert once == "<1 min"
    assert plain_text(once) == once


def test_visible_len_counts_the_rendered_text() -> None:
    """Markup costs nothing in Telegram's length budget; entities cost one
    character each. Under-counting is the dangerous direction — it would
    let an over-limit body through and the send 400s.
    """
    assert visible_len("<b>abc</b>") == 3
    assert visible_len("&lt;") == 1
    assert visible_len("plain") == len("plain")
    assert TELEGRAM_TEXT_LIMIT == 4096


# ----- #187: the unit of the count is UTF-16, not characters ---------
#
# Telegram measures its 4096 ceiling in UTF-16 code units, so anything
# outside the BMP — every emoji a user can put in a display name —
# costs two. ``len()`` charged one, which is the under-counting
# direction: the guard let an over-limit body through and the send came
# back 400 "message is too long".


def test_visible_len_counts_utf16_code_units_not_characters() -> None:
    """One emoji is one character and two of Telegram's units."""
    assert len("😀") == 1
    assert visible_len("😀") == 2
    assert visible_len("<b>😀😀</b>") == 4
    # BMP text is unaffected — one character, one unit, as before.
    assert visible_len("привет") == len("привет")


def test_visible_len_agrees_with_the_paginator() -> None:
    """``/top`` and the ``/help`` paginator must not disagree about how
    long the same string is; #187 made them one implementation.
    """
    line = html_user_mention(7, "😀 Аня 🎉")
    assert visible_len(line) == utf16_length("😀 Аня 🎉")


def test_html_user_mention_escapes_the_display_name() -> None:
    """``first_name`` is attacker-controlled — a name carrying markup must
    not be honoured as HTML inside the mention.
    """
    rendered = html_user_mention(7, '<a href="evil">Pwned')
    assert rendered == '<a href="tg://user?id=7">&lt;a href=&quot;evil&quot;&gt;Pwned</a>'


# ---------------------------------------------------------------------------
# legacy_md_to_html (#708)
# ---------------------------------------------------------------------------


def test_legacy_md_to_html_converts_the_three_span_kinds() -> None:
    """``**b**``, ``` `c` `` and ``_i_`` are the only Markdown constructs
    the ported values use, and all three must reach Telegram as HTML: the
    bot's parse_mode is HTML, so an unconverted marker is shown literally.
    """
    assert legacy_md_to_html("**Важно:**") == "<b>Важно:</b>"
    assert legacy_md_to_html("`/forecast Москва`") == "<code>/forecast Москва</code>"
    assert legacy_md_to_html("_Сбер; 1000_") == "<i>Сбер; 1000</i>"


def test_legacy_md_to_html_leaves_identifiers_alone() -> None:
    """An underscore glued to a word character belongs to an identifier,
    not to an italic span. Without the word-boundary guards a sentence
    naming two settings would be swallowed into one ``<i>``.
    """
    assert legacy_md_to_html("rp_18_enabled и group_settings") == ("rp_18_enabled и group_settings")
    assert legacy_md_to_html("__dunder__") == "__dunder__"


def test_legacy_md_to_html_bold_wins_over_italic() -> None:
    """``**x**`` must not be re-read as two italic markers once the bold
    pass has already consumed it.
    """
    assert legacy_md_to_html("**{count}**") == "<b>{count}</b>"


def test_legacy_md_to_html_passes_unpaired_markers_through() -> None:
    """A lone marker is punctuation, not markup — rewriting it would
    produce an unclosed tag and Telegram would reject the whole message.
    """
    assert legacy_md_to_html("2 ** 3 = 8") == "2 ** 3 = 8"
    assert legacy_md_to_html("цена ~ 5 `") == "цена ~ 5 `"
