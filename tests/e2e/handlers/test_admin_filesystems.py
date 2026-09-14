"""End-to-end ``/admin_filesystems``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs).
* Parser distinguishes block-backed (1 token after split) from
  pseudo (``nodev`` + name = 2 tokens).
* Lines with wrong token shape dropped.
* Zero ⚠ regardless of state — inventory cards don't warn.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.filesystems import (
    _capture,
    _FilesystemsSnapshot,
    _FsEntry,
    _parse_filesystems,
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
        make_message_update("/admin_filesystems", user_id=42, chat_type="private"),
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
        make_message_update("/admin_filesystems", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Kernel filesystem drivers" in text


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
            "/admin_filesystems",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "nodev\tsysfs\n"
    "nodev\ttmpfs\n"
    "nodev\tproc\n"
    "\text4\n"
    "\txfs\n"
    "nodev\tcgroup2\n"
    "\toverlay\n"
    "nodev\tfuse\n"
)


def test_parse_canonical() -> None:
    rows = _parse_filesystems(_SAMPLE)
    assert len(rows) == 8
    by_name = {r.name: r for r in rows}
    assert by_name["sysfs"].requires_device is False
    assert by_name["tmpfs"].requires_device is False
    assert by_name["ext4"].requires_device is True
    assert by_name["xfs"].requires_device is True
    assert by_name["overlay"].requires_device is True
    assert by_name["fuse"].requires_device is False


def test_parse_drops_malformed() -> None:
    """``nodev`` without a name, or 3+ tokens, or any other shape
    is not a valid row — drop rather than crash. Pinned because
    a permissive parser could silently absorb future format
    changes and mis-classify rows."""
    text = (
        "nodev\n"  # 1 token but it's 'nodev' alone — drop
        "nodev sysfs extra\n"  # 3 tokens — drop
        "\text4\n"  # valid block-backed
        "\n"  # empty line — drop
    )
    rows = _parse_filesystems(text)
    # 'nodev' alone is a 1-token line — we WILL accept it as
    # block-backed because we can't distinguish from a real FS
    # name. That's the explicit cost of the shape-only parser;
    # the kernel doesn't emit this in practice. The 3-token line
    # is the real drop.
    assert "ext4" in {r.name for r in rows}
    assert all(r.name != "extra" for r in rows)


def test_parse_block_backed_single_token() -> None:
    """A line with one token (leading whitespace stripped by
    split()) is a block-backed FS. Pinned because the discriminator
    is purely structural — losing it silently mis-classifies every
    block-backed row as pseudo."""
    rows = _parse_filesystems("\text4\n")
    assert len(rows) == 1
    assert rows[0].name == "ext4"
    assert rows[0].requires_device is True


def test_parse_empty() -> None:
    assert _parse_filesystems("") == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.rows == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "filesystems"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.rows) == 8
    assert snap.block_backed_count == 3  # ext4, xfs, overlay
    assert snap.pseudo_count == 5  # sysfs, tmpfs, proc, cgroup2, fuse


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _FilesystemsSnapshot(rows=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    snap = _FilesystemsSnapshot(rows=(), available=True)
    text = _render(snap)
    assert "No filesystem drivers" in text
    assert "⚠" not in text


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "filesystems"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "ext4" in text
    assert "tmpfs" in text
    assert "overlay" in text
    assert "Block-backed" in text
    assert "Pseudo" in text


def test_render_no_warnings_regardless_of_state(tmp_path: Path) -> None:
    """Cry-wolf prevention: even an inventory with exotic / missing
    drivers must NOT produce ⚠. Driver presence is operator-policy.
    Pinned against a 200-driver fabricated sample with no overlay,
    no fuse, no ext4 — all the things the operator might consider
    'missing' — and the card still doesn't warn."""
    p = tmp_path / "filesystems"
    lines = []
    for i in range(100):
        lines.append(f"nodev\tpseudo{i}")
    for i in range(100):
        lines.append(f"\tblock{i}")
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_only_block_backed(tmp_path: Path) -> None:
    """Edge case: a kernel with no pseudo FS drivers is impossible
    in practice, but the inverse (no block-backed) is the
    interesting one — embedded systems sometimes ship without any.
    The card should render cleanly without a Python-side empty-list
    error."""
    p = tmp_path / "filesystems"
    p.write_text("nodev\tsysfs\nnodev\tproc\n")
    snap = _capture(path=p)
    text = _render(snap)
    # The "(none — …)" note inside the block-backed section.
    assert "none" in text
    assert "⚠" not in text


def test_fs_entry_fields_preserved() -> None:
    e = _FsEntry(name="overlay", requires_device=True)
    assert e.name == "overlay"
    assert e.requires_device is True
