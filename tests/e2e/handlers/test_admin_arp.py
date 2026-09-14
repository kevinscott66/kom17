"""End-to-end ``/admin_arp``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Empty cache renders an explicit "no recent traffic" note — not the
  same as unavailable (one is Linux + zero rows; the other is no
  Linux). Both observably distinct in the rendered output.
* Header row is recognised and skipped (sniffed via first token).
* Non-IPv4 lines (e.g. ``garbage``, IPv6) → rejected by the
  dotted-quad sniff. We don't try to recover unknown formats.
* Non-hex flags column → row dropped.
* ATF_COM (0x2) bit drives is_complete; absence → INCOMPLETE.
* ATF_PERM (0x4) drives is_permanent.
* ⚠ predicate fires ONLY when incomplete count exceeds
  _INCOMPLETE_WARN_THRESHOLD. Cry-wolf pin: 1 or 2 INCOMPLETE
  entries (normal background) must NOT produce ⚠.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.arp import (
    _INCOMPLETE_WARN_THRESHOLD,
    _ArpSnapshot,
    _capture,
    _parse_arp,
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
        bot, make_message_update("/admin_arp", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_arp", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "ARP neighbour table" in text


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
        make_message_update("/admin_arp", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


# Genuine /proc/net/arp content. The header row's split has 9 tokens
# (the column labels are multi-word: "IP address", "HW type", "HW
# address"), which doesn't match our 6-token shape — the IP-token
# sniff catches the header instead.
_SAMPLE = (
    "IP address       HW type     Flags       HW address            Mask     Device\n"
    "192.168.1.1      0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
    "192.168.1.2      0x1         0x6         11:22:33:44:55:66     *        eth0\n"
    "192.168.1.99     0x1         0x0         00:00:00:00:00:00     *        eth0\n"
)


def test_parse_canonical() -> None:
    entries = _parse_arp(_SAMPLE)
    assert len(entries) == 3
    by_ip = {e.ip: e for e in entries}
    assert by_ip["192.168.1.1"].is_complete
    assert not by_ip["192.168.1.1"].is_permanent
    assert by_ip["192.168.1.2"].is_complete
    assert by_ip["192.168.1.2"].is_permanent
    assert not by_ip["192.168.1.99"].is_complete


def test_parse_header_is_skipped() -> None:
    """Header line has the IP-token sniff (first token literally
    ``IP``) so it doesn't show up as an entry even though the column
    count differs from data rows."""
    entries = _parse_arp("IP address HW type Flags HW Mask Device\n")
    assert entries == ()


def test_parse_non_ipv4_lines_rejected() -> None:
    """An IPv6 address or a free-form garbage line must not become
    an entry — the dotted-quad sniff is what gates the parse."""
    text = (
        "garbage line not parseable here at all\n"
        "fe80::1 0x1 0x2 aa:bb:cc:dd:ee:ff * eth0\n"
        "192.168.1.5 0x1 0x2 aa:bb:cc:dd:ee:01 * eth0\n"
    )
    entries = _parse_arp(text)
    assert [e.ip for e in entries] == ["192.168.1.5"]


def test_parse_non_hex_flags_dropped() -> None:
    """Non-hex flags column → row dropped (degraded format)."""
    text = (
        "192.168.1.5 0x1 garbage aa:bb:cc:dd:ee:01 * eth0\n"
        "192.168.1.6 0x1 0x2 aa:bb:cc:dd:ee:02 * eth0\n"
    )
    entries = _parse_arp(text)
    assert [e.ip for e in entries] == ["192.168.1.6"]


def test_parse_wrong_column_count_dropped() -> None:
    """Row with the wrong column count (mid-update read) is dropped."""
    text = (
        "192.168.1.5 0x1 0x2 aa:bb:cc:dd:ee:01 *\n"  # 5 tokens
        "192.168.1.6 0x1 0x2 aa:bb:cc:dd:ee:02 * eth0\n"  # 6 tokens, good
    )
    entries = _parse_arp(text)
    assert [e.ip for e in entries] == ["192.168.1.6"]


def test_complete_predicate_only_atf_com() -> None:
    """is_complete is purely the ATF_COM bit — proxy-only entries
    (PUB without COM) are not 'complete' for our purposes because
    they have no usable HW address from this host's perspective."""
    text = "192.168.1.5 0x1 0x8 00:00:00:00:00:00 * eth0\n"  # ATF_PUB
    entries = _parse_arp(text)
    assert not entries[0].is_complete
    assert not entries[0].is_permanent


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.entries == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "arp"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.entries) == 3


def test_capture_empty_file_distinct_from_missing(tmp_path: Path) -> None:
    """Linux + empty cache (no traffic) is observably different from
    no Linux at all — the available flag distinguishes them."""
    p = tmp_path / "arp"
    p.write_text("IP address HW type Flags HW address Mask Device\n")
    snap = _capture(path=p)
    assert snap.available
    assert snap.entries == ()


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _ArpSnapshot(entries=(), available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_empty_distinct_from_unavailable() -> None:
    """Empty cache renders 'no recent IPv4 traffic' — confirms an
    operator on a Linux host with a fresh boot can distinguish 'this
    surface works, nothing to show' from 'this surface is absent'."""
    snap = _ArpSnapshot(entries=(), available=True)
    rendered = _render(snap)
    assert "empty" in rendered or "no recent" in rendered
    assert "⚠" not in rendered


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "arp"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "192.168.1.1" in rendered
    assert "aa:bb:cc:dd:ee:ff" in rendered
    assert "eth0" in rendered
    # Three entries: 2 complete (1 perm), 1 incomplete.
    assert "INCOMPLETE" in rendered
    # 1 incomplete < threshold → no ⚠.
    assert "⚠" not in rendered


def test_render_cry_wolf_low_incomplete(tmp_path: Path) -> None:
    """Cry-wolf prevention pin: a handful of INCOMPLETE entries
    (normal background) must NOT produce ⚠. Pinned with a count
    just under the threshold."""
    lines = []
    just_under = _INCOMPLETE_WARN_THRESHOLD
    # Build (threshold) incomplete entries — that's the boundary
    # (strict `>` means equal-to-threshold doesn't fire).
    for i in range(just_under):
        lines.append(f"192.168.1.{i + 10} 0x1 0x0 00:00:00:00:00:00 * eth0")
    p = tmp_path / "arp"
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_warns_above_threshold(tmp_path: Path) -> None:
    """One above the threshold → ⚠ fires and footer points at the
    follow-up cards."""
    lines = []
    over = _INCOMPLETE_WARN_THRESHOLD + 1
    for i in range(over):
        lines.append(f"192.168.1.{i + 10} 0x1 0x0 00:00:00:00:00:00 * eth0")
    p = tmp_path / "arp"
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" in rendered
    assert "/admin_netdev" in rendered or "/admin_route" in rendered


def test_render_cap_truncates(tmp_path: Path) -> None:
    """Many entries → render truncates with explicit 'more not shown'
    note. Full count still surfaces in the total line."""
    lines = []
    big = 60
    for i in range(big):
        lines.append(f"192.168.1.{i + 1} 0x1 0x2 aa:bb:cc:dd:ee:{i:02x} * eth0")
    p = tmp_path / "arp"
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    assert len(snap.entries) == big
    rendered = _render(snap)
    assert "more entries not shown" in rendered
    # Total line still includes the actual count.
    assert "<code>60</code>" in rendered
