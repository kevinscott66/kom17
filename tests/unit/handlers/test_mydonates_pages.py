"""``/mydonates`` must fit inside Telegram's 4096-character ceiling.

``_HISTORY_LIMIT`` caps the row COUNT at 25, which reads like a bound
until you notice nothing caps a row's WIDTH. The group name is the chat
title, Telegram allows 128 characters of it, and 25 rows of that shape
render 4095 characters — one under the ceiling. A seven-digit amount
takes it over; so does a single emoji in a title, because Telegram
counts UTF-16 units and an astral character costs two while ``len()``
charges one.

Past the ceiling ``answer`` comes back a 400 and the user — who ran the
command in their own DM, with nobody else to notice — gets no reply at
all, on the exact card that tells them where their money went.

What these tests pin:

* the everyday card still goes out as a single message, empty history
  included — pagination must not turn it into a burst;
* a full history of long names splits, every page fits when measured
  the way Telegram measures, and no donation is lost;
* emoji names are budgeted in UTF-16 units, not code points.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from telegram_invite_bot.handlers.mydonates import _HISTORY_LIMIT, _render
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

#: ``• Альфа — 100 🪙 (2026-01-01 12:00)``
_ROW = re.compile(r"^• (?P<name>.+) — (?P<amount>\d+) 🪙 \(")

_TS = datetime(2026, 1, 1, 12, 0)


def _rows(pages: list[str]) -> list[tuple[str, str]]:
    """Every rendered donation row, in order, across all pages."""
    return [
        (m["name"], m["amount"])
        for page in pages
        for line in page.splitlines()
        if (m := _ROW.match(line))
    ]


def _history(
    count: int = _HISTORY_LIMIT,
    *,
    name: str = "я" * 128,
    amount: int = 999_999,
) -> list[tuple[int, int, datetime | None, str | None]]:
    """A full page of donations to groups with maximum-length titles.

    128 characters is what Telegram itself allows in a chat title, so
    this is a legitimate history, not an abuse case.
    """
    return [(-100 - i, amount, _TS, f"{name}{i:02d}") for i in range(count)]


def test_the_ordinary_card_still_goes_out_as_one_message() -> None:
    """Two donations to normally-named groups: one message, blank line
    between the lifetime total and the first row, as before."""
    pages = _render("ru", total=350, rows=[(-100, 250, _TS, "Бета"), (-200, 100, _TS, "Альфа")])

    assert len(pages) == 1
    assert pages[0] == (
        "💸 <b>Мои донаты</b>\n\n"
        "Всего задоначено: <b>350</b> 🪙\n\n"
        "• Бета — 250 🪙 (2026-01-01 12:00)\n"
        "• Альфа — 100 🪙 (2026-01-01 12:00)"
    )


def test_empty_history_still_renders_the_zero_total_card() -> None:
    """The empty branch never paginated and must not start now — a user
    with no donations gets one message showing the zero total."""
    pages = _render("ru", total=0, rows=[])

    assert pages == ["💸 <b>Мои донаты</b>\n\nВсего задоначено: <b>0</b> 🪙\n\nИстория пуста."]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_a_full_history_of_long_names_is_split_and_loses_nothing(lang: str) -> None:
    """25 donations to 128-character groups is a 4100-character 400."""
    rows = _history()

    pages = _render(lang, total=1_000_000, rows=rows)

    assert sum(parsed_length(page) for page in pages) > TELEGRAM_TEXT_LIMIT, (
        "fixture must exceed the ceiling — otherwise this proves nothing"
    )
    assert len(pages) > 1
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)
    # Every donation survives the split, in the order the query returned.
    assert _rows(pages) == [(str(name), str(amount)) for _, amount, _, name in rows]


def test_emoji_names_are_budgeted_in_utf16_units() -> None:
    """An emoji costs Telegram two units and ``len()`` one.

    Budgeting on code points would call this history a comfortable
    3400 characters and send it as one message Telegram measures at
    5900 — the very 400 the pagination exists to prevent. 100 emoji is
    well inside the 128-character title Telegram lets a group have.
    """
    rows = _history(name="😀" * 100)

    pages = _render("ru", total=1, rows=rows)

    assert sum(len(page) for page in pages) < TELEGRAM_TEXT_LIMIT, (
        "fixture must look harmless to a code-point count — that's the point"
    )
    assert len(pages) > 1
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)


def test_a_pathological_history_stays_bounded_and_says_so() -> None:
    """The row cap is a query LIMIT, not an invariant of ``_render``.

    Should a caller ever hand it an unbounded list, the answer is a
    bounded burst with a "and N more" tail — not one message per
    donation.
    """
    rows = _history(1000, name="я" * 128)

    pages = _render("ru", total=1, rows=rows)

    assert len(pages) <= 5
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)
    dropped = len(rows) - len(_rows(pages))
    assert dropped > 0
    assert t("h_mydonates_history_more", "ru", count=dropped) in pages[-1]
