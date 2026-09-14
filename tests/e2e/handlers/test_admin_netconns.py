"""End-to-end ``/admin_netconns``.

Pins:

* Non-developer → silent drop.
* Live card renders both ipv4 + ipv6 sections.
* Healthy snapshot (modest counts) → zero ⚠ on data rows.
* TIME_WAIT > threshold (combined v4+v6) → ⚠ on the row AND on
  the combined-line footer summary.
* Missing /proc/net/tcp (non-Linux host fake) → informational
  bare row, NO ⚠ — file-genuinely-cannot-exist is not concerning.
* UNKNOWN_<hex> state surfaces with ⚠ — a future kernel state
  must not silently disappear from the census.
* ``_parse_proc_net_tcp`` skips the header line.
* ``_parse_proc_net_tcp`` decodes ESTABLISHED (01), LISTEN (0A),
  TIME_WAIT (06) correctly.
* ``_parse_proc_net_tcp`` is case-insensitive on the state hex
  (kernel uses upper, but the constant table being lowercase
  shouldn't break the lookup).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.netconns import (
    _TIME_WAIT_CONCERNING,
    _capture,
    _NetSnapshot,
    _parse_proc_net_tcp,
    _render,
    _total_time_wait,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
    "retrnsmt   uid  timeout inode"
)


def _row(state_hex: str) -> str:
    """Synthetic /proc/net/tcp row with the given state code in column 3."""
    return (
        f"   0: 0100007F:DEC0 00000000:0000 {state_hex} "
        "00000000:00000000 00:00000000 00000000     0        0 12345 1 "
        "0000000000000000 100 0 0 10 0"
    )


def _snap(
    *,
    v4_states: dict[str, int] | None = None,
    v4_present: bool = True,
    v6_states: dict[str, int] | None = None,
    v6_present: bool = True,
) -> _NetSnapshot:
    return _NetSnapshot(
        v4_states=v4_states or {"ESTABLISHED": 5, "LISTEN": 1, "TIME_WAIT": 10},
        v4_present=v4_present,
        v6_states=v6_states or {"ESTABLISHED": 2},
        v6_present=v6_present,
    )


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_netconns", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_netconns", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "TCP socket census" in text
    assert "ipv4" in text
    assert "ipv6" in text


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
            "/admin_netconns",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_healthy_snapshot_no_warn() -> None:
    """Modest socket counts → zero data-row ⚠. Same partition-before-
    count idiom as every other admin card."""
    rendered = _render(_snap())
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0


def test_time_wait_high_marks_row_and_summary() -> None:
    """TIME_WAIT over the threshold (combined v4+v6) marks the state
    row AND adds the combined-line summary with ⚠. This is the
    port-exhaustion early warning the card exists for."""
    high = _TIME_WAIT_CONCERNING + 500
    rendered = _render(
        _snap(
            v4_states={"ESTABLISHED": 5, "TIME_WAIT": high},
            v6_states={},
        )
    )
    head, _, _ = rendered.partition("<i>⚠")
    # Two ⚠: one on the v4 TIME_WAIT row, one on the combined summary.
    assert head.count("⚠") == 2
    assert "combined TIME_WAIT" in head
    tw_line = next(
        line for line in head.splitlines() if "TIME_WAIT:" in line and "combined" not in line
    )
    assert "⚠" in tw_line


def test_missing_proc_file_no_warn() -> None:
    """Non-Linux host: ``v4_present=False`` renders informational,
    not ⚠. We can't measure what doesn't exist."""
    rendered = _render(_snap(v4_states={}, v4_present=False, v6_states={}, v6_present=False))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0
    assert "non-Linux host" in head


def test_unknown_state_surfaces_with_warn() -> None:
    """A state code the kernel adds after this card is written must
    not silently disappear — bucketed under UNKNOWN_<hex> and ⚠'d
    so the operator notices and refreshes the decoding table."""
    rendered = _render(
        _snap(
            v4_states={"ESTABLISHED": 1, "UNKNOWN_FF": 1},
            v6_states={},
        )
    )
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    assert "UNKNOWN_FF" in head


def test_parse_skips_header() -> None:
    text = "\n".join([_HEADER, _row("01"), _row("01"), _row("0A")])
    counts = _parse_proc_net_tcp(text)
    assert counts == {"ESTABLISHED": 2, "LISTEN": 1}


def test_parse_decodes_known_states() -> None:
    """Pin the three load-bearing states. The full table is documented
    in the module; these are the ones the card's ⚠ logic depends on."""
    text = "\n".join([_HEADER, _row("01"), _row("06"), _row("06"), _row("0A")])
    counts = _parse_proc_net_tcp(text)
    assert counts["ESTABLISHED"] == 1
    assert counts["TIME_WAIT"] == 2
    assert counts["LISTEN"] == 1


def test_parse_state_hex_case_insensitive() -> None:
    """Kernel emits upper-case hex; lower-case must decode the same
    way so a future kernel quirk doesn't bucket everything as UNKNOWN."""
    text = "\n".join([_HEADER, _row("01"), _row("0a"), _row("06")])
    counts = _parse_proc_net_tcp(text)
    assert counts.get("ESTABLISHED") == 1
    assert counts.get("LISTEN") == 1
    assert counts.get("TIME_WAIT") == 1


def test_parse_unknown_state_bucketed() -> None:
    text = "\n".join([_HEADER, _row("FF")])
    counts = _parse_proc_net_tcp(text)
    assert counts == {"UNKNOWN_FF": 1}


def test_parse_skips_short_lines() -> None:
    """Defensive against truncated lines / blank tail lines from
    /proc — never raise IndexError when the kernel emits an
    unexpected shape."""
    text = "\n".join([_HEADER, "", _row("01"), "  too short  "])
    counts = _parse_proc_net_tcp(text)
    assert counts == {"ESTABLISHED": 1}


def test_total_time_wait_sums_v4_and_v6() -> None:
    """Port exhaustion is host-wide; the kernel doesn't track
    ephemeral ports per family. The summary must add both."""
    snap = _snap(
        v4_states={"TIME_WAIT": 600},
        v6_states={"TIME_WAIT": 500},
    )
    assert _total_time_wait(snap) == 1100


def test_capture_live(tmp_path: Path) -> None:
    """Real ``_capture`` against fixture files; the missing-tcp6
    fixture exercises the absent-file branch on a host that DOES
    have /proc/net/tcp."""
    good = tmp_path / "tcp"
    good.write_text("\n".join([_HEADER, _row("01"), _row("06")]))
    missing = tmp_path / "tcp6_does_not_exist"
    snap = _capture(tcp_path=good, tcp6_path=missing)
    assert snap.v4_present
    assert not snap.v6_present
    assert snap.v4_states == {"ESTABLISHED": 1, "TIME_WAIT": 1}
    assert snap.v6_states == {}
