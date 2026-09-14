"""End-to-end ``/admin_route``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Hex little-endian IPv4 decoding (the easy-to-get-backwards bit).
* Single ⚠ only on no-default-route; never on per-route oddities
  (multiple defaults, unusual metrics, lo routes) — those are
  operator policy, not the card's call.
* Header line ("Iface Destination Gateway...") skipped.
* Short / malformed lines degrade rather than crash.
* Default-route block renders gateway+iface+metric for each.
* Direct (gateway 0.0.0.0) marked inline as "direct".
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.route import (
    _capture,
    _decode_flags,
    _decode_hex_ipv4,
    _no_default_route,
    _parse_route,
    _render,
    _RouteSnapshot,
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
        bot, make_message_update("/admin_route", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_route", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "IPv4 routing table" in text


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
        make_message_update("/admin_route", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- hex decode ------------------------------------------------------------


def test_decode_hex_ipv4_canonical() -> None:
    """0102A8C0 = bytes 01 02 A8 C0 = 192.168.2.1 (little-endian).

    This is the easy-to-get-backwards bit — pin it explicitly with
    a known kernel example so a future "fix" can't invert byte order
    silently."""
    assert _decode_hex_ipv4("0102A8C0") == "192.168.2.1"


def test_decode_hex_ipv4_default() -> None:
    """00000000 = 0.0.0.0 — what /proc emits for the default route."""
    assert _decode_hex_ipv4("00000000") == "0.0.0.0"


def test_decode_hex_ipv4_bad_length() -> None:
    """Anything other than 8 hex chars → ? rather than raise."""
    assert _decode_hex_ipv4("FFFF") == "?"
    assert _decode_hex_ipv4("") == "?"


def test_decode_hex_ipv4_non_hex() -> None:
    """Non-hex input → ? (degrade-don't-crash)."""
    assert _decode_hex_ipv4("ZZZZZZZZ") == "?"


# --- flags decode ---------------------------------------------------------


def test_decode_flags_up_gateway() -> None:
    """U|G — the standard default-route flag combo."""
    # 0x0001 (U) | 0x0002 (G) = 0x0003
    assert _decode_flags(0x0003) == "UG"


def test_decode_flags_unknown_bits_preserved() -> None:
    """Unknown bits render as +0xNN so forward-compat doesn't drop them silently."""
    # 0x0001 (U) | 0x0080 (unknown) = 0x0081
    decoded = _decode_flags(0x0081)
    assert "U" in decoded
    assert "+0x80" in decoded


def test_decode_flags_empty() -> None:
    """No bits set → dash placeholder."""
    assert _decode_flags(0) == "-"


# --- parser ----------------------------------------------------------------


def test_parse_skips_header() -> None:
    text = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
    assert _parse_route(text) == ()


def test_parse_basic_default_route() -> None:
    """Canonical default-route line: 0.0.0.0 dest, gateway 192.168.1.1."""
    text = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    )
    rows = _parse_route(text)
    assert len(rows) == 1
    assert rows[0].iface == "eth0"
    assert rows[0].destination == "0.0.0.0"
    assert rows[0].gateway == "192.168.1.1"
    assert rows[0].mask == "0.0.0.0"
    assert "U" in rows[0].flags and "G" in rows[0].flags
    assert rows[0].metric == 100


def test_parse_skips_short_lines() -> None:
    text = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0 short\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    )
    rows = _parse_route(text)
    assert len(rows) == 1


def test_parse_skips_malformed_flags() -> None:
    """Non-hex flags → drop that row, keep others."""
    text = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\tZZZZ\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth0\t0000FEA9\t00000000\t0001\t0\t0\t1000\t0000FFFF\t0\t0\t0\n"
    )
    rows = _parse_route(text)
    assert len(rows) == 1


# --- capture ---------------------------------------------------------------


def test_capture_unavailable_when_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "nope")
    assert not snap.available
    assert snap.rows == ()


def test_capture_reads_tmp_file(tmp_path: Path) -> None:
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.rows) == 1


# --- ⚠ predicate ---------------------------------------------------------


def test_no_default_route_predicate_true(tmp_path: Path) -> None:
    """LAN-only route, no default → predicate fires."""
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    assert _no_default_route(snap) is True


def test_no_default_route_predicate_false_with_default(tmp_path: Path) -> None:
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    assert _no_default_route(snap) is False


def test_no_default_route_predicate_false_when_unavailable() -> None:
    """Absence of data is NOT a warning — macOS dev must not ⚠."""
    snap = _RouteSnapshot(rows=(), available=False)
    assert _no_default_route(snap) is False


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _RouteSnapshot(rows=(), available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered.partition("<i>⚠ markers")[0]


def test_render_warns_on_no_default(tmp_path: Path) -> None:
    """No default route → single hoisted ⚠ in body."""
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" in head
    assert "no default route" in head


def test_render_no_warning_with_default(tmp_path: Path) -> None:
    """Default route present → ZERO ⚠ in body (legend footer doesn't count)."""
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth0\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" not in head


def test_render_default_routes_block(tmp_path: Path) -> None:
    """Default routes hoisted into a dedicated block above the full table."""
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "Default routes" in rendered
    assert "192.168.1.1" in rendered


def test_render_direct_route_inline_label(tmp_path: Path) -> None:
    """gateway 0.0.0.0 means an on-link route — mark inline as "direct"
    so operators don't have to know the /proc convention."""
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "direct" in rendered


def test_render_no_warning_on_multiple_defaults(tmp_path: Path) -> None:
    """Cry-wolf pin: multiple default routes is multi-homing, not a
    bug. Card MUST NOT ⚠ — that policy call is the operator's."""
    p = tmp_path / "route"
    p.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth1\t00000000\t0102A8C0\t0003\t0\t0\t200\t00000000\t0\t0\t0\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" not in head
