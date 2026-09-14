"""#1957: ``/vip`` and ``/profile`` must print the same VIP expiry.

Both cards read the SAME ``users.vip_till`` — a unix timestamp — so the
stored value was never in question. What differed was the frame each
one rendered it in: ``handlers/vip.py`` formatted in UTC while
``handlers/profile.py::_vip_line`` formats in
``ZoneInfo(stats_config.timezone)``. On the prod host (MSK, UTC+3) a
grant expiring between 00:00 and 03:00 local therefore printed one
calendar date under ``/vip`` and the next one under ``/profile``.

The day count disagreed on top of that: ``max(1, ceil(remaining /
86400))`` against a calendar-date difference, so the last day of a
grant read "1 дн." on one card and "0 дн." on the other.

Legacy settles both — ``bot.py:40240`` formats with a naive
``fromtimestamp`` (host local, i.e. MSK) and floors the remainder — so
UTC and the ``ceil`` were the outliers, and ``/vip`` is what moved.

The clock is left running rather than frozen: every instant below is
derived from the real ``now`` in the configured zone, and the
assertions are about the two cards AGREEING, which is the invariant
regardless of when the suite runs. Only the first test pins an
absolute date, and it constructs one that cannot be ambiguous.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from telegram_invite_bot.handlers.profile import _vip_line
from telegram_invite_bot.handlers.vip import handle_vip

# The prod display zone. UTC+3 with no DST since 2014, so the offset
# below is a constant and the boundary case is stable.
_TZ = ZoneInfo("Europe/Moscow")
_LANG = "ru"


@dataclass
class _FakeMessage:
    from_user: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(id=42))
    replies: list[str] = field(default_factory=list)

    async def reply(self, text: str, **_kwargs: object) -> None:
        self.replies.append(text)


async def _vip_card(vip_till: float) -> str:
    message = _FakeMessage()
    await handle_vip(
        cast("Any", message),
        cast("Any", SimpleNamespace(get_vip_till=AsyncMock(return_value=vip_till))),
        _LANG,
        tz=_TZ,
    )
    assert len(message.replies) == 1
    return message.replies[0]


def _profile_card(vip_till: float) -> str:
    line = _vip_line(vip_till, datetime.now(_TZ), _LANG)
    assert line is not None
    return line


def _date_in(text: str) -> str:
    match = re.search(r"\d{2}\.\d{2}\.\d{4}", text)
    assert match is not None, text
    return match.group(0)


def _days_in(text: str) -> str:
    match = re.search(r"(\d+) дн\.", text)
    assert match is not None, text
    return match.group(1)


def _tomorrow_at_one_am() -> float:
    """An instant whose MSK and UTC calendar dates always differ.

    01:00 MSK is 22:00 UTC of the previous day, and "tomorrow" is
    between one and twenty-five hours away whatever the current local
    time is — so the grant is always live and the two frames always
    disagree.
    """
    local_now = datetime.now(_TZ)
    return datetime.combine(
        local_now.date() + timedelta(days=1), time(1, 0), tzinfo=_TZ
    ).timestamp()


async def test_the_expiry_date_is_the_configured_zones_date() -> None:
    """The bug itself: UTC named the previous day."""
    vip_till = _tomorrow_at_one_am()
    expected = datetime.fromtimestamp(vip_till, _TZ).strftime("%d.%m.%Y")
    yesterdays = (datetime.fromtimestamp(vip_till, _TZ) - timedelta(days=1)).strftime("%d.%m.%Y")

    text = await _vip_card(vip_till)

    assert _date_in(text) == expected
    assert yesterdays not in text


async def test_both_cards_print_the_same_date() -> None:
    """The invariant, stated directly."""
    vip_till = _tomorrow_at_one_am()

    assert _date_in(await _vip_card(vip_till)) == _date_in(_profile_card(vip_till))


@pytest.mark.parametrize(
    "ahead",
    [
        timedelta(hours=1),  # the last-day case: ceil said 1, calendar said 0
        timedelta(days=7),
        timedelta(days=31),
    ],
)
async def test_both_cards_print_the_same_days_left(ahead: timedelta) -> None:
    vip_till = (datetime.now(_TZ) + ahead).timestamp()

    assert _days_in(await _vip_card(vip_till)) == _days_in(_profile_card(vip_till))


async def test_the_day_count_no_longer_rounds_up_a_whole_extra_day() -> None:
    """The ``ceil`` half, pinned at an instant no clock can blur.

    23:59:59 local tomorrow is between 24 h and 48 h away whatever the
    current local time is, so the old ``max(1, ceil(remaining /
    86400))`` always answered 2 while the calendar difference
    ``/profile`` uses always answers 1. The other agreement test above
    is honest but time-dependent — at some hours both formulas happen
    to land on the same number. This one never does.
    """
    local_now = datetime.now(_TZ)
    vip_till = datetime.combine(
        local_now.date() + timedelta(days=1), time(23, 59, 59), tzinfo=_TZ
    ).timestamp()

    assert _days_in(await _vip_card(vip_till)) == "1"
    assert _days_in(_profile_card(vip_till)) == "1"


def test_the_card_cannot_fall_back_to_a_zone_of_its_own() -> None:
    """``tz`` is keyword-only and has no default, on purpose.

    A default would let a future caller — or a re-wiring that forgets
    the argument — reintroduce exactly this bug silently. The router
    factory binds the configured zone once (``vip.py::build_router``),
    the same way ``mygroups`` does since #1953.
    """
    tz_param = inspect.signature(handle_vip).parameters["tz"]

    assert tz_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert tz_param.default is inspect.Parameter.empty
