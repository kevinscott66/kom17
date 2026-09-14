"""End-to-end ``/admin_zoneinfo``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Parser handles the ``Node N, zone NAME`` block format with the
  ``pages free 1234`` prefix-stripped watermark trio and other
  indented kv pairs.
* protection: line (multi-value tuple) is skipped, not parsed as
  a single int.
* Non-int values for known keys → pair skipped, block survives.
* DMA / Device zones are intentionally NOT surfaced in the
  rendered card even when present on the snapshot — they're tiny
  and routinely near their watermarks; including them would
  produce false-positive markers. The cry-wolf prevention pin
  asserts a DMA zone at min must NOT trigger ⚠.
* free <= low → ⚠ + kswapd-active footer.
* free <= min → ⚠ + direct-reclaim footer (stricter wording).
* Missing watermark / missing free in the block → predicates return
  False (degrade-don't-fire on incomplete parses).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.zoneinfo import (
    _RENDERED_ZONES,
    _capture,
    _has_direct_reclaim,
    _has_pressure,
    _parse_zoneinfo,
    _render,
    _ZoneinfoSnapshot,
    _ZoneStats,
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
        bot, make_message_update("/admin_zoneinfo", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_zoneinfo", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Zone watermarks" in text


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
            "/admin_zoneinfo", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE_HEALTHY = (
    "Node 0, zone DMA\n"
    "  pages free 100\n"
    "        min 10\n"
    "        low 12\n"
    "        high 15\n"
    "Node 0, zone DMA32\n"
    "  pages free 50000\n"
    "        min 678\n"
    "        low 848\n"
    "        high 1018\n"
    "  protection: (0, 0, 0, 0)\n"
    "Node 0, zone Normal\n"
    "  pages free 1000000\n"
    "        min 5000\n"
    "        low 6250\n"
    "        high 7500\n"
    "        present 2097152\n"
)


def test_parse_canonical() -> None:
    zones = _parse_zoneinfo(_SAMPLE_HEALTHY)
    assert len(zones) == 3
    by_zone = {z.zone: z for z in zones}
    assert by_zone["Normal"].free == 1_000_000
    assert by_zone["Normal"].min == 5_000
    assert by_zone["Normal"].low == 6_250
    assert by_zone["Normal"].high == 7_500
    assert by_zone["DMA32"].free == 50_000
    # protection: line is skipped (multi-value tuple), not stored
    # under any key.
    assert "protection:" not in by_zone["DMA32"].fields
    assert "protection" not in by_zone["DMA32"].fields


def test_parse_ignores_preamble() -> None:
    """Lines before the first Node block are discarded — no current
    block to attach kv pairs to."""
    text = (
        "garbage preamble\n"
        "  min 99\n"  # would attach somewhere if state was wrong
        "Node 0, zone Normal\n"
        "  pages free 100\n"
    )
    zones = _parse_zoneinfo(text)
    assert len(zones) == 1
    assert zones[0].fields == {"free": 100}


def test_parse_non_int_value_skipped() -> None:
    """A non-int value for a known key skips the pair, doesn't drop
    the block."""
    text = "Node 0, zone Normal\n  pages free abc\n        min 100\n"
    zones = _parse_zoneinfo(text)
    assert "free" not in zones[0].fields
    assert zones[0].fields["min"] == 100


def test_parse_malformed_node_line_skipped() -> None:
    """Malformed Node header (no ``zone`` keyword) → no block
    started, subsequent kv pairs go into the previous block."""
    text = "Node 0, zone Normal\n  pages free 100\nNode 0, junk Wrong\n        min 999\n"
    zones = _parse_zoneinfo(text)
    # min went into Normal because no new block was opened.
    assert len(zones) == 1
    assert zones[0].fields["free"] == 100
    assert zones[0].fields["min"] == 999


# --- predicates ------------------------------------------------------------


def test_under_low_predicate() -> None:
    zone = _ZoneStats(node=0, zone="Normal", fields={"free": 50, "min": 30, "low": 60, "high": 90})
    assert zone.under_low
    assert not zone.under_min


def test_under_min_implies_under_low() -> None:
    zone = _ZoneStats(node=0, zone="Normal", fields={"free": 20, "min": 30, "low": 60, "high": 90})
    assert zone.under_low
    assert zone.under_min


def test_missing_fields_do_not_fire_predicate() -> None:
    """Degrade-don't-fire: incomplete parse must NOT trigger ⚠.
    Missing free or missing low → False, even though "obviously"
    pressure-implying state would be missing.free + present.low."""
    z_no_free = _ZoneStats(node=0, zone="Normal", fields={"min": 30, "low": 60})
    assert not z_no_free.under_low
    assert not z_no_free.under_min

    z_no_low = _ZoneStats(node=0, zone="Normal", fields={"free": 1, "min": 30})
    assert not z_no_low.under_low


def test_pressure_predicates_only_count_rendered_zones() -> None:
    """Cry-wolf prevention pin: a DMA zone under its watermark must
    NOT trip the snapshot-level pressure predicates. DMA is tiny
    and frequently near min on idle hosts — including it would
    fire ⚠ on healthy systems. Predicates filter to _RENDERED_ZONES."""
    # DMA under min, Normal healthy.
    dma = _ZoneStats(node=0, zone="DMA", fields={"free": 5, "min": 100, "low": 200, "high": 300})
    normal = _ZoneStats(
        node=0,
        zone="Normal",
        fields={"free": 1_000_000, "min": 5000, "low": 6250, "high": 7500},
    )
    snap = _ZoneinfoSnapshot(zones=(dma, normal), available=True)
    assert not _has_pressure(snap)
    assert not _has_direct_reclaim(snap)
    # And sanity: DMA itself does show under_min at the zone level,
    # confirming the filter is in the snapshot predicates and not
    # in the zone-state machinery.
    assert dma.under_min


def test_rendered_zones_set_includes_normal() -> None:
    """Pin the curated zone set — refactor that drops Normal would
    silently kill the operationally-most-important reading."""
    assert "Normal" in _RENDERED_ZONES


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "zoneinfo"
    p.write_text(_SAMPLE_HEALTHY)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.zones) == 3


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _ZoneinfoSnapshot(zones=(), available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_healthy_no_warnings(tmp_path: Path) -> None:
    p = tmp_path / "zoneinfo"
    p.write_text(_SAMPLE_HEALTHY)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "Normal" in rendered
    assert "DMA32" in rendered
    # DMA is filtered out of the curated render even though it's in
    # the parsed snapshot.
    body, _, _footer = rendered.partition("<i>No reclaim activity")
    # The DMA *zone* won't appear in the body (only Normal/DMA32);
    # this also confirms no markers in the body.
    assert "Node 0 DMA:" not in body
    assert "⚠" not in body


def test_render_dma_at_min_no_warnings(tmp_path: Path) -> None:
    """Cry-wolf rendering pin: even with DMA literally at min, the
    rendered card must have ZERO ⚠ markers anywhere. DMA is the
    classic false-positive zone."""
    text = (
        "Node 0, zone DMA\n"
        "  pages free 5\n"
        "        min 100\n"
        "        low 200\n"
        "        high 300\n"
        "Node 0, zone Normal\n"
        "  pages free 1000000\n"
        "        min 5000\n"
        "        low 6250\n"
        "        high 7500\n"
    )
    p = tmp_path / "zoneinfo"
    p.write_text(text)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_kswapd_active(tmp_path: Path) -> None:
    """Normal zone free <= low (but > min) → kswapd-active footer
    wording, ⚠ on the row."""
    text = (
        "Node 0, zone Normal\n"
        "  pages free 6000\n"
        "        min 5000\n"
        "        low 6250\n"
        "        high 7500\n"
    )
    p = tmp_path / "zoneinfo"
    p.write_text(text)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" in rendered
    assert "kswapd" in rendered.lower()


def test_render_direct_reclaim(tmp_path: Path) -> None:
    """Normal zone free <= min → direct-reclaim footer wording."""
    text = (
        "Node 0, zone Normal\n"
        "  pages free 4000\n"
        "        min 5000\n"
        "        low 6250\n"
        "        high 7500\n"
    )
    p = tmp_path / "zoneinfo"
    p.write_text(text)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" in rendered
    assert "direct reclaim" in rendered.lower()


def test_render_no_curated_zones_present(tmp_path: Path) -> None:
    """Kernel exposes only DMA (no Normal/DMA32/Movable) → explicit
    note, not silent empty render."""
    text = "Node 0, zone DMA\n  pages free 100\n        min 10\n        low 12\n        high 15\n"
    p = tmp_path / "zoneinfo"
    p.write_text(text)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "curated zones" in rendered.lower()


def test_render_parse_failed_explains(tmp_path: Path) -> None:
    """Available file but parser yields no zones → explicit note."""
    p = tmp_path / "zoneinfo"
    p.write_text("garbage\nmore garbage\n")
    snap = _capture(path=p)
    assert snap.available
    rendered = _render(snap)
    assert "parse failed" in rendered or "empty" in rendered
    assert "⚠" not in rendered
