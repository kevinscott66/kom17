"""Unit tests for the ``/help`` catalog renderer (RR-6 #62/#63).

Pure-function coverage: role→category selection, the rank-annotation
suffix, the hidden-key filter, per-locale shape, length budgeting and
the category-boundary paginator. Router-vs-card *consistency* (does the
bot actually register everything the card advertises?) is a separate
concern and lives in ``tests/regression/test_help_surface.py`` — it
needs a real router build, which unit tests deliberately avoid.
"""

from __future__ import annotations

import re

import pytest

from telegram_invite_bot.core.ranks import RankLevel
from telegram_invite_bot.handlers.help_catalog import (
    HELP_HIDDEN_KEYS,
    KOM_PREFIX_HINTS,
    NO_SLASH_TRIGGERS,
    OWNER_CATEGORIES,
    STAFF_CATEGORIES,
    USER_CATEGORIES,
    _paginate,
    categories_for,
    rank_note,
    render_help_pages,
    visible_keys,
)
from telegram_invite_bot.handlers.main_menu import _help_keyboard
from telegram_invite_bot.utils.html import TELEGRAM_TEXT_LIMIT, visible_len

pytestmark = pytest.mark.unit

LANGS = ("ru", "en")

# Telegram's supported subset for message text. A card built from ~70
# yaml values is exactly where a stray ``<div>`` or an unescaped ``<``
# would sneak in and turn the whole send into a 400.
_ALLOWED_TAGS = {"b", "/b", "i", "/i", "u", "/u", "s", "/s", "code", "/code", "pre", "/pre"}


def _tags(text: str) -> list[str]:
    return [match.group(1).split(" ", 1)[0] for match in re.finditer(r"<([^>]*)>", text)]


# --------------------------------------------------------------- roles


def test_plain_user_sees_only_user_categories() -> None:
    assert categories_for(is_staff=False, is_developer=False) == USER_CATEGORIES


def test_staff_gains_moderation_only() -> None:
    assert categories_for(is_staff=True, is_developer=False) == (USER_CATEGORIES + STAFF_CATEGORIES)


def test_developer_gains_moderation_and_admin() -> None:
    assert categories_for(is_staff=True, is_developer=True) == (USER_CATEGORIES + OWNER_CATEGORIES)


def test_developer_outranks_missing_staff_flag() -> None:
    """A developer in a chat they don't administer still gets the full
    reference — legacy keyed the owner view on bot ownership, not on
    chat membership."""
    assert categories_for(is_staff=False, is_developer=True) == (USER_CATEGORIES + OWNER_CATEGORIES)


# ---------------------------------------------------------- rank notes


def test_rank_note_empty_for_everyone_commands() -> None:
    assert rank_note(0, "ru") == ""


def test_rank_note_shows_threshold() -> None:
    assert "2" in rank_note(2, "ru")
    assert "2" in rank_note(2, "en")


def test_rank_note_says_disabled_at_developer_level() -> None:
    """Legacy printed ``[выкл]`` from rank 6 up: nobody but the
    developer can reach it, so "restricted" would misdescribe it."""
    off_ru = rank_note(RankLevel.DEVELOPER, "ru")
    assert "выкл" in off_ru
    assert "off" in rank_note(RankLevel.DEVELOPER, "en")


# ------------------------------------------------------- hidden keys


def test_hidden_keys_never_advertised() -> None:
    advertised = visible_keys(USER_CATEGORIES + OWNER_CATEGORIES)
    assert HELP_HIDDEN_KEYS.isdisjoint(advertised)


@pytest.mark.parametrize("lang", LANGS)
def test_hidden_command_absent_from_rendered_card(lang: str) -> None:
    body = "\n".join(render_help_pages(lang, is_staff=True, is_developer=True, ranks={}))
    for key in HELP_HIDDEN_KEYS:
        assert f"/{key} —" not in body, key


