"""End-to-end ``/admin_dirty``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note (only when ALL sources missing).
* Partial availability: one source present, others missing → render
  shows the present one, 'unknown' for the rest.
* Meminfo parsing: extracts Dirty / Writeback / MemTotal kB; missing
  field → -1.
* Cry-wolf must-not-fire on canonical-healthy sample (Dirty=2MiB,
  MemTotal=8GiB, dirty_ratio=20).
* ⚠ fires when usage crosses 80% of vm.dirty_ratio threshold.
* Defensive: MemTotal=0 / dirty_ratio=0 (bytes-mode) / sentinel
  doesn't fire spurious ⚠.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.dirty import (
    _DIRTY_WARN_RATIO,
    _capture,
    _DirtySnapshot,
    _fmt_kb,
    _fmt_pct,
    _fmt_pct_int,
    _parse_meminfo,
    _read_int,
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
        bot, make_message_update("/admin_dirty", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_dirty", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Dirty-page" in text or "writeback" in text


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
        make_message_update("/admin_dirty", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parsers ---------------------------------------------------------------


def test_read_int_present(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("20\n")
    assert _read_int(p) == 20


def test_read_int_missing(tmp_path: Path) -> None:
    assert _read_int(tmp_path / "absent") == -1


def test_read_int_non_numeric(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("garbage\n")
    assert _read_int(p) == -1


def test_parse_meminfo_canonical() -> None:
    text = (
        "MemTotal:       8000000 kB\n"
        "MemFree:        4000000 kB\n"
        "Dirty:             2048 kB\n"
        "Writeback:          512 kB\n"
    )
    dirty, writeback, memtotal = _parse_meminfo(text)
    assert dirty == 2048
    assert writeback == 512
    assert memtotal == 8000000


def test_parse_meminfo_missing_fields() -> None:
    text = "MemTotal:       8000000 kB\n"
    dirty, writeback, memtotal = _parse_meminfo(text)
    assert dirty == -1
    assert writeback == -1
    assert memtotal == 8000000


def test_parse_meminfo_empty() -> None:
    assert _parse_meminfo("") == (-1, -1, -1)


def test_parse_meminfo_garbage_value() -> None:
    text = "Dirty:    garbage kB\n"
    dirty, _, _ = _parse_meminfo(text)
    assert dirty == -1


# --- ratio + warn ---------------------------------------------------------


def test_usage_ratio_basic() -> None:
    # 8 GiB total, dirty_ratio=20 → threshold = 1.6 GiB = 1_677_721.6 kB.
    # Dirty = 1.4 GiB ~ 1_468_006 kB → ratio ~0.875 > 0.8 → warn.
    snap = _DirtySnapshot(
        dirty_kb=1_468_006,
        writeback_kb=0,
        memtotal_kb=8_388_608,
        dirty_ratio=20,
        dirty_bg_ratio=10,
        available=True,
    )
    assert snap.usage_ratio > _DIRTY_WARN_RATIO
    assert snap.under_pressure is True


def test_usage_ratio_below_threshold() -> None:
    snap = _DirtySnapshot(
        dirty_kb=2_048,
        writeback_kb=0,
        memtotal_kb=8_388_608,
        dirty_ratio=20,
        dirty_bg_ratio=10,
        available=True,
    )
    assert snap.under_pressure is False


def test_memtotal_zero_no_crash() -> None:
    snap = _DirtySnapshot(
        dirty_kb=100,
        writeback_kb=0,
        memtotal_kb=0,
        dirty_ratio=20,
        dirty_bg_ratio=10,
        available=True,
    )
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_dirty_ratio_zero_bytes_mode() -> None:
    """vm.dirty_ratio=0 is legal — kernel uses vm.dirty_bytes
    (absolute knob) instead. From the percent knob alone we
    don't know the threshold, so don't fabricate a ratio."""
    snap = _DirtySnapshot(
        dirty_kb=1_000_000,
        writeback_kb=0,
        memtotal_kb=8_388_608,
        dirty_ratio=0,
        dirty_bg_ratio=0,
        available=True,
    )
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_dirty_sentinel_no_crash() -> None:
    snap = _DirtySnapshot(
        dirty_kb=-1,
        writeback_kb=-1,
        memtotal_kb=8_388_608,
        dirty_ratio=20,
        dirty_bg_ratio=10,
        available=True,
    )
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_threshold_constant_sane() -> None:
    assert 0.5 < _DIRTY_WARN_RATIO < 1.0


# --- capture ---------------------------------------------------------------


def test_capture_all_missing(tmp_path: Path) -> None:
    snap = _capture(
        meminfo_path=tmp_path / "no1",
        dirty_ratio_path=tmp_path / "no2",
        dirty_bg_ratio_path=tmp_path / "no3",
    )
    assert not snap.available


def test_capture_partial_dirty_ratio_only(tmp_path: Path) -> None:
    dr = tmp_path / "dirty_ratio"
    dr.write_text("20\n")
    snap = _capture(
        meminfo_path=tmp_path / "absent",
        dirty_ratio_path=dr,
        dirty_bg_ratio_path=tmp_path / "absent2",
    )
    assert snap.available
    assert snap.dirty_ratio == 20
    assert snap.dirty_kb == -1
    assert snap.memtotal_kb == -1


def test_capture_all_present(tmp_path: Path) -> None:
    mi = tmp_path / "meminfo"
    mi.write_text(
        "MemTotal:       8000000 kB\nDirty:             2048 kB\nWriteback:          512 kB\n"
    )
    dr = tmp_path / "dirty_ratio"
    dr.write_text("20\n")
    bgr = tmp_path / "dirty_bg_ratio"
    bgr.write_text("10\n")
    snap = _capture(meminfo_path=mi, dirty_ratio_path=dr, dirty_bg_ratio_path=bgr)
    assert snap.available
    assert snap.dirty_kb == 2048
    assert snap.writeback_kb == 512
    assert snap.memtotal_kb == 8000000
    assert snap.dirty_ratio == 20
    assert snap.dirty_bg_ratio == 10


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _DirtySnapshot(
        dirty_kb=-1,
        writeback_kb=-1,
        memtotal_kb=-1,
        dirty_ratio=-1,
        dirty_bg_ratio=-1,
        available=False,
    )
    text = _render(snap)
    assert "non-procfs" in text or "readable" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy() -> None:
    """Cry-wolf pin: realistic healthy sample. 2 MiB dirty of
    8 GiB total with dirty_ratio=20 — vastly under the
    synchronous-stall threshold. ⚠ MUST NOT appear."""
    snap = _DirtySnapshot(
        dirty_kb=2_048,
        writeback_kb=0,
        memtotal_kb=8_388_608,
        dirty_ratio=20,
        dirty_bg_ratio=10,
        available=True,
    )
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_pressure() -> None:
    snap = _DirtySnapshot(
        dirty_kb=1_468_006,
        writeback_kb=0,
        memtotal_kb=8_388_608,
        dirty_ratio=20,
        dirty_bg_ratio=10,
        available=True,
    )
    text = _render(snap)
    assert "⚠" in text
    assert "synchronously" in text or "stall" in text or "polling" in text


def test_render_bytes_mode_shows_unknown() -> None:
    """dirty_ratio=0 (vm.dirty_bytes mode) → ratio not computable;
    render shows 'unknown' for the percentage row, no ⚠."""
    snap = _DirtySnapshot(
        dirty_kb=1_000_000,
        writeback_kb=0,
        memtotal_kb=8_388_608,
        dirty_ratio=0,
        dirty_bg_ratio=0,
        available=True,
    )
    text = _render(snap)
    assert "unknown" in text
    assert "⚠" not in text


# --- helpers ---------------------------------------------------------------


def test_fmt_kb_gib() -> None:
    # 4 GiB worth of kB → "4.00 GiB"
    assert _fmt_kb(4 * 1024 * 1024) == "4.00 GiB"


def test_fmt_kb_mib() -> None:
    assert _fmt_kb(2048) == "2.00 MiB"


def test_fmt_kb_small() -> None:
    assert _fmt_kb(100) == "100 kB"


def test_fmt_kb_sentinel() -> None:
    assert _fmt_kb(-1) == "unknown"


def test_fmt_pct_int_present() -> None:
    assert _fmt_pct_int(20) == "20%"


def test_fmt_pct_int_sentinel() -> None:
    assert _fmt_pct_int(-1) == "unknown"


def test_fmt_pct() -> None:
    assert _fmt_pct(0.8) == "80.0%"
    assert _fmt_pct(0.0) == "0.0%"
