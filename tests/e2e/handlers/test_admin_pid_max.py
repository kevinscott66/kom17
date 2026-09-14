"""End-to-end ``/admin_pid_max``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note (only when ALL three
  sources missing).
* Partial availability: one sysctl present, others missing →
  card renders the present one; missing ones show 'unknown'.
* Loadavg parsing: extracts ``total`` from running/total;
  malformed field → -1.
* Cry-wolf must-not-fire on canonical-healthy sample (tasks
  << min(pid_max, threads-max)).
* ⚠ fires when EITHER ratio crosses threshold (binding constraint
  flips per host — pinned independently).
* Defensive: ceiling=0 / sentinel doesn't fire spurious ⚠.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.pid_max import (
    _PID_WARN_RATIO,
    _capture,
    _fmt,
    _fmt_pct,
    _parse_loadavg_total,
    _PidMaxSnapshot,
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
        bot, make_message_update("/admin_pid_max", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_pid_max", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "PID / task-count" in text


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
        make_message_update("/admin_pid_max", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parsers ---------------------------------------------------------------


def test_read_int_present(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("4194304\n")
    assert _read_int(p) == 4194304


def test_read_int_missing(tmp_path: Path) -> None:
    assert _read_int(tmp_path / "absent") == -1


def test_read_int_non_numeric(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("garbage\n")
    assert _read_int(p) == -1


def test_parse_loadavg_total_canonical() -> None:
    assert _parse_loadavg_total("0.00 0.01 0.05 3/512 99999\n") == 512


def test_parse_loadavg_total_short() -> None:
    assert _parse_loadavg_total("0.00 0.01 0.05\n") == -1


def test_parse_loadavg_total_no_slash() -> None:
    assert _parse_loadavg_total("0.00 0.01 0.05 nothing 99999\n") == -1


def test_parse_loadavg_total_bad_total() -> None:
    assert _parse_loadavg_total("0.00 0.01 0.05 3/garbage 99999\n") == -1


def test_parse_loadavg_total_empty() -> None:
    assert _parse_loadavg_total("") == -1


# --- ratio + warn ---------------------------------------------------------


def test_pid_ratio_basic() -> None:
    snap = _PidMaxSnapshot(pid_max=1000, threads_max=10000, tasks_total=800, available=True)
    assert snap.pid_ratio == 0.8
    assert snap.thread_ratio == 0.08
    assert snap.under_pressure is True


def test_thread_ratio_binds_first() -> None:
    """Pinned: a tight threads-max with a loose pid_max must
    still fire ⚠ on the threads-max ratio. This is the bare-
    metal-with-tight-RAM case where threads-max is the binding
    constraint — the dual-ceiling design exists exactly to catch
    this."""
    snap = _PidMaxSnapshot(pid_max=4_000_000, threads_max=1000, tasks_total=900, available=True)
    assert snap.thread_ratio == 0.9
    assert snap.under_pressure is True


def test_pid_ratio_binds_first() -> None:
    """Pinned: tight pid_max (containerised) with loose
    threads-max must fire ⚠ on the pid_max ratio. This is
    the per-cgroup-pid-cap case common in container runtimes."""
    snap = _PidMaxSnapshot(pid_max=1024, threads_max=4_000_000, tasks_total=900, available=True)
    assert snap.pid_ratio > _PID_WARN_RATIO
    assert snap.under_pressure is True


def test_below_threshold_no_warn() -> None:
    snap = _PidMaxSnapshot(
        pid_max=4_000_000, threads_max=4_000_000, tasks_total=500, available=True
    )
    assert snap.under_pressure is False


def test_ceiling_zero_no_crash() -> None:
    snap = _PidMaxSnapshot(pid_max=0, threads_max=0, tasks_total=100, available=True)
    assert snap.pid_ratio == 0.0
    assert snap.thread_ratio == 0.0
    assert snap.under_pressure is False


def test_tasks_sentinel_no_crash() -> None:
    snap = _PidMaxSnapshot(pid_max=1000, threads_max=10000, tasks_total=-1, available=True)
    assert snap.pid_ratio == 0.0
    assert snap.under_pressure is False


def test_threshold_constant_sane() -> None:
    assert 0.5 < _PID_WARN_RATIO < 1.0


# --- capture ---------------------------------------------------------------


def test_capture_all_missing(tmp_path: Path) -> None:
    """All three sources absent → available=False. Render then
    shows the unavailable note instead of three 'unknown' rows
    that imply we partially succeeded."""
    snap = _capture(
        pid_max_path=tmp_path / "no1",
        threads_max_path=tmp_path / "no2",
        loadavg_path=tmp_path / "no3",
    )
    assert not snap.available


def test_capture_partial_pid_max_only(tmp_path: Path) -> None:
    """One source present is enough for available=True. This
    is the stripped-container case — render shows what it can,
    'unknown' for the rest."""
    pm = tmp_path / "pid_max"
    pm.write_text("4194304\n")
    snap = _capture(
        pid_max_path=pm,
        threads_max_path=tmp_path / "absent",
        loadavg_path=tmp_path / "absent2",
    )
    assert snap.available
    assert snap.pid_max == 4194304
    assert snap.threads_max == -1
    assert snap.tasks_total == -1


def test_capture_all_present(tmp_path: Path) -> None:
    pm = tmp_path / "pid_max"
    pm.write_text("4194304\n")
    tm = tmp_path / "threads_max"
    tm.write_text("63000\n")
    la = tmp_path / "loadavg"
    la.write_text("0.00 0.01 0.05 3/512 99999\n")
    snap = _capture(pid_max_path=pm, threads_max_path=tm, loadavg_path=la)
    assert snap.available
    assert snap.pid_max == 4194304
    assert snap.threads_max == 63000
    assert snap.tasks_total == 512


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _PidMaxSnapshot(pid_max=-1, threads_max=-1, tasks_total=-1, available=False)
    text = _render(snap)
    assert "unavailable" in text or "non-procfs" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy() -> None:
    """Cry-wolf pin: realistic healthy sample. 512 tasks of
    4M pid_max / 63K threads-max — ratios well under threshold.
    ⚠ MUST NOT appear."""
    snap = _PidMaxSnapshot(pid_max=4_194_304, threads_max=63_000, tasks_total=512, available=True)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_pressure() -> None:
    snap = _PidMaxSnapshot(pid_max=1000, threads_max=10000, tasks_total=900, available=True)
    text = _render(snap)
    assert "⚠" in text
    assert "EAGAIN" in text


def test_render_partial_shows_unknown() -> None:
    """When threads-max source was unreadable, render shows
    'unknown' for that field — must NOT spuriously warn on the
    -1 sentinel."""
    snap = _PidMaxSnapshot(pid_max=4_194_304, threads_max=-1, tasks_total=512, available=True)
    text = _render(snap)
    assert "unknown" in text
    assert "⚠" not in text


# --- helpers ---------------------------------------------------------------


def test_fmt_thousands() -> None:
    assert _fmt(4_194_304) == "4,194,304"


def test_fmt_sentinel() -> None:
    assert _fmt(-1) == "unknown"


def test_fmt_pct() -> None:
    assert _fmt_pct(0.8) == "80.0%"
    assert _fmt_pct(0.0) == "0.0%"
