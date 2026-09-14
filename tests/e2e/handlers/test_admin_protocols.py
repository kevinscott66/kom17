"""End-to-end ``/admin_protocols``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux /proc/net/protocols) OR unavailable note.
* Header line skipped; data lines parsed.
* Defensive: short lines drop, non-integer numeric columns drop,
  unrecognised header bails entirely (kernel emits unknown format).
* press=NI ≠ press=yes — must distinguish "no accounting" from
  "under pressure".
* Cry-wolf must-not-fire on canonical-healthy sample (every
  family press=no or press=NI).
* ⚠ fires when any family has press=yes.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.protocols import (
    _capture,
    _fmt_memory,
    _parse_protocols,
    _ProtocolRow,
    _ProtocolsSnapshot,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


_SAMPLE = (
    "protocol  size sockets  memory press maxhdr  slab module     cl co di\n"
    "PACKET    1344     32      -1   NI       0   no   kernel      n  n  n\n"
    "UNIX      1280   1200      -1   NI       0   yes  kernel      n  n  n\n"
    "TCPv6     2280     12      -1   NI       0   yes  kernel      y  y  y\n"
    "UDPv6     1408      8      -1   NI       0   yes  kernel      y  y  y\n"
    "TCP       2208     32  131072   no     320   yes  kernel      y  y  y\n"
    "UDP       1376      6  524288   no       0   yes  kernel      y  y  y\n"
    "RAW       1216      1      -1   NI       0   yes  kernel      y  y  y\n"
    "NETLINK   1408     20      -1   NI       0   no   kernel      n  n  n\n"
)


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_protocols", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_protocols", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "protocol families" in text


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
            "/admin_protocols", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


def test_parse_canonical() -> None:
    rows = _parse_protocols(_SAMPLE)
    names = [r.name for r in rows]
    assert names == ["PACKET", "UNIX", "TCPv6", "UDPv6", "TCP", "UDP", "RAW", "NETLINK"]
    tcp = next(r for r in rows if r.name == "TCP")
    assert tcp.size == 2208
    assert tcp.sockets == 32
    assert tcp.memory == 131072
    assert tcp.press == "no"


def test_parse_header_skipped() -> None:
    rows = _parse_protocols(_SAMPLE)
    assert not any(r.name == "protocol" for r in rows)


def test_parse_unknown_format_bails() -> None:
    """If the first non-blank line doesn't start with the literal
    'protocol', the file shape is something we don't recognise —
    bail entirely rather than treat the header as data."""
    text = "wat is dis\nTCP 1 2 3 no\n"
    assert _parse_protocols(text) == []


def test_parse_short_line_dropped() -> None:
    text = "protocol size sockets memory press\nTCP 1 2\n"
    assert _parse_protocols(text) == []


def test_parse_non_integer_dropped() -> None:
    text = "protocol size sockets memory press\nTCP nope 2 3 no\n"
    assert _parse_protocols(text) == []


def test_parse_empty() -> None:
    assert _parse_protocols("") == []


# --- snapshot --------------------------------------------------------------


def test_row_pressure_distinguishes_ni_from_yes() -> None:
    """press=NI (no indication, paired with memory=-1) must NOT
    be conflated with press=yes (over the squeeze threshold).
    Collapsing both to True would surface ⚠ on every healthy
    host (UNIX/PACKET/NETLINK are always NI)."""
    ni = _ProtocolRow(name="UNIX", size=1, sockets=1, memory=-1, press="NI")
    yes = _ProtocolRow(name="TCP", size=1, sockets=1, memory=100, press="yes")
    no = _ProtocolRow(name="UDP", size=1, sockets=1, memory=100, press="no")
    assert ni.under_pressure is False
    assert yes.under_pressure is True
    assert no.under_pressure is False


def test_snapshot_pressured_filter() -> None:
    snap = _ProtocolsSnapshot(
        rows=[
            _ProtocolRow(name="TCP", size=1, sockets=1, memory=1, press="yes"),
            _ProtocolRow(name="UDP", size=1, sockets=1, memory=1, press="no"),
        ],
        available=True,
    )
    assert [r.name for r in snap.pressured] == ["TCP"]


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.rows == []


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "protocols"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.rows) == 8


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _ProtocolsSnapshot(rows=[], available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy(tmp_path: Path) -> None:
    """Cry-wolf pin: ⚠ MUST NOT appear when every family is
    press=no or press=NI. Canonical /proc/net/protocols on a
    healthy host has zero press=yes — surfacing ⚠ here would
    burn the operator every invocation."""
    p = tmp_path / "protocols"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_pressure(tmp_path: Path) -> None:
    p = tmp_path / "protocols"
    p.write_text(
        _SAMPLE.replace("TCP       2208     32  131072   no", "TCP       2208     32  131072   yes")
    )
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "TCP" in text
    assert "squeeze" in text


def test_render_empty_rows_but_available() -> None:
    snap = _ProtocolsSnapshot(rows=[], available=True)
    text = _render(snap)
    assert "unrecognised format" in text
    assert "⚠" not in text


# --- helpers ---------------------------------------------------------------


def test_fmt_memory_pages() -> None:
    assert _fmt_memory(131072) == "131,072"


def test_fmt_memory_no_accounting() -> None:
    assert _fmt_memory(-1) == "n/a"


def test_fmt_memory_zero() -> None:
    assert _fmt_memory(0) == "0"
