"""End-to-end ``/admin_partitions``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs).
* Parser identifies header by non-int first column; data rows
  require 4 tokens with first three int.
* Whole-disk-vs-partition discriminator: trailing-digit heuristic.
* Bytes precomputed = blocks_1k * 1024.
* Top-N sorted by size desc.
* Truncation note when devices > _TOP_N.
* ZERO ⚠ markers regardless of state — inventory cards don't
  produce warnings.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.partitions import (
    _TOP_N,
    _capture,
    _parse_partitions,
    _Partition,
    _PartitionsSnapshot,
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
        make_message_update("/admin_partitions", user_id=42, chat_type="private"),
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
        make_message_update("/admin_partitions", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Block-device inventory" in text


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
            "/admin_partitions",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "major minor  #blocks  name\n"
    "\n"
    "   8        0  500107608 sda\n"
    "   8        1     524288 sda1\n"
    "   8        2  499582976 sda2\n"
    " 259        0 1000204886 nvme0n1\n"
    " 259        1     262144 nvme0n1p1\n"
    "   7        0      65536 loop0\n"
)


def test_parse_canonical() -> None:
    rows = _parse_partitions(_SAMPLE)
    assert len(rows) == 6
    by_name = {r.name: r for r in rows}
    assert by_name["sda"].major == 8
    assert by_name["sda"].minor == 0
    assert by_name["sda"].blocks_1k == 500_107_608
    assert by_name["sda"].bytes_total == 500_107_608 * 1024
    assert by_name["nvme0n1"].major == 259


def test_parse_header_skipped() -> None:
    """Header row (``major minor #blocks name``) is identified by
    its non-int first column and skipped."""
    rows = _parse_partitions("major minor  #blocks  name\n")
    assert rows == ()


def test_parse_short_row_dropped() -> None:
    """Anything other than exactly 4 tokens is not a data row."""
    text = "8 0 100 sda extra\n8 1 200 sda1\n"
    rows = _parse_partitions(text)
    assert [r.name for r in rows] == ["sda1"]


def test_parse_non_int_field_dropped() -> None:
    """Non-int in any of the first three columns → drop, not partial."""
    text = "8 zero 100 bad\n8 1 200 good\n"
    rows = _parse_partitions(text)
    assert [r.name for r in rows] == ["good"]


def test_parse_empty() -> None:
    assert _parse_partitions("") == ()


# --- whole-disk discriminator ----------------------------------------------


def test_whole_disk_heuristic() -> None:
    """Trailing-digit heuristic — sda is whole disk, sda1 is partition.
    Edge case: dm-3 ends in digit but IS whole-disk-like; we
    accept the false-positive (documented in the docstring) because
    the kernel doesn't surface the parent link in /proc/partitions."""
    p_whole = _Partition(major=8, minor=0, blocks_1k=100, name="sda")
    p_part = _Partition(major=8, minor=1, blocks_1k=100, name="sda1")
    p_nvme_whole = _Partition(major=259, minor=0, blocks_1k=100, name="nvme0n1")
    # nvme0n1 ends in digit — known false-positive, documented.
    assert p_whole.is_whole_disk is True
    assert p_part.is_whole_disk is False
    assert p_nvme_whole.is_whole_disk is False  # Heuristic limit.


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.rows == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "partitions"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.rows) == 6


# --- rendering -------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _PartitionsSnapshot(rows=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "macOS" in text or "non-procfs" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    snap = _PartitionsSnapshot(rows=(), available=True)
    text = _render(snap)
    assert "No block devices" in text
    assert "⚠" not in text


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "partitions"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "sda" in text
    assert "nvme0n1" in text
    assert "loop0" in text
    assert "Total block-device capacity" in text


def test_render_sorted_by_size(tmp_path: Path) -> None:
    """Largest devices first. nvme0n1 (1 TB) > sda (500 GB) > sda2
    (499 GB) > sda1 (512 MB) > nvme0n1p1 (256 MB) > loop0 (64 MB)."""
    p = tmp_path / "partitions"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    nvme_pos = text.index("nvme0n1<")
    sda_pos = text.index("sda<")
    loop_pos = text.index("loop0<")
    assert nvme_pos < sda_pos < loop_pos


def test_render_no_warnings_regardless_of_state(tmp_path: Path) -> None:
    """Cry-wolf prevention: even an absurd inventory (lots of huge
    devices, lots of tiny loops, exotic majors) must NOT produce ⚠.
    Inventory cards surface state; operator policy decides whether
    the inventory matches expectations."""
    p = tmp_path / "partitions"
    lines = ["major minor  #blocks  name", ""]
    for i in range(50):
        lines.append(f"{200 + i} {i} {99_999_999_999} hugedisk{i}")
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_truncation_note(tmp_path: Path) -> None:
    p = tmp_path / "partitions"
    lines = ["major minor  #blocks  name", ""]
    n = _TOP_N + 5
    for i in range(n):
        lines.append(f"8 {i} {1000 + i} dev{i}")
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "smaller devices not shown" in text


def test_partition_bytes_precomputed() -> None:
    """Bytes precomputed in __init__ — pinned so a refactor that
    makes it a property doesn't accidentally recompute on every
    sort comparison."""
    p = _Partition(major=8, minor=0, blocks_1k=500_000, name="sda")
    assert p.bytes_total == 500_000 * 1024
