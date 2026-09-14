"""``/group_stats`` must fit inside Telegram's 4096-character ceiling.

The card renders one line per day of ``StatsConfig.period_days``, and
that field is operator config declared ``ge=1, le=365``. A day line is
about twenty characters, so the validator's own upper half — anything
past roughly 200 days — is a message Telegram refuses: ``answer``
returns 400 and ``/group_stats`` answers nothing at all. The default of
7 hides it completely, which is why this is pinned on the pure renderer
rather than left to the e2e fixture.

What these tests pin:

* the default 7-day card is still one message, with the blank line
  between the header and the first day;
* the whole declared range renders — 365 days splits into pages that
  each fit, and not one day is dropped;
* the empty-window card keeps legacy's exact copy and stays single.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from telegram_invite_bot.config.settings import StatsConfig
from telegram_invite_bot.handlers.stats import _format
from telegram_invite_bot.repositories.message_stats_repo import DailyCount
from telegram_invite_bot.utils.render import PAGE_MAX, TELEGRAM_TEXT_LIMIT, parsed_length

#: ``• 2026-08-13: 12345``
_ROW = re.compile(r"^• (?P<date>\d{4}-\d{2}-\d{2}): (?P<count>\d+)$")

_TODAY = date(2026, 8, 13)


def _days(count: int, *, per_day: int = 12_345) -> list[DailyCount]:
    """A dense window, newest first — the shape ``chat_totals_by_date``
    returns for a chat that was busy every single day."""
    return [
        DailyCount(date=(_TODAY - timedelta(days=offset)).isoformat(), count=per_day)
        for offset in range(count)
    ]


def _rows(pages: list[str]) -> list[tuple[str, str]]:
    return [
        (m["date"], m["count"])
        for page in pages
        for line in page.splitlines()
        if (m := _ROW.match(line))
    ]


def test_the_default_week_is_still_a_single_message() -> None:
    """Seven days is the default window and the one every operator
    actually sees — it must render exactly as it did before paging."""
    pages = _format(_days(2, per_day=7), days=7, lang="ru")

    assert pages == [
        "📊 <b>Активность за 7 дн.</b> Всего сообщений: <b>14</b>\n"
        "\n"
        "• 2026-08-13: 7\n"
        "• 2026-08-12: 7"
    ]


def test_the_empty_window_keeps_legacy_copy_in_one_message() -> None:
    """Legacy's "сообщений нет" line is pinned byte-for-byte elsewhere;
    here the point is that the empty branch never becomes a burst."""
    pages = _format([], days=7, lang="ru")

    assert pages == [
        "За последние 7 дн. сообщений нет. Данные собираются при включённой настройке в группе."
    ]


def test_the_whole_declared_period_range_renders() -> None:
    """``STATS_PERIOD_DAYS`` is validated ``le=365``; 365 day-lines is
    ~7300 characters, i.e. a silent 400 on a value the operator was
    explicitly allowed to set. Every day must survive the split."""
    limit = StatsConfig.model_fields["period_days"].metadata
    assert any(getattr(rule, "le", None) == 365 for rule in limit), (
        "the validator's upper bound moved — re-derive what this test guards"
    )
    rows = _days(365)

    pages = _format(rows, days=365, lang="ru")

    assert sum(parsed_length(page) for page in pages) > TELEGRAM_TEXT_LIMIT, (
        "fixture must exceed the ceiling — otherwise this proves nothing"
    )
    assert len(pages) > 1
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)
    assert _rows(pages) == [(row.date, str(row.count)) for row in rows]


def test_the_total_counts_the_window_not_the_first_page() -> None:
    """The header lives on page one but sums the whole window — a total
    computed per page would understate a long window."""
    pages = _format(_days(365, per_day=10), days=365, lang="ru")

    assert "Всего сообщений: <b>3650</b>" in pages[0]


def test_the_overflow_footer_says_how_many_days_were_dropped() -> None:
    """``paginate_lines`` caps at ``PAGE_MAX`` pages and appends a
    ``more_line`` for whatever didn't fit. The declared ``le=365`` window
    fits inside that cap, so the branch is unreachable through the
    handler — it is the guard for a future wider window, and it renders
    its own copy, so it gets its own pin here rather than an e2e test
    that cannot reach it.
    """
    rows = _days(1200)

    pages = _format(rows, days=1200, lang="ru")

    assert len(pages) == PAGE_MAX
    rendered = _rows(pages)
    assert len(rendered) < len(rows), "fixture must overflow — otherwise this proves nothing"
    dropped = len(rows) - len(rendered)
    assert f"…и ещё {dropped} дн." in pages[-1]


def test_the_overflow_footer_follows_the_callers_language() -> None:
    """Same branch, English caller — the footer is built by a lambda the
    translator never sees from the header, so it can be missed."""
    pages = _format(_days(1200), days=1200, lang="en")

    tail = pages[-1]
    assert "more d." in tail
    assert not any("Ѐ" <= ch <= "ӿ" for ch in tail), tail
