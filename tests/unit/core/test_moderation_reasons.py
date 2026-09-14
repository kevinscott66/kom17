"""#1346: the reasons the bot writes for itself follow the reader's language.

``moderation_log.reason`` is free text on almost every row — a moderator
typed it — and both renderers (``handlers/groupadmin.py``'s stats card
and the "last actions" block in ``handlers/profile.py``) print it
verbatim. The one row the bot writes itself, the captcha-timeout kick,
used to store the Russian sentence "Капча: не пройдена", so an
English-speaking operator got one Russian line in an otherwise English
card. The column now holds a slug and the wording lives in the locale
files.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.moderation_reasons import (
    CAPTCHA_FAILED,
    reason_html,
    reason_label,
)
from telegram_invite_bot.i18n import t


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_a_known_slug_becomes_its_localized_label(lang: str) -> None:
    assert reason_label(CAPTCHA_FAILED, lang) == t("h_mod_reason_captcha_failed", lang)


def test_the_english_label_has_no_cyrillic() -> None:
    """The whole point of the ticket, pinned directly.

    Asserting equality with the locale value alone would pass just as
    happily if someone pasted the Russian sentence into ``en.yaml``.
    """
    label = reason_label(CAPTCHA_FAILED, "en")
    assert not any("Ѐ" <= ch <= "ӿ" for ch in label), label


def test_moderator_free_text_is_returned_unchanged() -> None:
    """Anything unrecognised is a human's own wording; do not touch it."""
    typed = "спам в личку"
    assert reason_label(typed, "en") == typed


def test_legacy_rows_still_render() -> None:
    """Rows written before #1346 hold the Russian sentence itself.

    There is no backfill — the audit log is append-only and those rows
    age out of the 90-day profile window on their own — so the fallback
    branch is the only thing standing between an old row and a
    missing-key placeholder.
    """
    assert reason_label("Капча: не пройдена", "en") == "Капча: не пройдена"


def test_the_label_is_not_html_escaped() -> None:
    """This function returns words, not finished HTML.

    A caller that escapes what comes back would double-encode the
    label — which is exactly what ``handlers/profile.py`` did until
    #1639 moved it to :func:`reason_html` below.
    """
    assert "&" not in reason_label(CAPTCHA_FAILED, "ru")


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_a_known_slug_goes_out_whole_and_undressed(
    lang: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1639: a label is neither escaped again nor clipped.

    Both current labels are short and carry no markup, so the bug
    was latent; the locale value is dressed here to make it live.
    Escaping would ship ``&amp;`` where the label says ``&``, and
    clipping to a budget written for a moderator's paragraph would
    cut a tag in half — the failure is silent in both cases,
    because a broken tag renders as nothing rather than as an
    error.
    """
    dressed = "<b>Капча</b> & " + "длинная причина " * 8

    def fake(key: str, lg: str | None = None, /, **kwargs: object) -> str:
        return dressed

    monkeypatch.setattr("telegram_invite_bot.core.moderation_reasons.t", fake)
    assert reason_html(CAPTCHA_FAILED, lang, limit=50) == dressed


def test_free_text_is_clipped_before_it_is_escaped() -> None:
    """Legacy parity, and the reason the clip cannot move (bot.py:39802).

    Counting escaped length would show 12 characters of a reason
    made of ``<`` and 50 of a reason made of letters. Asserted
    through the output: 50 raw characters survive, each four bytes
    wide once escaped.
    """
    assert reason_html("<" * 60, "ru", limit=50) == "&lt;" * 50


def test_an_empty_reason_renders_as_nothing() -> None:
    """The caller decides what a row with no reason looks like."""
    assert reason_html("   ", "ru", limit=50) == ""


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_the_two_renderers_agree_on_the_words(lang: str) -> None:
    """Undressed, the HTML form says exactly what the label says."""
    assert reason_html(CAPTCHA_FAILED, lang, limit=50) == reason_label(CAPTCHA_FAILED, lang)
