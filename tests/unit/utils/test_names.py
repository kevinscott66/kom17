"""The stand-in label for a user Telegram gave us no name for (#128).

``handlers/marriage`` hardcoded the Russian word ``Пользователь`` in
fourteen places, so an English-speaking user read a Russian noun inside
an otherwise English wedding card. Three sibling handlers each carried
their own correct-but-duplicated two-liner; all four now share these two
functions, which is what these tests pin.
"""

from __future__ import annotations

from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.names import display_name, mention

RU_FALLBACK = t("h_relations_default_name", "ru")
EN_FALLBACK = t("h_relations_default_name", "en")


def test_the_fallback_follows_the_users_language() -> None:
    """The whole point of #128: no Russian noun in an English card."""
    assert display_name(None, "ru") == RU_FALLBACK
    assert display_name(None, "en") == EN_FALLBACK
    assert RU_FALLBACK != EN_FALLBACK


def test_a_real_name_is_returned_untouched() -> None:
    assert display_name("Аня", "ru") == "Аня"


def test_a_whitespace_only_name_falls_back() -> None:
    """A name of three spaces renders an invisible mention — a bug, not
    a name. Telegram permits it, so the empty case is not enough."""
    assert display_name("   ", "en") == EN_FALLBACK
    assert display_name("", "en") == EN_FALLBACK


def test_a_name_keeps_its_inner_spacing() -> None:
    """Only the edges are trimmed; ``Анна Мария`` is one first_name."""
    assert display_name("  Анна Мария  ", "ru") == "Анна Мария"


def test_the_mention_escapes_the_name() -> None:
    """first_name is attacker-controlled: a name of ``<b>admin</b>`` must
    not reach Telegram as markup (see utils/html's security note)."""
    rendered = mention(42, '<a href="evil">Pwned', "ru")

    assert rendered == ('<a href="tg://user?id=42">&lt;a href=&quot;evil&quot;&gt;Pwned</a>')


def test_a_nameless_user_still_gets_a_clickable_mention() -> None:
    """The fallback goes *inside* the link, so the mention never renders
    as an empty (unclickable) span."""
    assert mention(7, None, "en") == f'<a href="tg://user?id=7">{EN_FALLBACK}</a>'
