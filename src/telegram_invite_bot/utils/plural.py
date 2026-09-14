"""Grammatical number for "count + noun" copy.

``t()`` fills a template; it does not decline the noun inside it. That is
why ``h_top_games_row`` read ``"{value} игр"`` and rendered "1 игр" for
every user whose first game was also their only one — the noun was frozen
in the form Russian uses from five upwards, and the two forms below it
simply never existed.

The forms themselves stay in the catalogue, under an ``h_plural_*`` key
as a ``|``-separated list, so a translator owns them exactly the way they
own every other string. Only the *choice* is code, because the rule
belongs to the language rather than to the sentence: Russian needs three
forms, English two.

The rule is selected by how many forms the catalogue supplied, not by a
language argument. That is deliberate. :func:`~telegram_invite_bot.i18n.t`
has already resolved the language — including its fallback to the other
catalogue when a key is missing on one side — so the string handed back
here is by construction the one the user will read. Re-deriving the
language from the caller's hint would be a second, independent
normalisation, free to disagree with the first; counting forms cannot.
"""

from __future__ import annotations

from telegram_invite_bot.i18n import t

__all__ = ("plural",)


def _slavic_index(count: int) -> int:
    """CLDR ``one``/``few``/``many`` for Russian.

    11 through 14 are the case a bare modulo gets wrong: they end in 1-4
    yet take the ``many`` form ("11 игр", "12 игр"), because it is the
    last *two* digits that decide the teens.
    """
    if count % 10 == 1 and count % 100 != 11:
        return 0
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return 1
    return 2


def plural(count: int, key: str, lang: str | None = None) -> str:
    """The form of ``key`` that agrees with ``count``.

    ``count`` must be the raw integer, never a formatted one: "1 000" has
    to pick the same form as ``1000``, and a thousands separator would
    break the modulo. Sign is ignored — "-2 монеты" declines like "2".

    Degrades the way the rest of :mod:`~telegram_invite_bot.i18n` does. A
    missing key makes ``t()`` return the key name, which splits into a
    single "form" and comes back verbatim: visible in the bubble, not a
    crash. Too few forms for the rule clamps to the last one, for the
    same reason — a half-translated entry should read awkwardly, not
    raise ``IndexError`` inside a handler.
    """
    forms = t(key, lang).split("|")
    if len(forms) < 2:
        return forms[0]
    index = _slavic_index(abs(count)) if len(forms) > 2 else int(abs(count) != 1)
    return forms[min(index, len(forms) - 1)]
