"""End-to-end ``/admin_diskstats``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* in_flight > 10 on a device triggers ⚠ (per-row + hoisted summary).
* in_flight ≤ 10 → no ⚠ even with huge cumulative read/write totals
  (anti-cry-wolf: historical bytes are informational).
* 20-field lines (kernel 4.18+) parse identically to 14-field
  (forward-compat).
* Short / malformed lines degrade — don't crash.
* sectors → bytes via 512 multiplier (kernel internal unit).
* Truncates long device lists with explicit hidden-count.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.diskstats import (
    _capture,
    _DiskstatsSnapshot,
    _parse_diskstats,
    _render,
    _saturated_devices,
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
        make_message_update("/admin_diskstats", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_diskstats", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Block I/O" in text


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
            "/admin_diskstats",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ---------------------------------------------------------------


def _line(device: str, in_flight: int = 0, sectors_read: int = 0, sectors_written: int = 0) -> str:
    """Build a 14-field /proc/diskstats line for tests."""
    return f"  8  0 {device} 100 0 {sectors_read} 50 200 0 {sectors_written} 75 {in_flight} 99 0\n"


def test_parse_14_field_line() -> None:
    """Pre-4.18 kernel format — 14 fields. Must parse."""
    rows = _parse_diskstats(_line("sda", in_flight=3))
    assert len(rows) == 1
    assert rows[0].device == "sda"
    assert rows[0].in_flight == 3


def test_parse_20_field_line() -> None:
    """Post-4.18 kernel format — 20 fields (discard/flush counters
    appended). Must parse and ignore the trailing fields."""
    text = "  8  0 sda 100 0 1024 50 200 0 2048 75 5 99 0 10 1 32 2 3 100\n"
    rows = _parse_diskstats(text)
    assert len(rows) == 1
    assert rows[0].device == "sda"
    assert rows[0].in_flight == 5


def test_parse_short_line_dropped() -> None:
    """<12 fields = not a real diskstats line. Drop rather than
    guess. Degrade-don't-crash."""
    rows = _parse_diskstats("8 0 sda 100 0\n")
    assert rows == ()


def test_parse_malformed_value_dropped() -> None:
    """Non-int counter → drop that one row, keep others."""
    text = "8 0 sda 100 0 1024 50 200 0 2048 75 garbage 99 0\n" + _line("sdb")
    rows = _parse_diskstats(text)
    assert len(rows) == 1
    assert rows[0].device == "sdb"


# --- ⚠ predicate ---------------------------------------------------------


def test_saturation_predicate(tmp_path: Path) -> None:
    """in_flight > 10 → saturated. in_flight == 10 → not yet
    (strict > pin)."""
    p = tmp_path / "diskstats"
    p.write_text(_line("sda", in_flight=11) + _line("sdb", in_flight=10))
    snap = _capture(path=p)
    sat = _saturated_devices(snap)
    assert sat == ("sda",)


def test_healthy_disks_no_warning(tmp_path: Path) -> None:
    """Cry-wolf pin: huge cumulative read/write totals don't ⚠
    when current in_flight is low. Historical bytes are
    informational, not a health signal."""
    p = tmp_path / "diskstats"
    p.write_text(
        _line("sda", in_flight=2, sectors_read=10_000_000_000, sectors_written=5_000_000_000)
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_saturated_disk_warns(tmp_path: Path) -> None:
    """Saturated device → ⚠ in body, hoisted summary names the
    specific device."""
    p = tmp_path / "diskstats"
    p.write_text(_line("nvme0n1", in_flight=42))
    snap = _capture(path=p)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" in head
    assert "nvme0n1" in head


def test_only_one_device_saturated(tmp_path: Path) -> None:
    """Multi-device host with one saturated: hoisted summary
    names only that one — operator's eye lands on the culprit."""
    p = tmp_path / "diskstats"
    p.write_text(_line("sda", in_flight=0) + _line("sdb", in_flight=15) + _line("sdc", in_flight=1))
    snap = _capture(path=p)
    sat = _saturated_devices(snap)
    assert sat == ("sdb",)


# --- rendering ------------------------------------------------------------


def test_sectors_converted_to_bytes(tmp_path: Path) -> None:
    """sectors are 512-byte units. 2048 sectors = 1 MiB."""
    p = tmp_path / "diskstats"
    p.write_text(_line("sda", sectors_read=2048))
    snap = _capture(path=p)
    rendered = _render(snap)
    # 2048 * 512 = 1048576 bytes = exactly 1 MiB
    assert "1.0 MiB" in rendered


def test_truncates_long_device_list(tmp_path: Path) -> None:
    """50 devices — render caps at 30 + explicit hidden marker."""
    p = tmp_path / "diskstats"
    p.write_text("".join(_line(f"sd{i}") for i in range(50)))
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "hidden" in rendered
    assert "Total devices: 50" in rendered


def test_render_empty_explains() -> None:
    """Available but empty (private mount namespace) → explanatory
    text, not a confusing blank card."""
    snap = _DiskstatsSnapshot(rows=(), available=True)
    rendered = _render(snap)
    assert "empty" in rendered


def test_render_unavailable_explains() -> None:
    """macOS dev → explicit unavailable note."""
    snap = _DiskstatsSnapshot(rows=(), available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered


def test_capture_unavailable_when_path_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "does_not_exist")
    assert not snap.available
    assert snap.rows == ()
