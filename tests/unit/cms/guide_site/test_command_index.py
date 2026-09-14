"""The generated command index on ``/commands``.

The page exists because the old Telegraph guide was a hand-kept copy of
a list that kept moving, and drifted. These tests defend the property
that replaced it: the index is *derived*, so the website and ``/help``
cannot answer "what commands are there" differently.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.cms.guide_site.command_index import (
    _AI_PREFIXES,
    _LATIN_RUSSIAN,
    SITE_CATEGORIES,
    _clean_label,
    _unwrap_telegram_html,
    build_index,
    readable_in,
    render_index_html,
    render_plain_note_html,
)
from telegram_invite_bot.core.ranks import COMMAND_ENTRIES
from telegram_invite_bot.handlers.ai import extract_ai_direct_question
from telegram_invite_bot.handlers.help_catalog import (
    HELP_HIDDEN_KEYS,
    STAFF_CATEGORIES,
    USER_CATEGORIES,
    visible_keys,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.text_alias import BARE_GROUP_PHRASES, GROUP_PREFIXES


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_site_advertises_exactly_what_help_advertises(lang: str) -> None:
    """The guard the whole module is built around.

    ``/help`` shows a staff viewer ``USER_CATEGORIES + STAFF_CATEGORIES``;
    the public page shows the same set (a group admin looking up
    ``/mute`` syntax is exactly who it is for). If those two ever
    diverge, one of them is lying to a user — fail here, in CI, and not
    on a live page nobody re-reads.
    """
    site_keys = tuple(cmd.name for cat in build_index(lang) for cmd in cat.commands)
    assert site_keys == visible_keys(USER_CATEGORIES + STAFF_CATEGORIES)


def test_admin_category_is_not_published() -> None:
    """``/admin_*`` is an operations surface. Publishing it invites
    people to knock on doors that will not open — and tells them which
    doors exist.
    """
    assert "admin" not in SITE_CATEGORIES
    assert SITE_CATEGORIES == USER_CATEGORIES + STAFF_CATEGORIES
    html = render_index_html("ru")
    assert "/admin_" not in html


def test_unregistered_commands_stay_off_the_page() -> None:
    """The catalog still carries legacy rows the new pipeline never
    registered. Advertising one on a public page is worse than in a DM:
    it outlives the conversation.
    """
    published = {cmd.name for cat in build_index("ru") for cmd in cat.commands}
    assert published.isdisjoint(HELP_HIDDEN_KEYS)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_command_has_a_real_description(lang: str) -> None:
    """A missing ``h_cmd_<key>`` string makes :func:`t` echo the key, so
    the page would show ``h_cmd_balance`` to a user. Cheap to catch.
    """
    for category in build_index(lang):
        for command in category.commands:
            assert command.description
            assert not command.description.startswith("h_cmd_")


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_category_titles_carry_no_telegram_markup(lang: str) -> None:
    """Titles are reused from the ``h_cmdcfg_cat_*`` strings, which are
    written for Telegram's HTML parse mode. The tags must come off
    before the page re-escapes them, or the heading reads
    ``<b>Модерация</b>`` literally.
    """
    for category in build_index(lang):
        assert "<b>" not in category.title
        assert "&lt;b&gt;" not in category.title


def test_moderation_section_explains_who_it_is_for() -> None:
    note = next(cat.note for cat in build_index("ru") if cat.key == "moderation")
    assert note
    assert note != "site_cat_note_moderation"
    assert note == t("site_cat_note_moderation", "ru")


def test_kom_duplicates_are_not_listed_as_aliases() -> None:
    """``/kom_top`` exists to disambiguate between bots in a shared
    group. Listing it beside every name doubles the visual weight of
    each row for a detail almost nobody on this page needs.
    """
    aliases = [a for cat in build_index("ru") for cmd in cat.commands for a in cmd.aliases]
    assert aliases  # the fixture is only meaningful if some exist
    assert not any(alias.startswith("kom_") for alias in aliases)


def test_cards_are_escaped_and_searchable() -> None:
    html = render_index_html("ru")
    assert 'data-search="' in html
    assert 'data-copy="/balance"' in html
    # data-search is pre-lowercased at render time so the client-side
    # filter never normalises 80 rows on every keystroke.
    haystack = html.split('data-search="')[1].split('"')[0]
    assert haystack == haystack.lower()


def test_index_is_stable_across_calls() -> None:
    """Pure function of the catalog + i18n: two renders in one process
    must agree, or caching the page would be unsafe.
    """
    assert render_index_html("ru") == render_index_html("ru")


# --------------------------------------------------------------------
# The no-slash surface — «баланс», «кто я», «погода Казань»
# --------------------------------------------------------------------


def test_plain_triggers_reach_the_rows_that_have_them() -> None:
    """The half of the bot a slash-only list makes invisible.

    Most people never type a slash; they type «баланс». A command page
    that shows only ``/balance`` documents the surface they don't use
    and stays silent about the one they do.
    """
    by_name = {cmd.name: cmd for cat in build_index("ru") for cmd in cat.commands}
    assert "кто я" in by_name["profile"].plain
    assert "баланс" in by_name["balance"].plain
    assert "погода" in by_name["weather"].plain
    # …and a command with no plain-text form says nothing rather than
    # inventing one.
    assert by_name["start"].plain == ()


def test_yo_spellings_are_folded_to_one_trigger() -> None:
    """``кошелёк`` and ``кошелек`` are two keys to the matcher and one
    word to a reader; printing both makes the row look like a typo.
    """
    balance = next(
        cmd for cat in build_index("ru") for cmd in cat.commands if cmd.name == "balance"
    )
    assert "кошелёк" in balance.plain
    assert "кошелек" not in balance.plain


def test_plain_triggers_are_searchable() -> None:
    """Someone who knows the bot as «кто я» has to find the row by
    typing what they type into Telegram, not by guessing ``/profile``.
    """
    html = render_index_html("ru")
    rows = html.split('<article class="cmd"')
    row = next(part for part in rows if 'data-copy="/profile"' in part)
    haystack = row.split('data-search="')[1].split('"')[0]
    assert "кто я" in haystack


def test_plain_row_carries_a_label_not_a_slash() -> None:
    """The words are typed *without* a slash. Rendering them in the
    ``/alias`` chip shape would teach the opposite.
    """
    html = render_index_html("ru")
    rows = html.split('<article class="cmd"')
    row = next(part for part in rows if 'data-copy="/balance"' in part)
    plain = row.split('<div class="cmd-plain">')[1].split("</div>")[0]
    assert "баланс" in plain
    assert "/баланс" not in plain


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_note_states_the_rule_and_prints_the_tokens(lang: str) -> None:
    """The rule people get wrong: «баланс» answers in a DM and is
    ignored in a group unless it is marked. Both the prefixes and the
    un-prefixed whitelist come from the matcher's own constants, so the
    page cannot advertise a phrase the bot stopped honouring.
    """
    html = render_plain_note_html(lang)
    phrases = readable_in(lang, BARE_GROUP_PHRASES)
    prefixes = readable_in(lang, [prefix.strip() for prefix in GROUP_PREFIXES])
    # Both runs must survive the language filter with something in them:
    # a note that lists no un-prefixed phrase, or no prefix at all, states
    # a rule the reader has no way to obey.
    assert phrases and prefixes
    for phrase in phrases:
        assert f">{phrase}</code>" in html
    for prefix in prefixes:
        assert f">{prefix}</code>" in html
    assert t("site_plain_heading", lang) in html
    if lang == "en":
        # The other half of the same rule: «меню» is a real trigger, and
        # printing it here would hand an English reader a word they can
        # neither read nor type (#176).
        for russian in set(BARE_GROUP_PHRASES) - set(phrases):
            assert russian not in html


def test_note_is_escaped() -> None:
    html = render_plain_note_html("ru")
    assert "<script" not in html
    # The «...» in the prose survives; raw angle brackets do not appear
    # outside the tags this function writes itself.
    assert html.startswith('<aside class="plain-note"')
    assert html.endswith("</aside>")


@pytest.mark.parametrize("prefix", _AI_PREFIXES)
def test_advertised_ai_prefixes_really_reach_the_ai(prefix: str) -> None:
    """``_AI_PREFIXES`` is a copy — the handler owns the real list, and
    importing it into a page renderer would drag the service graph
    along. This is the check that keeps the copy honest.
    """
    assert extract_ai_direct_question(f"{prefix}, кто ты?") == "кто ты?"


def test_ai_example_is_shown_with_a_real_marker() -> None:
    """The EN translation holds only the question; the marker in front
    of it comes from the code, which is what keeps ``en.yaml`` free of
    Cyrillic while the example stays something you can actually type.

    Which marker that is depends on the page: «ком» heads the list, but
    on the English page it is filtered out and the example is built from
    the first Latin marker instead — otherwise the one line on the page
    that is meant to be copied verbatim is the one line an English
    reader cannot type (#176).

    The two spellings each page leads with are pinned below, not just
    derived: both are a deliberate choice. Reordering ``_AI_PREFIXES``
    would silently hand the English reader ``ai`` — the generic word,
    and the one marker whose space form a group refuses — in place of
    the assistant's own name (#206).
    """
    for lang in ("ru", "en"):
        marker = readable_in(lang, _AI_PREFIXES)[0]
        assert f"{marker}, " in render_plain_note_html(lang)
    assert readable_in("ru", _AI_PREFIXES)[0] == _AI_PREFIXES[0] == "ком"
    assert readable_in("en", _AI_PREFIXES)[0] == "kom"


def test_every_catalog_entry_keeps_a_latin_form() -> None:
    """The invariant that makes the language filter safe to apply.

    ``readable_in`` drops Cyrillic on the English page, so a command
    that ships Russian-only would not merely look wrong there — it
    would lose every trigger it has and the row would advertise a
    command nobody can invoke. The filter cannot fix that; only the
    catalog can, by never carrying an entry without a Latin spelling.

    Subcommands are checked for the same reason: they are printed as
    their own chips («/weather forecast»), and one that exists only as
    «прогноз» leaves an English reader with a command whose second
    word is unreachable (#176). ``forecast`` now also carries ``fc``,
    the short second spelling the owner asked for.
    """
    latin_only = [entry.key for entry in COMMAND_ENTRIES if not readable_in("en", entry.aliases)]
    assert not latin_only, f"catalog entries with no Latin trigger: {latin_only}"

    # "Something survived the filter" passes by construction — every
    # entry carries its own key among its aliases and every key is
    # Latin today. The property that actually has to hold is that the
    # key itself stays readable, deny-list included: it is the spelling
    # the row is titled with and the one ``/help`` prints (#446).
    unreadable = [
        entry.key for entry in COMMAND_ENTRIES if readable_in("en", [entry.key]) != (entry.key,)
    ]
    assert not unreadable, f"catalog keys unreadable on /commands/en: {unreadable}"

    subs = [
        (entry.key, sub.aliases)
        for entry in COMMAND_ENTRIES
        for sub in entry.subcommands
        if not readable_in("en", sub.aliases)
    ]
    assert not subs, f"subcommands with no Latin trigger: {subs}"

    unreadable_subs = [
        (entry.key, sub.key)
        for entry in COMMAND_ENTRIES
        for sub in entry.subcommands
        if readable_in("en", [sub.key]) != (sub.key,)
    ]
    assert not unreadable_subs, f"subcommand keys unreadable on /commands/en: {unreadable_subs}"


def test_section_titles_do_not_double_escape_entities() -> None:
    """#1019: an entity in a category label must reach the page once.

    The shared ``h_cmdcfg_cat_*`` labels are written for Telegram's HTML
    parse mode, so ``&`` is spelled ``&amp;`` at rest. ``_clean_label``
    stripped the tags but left the entity, and the renderer then escaped
    the whole string again — the public English page really did serve
    ``<h3>Profile &amp;amp; progress</h3>``.
    """
    html = render_index_html("en")

    assert "&amp;amp;" not in html
    assert "<h3>Profile &amp; progress</h3>" in html


def test_clean_label_unescapes_after_stripping_tags() -> None:
    """Order matters: a label spelling a literal ``<`` must stay text.

    Unescaping first would turn ``&lt;b&gt;`` into a real tag that the
    strip then silently removed, deleting the author's own characters —
    hence the second case, which only holds for strip-then-unescape.
    """
    assert _clean_label("⚡ <b>A &amp; B</b>") == "A & B"
    assert _clean_label("<b>Tags: &lt;b&gt;</b>") == "Tags: <b>"


_DRESSED = "<b>A &amp; B</b>"


def _dressing(prefix: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``h_*`` key under ``prefix`` answer in Telegram HTML."""

    def fake(key: str, lang: str | None = None, /, **kwargs: object) -> str:
        if key.startswith(prefix):
            return _DRESSED
        return t(key, lang, **kwargs)

    monkeypatch.setattr("telegram_invite_bot.cms.guide_site.command_index.t", fake)


@pytest.mark.parametrize("prefix", ["h_cmd_", "h_subcmd_", "site_cat_note_"])
def test_index_data_is_unwrapped_before_the_page_escapes_it(
    prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1636: the #1019 fix reached the headings and stopped there.

    Descriptions and category notes come from the same Telegram-HTML
    ``h_*``/``site_*`` families as the labels and went to the renderer
    still dressed, so a value spelling ``&`` as ``&amp;`` would have
    shipped as a literal ``&amp;amp;`` — the exact bug the category
    labels were fixed for. The live values happen to carry no markup
    today, which is why this pins the seam rather than the data.
    """
    _dressing(prefix, monkeypatch)

    texts = [
        text
        for section in build_index("en")
        for text in (
            section.note,
            *(command.description for command in section.commands),
            *(sub.description for command in section.commands for sub in command.subcommands),
        )
    ]
    assert "A & B" in texts, prefix
    assert not any("<b>" in text or "&amp;" in text for text in texts)
    assert "&amp;amp;" not in render_index_html("en")


def test_a_description_keeps_the_emoji_a_heading_loses() -> None:
    """#1636: the unwrap is shared, the emoji strip is not.

    Routing descriptions through ``_clean_label`` would have unwrapped
    them and eaten a leading pictogram as well — on a heading that is a
    section marker the numbered rule already draws, but in a
    description it is the author's own first character.
    """
    assert _clean_label("⚡ <b>Базовые</b>") == "Базовые"
    assert _unwrap_telegram_html("⚡ <b>Базовые</b>") == "⚡ Базовые"


#: #446: the Latin twins the owner asked for — a second spelling for
#: every row whose only synonym was a Russian word saying something the
#: row name does not already say in English. Listed here rather than
#: derived from the catalog, so that dropping one is a failure and not
#: a silent regression of the page the owner complained about.
_EN_TWINS: tuple[tuple[str, str], ...] = (
    ("questions", "faq"),
    ("fc", "weather"),
    ("funny18", "joke18"),
    ("store", "shop"),
    ("review", "feedback"),
    ("couples", "marriages"),
    ("donors", "donaters"),
    ("reject", "decline"),
)


def _page_tokens(lang: str) -> set[str]:
    """Every trigger word the rendered index offers a reader of ``lang``."""
    tokens: set[str] = set()
    for section in build_index(lang):
        for command in section.commands:
            tokens.add(command.name)
            tokens.update(command.aliases)
            tokens.update(command.plain)
            for sub in command.subcommands:
                tokens.add(sub.name)
                tokens.update(sub.aliases)
    return tokens


def test_transliteration_denylist_names_only_live_tokens() -> None:
    """``_LATIN_RUSSIAN`` must not outlive the aliases it hides.

    A deny-list is a hand-kept list, which is the thing the module
    otherwise refuses to have — so it earns its place only while every
    entry is doing work. The moment an alias is renamed or dropped the
    entry becomes a rule about nothing, and the next reader has no way
    to tell it apart from a rule that still matters.
    """
    catalog: set[str] = set()
    for entry in COMMAND_ENTRIES:
        catalog.update(entry.aliases)
        for sub in entry.subcommands:
            catalog.update(sub.aliases)

    dead = sorted(token for token in _LATIN_RUSSIAN if token not in catalog)
    assert not dead, f"deny-list entries that name no catalog alias: {dead}"

    # Hiding a token must not leave its row with nothing to print. Each
    # of these rows keeps another Latin spelling: ``cpc`` keeps ``rps``,
    # ``ad`` keeps ``ads``, ``voice_settings`` falls back to its own
    # name, which was always the English form.
    stranded = [
        entry.key
        for entry in COMMAND_ENTRIES
        if _LATIN_RUSSIAN.intersection(entry.aliases) and not readable_in("en", entry.aliases)
    ]
    assert not stranded, f"rows left with no English trigger by the deny-list: {stranded}"


def test_english_page_carries_no_russian_word_in_latin_letters() -> None:
    """The half of #446 the script test provably cannot reach.

    ``_CYRILLIC`` finds nothing to strip in ``knb`` or ``reklama``, so
    before the deny-list the English page really did print two Russian
    words — plus ``voice_settings_ru``, a name whose whole content is
    "this one is the Russian one". Asserted on the rendered page and
    not only on the filter, because the page is what the owner read.
    """
    # Pinned, not merely iterated: every assertion here is *about* the
    # contents of the list, so a token quietly dropped from it would
    # take its own guard along with it. The set is the result of a hand
    # audit of all 408 catalog tokens (#446) — the one judgement no
    # script can make — and changing it means redoing that audit.
    assert set(_LATIN_RUSSIAN) == {"knb", "reklama", "voice_settings_ru"}

    english = _page_tokens("en")
    leaked = sorted(_LATIN_RUSSIAN.intersection(english))
    assert not leaked, f"Russian words in Latin letters on /commands/en: {leaked}"

    # The Russian page keeps them: they are spellings a Russian reader
    # can type, and the asymmetry is the whole point of ``readable_in``.
    russian = _page_tokens("ru")
    assert _LATIN_RUSSIAN.issubset(russian)

    html = render_index_html("en")
    for token in sorted(_LATIN_RUSSIAN):
        assert f"<code>/{token}</code>" not in html


@pytest.mark.parametrize(("token", "row"), _EN_TWINS)
def test_446_latin_twin_reaches_the_english_page(token: str, row: str) -> None:
    """Each twin must be advertised, on the right row, in both languages.

    The catalog is the only place these are declared, so a twin that
    stopped rendering would do so silently — the row keeps its name and
    still looks complete. That every one of them also reaches a live
    handler is enforced separately and globally by
    ``tests/regression/test_help_surface``: the catalog will print an
    alias whether or not anything answers it.
    """
    assert token in _page_tokens("en")
    assert token in _page_tokens("ru")

    owners = [
        entry.key
        for entry in COMMAND_ENTRIES
        if token in entry.aliases or any(token in sub.aliases for sub in entry.subcommands)
    ]
    assert owners == [row], f"{token} is advertised by {owners}, expected {row!r}"
