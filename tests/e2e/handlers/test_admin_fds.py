"""End-to-end ``/admin_fds``.

Pins:

* Non-developer → silent drop.
* Card renders total + by-kind breakdown when /proc/self/fd
  is readable (Linux real-host case).
* Non-Linux / unreadable /proc → "unavailable" message, no
  fake zeros.
* Every kind in _KINDS appears in the by-kind section even
  when its count is 0 — operator scans the same shape every
  time regardless of which buckets are empty.
* Group invocation → router-level private filter rejects.

Capture-layer tests use a synthesised fd directory with
symlinks so the classification logic is exercised without
needing a real /proc.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.fds import (
    _KINDS,
    _capture,
    _FDSnapshot,
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
        bot, make_message_update("/admin_fds", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_fds", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    # Card renders regardless of platform — header is universal;
    # body is either the live tally or the "unavailable" branch.
    assert "Open file descriptors" in text


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
            "/admin_fds",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_unavailable_branch() -> None:
    """Non-Linux / unreadable /proc/self/fd → "unavailable" hint.
    A bare zero count would lie — even an empty bot has stdin/
    stdout/stderr open. Surface absence explicitly."""
    rendered = _render(
        _FDSnapshot(available=False, total=0, by_kind={k: 0 for k in _KINDS}),
    )
    assert "unavailable" in rendered
    # Must NOT render a "total open: 0" line — that would be a lie
    # on any platform where the bot is actually running.
    assert "total open" not in rendered


def test_render_every_kind_appears_even_at_zero() -> None:
    """The by-kind block lists every kind in _KINDS regardless of
    count. Operator scans the same shape every sample — if zeros
    were elided, a leak in (say) the anon_inode bucket would
    silently disappear from the previous sample's diff."""
    rendered = _render(
        _FDSnapshot(
            available=True,
            total=5,
            by_kind={"regular": 5, "socket": 0, "pipe": 0, "anon_inode": 0, "other": 0},
        ),
    )
    for kind in _KINDS:
        assert kind in rendered, f"kind {kind!r} missing from card"
    assert "total open" in rendered


def test_capture_classifies_regular_file(tmp_path: Path) -> None:
    """A symlink under the fd tree pointing at a regular file
    must bucket as ``regular``. Validates the classify path
    without needing root or a live /proc."""
    real = tmp_path / "data.bin"
    real.write_bytes(b"x")
    fd_root = tmp_path / "fd"
    fd_root.mkdir()
    os.symlink(str(real), str(fd_root / "3"))
    snap = _capture(fd_root)
    assert snap.available is True
    assert snap.total == 1
    assert snap.by_kind["regular"] == 1


def test_capture_classifies_anon_inode(tmp_path: Path) -> None:
    """Symlinks whose target string starts with ``anon_inode:`` are
    the epoll/eventfd/timerfd kind — the readlink target is the
    only way to spot them (stat falls back through to the kernel
    pseudo-inode and lies). Validates the classify branch."""
    fd_root = tmp_path / "fd"
    fd_root.mkdir()
    # Linux symlink targets can be arbitrary strings, including
    # ones that don't resolve to a real path. Use the kernel-style
    # "anon_inode:[eventpoll]" target the classifier looks for.
    os.symlink("anon_inode:[eventpoll]", str(fd_root / "7"))
    snap = _capture(fd_root)
    assert snap.available is True
    assert snap.by_kind["anon_inode"] == 1


def test_capture_unavailable_when_root_missing(tmp_path: Path) -> None:
    """Pointing capture at a non-existent path is the macOS /
    Windows case. Must return ``available=False`` rather than
    raise — keeps the diagnostic useful on dev laptops."""
    snap = _capture(tmp_path / "does-not-exist")
    assert snap.available is False
    assert snap.total == 0
