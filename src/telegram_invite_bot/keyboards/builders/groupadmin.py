"""/groupadmin inline keyboards — CallbackData factories + markup builders.

Legacy ``cmd_groupadmin`` (bot.py:32279-32320) renders buttons that open
four sub-pages: banned words, settings, stats and staff
(``moderation_words`` / ``moderation_settings`` / ``moderation_stats`` /
``moderation_mods``, bot.py:32302-32309). The L-42 port shipped the
overview card only: every button re-rendered that same card, so none of
the sub-pages were reachable (RR-4 #35).

RR-4 #35/#36/#37 restores the sub-pages as a proper page-based panel:

* the overview keeps its per-section buttons (each still a refresh —
  every subsystem has a dedicated edit command, and the section tokens
  feed observability);
* a nav row opens the **Settings**, **Stats** and **Words** pages;
* Settings is genuinely interactive again — inline toggles for the six
  boolean gates and numeric pickers for the warn limit and the mute
  duration, over the PER-GROUP ``group_mod_config`` store (legacy's
  equivalents wrote process-global ``settings.json`` values, so one
  group's admin changed every group's moderation — see the handler).

RR-4 #38 adds the **Staff** page on the same nav row: a roster keyboard
whose two write buttons appear only for an actor who may change ranks,
and a one-person-per-row demote grid built from the roster the handler
had just read (legacy made the admin type a raw user id instead).

RR-4 #43 gives the **Words** page the same treatment: ➕/➖ buttons and a
per-word delete grid, which legacy had (bot.py:32335-32394) and the port
had reduced to a read-only preview. The grid's payload carries the row
id rather than the word — see :class:`GroupAdminWordDrop` for why
legacy's word-in-the-payload could not survive here.

Wire format
-----------

Strangler invariant (same reasoning as ``keyboards/builders/faq.py``):
the legacy callback literals (``moderation_words`` etc.) are dead wire —
T-011 stopped the process that answered them — but they are still
sitting in scrollback on keyboards legacy sent, and Telegram will still
deliver a tap on one. So every prefix here stays distinct from them: an
old tap must resolve to nothing rather than into a handler that never
authored its payload.

Payload fields are SHORT TOKENS (``auto``, ``warns``), never raw column
names: the token vocabulary published here is exactly the set of fields
a button may write, so a forged payload naming any other column of
``group_mod_config`` resolves to nothing at all. Keeping the mapping in
the module that owns the wire format means the handler cannot widen it
by accident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telegram_invite_bot.core.callback_fields import DbInt
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.render import state_mark

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Page + section tokens
# ---------------------------------------------------------------------------

# Pages the panel can render. ``PAGE_HOME`` is the overview card.
PAGE_HOME = "home"
PAGE_SETTINGS = "set"
PAGE_STATS = "stats"
PAGE_WORDS = "words"
PAGE_STAFF = "staff"
#: The "pick someone to demote" grid — a page of its own so the back
#: button and the refresh button both have somewhere to point.
PAGE_STAFF_DROP = "sfdel"
#: The "pick a word to drop" grid (RR-4 #43), same shape as the staff
#: one and for the same reason.
PAGE_WORDS_DROP = "wdel"

_PAGES: Final[frozenset[str]] = frozenset(
    {
        PAGE_HOME,
        PAGE_SETTINGS,
        PAGE_STATS,
        PAGE_WORDS,
        PAGE_WORDS_DROP,
        PAGE_STAFF,
        PAGE_STAFF_DROP,
    }
)

# Stable per-section tokens on the overview card. They only feed
# logging/observability — every one of them re-renders the WHOLE card —
# but keeping them lets a future iteration route per-section without a
# wire-format change.
SECTION_MOD = "mod"
SECTION_FILTER = "filter"
SECTION_WELCOME = "welcome"
SECTION_ALIASES = "aliases"
SECTION_TREASURY = "treasury"
SECTION_RULES = "rules"
SECTION_ALL = "all"

_SECTIONS: tuple[str, ...] = (
    SECTION_MOD,
    SECTION_FILTER,
    SECTION_WELCOME,
    SECTION_ALIASES,
    SECTION_TREASURY,
    SECTION_RULES,
    SECTION_ALL,
)


class GroupAdminRefresh(CallbackData, prefix="gadm"):
    """Render a panel page.

    ``section`` is either a ``PAGE_*`` token (open that sub-page) or a
    ``SECTION_*`` token (refresh the overview). It is validated in the
    handler; an unknown token degrades to the overview — the payload is
    user-controlled wire data, never trusted.

    No ``group_id`` / ``user_id`` fields on purpose: the handler
    re-derives the group from ``callback.message.chat.id`` (set by
    Telegram, not the user) and re-gates the CLICKING user on every tap,
    so a forged payload cannot cross groups or impersonate an admin.
    """

    section: str


class GroupAdminSet(CallbackData, prefix="gadms"):
    """Write one moderation-config field for the current group.

    ``field`` is a short token from :data:`TOGGLE_FIELDS` or
    :data:`PICKER_FIELDS`; ``value`` is 0/1 for a toggle and the chosen
    number for a picker. Both are re-validated in the handler against
    the same tables before anything reaches the repository.
    """

    field: str
    value: DbInt


class GroupAdminPick(CallbackData, prefix="gadmp"):
    """Open the value picker for one numeric field."""

    field: str


class GroupAdminStaffAdd(CallbackData, prefix="gadmsa"):
    """Start the "grant a rank" input flow (RR-4 #38)."""


class GroupAdminStaffDrop(CallbackData, prefix="gadmsd"):
    """Strip ``user_id``'s global rank.

    The id is in the payload because the button was BUILT from a roster
    the bot itself just read — but it is still user-controlled wire data
    on the way back, so the handler re-derives the target's current rank
    and re-checks every guard (self, developer, rank-above-actor) rather
    than trusting that this id came from its own keyboard.
    """

    user_id: DbInt


class GroupAdminWordAdd(CallbackData, prefix="gadmwa"):
    """Start the "add a filter word" input flow (RR-4 #43)."""


class GroupAdminWordDrop(CallbackData, prefix="gadmwd"):
    """Delete filter row ``word_id`` from the current group's list.

    The payload carries the ROW ID, never the word. Two reasons, and
    both bit legacy (``callback_data=f"moderation_del_{word}"``,
    bot.py:32386): Telegram caps ``callback_data`` at 64 bytes, so a
    long or Cyrillic word makes the button impossible to build at all;
    and the handler re-derives the group from the card's own chat, so an
    id is checked against ``(group_id, id)`` and a stale or copied id
    simply matches no row.
    """

    word_id: DbInt


def is_known_section(token: str) -> bool:
    """True iff ``token`` is one of the published ``SECTION_*`` values."""
    return token in _SECTIONS


def resolve_page(token: str) -> str:
    """Map a callback token to the page to render.

    Anything that is not an explicit ``PAGE_*`` token — including every
    overview section button and any forged value — resolves to the
    overview. This is the "unknown token degrades to a full refresh"
    rule, kept in one function so both the handler and its tests read it
    from the same place.
    """
    return token if token in _PAGES else PAGE_HOME


# ---------------------------------------------------------------------------
# Settings page: the fields a button may write
# ---------------------------------------------------------------------------

# Short wire token → ``group_mod_config`` column. The six booleans
# legacy exposed as toggles (automod / profanity / autoban, bot.py:30848-
# 30857) plus the three the port added (antiflood, captcha, coins), which
# had no legacy equivalent at all.
TOGGLE_FIELDS: Final[dict[str, str]] = {
    "auto": "automod_enabled",
    "prof": "profanity_enabled",
    "aban": "autoban_enabled",
    "flood": "antiflood_enabled",
    "capt": "captcha_enabled",
    "coins": "coins_enabled",
}

# Short wire token → numeric ``group_mod_config`` column.
PICKER_FIELDS: Final[dict[str, str]] = {
    "warns": "max_warns",
    "mute": "mute_minutes",
}

# Legacy's pickers offered 1..10 warnings (bot.py:30913) and
# 1/3/6/12/24/48/72/168 hours of mute (bot.py:30949). The warn choices
# carry over as-is; the mute choices are the same hours expressed in the
# minutes our column stores.
WARN_CHOICES: Final[tuple[int, ...]] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
MUTE_CHOICES_MINUTES: Final[tuple[int, ...]] = (
    60,
    180,
    360,
    720,
    1440,
    2880,
    4320,
    10080,
)

_PICKER_CHOICES: Final[dict[str, tuple[int, ...]]] = {
    "warns": WARN_CHOICES,
    "mute": MUTE_CHOICES_MINUTES,
}


def picker_choices(token: str) -> tuple[int, ...]:
    """Allowed values for a picker token — empty for an unknown token."""
    return _PICKER_CHOICES.get(token, ())


def format_mute_choice(minutes: int, lang: str) -> str:
    """Button label for a mute duration: hours under a day, else days.

    Mirrors legacy's ``f"{hours}ч" if hours < 24 else f"{days}д"``
    (bot.py:30951) — a "168ч" button is harder to read than "7д".
    """
    hours = minutes // 60
    if hours < 24:
        return t("h_ga_unit_hours", lang, value=hours)
    return t("h_ga_unit_days", lang, value=hours // 24)


# ---------------------------------------------------------------------------
# Markup builders
# ---------------------------------------------------------------------------


def build_card_markup(lang: str) -> InlineKeyboardMarkup:
    """Overview card: per-section refreshes, then the sub-page nav.

    The section buttons keep their historical meaning (re-read that
    subsystem's store and re-render); the nav row below them is RR-4 #35
    — the three sub-pages legacy had and the port had lost.
    """
    builder = InlineKeyboardBuilder()
    section_buttons = (
        (t("h_ga_btn_mod", lang), SECTION_MOD),
        (t("h_ga_btn_filter", lang), SECTION_FILTER),
        (t("h_ga_btn_welcome", lang), SECTION_WELCOME),
        (t("h_ga_btn_aliases", lang), SECTION_ALIASES),
        (t("h_ga_btn_treasury", lang), SECTION_TREASURY),
        (t("h_ga_btn_rules", lang), SECTION_RULES),
        (t("h_ga_btn_settings", lang), PAGE_SETTINGS),
        (t("h_ga_btn_stats", lang), PAGE_STATS),
        (t("h_ga_btn_words", lang), PAGE_WORDS),
        (t("h_ga_btn_staff", lang), PAGE_STAFF),
    )
    for label, token in section_buttons:
        builder.button(text=label, callback_data=GroupAdminRefresh(section=token))
    builder.button(
        text=t("h_ga_btn_refresh", lang),
        callback_data=GroupAdminRefresh(section=SECTION_ALL),
    )
    # Six section buttons in pairs, then the four sub-pages two per row
    # (four across is unreadable on a phone), refresh alone at the
    # bottom.
    builder.adjust(2, 2, 2, 2, 2, 1)
    return builder.as_markup()


def build_settings_markup(
    *,
    lang: str,
    automod: bool,
    profanity: bool,
    autoban: bool,
    antiflood: bool,
    captcha: bool,
    coins: bool,
    max_warns: int,
    mute_minutes: int,
) -> InlineKeyboardMarkup:
    """Settings page: six toggles, two pickers, back to the overview.

    Every toggle label carries its CURRENT state (legacy did the same,
    bot.py:30850) so the button is both the readout and the control —
    tapping it writes the opposite value.
    """
    builder = InlineKeyboardBuilder()
    toggles = (
        ("h_ga_tgl_automod", "auto", automod),
        ("h_ga_tgl_profanity", "prof", profanity),
        ("h_ga_tgl_autoban", "aban", autoban),
        ("h_ga_tgl_antiflood", "flood", antiflood),
        ("h_ga_tgl_captcha", "capt", captcha),
        ("h_ga_tgl_coins", "coins", coins),
    )
    for key, token, value in toggles:
        builder.button(
            text=t(key, lang, mark=state_mark(value)),
            # Tapping writes the OPPOSITE of what the label shows.
            callback_data=GroupAdminSet(field=token, value=int(not value)),
        )
    builder.button(
        text=t("h_ga_pick_warns", lang, value=max_warns),
        callback_data=GroupAdminPick(field="warns"),
    )
    builder.button(
        text=t("h_ga_pick_mute", lang, value=format_mute_choice(mute_minutes, lang)),
        callback_data=GroupAdminPick(field="mute"),
    )
    builder.button(
        text=t("h_ga_btn_back", lang),
        callback_data=GroupAdminRefresh(section=PAGE_HOME),
    )
    builder.adjust(2, 2, 2, 1, 1, 1)
    return builder.as_markup()


def build_picker_markup(picker: str, lang: str) -> InlineKeyboardMarkup:
    """Value grid for one numeric field, plus back to Settings."""
    builder = InlineKeyboardBuilder()
    choices = picker_choices(picker)
    is_mute = picker == "mute"
    for value in choices:
        label = format_mute_choice(value, lang) if is_mute else str(value)
        builder.button(text=label, callback_data=GroupAdminSet(field=picker, value=value))
    # Legacy laid both grids out 3-wide (bot.py:30912/30948); 4 fits our
    # shorter labels. ``adjust`` is applied to the CHOICES only — the
    # trailing width would otherwise be swallowed whenever the choice
    # count is not a multiple of 4, dropping "back" into the last row of
    # numbers where a mis-tap costs a write.
    builder.adjust(*([4] * ((len(choices) + 3) // 4)))
    builder.row(
        InlineKeyboardButton(
            text=t("h_ga_btn_back", lang),
            callback_data=GroupAdminRefresh(section=PAGE_SETTINGS).pack(),
        )
    )
    return builder.as_markup()


def build_staff_markup(lang: str, *, can_manage: bool) -> InlineKeyboardMarkup:
    """Staff page: roster actions, refresh, back to the overview.

    ``can_manage`` gates the two write buttons. Whether the actor may
    change ranks is a STRONGER condition than whether they may open the
    panel (the panel opens for any group admin; rank changes need
    ``can_manage_mods``), so an actor who cannot grant sees the roster
    without controls rather than buttons that always refuse.

    The gate here is cosmetic — the handlers re-check it on every tap.
    Hiding a button is never a security boundary; it just stops the
    panel from advertising an action the tapper cannot take.
    """
    builder = InlineKeyboardBuilder()
    widths: list[int] = []
    if can_manage:
        builder.button(text=t("h_ga_staff_btn_add", lang), callback_data=GroupAdminStaffAdd())
        builder.button(
            text=t("h_ga_staff_btn_drop", lang),
            callback_data=GroupAdminRefresh(section=PAGE_STAFF_DROP),
        )
        widths.append(2)
    builder.button(
        text=t("h_ga_btn_refresh", lang),
        callback_data=GroupAdminRefresh(section=PAGE_STAFF),
    )
    builder.button(
        text=t("h_ga_btn_back", lang),
        callback_data=GroupAdminRefresh(section=PAGE_HOME),
    )
    widths.append(2)
    builder.adjust(*widths)
    return builder.as_markup()


def build_staff_drop_markup(people: Sequence[tuple[int, str]], lang: str) -> InlineKeyboardMarkup:
    """One demote button per removable person, then back to the roster.

    ``people`` is ``(user_id, label)`` already filtered by the handler to
    those the ACTOR may actually demote — legacy made the admin type a
    raw numeric user id into the chat (bot.py:31201), which is both
    worse to use and easy to typo into someone else's account.

    Labels come from Telegram/our users table and are rendered as button
    text, which Telegram treats as plain text — no escaping needed here,
    unlike the message body.
    """
    builder = InlineKeyboardBuilder()
    for user_id, label in people:
        builder.button(text=label, callback_data=GroupAdminStaffDrop(user_id=user_id))
    builder.button(
        text=t("h_ga_btn_back", lang),
        callback_data=GroupAdminRefresh(section=PAGE_STAFF),
    )
    # One person per row: names are long and a mis-tap here strips
    # somebody's rank.
    builder.adjust(*([1] * len(people)), 1)
    return builder.as_markup()


# ---------------------------------------------------------------------------
# Words page (RR-4 #43)
# ---------------------------------------------------------------------------

#: Longest word a delete button spells out in full. Telegram wraps a
#: long label into an unreadably tall button, and the list preview above
#: the grid already shows every word at full length.
WORD_BUTTON_MAX_LEN: Final[int] = 24


def word_button_label(word: str) -> str:
    """Button text for one filter word — clipped, never empty.

    Plain text as far as Telegram is concerned (button labels are not
    parsed as HTML), so the word goes in as stored.
    """
    if len(word) <= WORD_BUTTON_MAX_LEN:
        return word
    return word[: WORD_BUTTON_MAX_LEN - 1] + "…"


def build_words_markup(lang: str, *, has_words: bool) -> InlineKeyboardMarkup:
    """Words page: add / remove, refresh, back to the overview.

    ``has_words`` hides the ➖ button on an empty list — legacy showed it
    always and answered the tap with "nothing to delete"
    (bot.py:32375), which is a round trip to learn what the page above
    already said.
    """
    builder = InlineKeyboardBuilder()
    widths: list[int] = []
    builder.button(text=t("h_ga_words_btn_add", lang), callback_data=GroupAdminWordAdd())
    if has_words:
        builder.button(
            text=t("h_ga_words_btn_drop", lang),
            callback_data=GroupAdminRefresh(section=PAGE_WORDS_DROP),
        )
        widths.append(2)
    else:
        widths.append(1)
    builder.button(
        text=t("h_ga_btn_refresh", lang),
        callback_data=GroupAdminRefresh(section=PAGE_WORDS),
    )
    builder.button(
        text=t("h_ga_btn_back", lang),
        callback_data=GroupAdminRefresh(section=PAGE_HOME),
    )
    widths.append(2)
    builder.adjust(*widths)
    return builder.as_markup()


def build_words_drop_markup(words: Sequence[tuple[int, str]], lang: str) -> InlineKeyboardMarkup:
    """One delete button per word, then back to the Words page.

    ``words`` is ``(row_id, word)`` — already capped by the handler to
    the words the page above it previews, so everything the admin can
    see, they can tap.

    Two per row rather than legacy's three (bot.py:32384): a banned word
    is often a whole phrase, and three of those across a phone screen
    wrap into a wall of text.
    """
    builder = InlineKeyboardBuilder()
    for word_id, word in words:
        builder.button(
            text=word_button_label(word),
            callback_data=GroupAdminWordDrop(word_id=word_id),
        )
    builder.adjust(*([2] * ((len(words) + 1) // 2)))
    builder.row(
        InlineKeyboardButton(
            text=t("h_ga_btn_back", lang),
            callback_data=GroupAdminRefresh(section=PAGE_WORDS).pack(),
        )
    )
    return builder.as_markup()


def build_subpage_markup(page: str, lang: str) -> InlineKeyboardMarkup:
    """Refresh + back — the read-only sub-page keyboard.

    Used by Stats, and by any page whose read failed: offering write
    controls over data we could not load would mean acting on nothing.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("h_ga_btn_refresh", lang),
        callback_data=GroupAdminRefresh(section=page),
    )
    builder.button(
        text=t("h_ga_btn_back", lang),
        callback_data=GroupAdminRefresh(section=PAGE_HOME),
    )
    builder.adjust(2)
    return builder.as_markup()
