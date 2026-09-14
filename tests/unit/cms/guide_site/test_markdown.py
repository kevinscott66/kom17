"""The guide markdown renderer.

Replaces ``test_legacy_parity.py``, which asserted byte-identity with
the retired Flask renderer and so protected four defects (dead TOC
links, literal ``{#anchor}`` noise, flattened heading levels, bullets
as paragraphs). These tests assert the *behaviour we want* instead —
starting with those four, so a regression to the old shape fails here.

The escaping tests are the ones that matter most: ``/commands/edit``
lets an operator paste markdown into a form, so this renderer's input
is untrusted by construction.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.cms.guide_site.markdown import (
    extract_headings,
    md_to_html,
    render_inline,
    slugify,
    strip_own_contents,
    strip_title,
)

# --------------------------------------------------------------------
# The four defects the parity contract used to freeze
# --------------------------------------------------------------------


def test_in_page_links_survive() -> None:
    """The guides open with a hand-written table of contents made of
    ``[…](#anchor)`` links. The old renderer stripped them, leaving ten
    lines that look like navigation and do nothing.
    """
    assert md_to_html("[Раздел 1](#start)") == '<p><a href="#start">Раздел 1</a></p>'


def test_explicit_anchor_becomes_an_id_not_visible_text() -> None:
    html = md_to_html("## Полезные советы {#tips}")
    assert html == '<h3 id="tips">Полезные советы</h3>'
    assert "{#tips}" not in html


def test_heading_levels_stay_distinct() -> None:
    html = md_to_html("# Title\n\n## Section\n\n### Sub")
    # Offset by one: the page spends its single <h1> on the hero.
    assert '<h2 id="title">Title</h2>' in html
    assert '<h3 id="section">Section</h3>' in html
    assert '<h4 id="sub">Sub</h4>' in html


def test_bullets_become_one_real_list() -> None:
    html = md_to_html("- раз\n- два\n- три")
    assert html == "<ul>\n<li>раз</li>\n<li>два</li>\n<li>три</li>\n</ul>"


# --------------------------------------------------------------------
# Block structure
# --------------------------------------------------------------------


def test_ordered_list_is_ordered() -> None:
    assert md_to_html("1. первый\n2. второй") == "<ol>\n<li>первый</li>\n<li>второй</li>\n</ol>"


def test_switching_list_type_closes_the_previous_list() -> None:
    """A ``-`` line directly after a numbered one must not land inside
    the ``<ol>``; browsers render it, but the document lies about what
    is a step and what is a note.
    """
    html = md_to_html("1. шаг\n- заметка")
    assert html == "<ol>\n<li>шаг</li>\n</ol>\n<ul>\n<li>заметка</li>\n</ul>"


def test_paragraph_after_list_closes_the_list() -> None:
    html = md_to_html("- пункт\n\nОбычный текст.")
    assert html == "<ul>\n<li>пункт</li>\n</ul>\n<p>Обычный текст.</p>"


def test_rule_and_blank_lines() -> None:
    assert md_to_html("Один\n\n---\n\nДва") == "<p>Один</p>\n<hr>\n<p>Два</p>"


def test_empty_input_renders_nothing() -> None:
    assert md_to_html("") == ""
    assert md_to_html("\n\n  \n") == ""


# --------------------------------------------------------------------
# Inline
# --------------------------------------------------------------------


def test_inline_emphasis_and_code() -> None:
    assert render_inline("**жирный** и *курсив* и `код`") == (
        "<b>жирный</b> и <i>курсив</i> и <code>код</code>"
    )


def test_bold_is_not_read_as_two_italics() -> None:
    assert render_inline("**оба**") == "<b>оба</b>"


def test_external_link_opens_in_a_new_tab() -> None:
    out = render_inline("[канал](https://t.me/example)")
    assert 'href="https://t.me/example"' in out
    assert 'target="_blank"' in out
    assert 'rel="noopener noreferrer"' in out


def test_same_site_link_stays_in_the_tab() -> None:
    out = render_inline("[англ](/commands/en)")
    assert out == '<a href="/commands/en">англ</a>'


@pytest.mark.parametrize(
    "url",
    [
        "//evil.example/x",
        "///evil.example/x",
        # Browsers fold a backslash in the authority position back to a
        # slash (WHATWG URL, "relative slash state"), so these navigate
        # off-site exactly like the form above.
        "/\\evil.example/x",
        "/\\/evil.example/x",
    ],
)
def test_scheme_relative_link_is_not_treated_as_same_site(url: str) -> None:
    """``/`` on the allow-list means *this site* and nothing else (#214).

    A scheme-relative URL starts with ``/`` and lands on another origin.
    It used to pass :func:`_safe_url` and then be classified same-site,
    so it rendered without ``target``/``rel`` — a link to a third party
    that looked, in the markup, exactly like a link to our own page, and
    that carried our ``Referer`` there because ``noreferrer`` was never
    attached. Off-site means "write the scheme".
    """
    out = render_inline(f"[клик]({url})")
    assert out == "клик"
    assert "<a" not in out


def test_a_path_that_merely_contains_a_slash_pair_still_links() -> None:
    """Only the authority position matters — ``/a//b`` is a local path."""
    out = render_inline("[тут](/commands//en)")
    assert out == '<a href="/commands//en">тут</a>'


# --------------------------------------------------------------------
# Escaping / injection — the editor form feeds this renderer
# --------------------------------------------------------------------


def test_raw_html_is_escaped_not_passed_through() -> None:
    assert md_to_html("<script>alert(1)</script>") == (
        "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
    )


@pytest.mark.parametrize(
    "url",
    [
        # No parentheses in these: markdown's own link syntax ends the
        # URL at the first ``)``, so a payload carrying one would be
        # truncated before the scheme check even mattered.
        "javascript:alert%281%29",
        "JavaScript:void%200",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox",
        "file:///etc/passwd",
    ],
)
def test_dangerous_link_schemes_are_not_linked(url: str) -> None:
    """An unlinked URL is a cosmetic loss; a live ``javascript:`` URL is
    stored XSS on a page every user of the bot is sent to.
    """
    out = render_inline(f"[клик]({url})")
    assert "<a" not in out
    assert out == "клик"


def test_link_label_and_href_are_escaped() -> None:
    out = render_inline('[a"b](https://example.com/?x="y)')
    assert 'href="https://example.com/?x=&quot;y"' in out
    assert "a&quot;b" in out


def test_heading_anchor_is_attribute_escaped() -> None:
    """The explicit-anchor pattern only admits ``[A-Za-z0-9_-]``, and
    slugified anchors go through the same attribute escape — neither
    path can break out of the ``id="…"`` attribute.
    """
    html = md_to_html('## Заголовок "с кавычками"')
    assert 'id="заголовок-с-кавычками"' in html
    assert '"с' not in html.split(">")[0]


# --------------------------------------------------------------------
# Slugs and the table of contents
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Раздел 7. Мелочи", "раздел-7-мелочи"),
        ("📦 Инвентарь", "инвентарь"),
        ("Getting Started", "getting-started"),
        ("!!!", "section"),
        ("", "section"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_strip_title_removes_only_the_leading_h1() -> None:
    assert strip_title("# Гайд\n\n## Раздел\n\nтекст") == "\n## Раздел\n\nтекст"


def test_strip_title_leaves_a_document_without_one_alone() -> None:
    for md in ("## Раздел\n\nтекст", "Просто текст\n\n# Не первый", ""):
        assert strip_title(md) == md


# --------------------------------------------------------------------
# strip_own_contents — the page builds its own TOC, so the guide's
# hand-written one is a duplicate that also goes stale on its own.
# --------------------------------------------------------------------


def test_strip_own_contents_removes_the_section_and_stops_at_the_next_one() -> None:
    md = "## 📑 Содержание\n\n1. [Старт](#start)\n2. [Игры](#games)\n\n## 🚀 Раздел 1\n\nтекст\n"
    assert strip_own_contents(md) == "## 🚀 Раздел 1\n\nтекст\n"


def test_strip_own_contents_handles_english_and_a_trailing_section() -> None:
    """No heading follows the contents list — everything to the end of
    the document belongs to it, and goes.
    """
    assert strip_own_contents("## Table of contents\n\n1. [Start](#start)\n") == ""


def test_strip_own_contents_leaves_a_deeper_subsection_alone() -> None:
    """A ``###`` "Contents" *inside* a chapter is that chapter's own
    content, not the document's navigation — removing it would eat
    whatever the author actually wrote there.
    """
    md = "## Раздел\n\n### Содержание коробки\n\nтекст\n"
    assert strip_own_contents(md) == md


def test_strip_own_contents_tolerates_the_rule_the_shipped_guides_use() -> None:
    """The exact shape of both shipped guides: the contents list is
    closed with a ``---`` before the first chapter. Rejecting the rule
    as "prose" would quietly disable the strip on the only two
    documents it exists for.
    """
    md = "## 📑 Содержание\n\n1. [Старт](#start)\n\n---\n\n## 🚀 Раздел 1\n\nтекст\n"
    assert strip_own_contents(md) == "## 🚀 Раздел 1\n\nтекст\n"


def test_strip_own_contents_keeps_a_prose_section_that_shares_the_name() -> None:
    """``/commands/edit`` lets an operator write a chapter genuinely
    called "Содержание". Deleting it on save because of its title alone
    would be silent data loss — much worse than one duplicated list, so
    the body has to actually look like a list of links.
    """
    md = "## Содержание\n\nЗдесь мы описываем, что входит в подписку.\n\n## Дальше\n"
    assert strip_own_contents(md) == md


def test_strip_own_contents_keeps_a_trailing_section_that_is_prose() -> None:
    assert strip_own_contents("## Contents\n\nWhat the box holds.\n") == (
        "## Contents\n\nWhat the box holds.\n"
    )


def test_strip_own_contents_leaves_a_guide_without_one_alone() -> None:
    for md in ("## Раздел\n\nтекст", "просто текст", ""):
        assert strip_own_contents(md) == md


def test_extract_headings_skips_the_document_title() -> None:
    headings = extract_headings("# Гайд\n\n## Первый {#one}\n\n### Второй\n\nтекст")
    assert [(h.level, h.text, h.anchor) for h in headings] == [
        (2, "Первый", "one"),
        (3, "Второй", "второй"),
    ]


def test_extract_headings_returns_plain_text_for_link_labels() -> None:
    """The TOC renders each entry inside an ``<a>``; nested ``<b>``
    would fight the link styling, so inline markup is stripped.
    """
    (heading,) = extract_headings("## **Жирный** `код`")
    assert heading.text == "Жирный код"


def test_anchor_ids_match_the_toc_targets() -> None:
    """The contract that makes the TOC work: every anchor
    :func:`extract_headings` reports must exist as an ``id`` in the
    rendered body.
    """
    md = "# Гайд\n\n## Первый {#one}\n\n### Второй раздел\n\n## Третий"
    html = md_to_html(md)
    for heading in extract_headings(md):
        assert f'id="{heading.anchor}"' in html
