"""Soft-wrapped legal prose must publish as real paragraphs.

The documents are wrapped at 80 columns so a clause is reviewable in a
diff; the site's renderer treats one source line as one block. Without
:func:`unwrap_paragraphs` between them every wrapped line becomes its own
``<p>`` — visibly broken on the page an acquirer reads first.
"""

from __future__ import annotations

import re

import pytest

from telegram_invite_bot.cms.guide_site.markdown import md_to_html
from telegram_invite_bot.cms.legal.documents import (
    DOCUMENTS,
    SUPPORT,
    LegalDoc,
    unwrap_paragraphs,
)


def test_consecutive_prose_lines_become_one_paragraph() -> None:
    md = "Оператор вправе изменять соглашение. Действующая\nредакция публикуется на этой странице."
    assert unwrap_paragraphs(md) == (
        "Оператор вправе изменять соглашение. Действующая редакция публикуется на этой странице."
    )
    assert md_to_html(unwrap_paragraphs(md)).count("<p>") == 1


def test_blank_line_still_separates_paragraphs() -> None:
    html = md_to_html(unwrap_paragraphs("Первый\nабзац.\n\nВторой\nабзац."))
    assert html.count("<p>") == 2


def test_structural_lines_pass_through_and_end_a_paragraph() -> None:
    md = "## Раздел\nВводная\nстрока.\n- пункт один\n- пункт два\n1. шаг\n2) шаг\nХвост."
    out = unwrap_paragraphs(md).split("\n")
    assert out == [
        "## Раздел",
        "Вводная строка.",
        "- пункт один",
        "- пункт два",
        "1. шаг",
        "2) шаг",
        "Хвост.",
    ]


def test_list_items_are_not_glued_to_the_paragraph_above() -> None:
    html = md_to_html(unwrap_paragraphs("Контакты:\n- **Оператор:** ИП\n- **Почта:** a@b.ru"))
    assert html.count("<li>") == 2
    assert "<ul>" in html


def test_a_wrapped_line_starting_with_a_bare_number_stays_in_the_paragraph() -> None:
    """#1634: the ``.``/``)`` marker is required, so "30 дней" is prose.

    Verbatim from ``_SUPPORT_RU`` — the wrap that shipped broken.
    """
    md = (
        "Обращения по платежам рассматриваются в срок до 3 (трёх) рабочих дней;\n"
        "обращения по персональным данным (раздел 8 Политики конфиденциальности) — до\n"
        "30 дней; письменные претензии — до 30 календарных дней."
    )
    assert md_to_html(unwrap_paragraphs(md)).count("<p>") == 1


def test_support_ru_claim_deadlines_render_as_one_sentence() -> None:
    """The live page, not a reconstruction: re-wrapping the source must
    not be able to break the sentence again (#1634).
    """
    html = md_to_html(unwrap_paragraphs(SUPPORT.body("ru")))
    sentence = (
        "обращения по персональным данным (раздел 8 Политики конфиденциальности) — до 30 дней"
    )
    assert sentence in html


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("doc", DOCUMENTS, ids=lambda d: d.slug)
def test_no_document_line_is_broken_off_at_a_bare_number(doc: LegalDoc, lang: str) -> None:
    """Guard for the whole class, across every body in both languages.

    An unwrapped line may start with a digit only when it is a real
    ordered item; anything else means a paragraph was cut in half.
    """
    for line in unwrap_paragraphs(doc.body(lang)).split("\n"):
        if line[:1].isdigit():
            assert re.match(r"^\d+[.)]\s", line) is not None, (doc.slug, lang, line)
