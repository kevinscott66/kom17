"""``/currency`` picker keyboards — RR-6 #66.

Three contracts live here, and they fail in three different ways:

* **Strangler safety** — the legacy telebot process is still running and
  still owns ``set_currency_<CODE>`` / ``currency_menu``. A prefix
  collision would let one bot swallow the other's taps, which is
  invisible in production until a user reports "the button does
  nothing".
* **Truthfulness of the ✅** — the check-mark must follow the currency
  amounts are actually rendered in, not the raw stored cell.
* **Translation coverage** — ``t()`` returns the raw key when a
  translation is missing, so a button reading ``h_cur_btn_rates`` is
  what a missing key looks like on a real keyboard.
"""

from __future__ import annotations

import re

import pytest

from telegram_invite_bot.keyboards.builders.currency import (
    CurrencyBack,
    CurrencyPick,
    CurrencyRates,
    build_back_markup,
    build_picker_markup,
)
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.services.currency_service import AVAILABLE_CURRENCIES

# Every callback literal legacy's telebot still registers for this
# surface (bot.py:17632-17722). None of ours may start with one of them.
_LEGACY_PREFIXES = ("set_currency_", "currency_menu", "start_menu_profile", "main_menu")


def _labels(markup: object) -> list[str]:
    return [btn.text for row in markup.inline_keyboard for btn in row]  # type: ignore[attr-defined]


def _payloads(markup: object) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]  # type: ignore[attr-defined]


def test_prefixes_do_not_collide_with_the_live_legacy_bot() -> None:
    payloads = [
        CurrencyPick(code="USD").pack(),
        CurrencyRates().pack(),
        CurrencyBack().pack(),
    ]
    for payload in payloads:
        assert not any(payload.startswith(legacy) for legacy in _LEGACY_PREFIXES), payload
    # ...and are distinct from each other, so aiogram's first-match
    # dispatch can't route a "back" tap into the setter.
    assert len(set(payloads)) == len(payloads)


def test_pick_payload_carries_no_user_id() -> None:
    """A forged payload may only ever move the tapper's own preference."""
    packed = CurrencyPick(code="USD").pack()
    assert packed.split(":") == ["cur_set", "USD"]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_picker_offers_every_supported_currency_once(lang: str) -> None:
    markup = build_picker_markup(lang, "USD")
    codes = [CurrencyPick.unpack(p).code for p in _payloads(markup) if p.startswith("cur_set:")]
    assert codes == list(AVAILABLE_CURRENCIES)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_picker_rows_are_three_wide_plus_a_one_wide_footer(lang: str) -> None:
    rows = build_picker_markup(lang, "USD").inline_keyboard
    currency_rows = [row for row in rows if row[0].callback_data.startswith("cur_set:")]
    assert all(len(row) <= 3 for row in currency_rows)
    # The footer is two full-width buttons: all rates, then home.
    assert [len(row) for row in rows[-2:]] == [1, 1]
    assert rows[-1][0].callback_data == MainMenu(action="home").pack()


def test_exactly_one_check_mark_and_it_sits_on_the_current_code() -> None:
    labels = _labels(build_picker_markup("ru", "TON"))
    marked = [label for label in labels if label.startswith("✅")]
    assert len(marked) == 1
    assert marked[0].endswith(" TON")


def test_an_unsupported_current_code_marks_nothing() -> None:
    """Defensive: a stale cell must not crash the card, only lose the ✅."""
    labels = _labels(build_picker_markup("ru", "XXX"))
    assert not any(label.startswith("✅") for label in labels)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_no_button_shows_a_raw_translation_key(lang: str) -> None:
    labels = _labels(build_picker_markup(lang, "USD")) + _labels(build_back_markup(lang))
    assert not any(label.startswith("h_") for label in labels)


def test_english_keyboard_has_no_cyrillic() -> None:
    """en.yaml parity: a Russian word on an English button is a leak."""
    labels = _labels(build_picker_markup("en", "USD")) + _labels(build_back_markup("en"))
    leaked = [label for label in labels if re.search("[А-Яа-яЁё]", label)]
    assert leaked == []


def test_back_markup_leads_back_to_the_picker_and_home() -> None:
    payloads = _payloads(build_back_markup("ru"))
    assert payloads == [CurrencyBack().pack(), MainMenu(action="home").pack()]