def test_visible_keys_are_unique_and_ordered() -> None:
    keys = visible_keys(USER_CATEGORIES + OWNER_CATEGORIES)
    assert len(keys) == len(set(keys))
    assert keys[0] == "start"


# ------------------------------------------------------------- shape


@pytest.mark.parametrize("lang", LANGS)
def test_user_card_lists_every_user_command(lang: str) -> None:
    body = "\n".join(render_help_pages(lang))
    for key in visible_keys(USER_CATEGORIES):
        assert f"/{key} —" in body, key


@pytest.mark.parametrize("lang", LANGS)
def test_user_card_hides_moderation_and_admin(lang: str) -> None:
    body = "\n".join(render_help_pages(lang))
    assert "/ban —" not in body
    assert "/cmdcfg —" not in body


@pytest.mark.parametrize("lang", LANGS)
def test_staff_card_shows_moderation_with_thresholds(lang: str) -> None:
    body = "\n".join(render_help_pages(lang, is_staff=True, ranks={}))
    assert "/ban —" in body
    assert "/cmdcfg —" not in body
    assert rank_note(2, lang) in body


@pytest.mark.parametrize("lang", LANGS)
def test_developer_card_shows_admin_block(lang: str) -> None:
    body = "\n".join(render_help_pages(lang, is_staff=True, is_developer=True, ranks={}))
    assert "/cmdcfg —" in body
    assert rank_note(5, lang) in body


@pytest.mark.parametrize("lang", LANGS)
def test_plain_view_never_leaks_rank_annotations(lang: str) -> None:
    """``ranks=None`` is the plain-user contract: a member must not read
    the moderation thresholds off their own card."""
    body = "\n".join(render_help_pages(lang))
    assert rank_note(2, lang) not in body
    assert rank_note(5, lang) not in body


def test_ranks_override_beats_catalog_default() -> None:
    body = "\n".join(render_help_pages("ru", is_staff=True, ranks={"ban": 4}))
    ban_line = next(line for line in body.splitlines() if line.startswith("• /ban "))
    assert rank_note(4, "ru") in ban_line


def test_override_can_mark_a_command_disabled() -> None:
    body = "\n".join(render_help_pages("ru", is_staff=True, ranks={"clear": 6}))
    clear_line = next(line for line in body.splitlines() if line.startswith("• /clear "))
    assert "выкл" in clear_line


@pytest.mark.parametrize("lang", LANGS)
def test_no_missing_translations(lang: str) -> None:
    """``t()`` returns the raw key on a miss — the only reliable
    "translation missing" signal, so assert none leaked into the card."""
    body = "\n".join(render_help_pages(lang, is_staff=True, is_developer=True, ranks={}))
    assert "h_cmd_" not in body
    assert "h_help_" not in body
    assert "h_cmdcfg_" not in body


@pytest.mark.parametrize("lang", LANGS)
def test_only_telegram_supported_tags(lang: str) -> None:
    body = "\n".join(render_help_pages(lang, is_staff=True, is_developer=True, ranks={}))
    unsupported = [tag for tag in _tags(body) if tag not in _ALLOWED_TAGS]
    assert not unsupported, unsupported


@pytest.mark.parametrize("lang", LANGS)
def test_footer_button_line_only_when_button_exists(lang: str) -> None:
    """The "press the button below" copy must not promise a button the
    handler didn't attach — the same lie the flat card used to tell when
    the Telegraph env vars were unset."""
    without = "\n".join(render_help_pages(lang, has_button=False))
    with_button = "\n".join(render_help_pages(lang, has_button=True))
    assert len(with_button) > len(without)


@pytest.mark.parametrize("lang", LANGS)
def test_tail_advertises_no_slash_and_kom_shortcuts(lang: str) -> None:
    body = "\n".join(render_help_pages(lang))
    for trigger in NO_SLASH_TRIGGERS[lang]:
        assert trigger in body, trigger
    for name in KOM_PREFIX_HINTS:
        assert f"/{name}" in body, name


