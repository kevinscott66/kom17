"""End-to-end ``/admin_sysctl``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note when /proc/sys
  is missing.
* somaxconn ≤ 128 triggers ⚠ on that row only.
* somaxconn > 128 → no ⚠ anywhere (cry-wolf prevention).
* Boundary: 128 exactly ⚠'s (≤ semantics); 129 doesn't.
* Other keys NEVER ⚠ even with surprising values (anti-cry-wolf:
  ip_local_port_range, overcommit_memory are policy choices).
* Missing key → "n/a" rendered, no ⚠.
* Unparseable somaxconn → no ⚠ (absence-of-data anti-cry-wolf).
* Group invocation → router-level private filter rejects.

Tests use tmp_path to synthesise a /proc/sys-shaped tree so they
run on macOS too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.sysctl import (
    _capture,
    _render,
    _somaxconn_too_low,
    _SysctlSnapshot,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _make_proc_sys(tmp_path: Path, values: dict[str, str]) -> Path:
    """Synthesise a /proc/sys-shaped tree under tmp_path.

    Keys are dotted sysctl names ("net.core.somaxconn"); values
    become the file contents. Returns the base directory.
    """
    base = tmp_path / "proc_sys"
    base.mkdir()
    for key, value in values.items():
        parts = key.split(".")
        d = base.joinpath(*parts[:-1])
        d.mkdir(parents=True, exist_ok=True)
        d.joinpath(parts[-1]).write_text(value)
    return base


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_sysctl", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_sysctl", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Kernel tunables" in text


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
        make_message_update("/admin_sysctl", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_capture_unavailable_when_proc_sys_missing(tmp_path: Path) -> None:
    snap = _capture(base=tmp_path / "does_not_exist")
    assert not snap.proc_sys_available
    rendered = _render(snap)
    assert "not available" in rendered


def test_somaxconn_low_warns(tmp_path: Path) -> None:
    """128 ≤ threshold → ⚠. The classic legacy-default trap."""
    base = _make_proc_sys(tmp_path, {"net.core.somaxconn": "128\n"})
    snap = _capture(base=base)
    assert _somaxconn_too_low(snap)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" in head


def test_somaxconn_below_default_warns(tmp_path: Path) -> None:
    """Some images ship even lower defaults (32, 64). Must ⚠."""
    base = _make_proc_sys(tmp_path, {"net.core.somaxconn": "32\n"})
    snap = _capture(base=base)
    assert _somaxconn_too_low(snap)


def test_somaxconn_boundary(tmp_path: Path) -> None:
    """129 (one above threshold) must NOT ⚠ — strict ≤ boundary."""
    base = _make_proc_sys(tmp_path, {"net.core.somaxconn": "129\n"})
    snap = _capture(base=base)
    assert not _somaxconn_too_low(snap)


def test_somaxconn_modern_default_does_not_warn(tmp_path: Path) -> None:
    """4096 — Linux 5.4+ default. Healthy."""
    base = _make_proc_sys(tmp_path, {"net.core.somaxconn": "4096\n"})
    snap = _capture(base=base)
    assert not _somaxconn_too_low(snap)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_somaxconn_unparseable_no_warning(tmp_path: Path) -> None:
    """Garbage value → no ⚠. Absence/corruption of data is never
    a warning — cry-wolf prevention."""
    base = _make_proc_sys(tmp_path, {"net.core.somaxconn": "garbage\n"})
    snap = _capture(base=base)
    assert not _somaxconn_too_low(snap)


def test_other_keys_never_warn(tmp_path: Path) -> None:
    """Anti-cry-wolf pin: surprising values on non-somaxconn keys
    must not ⚠. They're policy choices (overcommit_memory=2 is
    intentional strict accounting; tiny port_range might be a
    deliberate sandbox)."""
    base = _make_proc_sys(
        tmp_path,
        {
            "net.core.somaxconn": "4096\n",
            "vm.overcommit_memory": "2\n",
            "net.ipv4.ip_local_port_range": "32768\t40000\n",
            "kernel.pid_max": "32768\n",
        },
    )
    snap = _capture(base=base)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_missing_key_renders_na(tmp_path: Path) -> None:
    """Stripped kernel / sandboxed namespace: key absent → 'n/a',
    no crash, no ⚠."""
    base = _make_proc_sys(tmp_path, {"net.core.somaxconn": "4096\n"})
    snap = _capture(base=base)
    # fs.file-max wasn't written → must render as n/a
    rendered = _render(snap)
    assert "n/a" in rendered


def test_multi_value_keys_kept_verbatim(tmp_path: Path) -> None:
    """ip_local_port_range is "low<TAB>high"; we keep the raw
    string so the operator sees the format the kernel uses.
    Splitting into a tuple would lose information."""
    base = _make_proc_sys(
        tmp_path,
        {
            "net.core.somaxconn": "4096\n",
            "net.ipv4.ip_local_port_range": "32768\t60999\n",
        },
    )
    snap = _capture(base=base)
    rendered = _render(snap)
    assert "32768" in rendered
    assert "60999" in rendered


def test_all_keys_appear_in_render(tmp_path: Path) -> None:
    """Operator-facing pin: every curated key must be listed in
    the card. Drift between the curated set and the rendered
    output would be exactly the bug a hand-written index is meant
    to catch."""
    base = _make_proc_sys(
        tmp_path,
        {
            "net.core.somaxconn": "4096",
            "net.ipv4.tcp_max_syn_backlog": "1024",
            "net.ipv4.ip_local_port_range": "32768\t60999",
            "net.ipv4.tcp_fin_timeout": "60",
            "net.ipv4.tcp_keepalive_time": "7200",
            "fs.file-max": "1000000",
            "kernel.pid_max": "4194304",
            "vm.overcommit_memory": "0",
        },
    )
    snap = _capture(base=base)
    rendered = _render(snap)
    for key in (
        "net.core.somaxconn",
        "net.ipv4.tcp_max_syn_backlog",
        "net.ipv4.ip_local_port_range",
        "net.ipv4.tcp_fin_timeout",
        "net.ipv4.tcp_keepalive_time",
        "fs.file-max",
        "kernel.pid_max",
        "vm.overcommit_memory",
    ):
        assert key in rendered


def test_unparseable_does_not_block_other_keys(tmp_path: Path) -> None:
    """One unparseable key must not break the card — other rows
    still render. Degrade-don't-crash posture."""
    base = _make_proc_sys(
        tmp_path,
        {
            "net.core.somaxconn": "garbage",
            "fs.file-max": "12345",
        },
    )
    snap = _capture(base=base)
    rendered = _render(snap)
    assert "fs.file-max" in rendered
    assert "12345" in rendered


def test_snapshot_render_when_unavailable() -> None:
    """Direct synthetic snapshot — proc_sys_available=False must
    render the explanatory note, not the header rows."""
    snap = _SysctlSnapshot(rows=(), proc_sys_available=False)
    rendered = _render(snap)
    assert "not available" in rendered
