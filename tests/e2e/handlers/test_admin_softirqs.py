"""End-to-end ``/admin_softirqs``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Parser detects CPU count from the header row.
* Rows whose token count doesn't match cpu_count are dropped — the
  file is being read mid-update or the kernel format changed.
* Zero-total rows (uncalled softirq kind) → max_share=0.0, not
  ZeroDivisionError.
* max_share is correctly the largest per-CPU count over total.
* ZERO ⚠ markers anywhere in the card body regardless of NET_RX
  skew — workload-dependent threshold, single-CPU undefined
  (operator policy, not card policy). Pinned so a future refactor
  doesn't accidentally add a marker.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.softirqs import (
    _capture,
    _parse_softirqs,
    _render,
    _row_by_kind,
    _SoftirqsSnapshot,
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
        bot, make_message_update("/admin_softirqs", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_softirqs", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Softirq distribution" in text


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
            "/admin_softirqs", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE_4CPU = (
    "                    CPU0       CPU1       CPU2       CPU3\n"
    "          HI:          0          0          0          0\n"
    "       TIMER:    1000000    1100000    1050000    1020000\n"
    "      NET_TX:        500        600        550        520\n"
    "      NET_RX:    9500000     200000     150000     150000\n"
    "       BLOCK:      10000      11000      10500      10200\n"
    "    IRQ_POLL:          0          0          0          0\n"
    "     TASKLET:       2000       2100       2050       2020\n"
    "       SCHED:    5000000    5100000    5050000    5020000\n"
    "     HRTIMER:        100        110        105        102\n"
    "         RCU:    3000000    3100000    3050000    3020000\n"
)


def test_parse_canonical() -> None:
    rows, cpu_count = _parse_softirqs(_SAMPLE_4CPU)
    assert cpu_count == 4
    # All 10 kinds parsed.
    assert len(rows) == 10
    net_rx = next(r for r in rows if r.kind == "NET_RX")
    assert net_rx.per_cpu == (9_500_000, 200_000, 150_000, 150_000)
    assert net_rx.total == 10_000_000
    # max=9.5M, total=10M → 0.95
    assert abs(net_rx.max_share - 0.95) < 1e-9


def test_parse_zero_total_no_zero_division() -> None:
    """Uncalled softirq kind (all zeros) → max_share=0.0, not a crash."""
    text = "CPU0 CPU1\nHI: 0 0\n"
    rows, cpu_count = _parse_softirqs(text)
    assert cpu_count == 2
    assert len(rows) == 1
    assert rows[0].total == 0
    assert rows[0].max_share == 0.0


def test_parse_mismatched_row_dropped() -> None:
    """Row with too few/many tokens is dropped — mid-update read or
    kernel format change. Prevents per_cpu tuple-length mismatch."""
    text = "CPU0 CPU1 CPU2\nGOOD: 1 2 3\nSHORT: 1 2\nLONG: 1 2 3 4\n"
    rows, cpu_count = _parse_softirqs(text)
    assert cpu_count == 3
    assert [r.kind for r in rows] == ["GOOD"]


def test_parse_non_int_row_dropped() -> None:
    """Single bad column poisons the row — the kernel emits ints in
    normal operation, so non-int is a corruption signal."""
    text = "CPU0 CPU1\nBAD: 1 xxx\nOK: 5 6\n"
    rows, _ = _parse_softirqs(text)
    assert [r.kind for r in rows] == ["OK"]


def test_parse_empty_text() -> None:
    rows, cpu_count = _parse_softirqs("")
    assert rows == ()
    assert cpu_count == 0


def test_parse_header_without_cpu_tokens() -> None:
    """Malformed header (no CPU* tokens) → cpu_count=0, rows=()."""
    rows, cpu_count = _parse_softirqs("junk junk junk\nHI: 1 2 3\n")
    assert cpu_count == 0
    assert rows == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.cpu_count == 0
    assert snap.rows == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "softirqs"
    p.write_text(_SAMPLE_4CPU)
    snap = _capture(path=p)
    assert snap.available
    assert snap.cpu_count == 4
    assert any(r.kind == "NET_RX" for r in snap.rows)


def test_row_by_kind_lookup(tmp_path: Path) -> None:
    p = tmp_path / "softirqs"
    p.write_text(_SAMPLE_4CPU)
    snap = _capture(path=p)
    assert _row_by_kind(snap, "NET_RX") is not None
    assert _row_by_kind(snap, "NONEXISTENT") is None


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _SoftirqsSnapshot(rows=(), cpu_count=0, available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "softirqs"
    p.write_text(_SAMPLE_4CPU)
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "CPU count" in rendered
    assert "NET_RX" in rendered
    assert "NET_TX" in rendered
    assert "TIMER" in rendered
    # Other (non-curated) kinds named in the footer line.
    assert "HI" in rendered or "TASKLET" in rendered


def test_render_parse_failed_explains(tmp_path: Path) -> None:
    """Available file but unparseable contents → explicit note, no
    crash, no ⚠."""
    p = tmp_path / "softirqs"
    p.write_text("garbage\n")
    snap = _capture(path=p)
    assert snap.available
    rendered = _render(snap)
    assert "parse failed" in rendered or "empty file" in rendered
    assert "⚠" not in rendered


def test_no_warnings_anywhere_extreme_skew(tmp_path: Path) -> None:
    """Cry-wolf prevention pin: even 100% NET_RX skew (every packet
    landed on CPU0) must NOT produce ⚠ anywhere in the rendered card.
    Skew threshold is workload-dependent and the single-CPU case
    makes &quot;skew&quot; meaningless — operator policy, not card
    policy. Pinned so a future refactor doesn't accidentally add a
    marker."""
    p = tmp_path / "softirqs"
    p.write_text("CPU0 CPU1 CPU2 CPU3\nNET_RX: 9999999 0 0 0\nTIMER: 100 100 100 100\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_missing_curated_kind_note(tmp_path: Path) -> None:
    """Curated kind absent from this kernel build → 'not exposed'
    label, not silent drop — operator sees what's missing."""
    p = tmp_path / "softirqs"
    p.write_text("CPU0\nTIMER: 100\n")  # only TIMER, no NET_RX
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "not exposed" in rendered
