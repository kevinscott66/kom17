"""End-to-end ``/admin_netdev``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* /proc/net/dev format: 2 header lines skipped, per-interface
  parse, 16 counter columns.
* Single ⚠ on non-loopback rx_errs > 0 OR tx_errs > 0.
* Cry-wolf prevention: high drops MUST NOT ⚠ (multicast / closed-
  sockets drops are normal). Loopback errors MUST NOT ⚠ (excluded
  by design).
* Forward-compat: short lines / non-int fields degrade per-field
  to None rather than crashing.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.netdev import (
    _capture,
    _interfaces_with_errors,
    _NetdevSnapshot,
    _parse_netdev,
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
        bot, make_message_update("/admin_netdev", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_netdev", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Per-interface counters" in text


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
        make_message_update("/admin_netdev", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE_HEADERS = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|"
    "bytes    packets errs drop fifo colls carrier compressed\n"
)


def _iface_line(
    name: str,
    *,
    rx_bytes: int = 0,
    rx_packets: int = 0,
    rx_errs: int = 0,
    rx_drop: int = 0,
    tx_bytes: int = 0,
    tx_packets: int = 0,
    tx_errs: int = 0,
    tx_drop: int = 0,
) -> str:
    # 16 counters: 8 receive (bytes packets errs drop fifo frame compressed
    # multicast) + 8 transmit (bytes packets errs drop fifo colls carrier
    # compressed). Zero-fill the ones we don't expose.
    return (
        f"  {name}: {rx_bytes} {rx_packets} {rx_errs} {rx_drop} 0 0 0 0 "
        f"{tx_bytes} {tx_packets} {tx_errs} {tx_drop} 0 0 0 0\n"
    )


def test_parse_skips_headers() -> None:
    assert _parse_netdev(_SAMPLE_HEADERS) == ()


def test_parse_basic_interface() -> None:
    text = _SAMPLE_HEADERS + _iface_line(
        "eth0", rx_bytes=1000, rx_packets=10, tx_bytes=2000, tx_packets=20
    )
    rows = _parse_netdev(text)
    assert len(rows) == 1
    assert rows[0].iface == "eth0"
    assert rows[0].rx_bytes == 1000
    assert rows[0].rx_packets == 10
    assert rows[0].tx_bytes == 2000
    assert rows[0].tx_packets == 20


def test_parse_short_line_skipped() -> None:
    """<16 counter columns → drop the row, keep others."""
    text = _SAMPLE_HEADERS + "  bogus: 1 2 3\n" + _iface_line("eth0", rx_bytes=999)
    rows = _parse_netdev(text)
    assert len(rows) == 1
    assert rows[0].iface == "eth0"


def test_parse_non_int_counter_yields_none() -> None:
    """Non-integer in one column → None for that field only, the
    rest of the row stays usable."""
    text = _SAMPLE_HEADERS + "  eth0: garbage 10 0 0 0 0 0 0 50 20 0 0 0 0 0 0\n"
    rows = _parse_netdev(text)
    assert len(rows) == 1
    assert rows[0].rx_bytes is None
    assert rows[0].rx_packets == 10
    assert rows[0].tx_bytes == 50


def test_parse_iface_with_colon_no_space() -> None:
    """Compact format (no space before colon) must still parse."""
    text = _SAMPLE_HEADERS + ("eth0:1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n")
    rows = _parse_netdev(text)
    assert len(rows) == 1
    assert rows[0].iface == "eth0"


# --- capture ---------------------------------------------------------------


def test_capture_unavailable_when_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "nope")
    assert not snap.available
    assert snap.rows == ()


def test_capture_reads_tmp_file(tmp_path: Path) -> None:
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("lo") + _iface_line("eth0"))
    snap = _capture(path=p)
    assert snap.available
    assert {r.iface for r in snap.rows} == {"lo", "eth0"}


# --- ⚠ predicate ---------------------------------------------------------


def test_predicate_clean(tmp_path: Path) -> None:
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("eth0", rx_bytes=1000))
    snap = _capture(path=p)
    assert _interfaces_with_errors(snap) == ()


def test_predicate_rx_errs(tmp_path: Path) -> None:
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("eth0", rx_errs=5))
    snap = _capture(path=p)
    assert _interfaces_with_errors(snap) == ("eth0",)


def test_predicate_tx_errs(tmp_path: Path) -> None:
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("eth0", tx_errs=1))
    snap = _capture(path=p)
    assert _interfaces_with_errors(snap) == ("eth0",)


def test_predicate_drops_do_not_fire(tmp_path: Path) -> None:
    """Cry-wolf pin: large drop counts on rx/tx are normal under
    multicast filtering and packets-to-closed-sockets. MUST NOT ⚠."""
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("eth0", rx_drop=999999, tx_drop=999999))
    snap = _capture(path=p)
    assert _interfaces_with_errors(snap) == ()


def test_predicate_loopback_errors_excluded(tmp_path: Path) -> None:
    """Cry-wolf pin: lo errs are out of scope for this card. The
    rare case where lo has errs would be a much deeper system
    problem; flagging would false-positive on no real host."""
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("lo", rx_errs=42))
    snap = _capture(path=p)
    assert _interfaces_with_errors(snap) == ()


def test_predicate_false_when_unavailable() -> None:
    snap = _NetdevSnapshot(rows=(), available=False)
    assert _interfaces_with_errors(snap) == ()


def test_predicate_multiple_interfaces(tmp_path: Path) -> None:
    p = tmp_path / "dev"
    p.write_text(
        _SAMPLE_HEADERS
        + _iface_line("lo")
        + _iface_line("eth0", rx_errs=2)
        + _iface_line("eth1", tx_errs=3)
        + _iface_line("eth2")
    )
    snap = _capture(path=p)
    assert set(_interfaces_with_errors(snap)) == {"eth0", "eth1"}


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _NetdevSnapshot(rows=(), available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_clean_no_warning(tmp_path: Path) -> None:
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("lo") + _iface_line("eth0", rx_bytes=1000))
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" not in head


def test_render_warns_on_errs(tmp_path: Path) -> None:
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("eth0", rx_errs=7))
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" in head
    assert "eth0" in head
    assert "non-zero errs" in head


def test_render_no_warning_for_drops(tmp_path: Path) -> None:
    """Drops on a non-loopback interface must NOT warn even with
    enormous values — the legend footer is the only place ⚠ appears."""
    p = tmp_path / "dev"
    p.write_text(_SAMPLE_HEADERS + _iface_line("eth0", rx_drop=10_000_000))
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" not in head
