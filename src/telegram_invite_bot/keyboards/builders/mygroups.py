"""CallbackData factories + keyboards for the ``/mygroups`` panel (L-58).

Legacy ``send_mygroups_panel`` (bot.py:24791) rendered each group as a
``t.me`` URL button (public groups only) plus a "back to menu" row; the
per-group admin tree lived behind separate raw-string callbacks. The
new pipeline replaces the URL buttons with typed callbacks that open a
read-only per-group summary card in place, so private groups (no
username) are tappable too and no ``get_chat`` round-trip is needed at
render time.

Two factories, two prefixes:

* :class:`MyGroupsPage` (``mygrp``) — list pagination. ``page`` is
  1-based; tapping the current-page indicator is a no-op re-render.
* :class:`MyGroupsCard` (``mygrc``) — open one group's summary card.
  ``page`` carries the list page the tap came from so the card's
  "back to list" button restores the exact page, not page 1.

No owner id is carried in the payload — the handler re-scopes every
callback by ``bot_groups.added_by_user_id == callback.from_user.id``,
so a forged/stale callback can never open another user's card.
"""

from __future__ import annotations

from math import ceil

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telegram_invite_bot.core.callback_fields import DbInt
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu

# Rows per list page. Mirrors the rating leaderboard's page size; the
# legacy panel hard-capped at 10 buttons with no pagination at all.
PAGE_SIZE = 10

# Button labels are capped well under Telegram's 64-char limit so the
# 🛡 prefix + a long group title can't overflow.
_BTN_TITLE_TRUNC = 35


class MyGroupsPage(CallbackData, prefix="mygrp"):
    """A ``/mygroups`` list page navigation tap. ``page`` is 1-based."""

    page: DbInt


class MyGroupsCard(CallbackData, prefix="mygrc"):
    """Open one group's read-only summary card from the list.

    ``page`` is the list page the tap originated from — the card's
    back button restores it.
    """

    group_id: DbInt
    page: DbInt


def total_pages(total: int) -> int:
    """Number of list pages for ``total`` rows (at least 1)."""
    return max(1, ceil(total / PAGE_SIZE))


def build_list_markup(
    lang: str,
    *,
    rows: list[tuple[int, str | None]],
    page: int,
    total: int,
) -> InlineKeyboardMarkup:
    """One button per visible group + nav row (when paginated) + menu.

    ``rows`` is the current page's slice — (chat_id, title) pairs. A
    missing title falls back to the chat id so the button is never
    blank. The back-to-menu row mirrors the legacy panel's last row;
    the ``MainMenu`` callback router is private-only, which matches
    this command's private-only surface.
    """
    builder = InlineKeyboardBuilder()
    for chat_id, title in rows:
        label = f"🛡 {(title or str(chat_id))[:_BTN_TITLE_TRUNC]}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=MyGroupsCard(group_id=chat_id, page=page).pack(),
            )
        )
    pages = total_pages(total)
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(text="⬅️", callback_data=MyGroupsPage(page=page - 1).pack())
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page}/{pages}",
                callback_data=MyGroupsPage(page=page).pack(),
            )
        )
        if page < pages:
            nav.append(
                InlineKeyboardButton(text="➡️", callback_data=MyGroupsPage(page=page + 1).pack())
            )
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(
            text=t("back_to_menu", lang),
            callback_data=MainMenu(action="home").pack(),
        )
    )
    return builder.as_markup()


def build_card_markup(lang: str, *, page: int) -> InlineKeyboardMarkup:
    """The summary card's single row: back to the originating list page."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("h_mygroups_back_to_list", lang),
            callback_data=MyGroupsPage(page=page).pack(),
        )
    )
    return builder.as_markup()
