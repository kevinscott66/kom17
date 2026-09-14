"""End-to-end ``/admin_max_map_count``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note (only when BOTH sources missing).
* Partial availability: one present, other missing → render shows
  the present one, 'unknown' for the missing one.
* Cry-wolf must-not-fire on canonical-healthy sample
  (200 maps of 65530 default).
* ⚠ fires when ratio crosses 80%.
* Defensive: ceiling=0 / sentinel doesn't fire spurious ⚠.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.max_map_count import (
    _MAP_WARN_RATIO,
    _capture,
    _count_maps,
    _fmt,
    _fmt_pct,
    _MaxMapSnapshot,
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
        bot, make_message_update("/admin_max_map_count", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_max_map_count", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "VMA" in text or "max_map_count" in text


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
            "/admin_max_map_count", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parsers ---------------------------------------------------------------


def test_read_int_present(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("65530\n")
    assert _read_int(p) == 65530


def test_read_int_missing(tmp_path: Path) -> None:
    assert _read_int(tmp_path / "absent") == -1


def test_read_int_non_numeric(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("garbage\n")
    assert _read_int(p) == -1


def test_count_maps_canonical(tmp_path: Path) -> None:
    """Three non-empty lines → count 3. The kernel format is one
    VMA per line; we mirror the operator's mental model of
    ``wc -l /proc/self/maps``."""
    p = tmp_path / "maps"
    p.write_text(
        "55a3b1c00000-55a3b1c0a000 r--p 00000000 fd:00 1 /usr/bin/python3\n"
        "55a3b1c0a000-55a3b1c8c000 r-xp 0000a000 fd:00 1 /usr/bin/python3\n"
        "55a3b1c8c000-55a3b1cad000 r--p 0008c000 fd:00 1 /usr/bin/python3\n"
    )
    assert _count_maps(p) == 3


def test_count_maps_skips_empty_lines(tmp_path: Path) -> None:
    """Defensive: future LSM or kernel patch that emits blank
    separator lines must not inflate the count."""
    p = tmp_path / "maps"
    p.write_text("one\n\ntwo\n\n\nthree\n")
    assert _count_maps(p) == 3


def test_count_maps_missing(tmp_path: Path) -> None:
    assert _count_maps(tmp_path / "absent") == -1


def test_count_maps_empty(tmp_path: Path) -> None:
    p = tmp_path / "maps"
    p.write_text("")
    assert _count_maps(p) == 0


# --- ratio + warn ---------------------------------------------------------


def test_usage_ratio_basic() -> None:
    snap = _MaxMapSnapshot(maps_count=800, max_map_count=1000, available=True)
    assert snap.usage_ratio == 0.8
    assert snap.under_pressure is True


def test_usage_ratio_below_threshold() -> None:
    snap = _MaxMapSnapshot(maps_count=200, max_map_count=65530, available=True)
    assert snap.under_pressure is False


def test_ceiling_zero_no_crash() -> None:
    snap = _MaxMapSnapshot(maps_count=100, max_map_count=0, available=True)
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_maps_sentinel_no_crash() -> None:
    snap = _MaxMapSnapshot(maps_count=-1, max_map_count=65530, available=True)
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_threshold_constant_sane() -> None:
    assert 0.5 < _MAP_WARN_RATIO < 1.0


# --- capture ---------------------------------------------------------------


def test_capture_all_missing(tmp_path: Path) -> None:
    snap = _capture(
        max_map_count_path=tmp_path / "no1",
        self_maps_path=tmp_path / "no2",
    )
    assert not snap.available


def test_capture_partial_max_only(tmp_path: Path) -> None:
    mc = tmp_path / "max"
    mc.write_text("65530\n")
    snap = _capture(max_map_count_path=mc, self_maps_path=tmp_path / "absent")
    assert snap.available
    assert snap.max_map_count == 65530
    assert snap.maps_count == -1


def test_capture_all_present(tmp_path: Path) -> None:
    mc = tmp_path / "max"
    mc.write_text("65530\n")
    mp = tmp_path / "maps"
    mp.write_text("a\nb\nc\n")
    snap = _capture(max_map_count_path=mc, self_maps_path=mp)
    assert snap.available
    assert snap.max_map_count == 65530
    assert snap.maps_count == 3


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _MaxMapSnapshot(maps_count=-1, max_map_count=-1, available=False)
    text = _render(snap)
    assert "unreadable" in text or "non-procfs" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy() -> None:
    """Cry-wolf pin: realistic healthy sample. 200 VMAs of
    65530 default — ratio ~0.3%. ⚠ MUST NOT appear."""
    snap = _MaxMapSnapshot(maps_count=200, max_map_count=65530, available=True)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_pressure() -> None:
    snap = _MaxMapSnapshot(maps_count=900, max_map_count=1000, available=True)
    text = _render(snap)
    assert "⚠" in text
    assert "ENOMEM" in text


def test_render_partial_shows_unknown() -> None:
    """One source unreadable → 'unknown' for that field, no
    spurious ⚠ on the -1 sentinel."""
    snap = _MaxMapSnapshot(maps_count=-1, max_map_count=65530, available=True)
    text = _render(snap)
    assert "unknown" in text
    assert "⚠" not in text


# --- helpers ---------------------------------------------------------------


def test_fmt_thousands() -> None:
    assert _fmt(65530) == "65,530"


def test_fmt_sentinel() -> None:
    assert _fmt(-1) == "unknown"


def test_fmt_pct() -> None:
    assert _fmt_pct(0.8) == "80.0%"
    assert _fmt_pct(0.0) == "0.0%"
