"""``/currency`` display-currency picker keyboards — RR-6 #66.

Legacy ``/currency`` (bot.py:17633) was an interactive setter: a grid of
every supported currency, three per row, a ✅ on the active one, and a tap
that wrote ``economy.users.display_currency`` and answered with a card of
worked examples. The monolith→split port kept the command name but turned
it into a read-only rate list, so the preference became unreachable from
the new pipeline while the live legacy bot kept honouring it.

This module owns the two keyboards that bring the setter back:

* :func:`build_picker_markup` — the currency grid plus a way into the
  full rate table and (in private) home.
* :func:`build_back_markup` — the way back to the grid, shared by the
  rate table and the post-tap confirmation.

Strangler invariant (same posture as
:mod:`~keyboards.builders.ai_controls`): the legacy callback literals
``set_currency_<CODE>``, ``currency_menu``, ``start_menu_profile`` and
``main_menu`` have had no process behind them since T-011, but they are
still on keyboards legacy sent and a tap on one is still delivered. Every
prefix here is a distinct ``cur_*`` so such a tap resolves to nothing —
and, since ``set_currency_<CODE>`` writes a real preference, so that it
cannot resolve to a write this pipeline never authored.

No user id rides in any payload — the write is keyed off
``callback.from_user.id``, so a forged payload only ever moves the
attacker's own preference. The currency code does ride in
:class:`CurrencyPick`, and is validated against ``AVAILABLE_CURRENCIES``
at the handler before it reaches the repo; an unknown code is refused
rather than stored, which matters because the LIVE bot reads the same
column and a junk value there would degrade ITS rendering too.

Divergence from legacy, deliberate: these keyboards are attached in
PRIVATE chats only. Legacy showed the grid in groups, where the card is
shared but the preference is per-user — so one member's tap re-drew the
✅ for everyone and the card became a lie for every other reader. In a
group ``/currency`` answers with the rate table instead and points at DM.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.services.currency_service import (
    AVAILABLE_CURRENCIES,
    display_code,
)

# Legacy packed three currency buttons per row; with 16 codes that is a
# tidy 5×3+1 grid that still fits a narrow phone without truncating the
# ``<emoji> <CODE>`` label.
_CODES_PER_ROW = 3


class CurrencyPick(CallbackData, prefix="cur_set"):
    """Store ``code`` as the tapping user's display currency."""

    code: str


class CurrencyRates(CallbackData, prefix="cur_rates"):
    """Swap the picker for the full ``1 COM = …`` rate table."""


class CurrencyBack(CallbackData, prefix="cur_back"):
    """Return to the picker from the rate table or the confirmation."""


def build_picker_markup(lang: str, current: str) -> InlineKeyboardMarkup:
    """The currency grid, with ``current`` check-marked.

    ``current`` is the EFFECTIVE code (what amounts actually render in),
    not the raw stored one — otherwise an English user whose row still
    says ``RUB`` would see the ✅ on a currency the bot is not using. The
    card text explains the gap when the two differ.
    """
    builder = InlineKeyboardBuilder()
    for code, meta in AVAILABLE_CURRENCIES.items():
        # The BUTTON shows the visible ticker; the callback still
        # carries the stored code, so the picker keeps writing ``COM``.
        label = f"{meta['emoji']} {display_code(code)}"
        builder.button(
            text=f"✅ {label}" if code == current else label,
            callback_data=CurrencyPick(code=code),
        )
    full_rows, remainder = divmod(len(AVAILABLE_CURRENCIES), _CODES_PER_ROW)
    sizes = [_CODES_PER_ROW] * full_rows
    if remainder:
        sizes.append(remainder)

    builder.button(text=t("h_cur_btn_rates", lang), callback_data=CurrencyRates())
    builder.button(text=t("h_cur_btn_home", lang), callback_data=MainMenu(action="home"))
    sizes += [1, 1]

    builder.adjust(*sizes)
    return builder.as_markup()


def build_back_markup(lang: str) -> InlineKeyboardMarkup:
    """Back-to-the-picker (and home).

    Shared by the rate table and the post-tap confirmation: both are
    leaves of the picker, and giving them the same footer means a user
    never has to learn two ways out of the same card.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("h_cur_btn_back", lang), callback_data=CurrencyBack())
    builder.button(text=t("h_cur_btn_home", lang), callback_data=MainMenu(action="home"))
    builder.adjust(1)
    return builder.as_markup()
