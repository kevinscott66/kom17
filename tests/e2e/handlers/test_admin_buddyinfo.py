"""End-to-end ``/admin_buddyinfo``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Parser handles ``Node N, zone NAME c0 c1 …`` and produces correct
  per-row counts.
* Mid-update / non-int counts → row dropped, not partial.
* Non-Node lines (blank, comment, garbage) → ignored.
* Variable-width zones (kernel with different MAX_ORDER) parse — we
  don't hardcode 11.
* ⚠ predicate fires ONLY on high-order (>= _HIGH_ORDER_THRESHOLD)
  exhaustion. Low-order zero (order < threshold) must NOT ⚠ — that's
  the cry-wolf prevention pin.
* No-exhaustion render contains zero ⚠ markers.
* Exhausted-zone render contains ⚠ and points at relevant cards.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.buddyinfo import (
    _HIGH_ORDER_THRESHOLD,
    _BuddyinfoSnapshot,
    _capture,
    _has_high_order_exhaustion,
    _parse_buddyinfo,
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
        bot, make_message_update("/admin_buddyinfo", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_buddyinfo", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Memory fragmentation" in text


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
            "/admin_buddyinfo", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE_HEALTHY = (
    "Node 0, zone DMA 1 1 1 5 2 1 1 3 1 1 3\n"
    "Node 0, zone DMA32 156 43 22 21 12 5 4 3 2 4 412\n"
    "Node 0, zone Normal 1234 567 234 123 67 45 23 12 5 2 89\n"
)

# Normal zone with zero free blocks at orders 4..10 — high-order
# exhaustion that must trigger the ⚠ predicate.
_SAMPLE_FRAGMENTED = "Node 0, zone Normal 9999 8888 7777 6666 0 0 0 0 0 0 0\n"

# Low-order zero (order 1 = 0) but high orders fine. ⚠ must NOT fire
# — that's the cry-wolf prevention pin.
_SAMPLE_LOW_ORDER_ZERO = "Node 0, zone Normal 1000 0 500 300 200 100 50 25 10 5 1\n"


def test_parse_canonical() -> None:
    rows, max_order = _parse_buddyinfo(_SAMPLE_HEALTHY)
    assert max_order == 11
    assert len(rows) == 3
    normal = next(r for r in rows if r.zone == "Normal")
    assert normal.node == 0
    assert normal.counts[0] == 1234
    assert normal.counts[10] == 89


def test_parse_ignores_non_node_lines() -> None:
    text = "garbage\n\nNode 0, zone Normal 100 50 25\n# comment\n"
    rows, _ = _parse_buddyinfo(text)
    assert len(rows) == 1
    assert rows[0].zone == "Normal"


def test_parse_drops_non_int_count() -> None:
    """Non-int in count column → whole row dropped (kernel emits
    ints; non-int signals format change or corruption)."""
    text = "Node 0, zone Normal 100 garbage 25\nNode 0, zone DMA 1 2 3\n"
    rows, _ = _parse_buddyinfo(text)
    assert [r.zone for r in rows] == ["DMA"]


def test_parse_variable_max_order() -> None:
    """A kernel with shorter or longer count vector parses identically.
    We don't hardcode 11."""
    text = "Node 0, zone Tiny 1 2 3\n"
    rows, max_order = _parse_buddyinfo(text)
    assert max_order == 3
    assert rows[0].counts == (1, 2, 3)


def test_parse_multi_node() -> None:
    """NUMA host with multiple nodes — each Node row parses
    independently with correct node id."""
    text = (
        "Node 0, zone Normal 100 50 25 10 5 1 0 0 0 0 0\n"
        "Node 1, zone Normal 200 150 75 30 5 1 0 0 0 0 0\n"
    )
    rows, _ = _parse_buddyinfo(text)
    nodes = [r.node for r in rows]
    assert nodes == [0, 1]


def test_parse_missing_zone_keyword() -> None:
    """Malformed line lacking the ``zone`` keyword token at position 2
    → skipped (we don't try to recover; the file format is fixed)."""
    text = "Node 0, garbage Normal 1 2 3\nNode 0, zone Real 4 5 6\n"
    rows, _ = _parse_buddyinfo(text)
    assert [r.zone for r in rows] == ["Real"]


