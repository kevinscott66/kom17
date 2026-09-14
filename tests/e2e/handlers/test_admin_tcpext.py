"""End-to-end ``/admin_tcpext``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Header/value pairing parses correctly for both TcpExt and IpExt.
* Forward-compat: extra unknown keys captured, not crashed.
* Length-mismatched key/value lines degrade rather than crash.
* Single ⚠ fires only on ListenOverflows or TCPAbortOnMemory > 0.
* Cry-wolf prevention: SyncookiesSent / TCPSynRetrans / TCPTimeouts
  non-zero must NOT ⚠ (any WAN-facing host sees these under
  transient packet loss).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.tcpext import (
    _capture,
    _parse_netstat,
    _render,
    _TcpExtSnapshot,
    _triggered_warns,
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
        bot, make_message_update("/admin_tcpext", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_tcpext", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "TCP extended counters" in text


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
        make_message_update("/admin_tcpext", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


def _sample(*, listen_overflows: int = 0, abort_on_memory: int = 0, syn_retrans: int = 0) -> str:
    """Canonical 4-line /proc/net/netstat sample with knobs for the
    fields the tests below need to vary. Kept minimal — just enough
    that the parser sees a well-formed file."""
    return (
        f"TcpExt: SyncookiesSent SyncookiesRecv ListenOverflows ListenDrops "
        f"TCPSynRetrans TCPTimeouts TCPAbortOnMemory\n"
        f"TcpExt: 0 0 {listen_overflows} 0 {syn_retrans} 0 {abort_on_memory}\n"
        f"IpExt: InOctets OutOctets\n"
        f"IpExt: 1000 2000\n"
    )


def test_parse_basic() -> None:
    tcpext, ipext = _parse_netstat(_sample())
    assert tcpext["SyncookiesSent"] == 0
    assert tcpext["ListenOverflows"] == 0
    assert ipext["InOctets"] == 1000
    assert ipext["OutOctets"] == 2000


def test_parse_unknown_keys_kept() -> None:
    """Forward-compat: a kernel that adds a new counter must not
    crash the parser. The new field appears in tcpext keyed by name."""
    text = "TcpExt: ListenOverflows FutureCounter\nTcpExt: 0 42\n"
    tcpext, _ = _parse_netstat(text)
    assert tcpext["FutureCounter"] == 42


def test_parse_length_mismatch_truncates() -> None:
    """More keys than values → zip-shortest stops at the shorter side
    rather than raising. Truncated trailing fields drop silently —
    by design, the alternative would be raising on mid-update writes
    from the kernel."""
    text = "TcpExt: ListenOverflows ListenDrops TCPSynRetrans\nTcpExt: 0 0\n"
    tcpext, _ = _parse_netstat(text)
    assert tcpext["ListenOverflows"] == 0
    assert tcpext["ListenDrops"] == 0
    assert "TCPSynRetrans" not in tcpext


def test_parse_non_int_value_skipped() -> None:
    """Non-integer value in the values line → skip that one field
    only, keep the rest of the line usable."""
    text = "TcpExt: ListenOverflows ListenDrops\nTcpExt: garbage 5\n"
    tcpext, _ = _parse_netstat(text)
    assert "ListenOverflows" not in tcpext
    assert tcpext["ListenDrops"] == 5


def test_parse_unknown_prefix_ignored() -> None:
    """A future kernel section (e.g. ``UdpExt:``) is captured by
    neither dict — keeps the parser strict about what it surfaces."""
    text = "UdpExt: SomeKey\nUdpExt: 99\n"
    tcpext, ipext = _parse_netstat(text)
    assert tcpext == {}
    assert ipext == {}


# --- capture ---------------------------------------------------------------


def test_capture_unavailable_when_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "nope")
    assert not snap.available
    assert snap.tcpext == {}
    assert snap.ipext == {}


def test_capture_reads_tmp_file(tmp_path: Path) -> None:
    p = tmp_path / "netstat"
    p.write_text(_sample())
    snap = _capture(path=p)
    assert snap.available
    assert snap.tcpext["ListenOverflows"] == 0


# --- ⚠ predicate ---------------------------------------------------------


def test_warn_predicate_clean() -> None:
    """All curated counters zero → no ⚠."""
    snap = _TcpExtSnapshot(
        tcpext={"ListenOverflows": 0, "TCPAbortOnMemory": 0},
        ipext={},
        available=True,
    )
    assert _triggered_warns(snap) == ()


def test_warn_predicate_listen_overflows() -> None:
    """ListenOverflows > 0 fires ⚠ — the canonical accept-queue smoke."""
    snap = _TcpExtSnapshot(
        tcpext={"ListenOverflows": 1, "TCPAbortOnMemory": 0},
        ipext={},
        available=True,
    )
    assert "ListenOverflows" in _triggered_warns(snap)


def test_warn_predicate_abort_on_memory() -> None:
    snap = _TcpExtSnapshot(
        tcpext={"ListenOverflows": 0, "TCPAbortOnMemory": 1},
        ipext={},
        available=True,
    )
    assert "TCPAbortOnMemory" in _triggered_warns(snap)


def test_warn_predicate_false_when_unavailable() -> None:
    """Absence of data is NOT a warning — macOS dev must not ⚠."""
    snap = _TcpExtSnapshot(tcpext={}, ipext={}, available=False)
    assert _triggered_warns(snap) == ()


def test_warn_predicate_no_cry_wolf_on_retrans(tmp_path: Path) -> None:
    """Cry-wolf pin: SyncookiesSent / TCPSynRetrans / TCPTimeouts
    non-zero must NOT trigger ⚠. These are normal on any WAN-facing
    host under transient packet loss and would erode trust if marked."""
    p = tmp_path / "netstat"
    p.write_text(_sample(syn_retrans=9999, listen_overflows=0, abort_on_memory=0))
    snap = _capture(path=p)
    assert _triggered_warns(snap) == ()


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _TcpExtSnapshot(tcpext={}, ipext={}, available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    # Legend footer not rendered when unavailable — no ⚠ at all.
    assert "⚠" not in rendered


def test_render_clean_no_warning(tmp_path: Path) -> None:
    """Healthy host (all warn-fields zero) → ZERO ⚠ in body."""
    p = tmp_path / "netstat"
    p.write_text(_sample())
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    # Curated description text contains the literal ⚠ token next to
    # the field label ("ListenOverflows ⚠") — that's the legend's
    # job, not the trigger's. Strip those legend-style ⚠'s before
    # asserting no trigger fired.
    legend_free = head.replace(" ⚠", "")
    assert "⚠" not in legend_free


def test_render_warns_on_overflow(tmp_path: Path) -> None:
    p = tmp_path / "netstat"
    p.write_text(_sample(listen_overflows=7))
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" in head
    assert "ListenOverflows" in head
    assert "non-zero warn counter" in head


def test_render_missing_curated_field_shows_na(tmp_path: Path) -> None:
    """Older kernel that doesn't expose a curated field → n/a marker,
    not silent drop. Operator on an older kernel can see what's missing."""
    p = tmp_path / "netstat"
    p.write_text("TcpExt: ListenOverflows\nTcpExt: 0\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    # TCPAbortOnMemory wasn't in the synthetic file — must render n/a
    assert "TCPAbortOnMemory" in rendered
    assert "n/a" in rendered
