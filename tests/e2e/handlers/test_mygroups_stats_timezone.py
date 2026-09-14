"""#1953: the ``/mygroups`` card counts days in ``STATS_TIMEZONE``.

``_fetch_activity`` used to compute its window from a bare
``date.today()`` — the HOST clock — under a comment claiming it made
"the same trade as ``/chatstats``", which was the one thing it did not
do. Every other reader of ``message_counts`` (``chatstats``, ``ads``,
``stats``, ``top``, ``profile``) threads the configured zone, and
:class:`MessageStatsRepo` deliberately refuses to default ``today`` so
the policy decision has to land in a handler.

The direction of the error is what makes it worth a regression file:
:func:`_date_in_window` is bounded at BOTH ends, so a host clock behind
the configured zone silently drops the newest day(s) instead of
over-reporting. The card then shows a quieter group than the one
``/chatstats`` describes for the same week.

The pair of zones below is chosen so the two clocks can never agree:
the host sits at UTC−12 and the configured zone at UTC+14, 26 hours
apart, so the configured calendar date is always at least one day ahead
of the host's.
"""

from __future__ import annotations

import ast
import inspect
import time
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import StatsConfig
from telegram_invite_bot.db.models.base import (
    EconomyBase,
    MessageStatsBase,
    ModerationBase,
    UsersBase,
)
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.mygroups import MyGroupsCard
from tests.e2e.handlers.conftest import make_callback_update

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_ALL_SCHEMAS = [UsersBase, EconomyBase, ModerationBase, MessageStatsBase]

# UTC−12 and UTC+14 — the two ends of the civil-time range, 26 h apart.
# ``Etc/GMT+N`` is inverted by POSIX convention: the sign is the offset
# to ADD to local time to get UTC.
_HOST_TZ = "Etc/GMT+12"
_CONFIGURED_TZ = "Etc/GMT-14"

_OWNER = 42
_GROUP = -1001


@pytest.fixture
def host_clock_far_behind(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the process's local zone to UTC−12 for the duration of a test.

    ``date.today()`` reads this; ``datetime.now(tz)`` does not. That is
    exactly the seam the ticket is about, so the bug is only observable
    with the host zone pinned away from the configured one.
    """
    monkeypatch.setenv("TZ", _HOST_TZ)
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def _configured_today() -> date:
    return datetime.now(ZoneInfo(_CONFIGURED_TZ)).date()


async def _wire(
    make_wired: WiredFactory, *, tz: str = _CONFIGURED_TZ
) -> tuple[Bot, Dispatcher, EngineRegistry]:
    bot, dispatcher, registry = await make_wired(
        schemas=_ALL_SCHEMAS,
        stats_config=StatsConfig(STATS_PERIOD_DAYS=7, STATS_TIMEZONE=tz),
    )
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(
            BotGroup(
                chat_id=_GROUP,
                chat_title="Alpha",
                added_by_user_id=_OWNER,
                is_active=1,
            )
        )
        await session.commit()
    return bot, dispatcher, registry


async def _seed_counts(registry: EngineRegistry, rows: list[tuple[int, date, int]]) -> None:
    engine = registry.engine(DBName.MESSAGE_STATS)
    async with AsyncSession(engine) as session:
        for user_id, day, count in rows:
            session.add(
                MessageCount(user_id=user_id, chat_id=_GROUP, date=day.isoformat(), count=count)
            )
        await session.commit()


async def _card_text(bot: Bot, dispatcher: Dispatcher, sent: list[dict[str, Any]]) -> str:
    await dispatcher.feed_update(
        bot, make_callback_update(MyGroupsCard(group_id=_GROUP, page=1).pack(), user_id=_OWNER)
    )
    edits = [e for e in sent if e["kind"] == "edit"]
    assert len(edits) == 1
    return str(edits[0]["text"])


@pytest.mark.usefixtures("host_clock_far_behind")
async def test_today_in_the_configured_zone_is_counted(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The card's newest day is the configured zone's, not the host's.

    Under the host clock the seeded rows are stamped in the FUTURE (the
    configured date runs a day or two ahead), and the upper bound of
    :func:`_date_in_window` drops them — the card reports an idle group
    while ``/chatstats`` reports five messages for the same week.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    sent = capture_callback_outgoing(bot)
    today = _configured_today()
    await _seed_counts(registry, [(7, today, 3), (8, today, 2)])

    text = await _card_text(bot, dispatcher, sent)
    assert t("h_mygroups_card_activity", "ru", messages=5, active=2) in text


@pytest.mark.usefixtures("host_clock_far_behind")
async def test_the_window_is_still_seven_days_wide(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The reverse-error guard: moving the anchor must not unbound it.

    ``days=7`` is today plus the previous six, so the row seven days
    back sits outside it. A "fix" that simply dropped the date
    predicate would pass the test above and fail this one.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    sent = capture_callback_outgoing(bot)
    today = _configured_today()
    await _seed_counts(
        registry,
        [(7, today - timedelta(days=6), 4), (8, today - timedelta(days=7), 99)],
    )

    text = await _card_text(bot, dispatcher, sent)
    assert t("h_mygroups_card_activity", "ru", messages=4, active=1) in text


@pytest.mark.usefixtures("host_clock_far_behind")
async def test_the_configured_zone_wins_over_the_host_one(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Pinning the two zones together must give the old answer back.

    ``STATS_TIMEZONE`` set to the host zone is the ordinary deploy
    (prod runs MSK on an MSK box), and there the ticket changes
    nothing: a row stamped with the host's own date is counted exactly
    as it was before.
    """
    bot, dispatcher, registry = await _wire(make_wired, tz=_HOST_TZ)
    sent = capture_callback_outgoing(bot)
    host_today = datetime.now(ZoneInfo(_HOST_TZ)).date()
    await _seed_counts(registry, [(7, host_today, 6)])

    text = await _card_text(bot, dispatcher, sent)
    assert t("h_mygroups_card_activity", "ru", messages=6, active=1) in text


def test_the_activity_reader_keeps_no_host_clock_fallback() -> None:
    """``tz`` has no default, and the module never calls the host clock.

    Both halves are load-bearing. A defaulted parameter would let a
    future call site silently reintroduce the host clock, and the AST
    walk catches the other spelling of the same regression — a bare
    ``date.today()`` reappearing anywhere in the handler. The walk is
    an AST one rather than a substring search precisely because the
    ticket's own docstring names the call it removed.
    """
    from telegram_invite_bot.handlers import mygroups

    param = inspect.signature(mygroups._fetch_activity).parameters["tz"]  # noqa: SLF001
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty

    tree = ast.parse(inspect.getsource(mygroups))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"date", "datetime"}
    }
    assert "today" not in called
