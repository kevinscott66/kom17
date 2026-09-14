"""Resolving a user's display name when Telegram gives us nothing.

``first_name`` is optional on Telegram's side and empty on ours whenever
the ``users`` row predates the field or the account was deleted, so every
surface that mentions a user needs a stand-in label. Four handlers had
grown their own copy of the same two lines — three spelled it
``t("h_relations_default_name", lang)`` (``relations``, ``rp``,
``couple_activities``) and the fourth (``marriage``) had hardcoded the
Russian word ``Пользователь`` in fourteen places, so an English-speaking
user read a Russian noun inside an otherwise English wedding card.

Both shapes live here now:

* :func:`display_name` — the bare string, for surfaces with no markup
  (inline-button captions) or that escape it themselves;
* :func:`mention` — the same string wrapped in a ``tg://user`` link.

Escaping still happens exactly once, inside
:func:`~telegram_invite_bot.utils.html.html_user_mention`; callers that
interpolate a name into HTML *without* a mention must escape it
themselves, which is why :func:`display_name` deliberately returns the
raw text.
"""

from __future__ import annotations

from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.html import html_user_mention


def display_name(first_name: str | None, lang: str) -> str:
    """``first_name`` trimmed, or the localized "no name on record" label.

    Whitespace-only names collapse to the fallback too: a name of three
    spaces renders as an invisible mention, which reads as a bug.
    """
    return (first_name or "").strip() or t("h_relations_default_name", lang)


def mention(user_id: int, first_name: str | None, lang: str) -> str:
    """An HTML ``tg://user`` mention that never renders empty.

    Same role as legacy ``get_user_mention`` when no first_name is on
    record (bot.py:15854-15856).
    """
    return html_user_mention(user_id, display_name(first_name, lang))
