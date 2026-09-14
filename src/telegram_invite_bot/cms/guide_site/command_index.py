"""The searchable command index rendered under the guide.

The guide prose explains *how the bot works*; this section answers the
much narrower question a returning user actually has — "what was the
command for X again?" — and it must never lie. So it is generated from
the same two sources ``/help`` reads:

* :data:`~telegram_invite_bot.core.ranks.COMMAND_ENTRIES` for the
  catalog (names, aliases, categories, default minimum rank), and
* the ``h_cmd_<key>`` i18n strings for the one-line descriptions.

A third source covers the half of the surface a slash-only list makes
invisible: :mod:`~telegram_invite_bot.middlewares.text_alias` answers
bare words too — «баланс», «кто я», «погода Казань» — and that is how
most people actually talk to the bot. Those triggers are printed under
each command from the middleware's own table, and the rule they follow
(free in a DM, prefixed in a group) is stated once in
:func:`render_plain_note_html`.

Nothing is written by hand here, which is the point: a command that
gets renamed, retired or re-ranked changes the website on the next
deploy without anyone remembering to edit an HTML file. The old
Telegraph guide drifted precisely because it was a hand-maintained
copy of a list that kept moving.

Category selection reuses ``handlers.help_catalog`` rather than
re-deriving it, so "what the site advertises" and "what /help
advertises" cannot become two different answers. Moderation commands
ARE listed (a group admin looking up ``/mute`` syntax is exactly who
this page is for) under their own heading; the developer-only
``admin`` category is not, because ``/admin_*`` is an operations
surface and publishing it invites people to knock on doors that will
not open.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import TYPE_CHECKING, Final, NamedTuple

from telegram_invite_bot.core.ranks import (
    default_min_rank,
    entries_in_category,
    synonym_aliases,
)
from telegram_invite_bot.handlers.help_catalog import (
    HELP_HIDDEN_KEYS,
    STAFF_CATEGORIES,
    USER_CATEGORIES,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.text_alias import (
    BARE_GROUP_PHRASES,
    GROUP_PREFIXES,
    plain_triggers_by_command,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Sections the public page lists, in reading order. ``admin`` is
#: deliberately absent — see the module docstring.
SITE_CATEGORIES: Final[tuple[str, ...]] = USER_CATEGORIES + STAFF_CATEGORIES

#: Category → short "who is this for" note. The catalog knows a
#: numeric rank; a visitor needs a sentence. Only categories that
#: genuinely need a caveat carry one.
_CATEGORY_NOTE_KEYS: Final[dict[str, str]] = {
    "moderation": "site_cat_note_moderation",
}

#: Any Cyrillic letter. The catalog and the alias map carry both
#: spellings of every trigger side by side — «баланс» and ``balance``
#: reach the same handler — and which of them a *reader* can use is not
#: a property of the token but of the page it is printed on. A visitor
#: on ``/commands/en`` who is shown «мои_обращения» has been handed a
#: word they cannot read, cannot type and cannot look up (#176).
_CYRILLIC: Final[re.Pattern[str]] = re.compile(r"[\u0400-\u04FF]")


#: Russian words typed in Latin letters — the residue the script test
#: above provably cannot decide. ``_CYRILLIC`` sees nothing to strip in
#: ``knb`` or ``reklama``, yet an English reader is handed a word they
#: can neither read nor look up, which is the #176 complaint exactly one
#: alphabet short. This is a DENY-list of named transliterations, not
#: the "this alias is English" ALLOW-list the ``readable_in`` docstring
#: argues against: leaving an alias out of it costs nothing, and
#: ``tests/unit/cms/guide_site/test_command_index`` fails if an entry
#: here stops naming a live catalog token. Each row keeps another
#: spelling on the English page — ``cpc`` keeps ``rps``, ``ad`` keeps
#: ``ads`` — except ``voice_settings_ru``, whose row name is already the
#: English form and whose ``_ru`` suffix is a legacy duplicate name
#: (bot.py:32266), useless to either reader.
_LATIN_RUSSIAN: Final[frozenset[str]] = frozenset(
    {
        "knb",  # «камень-ножницы-бумага», row ``cpc``
        "reklama",  # «реклама», row ``ad``
        "voice_settings_ru",  # row ``voice_settings``
    }
)


def readable_in(lang: str, tokens: Sequence[str]) -> tuple[str, ...]:
    """``tokens`` minus the ones a reader of ``lang`` cannot use.

    Only the English page filters. The Russian page keeps the Latin
    spellings too, because they genuinely work there and a Russian
    reader who knows ``/balance`` loses nothing by seeing it — the
    asymmetry is real, not an oversight.

    Filtering is by script rather than by a hand-kept "this alias is
    English" list for the same reason the index itself is generated:
    the moment the two lists exist, one of them is wrong. Almost every
    token here is a Telegram command name or a bare trigger word, so
    "has a Cyrillic letter" and "is the Russian spelling" are the same
    question — and where they are not, ``_LATIN_RUSSIAN`` names the
    handful of exceptions one by one.

    Public because ``tests/unit/cms/guide_site/test_command_index``
    asserts the whole English page against it — the guard that stops a
    newly added Russian alias from quietly reappearing there.
    """
    if lang == "ru":
        return tuple(tokens)
    return tuple(
        token for token in tokens if not _CYRILLIC.search(token) and token not in _LATIN_RUSSIAN
    )


#: The markers that hand a message to the AI instead of to a command.
#: Owned by
#: :func:`~telegram_invite_bot.handlers.ai.extract_ai_direct_question`
#: and repeated here rather than imported: pulling a handler module —
#: and with it the whole service graph it depends on — into a page
#: renderer to read two strings is a bad trade. The drift this would
#: normally invite is covered instead by a test that feeds each of
#: these to the real extractor
#: (``tests/unit/cms/guide_site/test_command_index``), which is fed the
#: ``«marker», question`` form the page itself prints — the one spelling
#: that works in every chat type. ``ai`` earned its place here in #171:
#: before that it was a bare word only, so an English reader was shown
#: two Cyrillic markers and no way to address the assistant in a
#: sentence. ``kom`` followed in #206 and sits ahead of it so that the
#: English page leads with the assistant's own name rather than with
#: the generic word — the same thing the Russian page does.
_AI_PREFIXES: Final[tuple[str, ...]] = ("ком", "ии", "kom", "ai")


class SiteSubCommand(NamedTuple):
    """A command that shares its catalog row with a different one.

    ``/forecast`` sits on the ``/weather`` row, ``/cpc_cancel`` on the
    ``/cpc`` row (see
    :class:`~telegram_invite_bot.core.ranks.SubCommand`). Until #164
    the page printed them among the alias chips, which said "another
    way to type /weather" about a command that does something else —
    so the multi-day forecast was, on the page whose whole job is
    discovery, undiscoverable.
    """

    name: str
    description: str
    #: Other spellings of *this* command — «/прогноз» for ``forecast``.
    aliases: tuple[str, ...]


class SiteCommand(NamedTuple):
    """One row of the index, already plain-text (not HTML)."""

    name: str
    description: str
    aliases: tuple[str, ...]
    min_rank: int
    #: Words that reach this command with no slash at all — «баланс»,
    #: «кто я», «погода». Most people who use the bot every day never
    #: type a slash, so a command list that only shows the slash form
    #: documents the half of the surface they don't use.
    plain: tuple[str, ...] = ()
    #: Commands sharing this row that are not spellings of it.
    subcommands: tuple[SiteSubCommand, ...] = ()


class SiteCategory(NamedTuple):
    """One rendered section of the index."""

    key: str
    title: str
    note: str
    commands: tuple[SiteCommand, ...]


def _unwrap_telegram_html(raw: str) -> str:
    """Strip the Telegram dressing a shared i18n value carries.

    ``h_*`` values are written for Telegram's HTML parse mode, so one
    arrives as ``⚡ <b>Базовые</b>`` and spells a literal ampersand as
    ``&amp;``. Reusing them keeps one wording of "what is Moderation"
    across bot and site; the tags — and the entities standing beside
    them — have to come off before the page re-escapes the result, or
    a value carrying ``&amp;`` ships to the reader as a literal
    ``&amp;amp;`` (#1019).
    """
    stripped = raw.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    # ``unescape`` after the tag strip, not before: a value that spells
    # a literal ``<`` as ``&lt;`` must stay text, and unescaping first
    # would turn it into a tag this strip then removes.
    return html_lib.unescape(stripped).strip()


def _clean_label(raw: str) -> str:
    """A section heading: unwrapped, and with the leading emoji off.

    The emoji strip belongs to headings and to nothing else. In a chat
    message it is the only thing separating one section from the next;
    on the page that job belongs to the numbered rule the heading
    already renders (see ``rendering._CSS``), and a pictogram in a
    monospaced small-caps heading reads as a stray character rather
    than a marker.

    #1636: descriptions and category notes take
    :func:`_unwrap_telegram_html` directly instead of coming through
    here. They need the same unwrapping — that half was missing and is
    what the ticket is about — but an emoji opening a description is
    content, not a section marker, and this function would eat it.
    """
    text = _unwrap_telegram_html(raw)
    # Drop everything up to the first letter/digit — emoji, variation
    # selectors and the space after them, whatever the label leads with.
    for index, char in enumerate(text):
        if char.isalnum():
            return text[index:].strip()
    return text


def _commands_in(
    category: str, lang: str, plain: dict[str, tuple[str, ...]]
) -> tuple[SiteCommand, ...]:
    """Rows for one section. ``plain`` is passed in rather than looked
    up here because inverting the alias map is per-*page* work, not
    per-section — building it once per category would redo the same
    scan of every alias nine times for one render.
    """
    rows: list[SiteCommand] = []
    for entry in entries_in_category(category):
        if entry.key in HELP_HIDDEN_KEYS:
            continue
        # Shortcuts worth showing (``/inv`` for ``/inventory``) minus
        # the ``kom_`` duplicates, which exist only to disambiguate
        # between bots in a shared group and would double the visual
        # weight of every row for a detail almost nobody needs here.
        # ``synonym_aliases`` — not ``entry.aliases`` — because a
        # handful of rows also carry tokens that are commands in their
        # own right; those come back below with their own description
        # instead of being passed off as spellings of this one (#164).
        aliases = readable_in(
            lang,
            [alias for alias in synonym_aliases(entry) if not alias.startswith("kom_")],
        )
        subcommands = tuple(
            SiteSubCommand(
                name=sub.key,
                description=_unwrap_telegram_html(t(f"h_subcmd_{sub.key}", lang)),
                aliases=readable_in(
                    lang,
                    [
                        token
                        for token in sub.aliases
                        if token != sub.key and not token.startswith("kom_")
                    ],
                ),
            )
            for sub in entry.subcommands
        )
        rows.append(
            SiteCommand(
                name=entry.key,
                description=_unwrap_telegram_html(t(f"h_cmd_{entry.key}", lang)),
                aliases=aliases,
                min_rank=default_min_rank(entry.key),
                plain=readable_in(lang, plain.get(entry.key, ())),
                subcommands=subcommands,
            )
        )
    return tuple(rows)


def build_index(lang: str, categories: Sequence[str] = SITE_CATEGORIES) -> tuple[SiteCategory, ...]:
    """The full index as data — HTML-free, so tests can assert on it.

    "HTML-free" is enforced here rather than at the render sites
    (#1636): every string this returns has already been through
    :func:`_unwrap_telegram_html`, so a caller that escapes one — and
    every caller does — cannot double-escape it, and a test asserting
    on the data sees the same text the reader sees.
    """
    sections: list[SiteCategory] = []
    plain = plain_triggers_by_command()
    for category in categories:
        commands = _commands_in(category, lang, plain)
        if not commands:
            continue
        note_key = _CATEGORY_NOTE_KEYS.get(category)
        sections.append(
            SiteCategory(
                key=category,
                title=_clean_label(t(f"h_cmdcfg_cat_{category}", lang)),
                note=_unwrap_telegram_html(t(note_key, lang)) if note_key else "",
                commands=commands,
            )
        )
    return tuple(sections)


def _command_row(command: SiteCommand, no_slash_label: str, subcmd_label: str) -> str:
    """One row of the ledger.

    The layout is a definition list, not a card: the command name sits
    in a fixed left column and its description in the right, so a reader
    scanning for ``/withdraw`` runs their eye down one straight edge
    instead of hopping between boxes. See ``rendering._CSS``.

    ``data-search`` carries everything the filter matches against,
    lowercased once at render time so the client-side search never has
    to normalise 130 rows on every keystroke. The plain-text triggers go
    in it too — someone who knows the bot as «кто я» has to be able to
    find the row by typing what they type into Telegram.

    ``no_slash_label`` and ``subcmd_label`` arrive **already escaped** —
    they are the same strings on every row, so they are escaped once by
    the caller instead of once per row here. (``build_index`` yields 7
    categories, 130 commands and 2 subcommands, identical in ru and en;
    this docstring said 80 and 60 until #719.)
    """
    name = html_lib.escape(command.name)
    description = html_lib.escape(command.description)
    haystack = html_lib.escape(
        " ".join(
            (
                command.name,
                *command.aliases,
                *command.plain,
                command.description,
                *(word for sub in command.subcommands for word in (sub.name, *sub.aliases)),
                *(sub.description for sub in command.subcommands),
            )
        ).casefold(),
        quote=True,
    )
    extras = ""
    if command.aliases:
        chips = " ".join(
            f'<span class="alias">/{html_lib.escape(alias)}</span>' for alias in command.aliases
        )
        extras += f'<div class="cmd-aliases">{chips}</div>'
    if command.plain:
        # A separated run rather than more chips: these are words, not
        # commands, and giving them the same shape as the ``/alias``
        # chips above would suggest a slash belongs in front of them.
        words = " · ".join(html_lib.escape(word) for word in command.plain)
        extras += f'<div class="cmd-plain"><span>{no_slash_label}</span>{words}</div>'
    if command.subcommands:
        # Own line, own label, own description. A chip run like the
        # aliases above is exactly the shape that caused #164 — the
        # point of this block is that these are NOT spellings of the
        # command in the left column.
        items = "".join(
            '<span class="cmd-sub">'
            + " ".join(
                f"<code>/{html_lib.escape(name)}</code>" for name in (sub.name, *sub.aliases)
            )
            + f" — {html_lib.escape(sub.description)}</span>"
            for sub in command.subcommands
        )
        extras += f'<div class="cmd-subs"><span>{subcmd_label}</span>{items}</div>'
    return (
        f'<article class="cmd" data-search="{haystack}">'
        f'<button class="cmd-name" type="button" data-copy="/{name}">'
        f"<code>/{name}</code></button>"
        f'<p class="cmd-desc">{description}</p>'
        f"{extras}"
        f"</article>"
    )


def render_index_html(lang: str) -> str:
    """The whole index section, ready to drop into the page shell."""
    sections = build_index(lang)
    no_slash_label = html_lib.escape(t("site_plain_row_label", lang))
    subcmd_label = html_lib.escape(t("site_subcmd_row_label", lang))
    out: list[str] = []
    for section in sections:
        title = html_lib.escape(section.title)
        anchor = f"cmd-{html_lib.escape(section.key, quote=True)}"
        out.append(f'<section class="cmd-group" id="{anchor}">')
        out.append(f"<h3>{title}</h3>")
        if section.note:
            out.append(f'<p class="cmd-note">{html_lib.escape(section.note)}</p>')
        out.append('<div class="cmd-list">')
        out.extend(
            _command_row(command, no_slash_label, subcmd_label) for command in section.commands
        )
        out.append("</div></section>")
    return "\n".join(out)


def _chip_run(items: Sequence[str], *, css: str) -> str:
    return " ".join(f'<code class="{css}">{html_lib.escape(item)}</code>' for item in items)


def _rule(text: str, *, chips: str = "", example: str = "") -> str:
    """One line of the panel: the rule, the tokens, then a typed example."""
    body = html_lib.escape(text)
    if chips:
        body += f" {chips}"
    if example:
        body += f' <code class="ex">{html_lib.escape(example)}</code>'
    return f"<li>{body}</li>"


def render_plain_note_html(lang: str) -> str:
    """The "you don't have to type a slash" panel above the index.

    Every row of the index below carries its own trigger words, but the
    *rule* they obey is not something a list of words can express, and
    it is the part people get wrong: the same «баланс» that works in a
    DM is ignored in a group unless it is prefixed, because a bot that
    answered every group message containing «баланс» would be unusable.
    So the rule is stated once, here, and everything that could drift —
    the prefixes, the un-prefixed whitelist, the AI markers — is printed
    from a constant rather than typed into a translation file.

    That split is also what keeps ``en.yaml`` free of Cyrillic: the
    tokens a user types («бот», «ком») are *data*, identical in both
    languages, and only the prose around them is translated.
    """
    prefixes = readable_in(lang, [prefix.strip() for prefix in GROUP_PREFIXES])
    bare = readable_in(lang, BARE_GROUP_PHRASES)
    ai_markers = readable_in(lang, _AI_PREFIXES)
    ai_example = f"{ai_markers[0]}, {t('site_plain_ai_ex', lang)}"
    rules = "".join(
        (
            _rule(
                t("site_plain_private", lang),
                example=t("site_plain_private_ex", lang),
            ),
            _rule(
                t("site_plain_group", lang),
                chips=_chip_run(prefixes, css="pfx"),
                example=t("site_plain_group_ex", lang),
            ),
            _rule(
                t("site_plain_bare", lang),
                chips=_chip_run(bare, css="pfx"),
            ),
            _rule(
                t("site_plain_weather", lang),
                example=t("site_plain_weather_ex", lang),
            ),
            _rule(
                t("site_plain_ai", lang),
                chips=_chip_run(ai_markers, css="pfx"),
                example=ai_example,
            ),
        )
    )
    heading = html_lib.escape(t("site_plain_heading", lang))
    return (
        '<aside class="plain-note" aria-labelledby="plain-h">'
        f'<p class="tag" id="plain-h">{heading}</p>'
        f"<ul>{rules}</ul></aside>"
    )
