"""End-to-end ``/admin_swaps``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* ZERO ⚠ in body regardless of swap state — pure information by
  design (swap config is operator intent). Pinned explicitly so a
  future refactor doesn't accidentally add a swap-usage marker.
* Empty swap list renders the "no swap configured" explanatory
  note — operator sees the absence is meaningful, not a bug.
* Multi-area swap renders an aggregate summary.
* kB→bytes conversion at the parse boundary.
* Header line ("Filename Type Size...") skipped.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.swaps import (
    _capture,
    _parse_swaps,
    _render,
    _SwapsSnapshot,
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
        bot, make_message_update("/admin_swaps", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_swaps", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Swap layout" in text


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
        make_message_update("/admin_swaps", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ---------------------------------------------------------------


def test_parse_basic() -> None:
    """Standard /proc/swaps: header + one swap area."""
    text = (
        "Filename                                Type            Size    Used    Priority\n"
        "/dev/sda5                               partition       8388604 0       -2\n"
    )
    rows = _parse_swaps(text)
    assert len(rows) == 1
    assert rows[0].filename == "/dev/sda5"
    assert rows[0].swap_type == "partition"
    # 8388604 kB * 1024 = bytes
    assert rows[0].size_bytes == 8388604 * 1024
    assert rows[0].used_bytes == 0
    assert rows[0].priority == -2


def test_parse_multiple_areas() -> None:
    """Multi-area swap: file + partition + different priorities."""
    text = (
        "Filename                                Type            Size    Used    Priority\n"
        "/dev/sda5                               partition       1000000 1000    10\n"
        "/swapfile                               file            500000  0       5\n"
    )
    rows = _parse_swaps(text)
    assert len(rows) == 2
    assert rows[0].swap_type == "partition"
    assert rows[0].used_bytes == 1000 * 1024
    assert rows[1].swap_type == "file"
    assert rows[1].priority == 5


def test_parse_skips_header() -> None:
    """The "Filename Type Size Used Priority" header must be
    skipped — otherwise it'd parse as a row with size="Size"
    (ValueError) which would be dropped anyway, but pinning the
    intentional skip is clearer."""
    text = "Filename Type Size Used Priority\n"
    assert _parse_swaps(text) == ()


def test_parse_skips_short_lines() -> None:
    """<5 fields = not a real swap line. Drop rather than guess."""
    text = "Filename Type Size Used Priority\nincomplete line\n/dev/sda5 partition 1000 0 -2\n"
    rows = _parse_swaps(text)
    assert len(rows) == 1


def test_parse_skips_malformed_int() -> None:
    """Non-int size → drop that one row, keep others."""
    text = (
        "Filename Type Size Used Priority\n"
        "/dev/sda5 partition garbage 0 -2\n"
        "/swapfile file 500 0 5\n"
    )
    rows = _parse_swaps(text)
    assert len(rows) == 1
    assert rows[0].filename == "/swapfile"


# --- capture --------------------------------------------------------------


def test_capture_unavailable_when_path_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "does_not_exist")
    assert not snap.available
    assert snap.rows == ()


def test_capture_zero_swap(tmp_path: Path) -> None:
    """Many production hosts have zero swap configured — the file
    exists with only the header. Must produce empty rows, not
    crash."""
    p = tmp_path / "swaps"
    p.write_text("Filename Type Size Used Priority\n")
    snap = _capture(path=p)
    assert snap.available
    assert snap.rows == ()


# --- rendering ------------------------------------------------------------


def test_no_warnings_on_any_state(tmp_path: Path) -> None:
    """Cry-wolf prevention pin: heavy swap usage on multiple
    devices must NOT ⚠. Swap doing its job IS swap doing its
    job — and the config itself is operator intent."""
    p = tmp_path / "swaps"
    p.write_text(
        "Filename Type Size Used Priority\n"
        "/dev/sda5 partition 1000000 999999 -2\n"  # 99.99% used
        "/swapfile file 500000 499999 -1\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    # ⚠ should NEVER appear anywhere in this card — there's no
    # disclaimer footer mentioning ⚠ either (cleaner test than
    # partitioning).
    assert "⚠" not in rendered


def test_empty_swap_explains() -> None:
    """No swap areas → explanatory text. Critical pin: an empty
    table is meaningful (OOM-kill semantics), not a bug — the
    operator must see that distinction explicitly."""
    snap = _SwapsSnapshot(rows=(), available=True)
    rendered = _render(snap)
    assert "no swap" in rendered.lower()
    assert "OOM" in rendered


def test_unavailable_explains() -> None:
    """macOS dev → explicit unavailable note."""
    snap = _SwapsSnapshot(rows=(), available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered


def test_aggregate_summary_appears_for_multi(tmp_path: Path) -> None:
    """Multi-area swap: aggregate line spares the operator mental
    addition."""
    p = tmp_path / "swaps"
    p.write_text(
        "Filename Type Size Used Priority\n"
        "/dev/sda5 partition 1024 100 -2\n"  # 1 MiB / 100 KiB
        "/swapfile file 2048 200 -1\n"  # 2 MiB / 200 KiB
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "Aggregate" in rendered


def test_single_area_no_aggregate(tmp_path: Path) -> None:
    """Single swap area: no aggregate line (redundant noise)."""
    p = tmp_path / "swaps"
    p.write_text("Filename Type Size Used Priority\n/dev/sda5 partition 1024 100 -2\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "Aggregate" not in rendered


def test_percentage_decoration(tmp_path: Path) -> None:
    """Per-area used% is decoration — verify it renders, but no
    ⚠ trigger."""
    p = tmp_path / "swaps"
    p.write_text(
        "Filename Type Size Used Priority\n/dev/sda5 partition 1000 500 -2\n"  # 50%
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "50.0%" in rendered
    assert "⚠" not in rendered
