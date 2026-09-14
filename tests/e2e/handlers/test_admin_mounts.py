"""End-to-end ``/admin_mounts``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* rootfs readonly triggers ⚠ in body (hoisted above the table).
* rootfs rw keeps body ⚠-free even when other mounts are ro
  (anti-cry-wolf pin: per-mount ro is legitimate for squashfs,
  configmap volumes, overlay lowerdirs).
* noexec/nosuid/nodev surfaced as flags but NOT ⚠'d (security
  hardening, not a fault).
* Long mount tables truncate with an explicit hidden-count note,
  not silent drop.
* Empty mount table renders explanatory text, not a parser-bug
  blank screen.
* Group invocation → router-level private filter rejects.

Parser unit tests use tmp_path so the suite runs on macOS too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.mounts import (
    _capture,
    _MountRow,
    _MountsSnapshot,
    _parse_mounts,
    _render,
    _rootfs_readonly,
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
        bot, make_message_update("/admin_mounts", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_mounts", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Mount table" in text


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
        make_message_update("/admin_mounts", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_parse_basic() -> None:
    """Standard /proc/self/mounts line: 6 fields, options as
    comma-separated tokens."""
    text = (
        "/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0\n"
        "tmpfs /tmp tmpfs rw,nosuid,nodev,size=2G 0 0\n"
    )
    rows = _parse_mounts(text)
    assert len(rows) == 2
    assert rows[0].mountpoint == "/"
    assert rows[0].fstype == "ext4"
    assert "rw" in rows[0].options
    assert "errors=remount-ro" in rows[0].options
    assert rows[1].mountpoint == "/tmp"
    assert "nosuid" in rows[1].options


def test_parse_skips_short_lines() -> None:
    """Short lines degrade — they're not in /proc/self/mounts on a
    real kernel, but a parser must not crash if it sees one."""
    text = "incomplete line\n/dev/sda1 / ext4 rw 0 0\n"
    rows = _parse_mounts(text)
    assert len(rows) == 1
    assert rows[0].mountpoint == "/"


def test_capture_unavailable_when_path_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "does_not_exist")
    assert not snap.available
    assert snap.rows == ()


def test_rootfs_readonly_predicate(tmp_path: Path) -> None:
    """The single ⚠ predicate of this card. rootfs mounted ro →
    True; rw → False; absent → False (degraded snapshot, not a
    warning)."""
    p = tmp_path / "mounts"
    p.write_text("/dev/sda1 / ext4 ro,relatime 0 0\n")
    snap = _capture(path=p)
    assert _rootfs_readonly(snap)

    p.write_text("/dev/sda1 / ext4 rw,relatime 0 0\n")
    snap = _capture(path=p)
    assert not _rootfs_readonly(snap)

    p.write_text("tmpfs /tmp tmpfs rw 0 0\n")  # no rootfs row
    snap = _capture(path=p)
    assert not _rootfs_readonly(snap)


def test_render_rootfs_readonly_warns(tmp_path: Path) -> None:
    """When rootfs is ro, ⚠ appears in body. Hoisted above the
    table so a 40-row scroll doesn't bury it."""
    p = tmp_path / "mounts"
    p.write_text("/dev/sda1 / ext4 ro,relatime 0 0\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" in head
    assert "readonly" in head


def test_render_per_mount_ro_does_not_warn(tmp_path: Path) -> None:
    """Anti-cry-wolf pin: a non-rootfs ro mount (squashfs,
    configmap, overlay lowerdir) must NOT trigger ⚠ in the body.
    The flag is shown, but no marker."""
    p = tmp_path / "mounts"
    p.write_text(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "squashfs /var/snap/foo squashfs ro,relatime 0 0\n"
        "tmpfs /run/configmap tmpfs ro 0 0\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head
    # The ro flag is still surfaced — operator sees it, just no
    # marker.
    assert "ro" in rendered


def test_render_noexec_nosuid_nodev_not_warned(tmp_path: Path) -> None:
    """Security-hardening flags must surface as flags but never
    drive ⚠. Pinned because they LOOK alarming in isolation but
    are routine on /tmp, /dev/shm, /home, etc."""
    p = tmp_path / "mounts"
    p.write_text("/dev/sda1 / ext4 rw,relatime 0 0\ntmpfs /tmp tmpfs rw,nosuid,nodev,noexec 0 0\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head
    assert "noexec" in rendered
    assert "nosuid" in rendered
    assert "nodev" in rendered


def test_render_truncates_long_table(tmp_path: Path) -> None:
    """A 100-row mount table must truncate with an explicit hidden
    count — silent drops would defeat the diagnostic purpose."""
    p = tmp_path / "mounts"
    lines = ["/dev/sda1 / ext4 rw 0 0"]
    for i in range(100):
        lines.append(f"tmpfs /mnt/x{i} tmpfs rw 0 0")
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "hidden" in rendered
    assert "Total mounts: 101" in rendered


def test_render_empty_table_explains() -> None:
    """Empty mount table inside chroot/namespace must render an
    explanation, not a confusing blank card."""
    snap = _MountsSnapshot(rows=(), available=True)
    rendered = _render(snap)
    assert "empty" in rendered


def test_render_unavailable_explains() -> None:
    """macOS dev: must render explicit unavailable note."""
    snap = _MountsSnapshot(rows=(), available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered


def test_options_set_contains_kv_pairs() -> None:
    """The parsed options frozenset keeps key=value tokens intact
    so predicates can exact-match (e.g. ``errors=remount-ro``).
    Stripping the value would lose information."""
    row = _MountRow(
        device="/dev/sda1",
        mountpoint="/",
        fstype="ext4",
        options_raw="rw,errors=remount-ro,data=ordered",
        options=frozenset({"rw", "errors=remount-ro", "data=ordered"}),
    )
    assert "errors=remount-ro" in row.options
    assert "data=ordered" in row.options
