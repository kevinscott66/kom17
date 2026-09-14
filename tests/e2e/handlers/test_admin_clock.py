"""End-to-end ``/admin_clock``.

Pins:

* Non-developer → silent drop.
* Card renders the four clock surfaces (UTC, local, monotonic,
  perf_counter) and the tzname legend — these are the surfaces
  the diagnostic exists for, so any future refactor that drops
  one silently must fail loudly here.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` rendering DST-aware zone notation
  when tzname pairs differ — load-bearing because the legend
  "host knows about its own DST" hinges on the two-name display.
* Unit pin on the equal-tzname branch (containerised hosts that
  default to UTC have both halves equal — must NOT render the
  "/DST" tail or operator confuses UTC-only for a DST zone).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.clock import (
    _ClockSnapshot,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_clock", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_all_four_clock_surfaces(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """All four clock readings must appear — UTC, local, monotonic,
    and perf_counter. Together they let the operator spot a
    wall-vs-monotonic divergence (NTP step) between two snapshots.
    Dropping any one breaks the documented two-snapshot workflow."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_clock", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Host clock" in text
    assert "UTC now" in text
    assert "Local now" in text
    assert "Monotonic since boot" in text
    assert "Perf counter since boot" in text
    assert "Zone" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_clock",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_dst_zone_pair_shows_both_names() -> None:
    """A host that knows about its DST transition reports a pair
    of different tzname strings (e.g. ``("CET", "CEST")``). The
    card must surface both so the operator sees the host's DST
    awareness, not just half of it."""
    snap = _ClockSnapshot(
        utc_now=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        local_now=datetime(2026, 5, 19, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        monotonic_s=123.456,
        perf_counter_s=124.789,
        tzname=("CET", "CEST"),
    )
    rendered = _render(snap)
    assert "<code>CET</code>" in rendered
    assert "<code>CEST</code>" in rendered
    assert "(DST)" in rendered


def test_render_equal_tzname_pair_omits_dst_tail() -> None:
    """A containerised host with no ``TZ`` env reports
    ``("UTC", "UTC")``. Rendering ``UTC / UTC (DST)`` would be
    actively misleading — there is no DST in UTC. The card must
    collapse equal pairs to the single name with no DST suffix."""
    snap = _ClockSnapshot(
        utc_now=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        local_now=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        monotonic_s=1.0,
        perf_counter_s=1.0,
        tzname=("UTC", "UTC"),
    )
    rendered = _render(snap)
    assert "Zone: <code>UTC</code>" in rendered
    # Load-bearing: equal-pair must NOT print the DST tail.
    assert "(DST)" not in rendered
    # And must not print the slash that the dual-name branch uses.
    assert "UTC</code> / <code>UTC" not in rendered
