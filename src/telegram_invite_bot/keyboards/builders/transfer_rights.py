"""CallbackData factories + keyboards for ``/transfer_rights`` (L-49).

Legacy ``cmd_transfer_rights`` (bot.py:41603) rendered a single
confirm/cancel pair over raw string callbacks
(``transfer_rights_confirm_<token>``); the group-picker step didn't
exist because legacy transferred the bot's single global owner. The new
pipeline transfers the per-group attribution
(``bot_groups.added_by_user_id``), so the flow grows a picker that
mirrors the ``/mygroups`` list (same page size, same stable chat_id
ordering).

Three factories:

* :class:`TransferPick` (``trpick``) — caller tapped one of THEIR
  groups in the picker. No owner id in the payload: the handler
  re-checks ``added_by_user_id == callback.from_user.id``, so a forged
  payload can't start a transfer of someone else's group.
* :class:`TransferPage` (``trpage``) — picker pagination, 1-based.
* :class:`TransferDecision` (``trconf``) — the final confirm/cancel
  pair. Carries ``owner_id`` (checks.py pattern) so a forwarded card
  can't be confirmed by anyone but the initiating owner; ``ok`` is the
  confirm/cancel axis (one factory, two registrations via
  ``TransferDecision.filter(F.ok)`` / ``filter(~F.ok)``).
"""

from __future__ import annotations

from math import ceil

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telegram_invite_bot.core.callback_fields import DbInt
from telegram_invite_bot.i18n import t

# Picker rows per page — mirrors /mygroups (legacy had no picker at all).
PAGE_SIZE = 10

# Keep 🔁-prefix + title under Telegram's 64-char button-label limit.
_BTN_TITLE_TRUNC = 35


class TransferPick(CallbackData, prefix="trpick"):
    """Caller picked ``group_id`` in the transfer picker."""

    group_id: DbInt


class TransferPage(CallbackData, prefix="trpage"):
    """Picker page navigation. ``page`` is 1-based."""

    page: DbInt


class TransferDecision(CallbackData, prefix="trconf"):
    """Confirm (``ok=True``) or cancel (``ok=False``) the transfer.

    ``owner_id`` pins the card to the initiating user — anyone else
    tapping (forwarded message, forged payload) gets the foreign-tap
    alert instead of driving the flow.
    """

    owner_id: DbInt
    ok: bool


def total_pages(total: int) -> int:
    """Number of picker pages for ``total`` rows (at least 1)."""
    return max(1, ceil(total / PAGE_SIZE))


def build_pick_markup(
    lang: str,
    *,
    rows: list[tuple[int, str | None]],
    page: int,
    total: int,
    owner_id: int,
) -> InlineKeyboardMarkup:
    """One button per visible group + nav row (when paginated) + cancel.

    ``rows`` is the current page's slice — ``(chat_id, title)`` pairs;
    a missing title falls back to the chat id so the button is never
    blank. The cancel row uses :class:`TransferDecision` with
    ``ok=False`` — the same handler that cancels the confirm card, so
    aborting works at every step of the flow.
    """
    builder = InlineKeyboardBuilder()
    for chat_id, title in rows:
        label = f"🔁 {(title or str(chat_id))[:_BTN_TITLE_TRUNC]}"
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=TransferPick(group_id=chat_id).pack(),
            )
        )
    pages = total_pages(total)
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(text="⬅️", callback_data=TransferPage(page=page - 1).pack())
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page}/{pages}",
                callback_data=TransferPage(page=page).pack(),
            )
        )
        if page < pages:
            nav.append(
                InlineKeyboardButton(text="➡️", callback_data=TransferPage(page=page + 1).pack())
            )
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(
            text=t("h_trights_cancel_btn", lang),
            callback_data=TransferDecision(owner_id=owner_id, ok=False).pack(),
        )
    )
    return builder.as_markup()


def build_confirm_markup(lang: str, *, owner_id: int) -> InlineKeyboardMarkup:
    """The confirm card's ✅/❌ row (legacy bot.py:41672-41676)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("h_trights_confirm_btn", lang),
            callback_data=TransferDecision(owner_id=owner_id, ok=True).pack(),
        ),
        InlineKeyboardButton(
            text=t("h_trights_cancel_btn", lang),
            callback_data=TransferDecision(owner_id=owner_id, ok=False).pack(),
        ),
    )
    return builder.as_markup()
