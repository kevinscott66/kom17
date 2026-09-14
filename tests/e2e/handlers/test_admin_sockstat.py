"""End-to-end ``/admin_sockstat``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Independent v4/v6 availability — v6-disabled container renders
  v4 data plus explicit v6-absent note (not a crash).
* Parser handles key/value pairs per family.
* Missing family / missing key renders explanatory text, not silent
  drop — operator sees what's absent on older kernels.
* ZERO ⚠ markers anywhere in the card body regardless of socket
  state — no universally-safe threshold for orphans/TIME_WAITs
  (operator policy, not card policy). Pinned explicitly so a
  future refactor doesn't accidentally add a marker.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.sockstat import (
    _capture,
    _parse_sockstat,
    _render,
    _SockstatSnapshot,
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
        bot, make_message_update("/admin_sockstat", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_sockstat", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Socket-state aggregates" in text


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
            "/admin_sockstat", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE_V4 = (
    "sockets: used 234\n"
    "TCP: inuse 12 orphan 0 tw 2 alloc 14 mem 1\n"
    "UDP: inuse 8 mem 0\n"
    "UDPLITE: inuse 0\n"
    "RAW: inuse 0\n"
    "FRAG: inuse 0 memory 0\n"
)

_SAMPLE_V6 = (
    "TCP6: inuse 3\nUDP6: inuse 1\nUDPLITEv6: inuse 0\nRAWv6: inuse 0\nFRAG6: inuse 0 memory 0\n"
)


def test_parse_v4_canonical() -> None:
    parsed = _parse_sockstat(_SAMPLE_V4)
    assert parsed["sockets"]["used"] == 234
    assert parsed["TCP"]["inuse"] == 12
    assert parsed["TCP"]["orphan"] == 0
    assert parsed["TCP"]["tw"] == 2
    assert parsed["TCP"]["alloc"] == 14
    assert parsed["UDP"]["inuse"] == 8


def test_parse_v6_canonical() -> None:
    parsed = _parse_sockstat(_SAMPLE_V6)
    assert parsed["TCP6"]["inuse"] == 3
    assert parsed["UDP6"]["inuse"] == 1


def test_parse_non_int_value_skipped() -> None:
    """Bad value in one key skips just that pair — rest of line stays."""
    parsed = _parse_sockstat("TCP: inuse garbage orphan 0 tw 5\n")
    assert "inuse" not in parsed["TCP"]
    assert parsed["TCP"]["orphan"] == 0
    assert parsed["TCP"]["tw"] == 5


def test_parse_short_line_skipped() -> None:
    """One-token line (just a family with no kv pairs) → skipped."""
    parsed = _parse_sockstat("TCP:\nUDP: inuse 5\n")
    assert "TCP" not in parsed
    assert parsed["UDP"]["inuse"] == 5


def test_parse_unknown_family_kept() -> None:
    """Forward-compat: a future kernel family (e.g. ``SCTP:``) is
    parsed into its own dict like every other family."""
    parsed = _parse_sockstat("SCTP: inuse 7\n")
    assert parsed["SCTP"]["inuse"] == 7


# --- capture ---------------------------------------------------------------


def test_capture_both_missing(tmp_path: Path) -> None:
    snap = _capture(v4_path=tmp_path / "no4", v6_path=tmp_path / "no6")
    assert not snap.available
    assert not snap.available_v4
    assert not snap.available_v6


def test_capture_v4_only(tmp_path: Path) -> None:
    """Container with IPv6 disabled — v4 file exists, v6 doesn't."""
    p4 = tmp_path / "sockstat"
    p4.write_text(_SAMPLE_V4)
    snap = _capture(v4_path=p4, v6_path=tmp_path / "absent")
    assert snap.available
    assert snap.available_v4
    assert not snap.available_v6
    assert snap.v4["TCP"]["inuse"] == 12


def test_capture_both_present(tmp_path: Path) -> None:
    p4 = tmp_path / "sockstat"
    p6 = tmp_path / "sockstat6"
    p4.write_text(_SAMPLE_V4)
    p6.write_text(_SAMPLE_V6)
    snap = _capture(v4_path=p4, v6_path=p6)
    assert snap.available_v4
    assert snap.available_v6
    assert snap.v6["TCP6"]["inuse"] == 3


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _SockstatSnapshot(v4={}, v6={}, available_v4=False, available_v6=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_no_warnings_anywhere_high_orphans(tmp_path: Path) -> None:
    """Cry-wolf prevention pin: even an absurd orphan count must NOT
    produce ⚠ anywhere in the rendered card body OR footer. The
    threshold for &quot;too many orphans&quot; is operator policy
    (compare against net.ipv4.tcp_max_orphans), not card policy."""
    p4 = tmp_path / "sockstat"
    p4.write_text(
        "sockets: used 50000\nTCP: inuse 12 orphan 99999 tw 50000 alloc 200000 mem 9999\n"
    )
    snap = _capture(v4_path=p4, v6_path=tmp_path / "absent")
    rendered = _render(snap)
    # Pin: literal ⚠ must not appear anywhere in the rendered text,
    # body OR footer. Simpler than the partition idiom — this card
    # genuinely has no ⚠ at all.
    assert "⚠" not in rendered


def test_render_v4_canonical(tmp_path: Path) -> None:
    p4 = tmp_path / "sockstat"
    p4.write_text(_SAMPLE_V4)
    snap = _capture(v4_path=p4, v6_path=tmp_path / "absent")
    rendered = _render(snap)
    # Operator must see: total used, TCP v4 with orphan, UDP v4.
    assert "Total sockets used" in rendered
    assert "TCP (v4)" in rendered
    assert "orphan" in rendered
    assert "UDP (v4)" in rendered


def test_render_v6_absent_note(tmp_path: Path) -> None:
    """v6-disabled namespace renders v4 then an explicit v6-absent
    note — operator sees the absence is intentional, not a crash."""
    p4 = tmp_path / "sockstat"
    p4.write_text(_SAMPLE_V4)
    snap = _capture(v4_path=p4, v6_path=tmp_path / "absent")
    rendered = _render(snap)
    assert "/proc/net/sockstat6 unavailable" in rendered


def test_render_missing_curated_key_shows_na(tmp_path: Path) -> None:
    """Older kernel without ``mem`` field on TCP → 'n/a' for that
    key, not silent drop."""
    p4 = tmp_path / "sockstat"
    p4.write_text("TCP: inuse 5 orphan 0\n")
    snap = _capture(v4_path=p4, v6_path=tmp_path / "absent")
    rendered = _render(snap)
    assert "mem=" in rendered
    assert "n/a" in rendered


def test_render_missing_tcp_section_explains(tmp_path: Path) -> None:
    """An empty sockstat file (no TCP section) gets an explicit
    'section absent on this kernel' note."""
    p4 = tmp_path / "sockstat"
    p4.write_text("sockets: used 0\n")
    snap = _capture(v4_path=p4, v6_path=tmp_path / "absent")
    rendered = _render(snap)
    assert "section absent" in rendered