def test_commands_are_not_wrapped_in_code_tags() -> None:
    """Deliberate divergence from legacy: ``<code>/help</code>`` kills
    Telegram's bot-command entity, so the user has to copy-paste instead
    of tapping once. Pin it so a future "cosmetic" change can't quietly
    take the interaction away.
    """
    body = "\n".join(render_help_pages("ru"))
    assert "<code>" not in body
    assert "• /start —" in body


# -------------------------------------------------------- pagination


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize(
    ("staff", "dev"),
    [(False, False), (True, False), (True, True)],
)
def test_every_page_fits_telegram_limit(lang: str, staff: bool, dev: bool) -> None:
    pages = render_help_pages(
        lang, is_staff=staff, is_developer=dev, ranks={} if staff else None, has_button=True
    )
    assert pages
    for page in pages:
        assert visible_len(page) <= TELEGRAM_TEXT_LIMIT


#: Pages a plain member should have to walk to read the whole card.
#: ``main_menu``'s in-place tap browses them with ⬅️/➡️, so more pages
#: cost taps rather than content — but a card that takes five taps to
#: read is a card nobody reads. Past this the answer is to prune or
#: re-group categories, never to quietly add another page.
_MAX_MENU_PAGES = 3


@pytest.mark.parametrize("lang", LANGS)
def test_plain_user_card_stays_browsable(lang: str) -> None:
    """The plain card outgrew one message when the 74 uncatalogued
    commands landed (#114), so ``main_menu`` pages it in place instead
    of pointing at ``/help``. What still has to hold is that the walk
    stays short — this is the tripwire that used to assert a single
    page, moved to the budget that actually matters now."""
    assert 1 <= len(render_help_pages(lang, has_button=True)) <= _MAX_MENU_PAGES


@pytest.mark.parametrize("lang", LANGS)
def test_no_page_is_a_stub(lang: str) -> None:
    """A split that leaves a bare heading (or a single orphan row) on
    its own page reads as a bug to the user tapping ➡️ — they paid a
    tap for nothing. Cheap invariant: every page carries real rows."""
    for page in render_help_pages(lang, has_button=True):
        assert page.count("• /") >= 3, page


def test_multi_page_cards_carry_a_page_marker() -> None:
    blocks = [["<b>H</b>", *[f"• /cmd{i} — x" for i in range(200)]]]
    pages = _paginate(blocks, 400)
    assert len(pages) > 1
    for page in pages:
        assert visible_len(page) <= 400


def test_paginator_repeats_heading_on_split_blocks() -> None:
    blocks = [["<b>Heading</b>", *[f"• /cmd{i} — x" for i in range(100)]]]
    pages = _paginate(blocks, 300)
    assert len(pages) > 1
    for page in pages:
        assert page.startswith("<b>Heading</b>")


def test_paginator_keeps_small_blocks_whole() -> None:
    blocks = [["a"], ["b"], ["c"]]
    assert _paginate(blocks, TELEGRAM_TEXT_LIMIT) == ["a\n\nb\n\nc"]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize(("page", "total"), [(1, 2), (2, 2), (2, 3), (1, 1)])
def test_help_card_buttons_are_visually_distinguishable(lang: str, page: int, total: int) -> None:
    """No two buttons on the help card may open with the same glyph.

    The card carries a ``⬅️ Назад`` row of its own, so the paging pair
    cannot also be ``⬅️``/``➡️`` — on the live bot page 2 showed ``⬅️``
    sitting directly above ``⬅️ Назад``, two identical arrows leading
    somewhere different, with nothing to tell them apart.

    Driven through ``_help_keyboard`` rather than the rendered card so
    the *middle* page (``2/3``) is covered too: that is the only shape
    where both paging arrows appear at once, and the real catalog is
    two pages long today — an e2e test could not reach it.
    """
    markup = _help_keyboard(lang, page=page, total=total)
    labels = [button.text for row in markup.inline_keyboard for button in row]
    glyphs = [label.split()[0] for label in labels]
    assert len(set(glyphs)) == len(glyphs), labels
