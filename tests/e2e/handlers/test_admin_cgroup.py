"""End-to-end ``/admin_cgroup``.

Pins:

* Non-developer → silent drop.
* Live card renders (the running host has SOME posture, even on
  macOS where /proc/self/cgroup is absent).
* No cgroup membership → informational, NO ⚠ (non-Linux signal).
* cgroup v2 with ``memory.max=max`` → "unlimited", NO ⚠.
* cgroup v2 with low pressure (50%) → numbers shown, NO ⚠.
* cgroup v2 with high pressure (95%) → ⚠ on memory.current row.
* cgroup v2 with pids pressure → ⚠ on pids.current row.
* cgroup v1 → mode + controllers shown, limits-section disclaimer
  (we don't chase per-controller dirs).
* ``_parse_proc_cgroup`` recognises v2 / v1 / hybrid / none.
* ``_parse_max_or_int`` handles ``max`` sentinel + int + junk.
* ``_pressure_fraction`` returns None for uncapped (no cry-wolf).
* ``_capture`` against fixtures — hermetic, no /proc dependency.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.cgroup import (
    _PRESSURE_WARN_FRACTION,
    _capture,
    _CgroupSnapshot,
    _is_pressured,
    _parse_max_or_int,
    _parse_proc_cgroup,
    _pressure_fraction,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    mode: str = "v2",
    path: str | None = "/system.slice/telegram-bot.service",
    controllers: tuple[str, ...] = (),
    memory_max: int | str | None = None,
    memory_current: int | None = None,
    cpu_max: str | None = None,
    pids_max: int | str | None = None,
    pids_current: int | None = None,
) -> _CgroupSnapshot:
    return _CgroupSnapshot(
        mode=mode,
        path=path,
        controllers=controllers,
        memory_max=memory_max,
        memory_current=memory_current,
        cpu_max=cpu_max,
        pids_max=pids_max,
        pids_current=pids_current,
    )


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_cgroup", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_cgroup", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "cgroup posture" in text


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
        make_message_update("/admin_cgroup", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_no_cgroup_membership_no_warn() -> None:
    """Missing /proc/self/cgroup → informational. Zero ⚠ — same
    cry-wolf posture as every other Linux-only admin card."""
    rendered = _render(_snap(mode="none", path=None))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "non-Linux host" in head


def test_unlimited_memory_no_warn() -> None:
    """``memory.max=max`` is the unlimited sentinel — operator
    intent (no cap). Zero ⚠ regardless of memory.current."""
    rendered = _render(_snap(memory_max="max", memory_current=8 * 1024 * 1024 * 1024))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "unlimited" in head


def test_low_memory_pressure_no_warn() -> None:
    """50% pressure → numbers surfaced, zero ⚠. The 90% floor
    exists precisely so bursty Python heaps don't cry wolf."""
    cap = 2 * 1024 * 1024 * 1024
    rendered = _render(_snap(memory_max=cap, memory_current=cap // 2))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "50%" in head


def test_high_memory_pressure_warn() -> None:
    """95% memory.current/memory.max → ⚠ on the current row.
    This is the OOM-clock countdown the card exists to surface."""
    cap = 2 * 1024 * 1024 * 1024
    rendered = _render(_snap(memory_max=cap, memory_current=int(cap * 0.95)))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 1
    assert "95%" in head


def test_pids_pressure_warn() -> None:
    """pids.current near pids.max → ⚠. Container defaults are
    surprisingly tight (often 4096) and chatty worker pools can
    actually reach the cap."""
    rendered = _render(_snap(pids_max=4096, pids_current=3900))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 1


def test_v1_renders_disclaimer_no_warn() -> None:
    """cgroup v1 → mode + controllers shown, explicit disclaimer
    that v1 limits aren't followed. Zero ⚠ — informational."""
    rendered = _render(_snap(mode="v1", path="/", controllers=("cpu", "memory", "pids")))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "cgroup v1 limit files" in head
    assert "cpu" in head
    assert "memory" in head


def test_parse_proc_cgroup_v2() -> None:
    """Pure cgroup v2: single ``0::<path>`` line."""
    mode, path, controllers = _parse_proc_cgroup("0::/system.slice/telegram-bot.service\n")
    assert mode == "v2"
    assert path == "/system.slice/telegram-bot.service"
    assert controllers == ()


def test_parse_proc_cgroup_v1() -> None:
    """cgroup v1: many ``hid:controller:path`` lines."""
    text = "11:cpu,cpuacct:/user.slice\n10:memory:/user.slice\n9:pids:/user.slice\n"
    mode, path, controllers = _parse_proc_cgroup(text)
    assert mode == "v1"
    assert path == "/user.slice"
    # Comma-separated controllers split out individually.
    assert "cpu" in controllers
    assert "cpuacct" in controllers
    assert "memory" in controllers
    assert "pids" in controllers


def test_parse_proc_cgroup_hybrid() -> None:
    """Hybrid: v2 line (hid=0, empty controller) + v1 lines."""
    text = "0::/system.slice/telegram-bot.service\n10:memory:/user.slice\n"
    mode, path, _ = _parse_proc_cgroup(text)
    assert mode == "hybrid"
    assert path == "/system.slice/telegram-bot.service"


def test_parse_proc_cgroup_empty() -> None:
    """Empty file → ``none``. Same path as a non-Linux host."""
    mode, path, controllers = _parse_proc_cgroup("")
    assert mode == "none"
    assert path is None
    assert controllers == ()


def test_parse_max_or_int() -> None:
    """``max`` sentinel preserved; int parsed; junk → None."""
    assert _parse_max_or_int("max") == "max"
    assert _parse_max_or_int("2147483648") == 2147483648
    assert _parse_max_or_int("garbage") is None
    assert _parse_max_or_int(None) is None


def test_pressure_fraction_uncapped_returns_none() -> None:
    """``max`` sentinel → None fraction → no ⚠. Operator intent
    (no cap) must never look like a problem."""
    assert _pressure_fraction(1_000_000, "max") is None
    assert _pressure_fraction(None, 1_000_000) is None
    assert _pressure_fraction(1_000_000, None) is None
    # Zero or negative cap is treated as "no signal".
    assert _pressure_fraction(1_000_000, 0) is None


def test_pressure_fraction_real_ratio() -> None:
    assert _pressure_fraction(500, 1000) == pytest.approx(0.5)


def test_is_pressured_boundary() -> None:
    """Boundary pin: exactly 90% → ⚠. Just under → no ⚠. This is
    the cry-wolf threshold and must be tested explicitly."""
    cap = 1000
    at_threshold = int(cap * _PRESSURE_WARN_FRACTION)
    assert _is_pressured(at_threshold, cap)
    assert not _is_pressured(at_threshold - 1, cap)
    assert not _is_pressured(0, cap)


def test_capture_with_v2_fixture(tmp_path: Path) -> None:
    """Real _capture against a tmp_path mimicking /proc + /sys —
    same parsing path, no real-host dependency."""
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text("0::/system.slice/telegram-bot.service\n")
    cg_dir = tmp_path / "system.slice" / "telegram-bot.service"
    cg_dir.mkdir(parents=True)
    (cg_dir / "memory.max").write_text("2147483648\n")
    (cg_dir / "memory.current").write_text("1073741824\n")
    (cg_dir / "cpu.max").write_text("max 100000\n")
    (cg_dir / "pids.max").write_text("max\n")
    (cg_dir / "pids.current").write_text("42\n")

    snap = _capture(proc_cgroup_path=proc, sys_fs_cgroup_root=tmp_path)
    assert snap.mode == "v2"
    assert snap.path == "/system.slice/telegram-bot.service"
    assert snap.memory_max == 2147483648
    assert snap.memory_current == 1073741824
    assert snap.cpu_max == "max 100000"
    assert snap.pids_max == "max"
    assert snap.pids_current == 42


def test_capture_missing_proc(tmp_path: Path) -> None:
    """Absent /proc/self/cgroup → mode=none, all fields cleared.
    The non-Linux signal."""
    snap = _capture(
        proc_cgroup_path=tmp_path / "does_not_exist",
        sys_fs_cgroup_root=tmp_path,
    )
    assert snap.mode == "none"
    assert snap.path is None
    assert snap.memory_max is None
    assert snap.memory_current is None
