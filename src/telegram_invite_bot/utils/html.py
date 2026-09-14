"""HTML-formatting helpers for handler replies.

The new bot's default ``parse_mode`` is HTML (see ``app.py``), so every
handler that renders user-supplied content has to escape it before
embedding into a markup string. This module centralises the small
primitives so the "where do we escape" boundary is one named function
per shape, rather than ``html.escape(...)`` peppered across handlers.

Security note: any string that comes from Telegram (first_name,
username, group title, callback payload) is attacker-controlled — a
display name like ``<b>admin</b>`` will visually impersonate the bot
unless we escape it. The helpers below treat *every* string argument
as untrusted; never bypass them for ostensibly-safe content.
"""

from __future__ import annotations

import html
import re
from typing import Final

from telegram_invite_bot.utils.render import parsed_length

#: Telegram's single-message cap. Measured on the *parsed* text — HTML
#: markup parses into entities and costs nothing — so anything sizing a
#: message body must count with :func:`visible_len`, not ``len``.
TELEGRAM_TEXT_LIMIT: Final[int] = 4096

# Match any inline tag (``<b>``, ``<a href…>``, ``<code>``) so length is
# measured on the rendered text Telegram actually counts.
_HTML_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")

# Legacy telebot-era Markdown that survived the YAML extraction. The
# three spans below are the only Markdown constructs the ported values
# actually use (verified by an AST sweep of every ``t("…")`` literal in
# ``src/``): ``**bold**``, ``` `code` ``` and ``_italic_``.
_MD_BOLD_RE: Final[re.Pattern[str]] = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`([^`\n]+?)`")
# ``(?<![\w*])`` / ``(?![\w])`` keep ``snake_case`` and ``__dunder__``
# out: an underscore glued to a word character is part of an identifier,
# not an italic marker.
_MD_ITALIC_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w*])_([^_\n]+?)_(?![\w])")


def legacy_md_to_html(text: str) -> str:
    """Render legacy Markdown spans as the HTML the bot actually parses.

    ``AppProvider.bot`` in ``di/providers.py`` builds the singleton with
    ``DefaultBotProperties(parse_mode=ParseMode.HTML)``, so HTML is the
    bot-wide default. But ~1500 of the i18n values were lifted verbatim
    out of the telebot-era
    ``translations.py``, where the same strings were sent with
    ``parse_mode="Markdown"``. Those values are byte-locked by
    ``tests/unit/i18n/test_legacy_parity.py`` — the YAML must keep
    matching ``translations.py`` — so the markers cannot be edited out of
    the data. They have to be translated at render time instead, or the
    user reads literal ``**`` and backticks.

    Apply this to the ``t(...)`` result, never to a whole assembled card:
    the rest of a card is already HTML, and re-scanning it would rewrite
    spans that are not Markdown at all.

    The span bodies are USUALLY YAML-authored, but not always: several
    values put a placeholder inside the markers (``p2p_express_enter_fiat``
    is ``Введите сумму в **{currency}**``), and six call sites feed this
    function a ``t(...)`` result that already carries interpolated values
    (the ``legacy_md_to_html`` calls in ``handlers/rp.py`` and
    ``handlers/p2p_trade.py``). So an interpolated value CAN end up
    being scanned for markers.

    That is bounded, not safe-by-luck. The caller escapes first, so ``<``
    is already ``&lt;`` by the time the regexes run and no interpolated
    value can introduce a tag of its own; the only markup it can create is
    ``<b>``/``<i>``/``<code>``, and each substitution emits a matched pair,
    so it cannot produce the unbalanced entity that would make Telegram
    reject the whole message. Every value reaching those six sites today is
    a number, an enum-shaped currency code or a table lookup — none of them
    attacker-controlled.

    The rule for new call sites follows from that: escaping is still the
    caller's job (this function does not make an unescaped placeholder
    safe), and a value a *user* chose — a nickname, a city, a free-text
    note — must not be interpolated into a string that is then passed
    through here, or the user gets to bold and monospace parts of the
    bot's own copy.
    """
    text = _MD_CODE_RE.sub(r"<code>\1</code>", text)
    text = _MD_BOLD_RE.sub(r"<b>\1</b>", text)
    return _MD_ITALIC_RE.sub(r"<i>\1</i>", text)


def plain_text(text: str) -> str:
    """Strip markup down to what a *non*-HTML surface would render.

    ``answerCallbackQuery`` has no ``parse_mode``: its toast/alert text is
    shown verbatim, so a card string reused as a popup leaks its markup —
    the user literally reads ``<b>500.00 RUB</b>``. Passing it through here
    first turns the same copy into the plain sentence the popup needs, so
    the two surfaces keep sharing ONE i18n key instead of drifting apart as
    a ``h_*`` / ``h_*_alert`` pair.

    Entities are un-escaped too (``&lt;`` → ``<``): they are markup for the
    HTML surface, and the popup wants the character they stand for.
    """
    return html.unescape(_HTML_TAG_RE.sub("", text))


def visible_len(text: str) -> int:
    """Length Telegram counts: tags stripped, entities un-escaped.

    Over-estimating is safe (a body would just be split/truncated a hair
    early); under-counting is the dangerous direction because it lets an
    over-limit body through and the send fails with a 400.

    #187: this used to be ``len(plain_text(text))``, and that ``len`` was
    the under-count. Stripping tags and un-escaping entities does yield
    exactly the text Telegram measures — but Telegram measures it in
    UTF-16 code units, and every character outside the BMP costs two of
    them while costing ``len`` one. An emoji-per-row leaderboard is
    precisely the shape that walks off the end of the ceiling this
    function exists to defend, and it is also precisely the shape ``/top``
    produces, since a display name is whatever the user set it to.

    :func:`~telegram_invite_bot.utils.render.parsed_length` already did
    this correctly for the paginator, so this is now that function under
    its older name rather than a second implementation with a different
    answer. The two differ on no input.

    Shared by ``/top`` (row-boundary truncation) and ``/help``
    (category-boundary pagination) — the two surfaces whose length is
    driven by data rather than by a fixed template.
    """
    return parsed_length(text)


def html_user_mention(user_id: int, display: str) -> str:
    """Render a ``tg://user?id=...`` mention with an escaped display name.

    Two handlers (``relations._mention`` and the ``/top`` leaderboard
    renderer) duplicated the same f-string with an inline
    ``html.escape``. Both feed the helper with a Telegram-provided
    ``first_name`` — exactly the attacker-controlled shape that makes
    escaping mandatory (see the WHY comment in
    ``handlers/relations.py``: a name like ``<a href="evil">Pwned``
    would otherwise be honoured as raw HTML).

    The helper takes the *already-resolved* display string — callers
    that need a fallback ("Unknown user", ``str(user_id)``, …) decide
    that before calling so this primitive stays single-purpose.
    """
    return f'<a href="tg://user?id={user_id}">{html.escape(display)}</a>'