# --- ⚠ predicate -----------------------------------------------------------


def test_exhaustion_predicate_high_order_fires() -> None:
    rows, _ = _parse_buddyinfo(_SAMPLE_FRAGMENTED)
    snap = _BuddyinfoSnapshot(rows=rows, max_order=11, available=True)
    assert _has_high_order_exhaustion(snap)


def test_exhaustion_predicate_low_order_zero_does_not_fire() -> None:
    """Cry-wolf prevention pin: a single zero at order 1 (8 KiB
    blocks) must NOT trigger ⚠. The buddy allocator coalesces freely
    at low orders, so order-1=0 is a momentary state, not a
    fragmentation signal."""
    rows, _ = _parse_buddyinfo(_SAMPLE_LOW_ORDER_ZERO)
    snap = _BuddyinfoSnapshot(rows=rows, max_order=11, available=True)
    assert not _has_high_order_exhaustion(snap)


def test_exhaustion_threshold_boundary() -> None:
    """Zero at the threshold order itself fires; zero one order below
    does not. Pins the boundary so a refactor that shifts it is
    visible in tests."""
    # Vector long enough to cover threshold; zero at threshold.
    counts_at_threshold = [10] * (_HIGH_ORDER_THRESHOLD + 1)
    counts_at_threshold[_HIGH_ORDER_THRESHOLD] = 0
    text_at = "Node 0, zone X " + " ".join(str(c) for c in counts_at_threshold) + "\n"
    rows_at, _ = _parse_buddyinfo(text_at)
    assert _has_high_order_exhaustion(
        _BuddyinfoSnapshot(rows=rows_at, max_order=len(counts_at_threshold), available=True)
    )

    # Zero one order below threshold.
    counts_below = [10] * (_HIGH_ORDER_THRESHOLD + 1)
    counts_below[_HIGH_ORDER_THRESHOLD - 1] = 0
    text_below = "Node 0, zone Y " + " ".join(str(c) for c in counts_below) + "\n"
    rows_below, _ = _parse_buddyinfo(text_below)
    assert not _has_high_order_exhaustion(
        _BuddyinfoSnapshot(rows=rows_below, max_order=len(counts_below), available=True)
    )


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "buddyinfo"
    p.write_text(_SAMPLE_HEALTHY)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.rows) == 3


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _BuddyinfoSnapshot(rows=(), max_order=0, available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_healthy_no_warnings(tmp_path: Path) -> None:
    p = tmp_path / "buddyinfo"
    p.write_text(_SAMPLE_HEALTHY)
    snap = _capture(path=p)
    rendered = _render(snap)
    # Healthy host shows zone counts but no ⚠ markers anywhere.
    assert "Normal" in rendered
    assert "DMA32" in rendered
    body, _, _footer = rendered.partition("<i>No high-order exhaustion")
    assert "⚠" not in body


def test_render_low_order_zero_no_warnings(tmp_path: Path) -> None:
    """Cry-wolf prevention rendering pin: even with order-1 = 0 in the
    visible columns, no ⚠ markers anywhere in the card."""
    p = tmp_path / "buddyinfo"
    p.write_text(_SAMPLE_LOW_ORDER_ZERO)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_fragmented_has_warning(tmp_path: Path) -> None:
    p = tmp_path / "buddyinfo"
    p.write_text(_SAMPLE_FRAGMENTED)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" in rendered
    # Footer points at the related diagnostic cards.
    assert "/admin_vmstat" in rendered
    assert "/admin_meminfo" in rendered


def test_render_parse_failed_explains(tmp_path: Path) -> None:
    """Available file but unparseable → explicit note, no ⚠."""
    p = tmp_path / "buddyinfo"
    p.write_text("garbage\n")
    snap = _capture(path=p)
    assert snap.available
    rendered = _render(snap)
    assert "parse failed" in rendered or "empty" in rendered
    assert "⚠" not in rendered
