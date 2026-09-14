"""End-to-end ``/admin_slabinfo``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux + root) OR unavailable note (macOS dev /
  Linux non-root). The note explicitly names both cases — operator
  must be able to distinguish "wrong OS" from "wrong uid" without
  shell access.
* Parser handles canonical SLUB output: ``slabinfo - version`` line
  + ``# name …`` comment + data rows with ``: tunables`` and
  ``: slabdata`` trailing groups.
* Lines with < 6 leading int columns are dropped.
* Non-int values poison just that row, not the whole parse.
* footprint = active_objs * objsize.
* Top-N rendering is sorted by footprint descending.
* ZERO ⚠ markers anywhere — workload-specific "big cache"
  thresholds (pinned explicitly so a refactor doesn't add one).
* Truncation note appears when caches > _TOP_N.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.slabinfo import (
    _TOP_N,
    _capture,
    _parse_slabinfo,
    _render,
    _SlabinfoSnapshot,
    _SlabRow,
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
        bot, make_message_update("/admin_slabinfo", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_slabinfo", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Kernel slab caches" in text


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
            "/admin_slabinfo", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "slabinfo - version: 2.1\n"
    "# name <active_objs> <num_objs> <objsize> <objperslab> <pagesperslab> :"
    " tunables <limit> <batchcount> <sharedfactor> :"
    " slabdata <active_slabs> <num_slabs> <sharedavail>\n"
    "dentry          50000  60000   192 42  2 : tunables 0 0 0 :"
    " slabdata 1500 1500 0\n"
    "inode_cache     20000  25000   600 13  2 : tunables 0 0 0 :"
    " slabdata 500  500  0\n"
    "kmalloc-256     1000   1500    256 32  2 : tunables 0 0 0 :"
    " slabdata 50   50   0\n"
    "task_struct     200    250    9216 3   8 : tunables 0 0 0 :"
    " slabdata 80   80   0\n"
)


def test_parse_canonical() -> None:
    rows, version = _parse_slabinfo(_SAMPLE)
    assert version == "2.1"
    assert len(rows) == 4
    by_name = {r.name: r for r in rows}
    assert by_name["dentry"].active_objs == 50_000
    assert by_name["dentry"].objsize == 192
    assert by_name["dentry"].footprint == 50_000 * 192
    assert by_name["task_struct"].footprint == 200 * 9216


def test_parse_version_default_when_missing() -> None:
    rows, version = _parse_slabinfo("dentry 1 2 3 4 5 : tunables 0 0 0\n")
    assert version == "unknown"
    assert len(rows) == 1


def test_parse_skip_header_comment() -> None:
    """Lines starting with ``#`` are header comments, not data."""
    text = (
        "# name <active_objs> <num_objs> <objsize> ...\nreal_cache 10 20 64 8 1 : tunables 0 0 0\n"
    )
    rows, _ = _parse_slabinfo(text)
    assert [r.name for r in rows] == ["real_cache"]


def test_parse_short_row_dropped() -> None:
    """Row with fewer than 6 leading columns is dropped — not a
    valid data row."""
    text = "short 1 2 3\nfull 10 20 32 4 1 : tunables 0 0 0\n"
    rows, _ = _parse_slabinfo(text)
    assert [r.name for r in rows] == ["full"]


def test_parse_non_int_field_dropped() -> None:
    """Non-int in a numeric column → row dropped, not partial."""
    text = "bad 10 garbage 32 4 1 : tunables\ngood 10 20 32 4 1 : tunables 0 0 0\n"
    rows, _ = _parse_slabinfo(text)
    assert [r.name for r in rows] == ["good"]


def test_parse_empty_text() -> None:
    rows, version = _parse_slabinfo("")
    assert rows == ()
    assert version == "unknown"


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    """Both ENOENT (macOS) and EACCES (Linux non-root) end up
    here — _capture catches OSError without distinguishing."""
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.rows == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "slabinfo"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.rows) == 4
    assert snap.version == "2.1"


# --- rendering -------------------------------------------------------------


def test_render_unavailable_names_both_failure_modes() -> None:
    """The unavailable note must mention BOTH the macOS-dev case AND
    the Linux-non-root case, because /proc/slabinfo's 0o400 default
    is a much more common reason for the failure than no-procfs."""
    snap = _SlabinfoSnapshot(rows=(), version="unknown", available=False)
    rendered = _render(snap)
    assert "non-root" in rendered or "permissions" in rendered
    assert "macOS" in rendered or "non-procfs" in rendered
    assert "⚠" not in rendered


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "slabinfo"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    rendered = _render(snap)
    # All four caches show up.
    assert "dentry" in rendered
    assert "inode_cache" in rendered
    assert "kmalloc-256" in rendered
    assert "task_struct" in rendered
    # Footprint header + version surfaces.
    assert "Total slab footprint" in rendered
    assert "2.1" in rendered


def test_render_sorted_by_footprint(tmp_path: Path) -> None:
    """task_struct (200 × 9216 = 1.84 MiB) is bigger than
    kmalloc-256 (1000 × 256 = 256 KiB), so should appear earlier in
    the top list. dentry (50000 × 192 = 9.16 MiB) is the largest."""
    p = tmp_path / "slabinfo"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    rendered = _render(snap)
    dentry_pos = rendered.index("dentry")
    inode_pos = rendered.index("inode_cache")
    task_pos = rendered.index("task_struct")
    kmalloc_pos = rendered.index("kmalloc-256")
    # dentry first (9.16 MiB), inode_cache second (12 MiB > task_struct?)
    # inode_cache: 20000 × 600 = 12 MiB — actually largest.
    # Order: inode_cache (12), dentry (9.16), task_struct (1.84), kmalloc (0.25)
    assert inode_pos < dentry_pos < task_pos < kmalloc_pos


def test_render_no_warnings_regardless_of_size(tmp_path: Path) -> None:
    """Cry-wolf prevention pin: an absurdly large dentry cache must
    NOT produce ⚠. &quot;Big cache&quot; is workload-specific —
    operator policy, not card policy."""
    p = tmp_path / "slabinfo"
    p.write_text("slabinfo - version: 2.1\ndentry 99999999 100000000 192 42 2 : tunables 0 0 0\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_truncation_note(tmp_path: Path) -> None:
    """More caches than _TOP_N → explicit truncation note. Pin so a
    refactor that quietly drops the cap-acknowledgment is visible."""
    lines = ["slabinfo - version: 2.1"]
    big = _TOP_N + 5
    for i in range(big):
        lines.append(f"cache{i} {i + 1} {i + 10} 64 8 1 : tunables 0 0 0")
    p = tmp_path / "slabinfo"
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "smaller caches not shown" in rendered


def test_render_parse_failed_explains(tmp_path: Path) -> None:
    """Available file but unparseable contents → explicit note, no ⚠."""
    p = tmp_path / "slabinfo"
    p.write_text("garbage\nstill garbage\n")
    snap = _capture(path=p)
    assert snap.available
    rendered = _render(snap)
    assert "parse failed" in rendered or "empty" in rendered
    assert "⚠" not in rendered


def test_slab_row_footprint_computed() -> None:
    """Footprint is precomputed in __init__ — pinned so a refactor
    that makes it a property doesn't accidentally double-compute on
    the sort key."""
    row = _SlabRow(
        name="x",
        active_objs=100,
        num_objs=200,
        objsize=64,
        objperslab=8,
        pagesperslab=1,
    )
    assert row.footprint == 100 * 64
