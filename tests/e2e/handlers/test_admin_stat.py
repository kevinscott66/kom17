"""End-to-end ``/admin_stat``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux /proc/stat) OR unavailable note.
* Parser filters to ``_INTERESTING`` keyset; unknown keys
  (per-cpu, intr, softirq) drop silently.
* Defensive: non-integer value drops without crash; lines
  shorter than 2 tokens drop; blank lines drop.
* Sentinel ``-1`` for absent keys renders as ``unknown`` (not 0
  — must not confuse "kernel didn't tell us" with "zero").
* Cry-wolf must-not-fire when procs_blocked < threshold.
* ⚠ fires when procs_blocked ≥ ``_BLOCKED_WARN_THRESHOLD``.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.stat import (
    _BLOCKED_WARN_THRESHOLD,
    _capture,
    _fmt,
    _parse_stat,
    _render,
    _StatSnapshot,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


_SAMPLE = (
    "cpu  3357 0 4313 1362393 13455 0 51 1 0 0\n"
    "cpu0 1000 0 2000 600000 6000 0 25 0 0 0\n"
    "cpu1 2357 0 2313 762393 7455 0 26 1 0 0\n"
    "intr 8345881 12 0 0 0 0 0 0\n"
    "ctxt 13458\n"
    "btime 1693847562\n"
    "processes 23456\n"
    "procs_running 1\n"
    "procs_blocked 0\n"
    "softirq 234567 1 2 3\n"
)


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_stat", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_stat", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "System counters" in text


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
        make_message_update("/admin_stat", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


def test_parse_canonical() -> None:
    parsed = _parse_stat(_SAMPLE)
    assert parsed == {
        "ctxt": 13458,
        "btime": 1693847562,
        "processes": 23456,
        "procs_running": 1,
        "procs_blocked": 0,
    }


def test_parse_uninteresting_keys_dropped() -> None:
    """Per-cpu, intr, softirq lines are covered by other admin
    cards — re-rendering them here would double the card length.
    Parser must drop them silently."""
    parsed = _parse_stat(_SAMPLE)
    assert "cpu" not in parsed
    assert "cpu0" not in parsed
    assert "intr" not in parsed
    assert "softirq" not in parsed


def test_parse_non_integer_value_dropped() -> None:
    """Defensive: a future kernel emitting a non-integer value
    under one of our keys must drop rather than crash render."""
    text = "ctxt notanumber\nbtime 1000\n"
    parsed = _parse_stat(text)
    assert parsed == {"btime": 1000}


def test_parse_short_line_dropped() -> None:
    text = "ctxt\nbtime 1000\n"
    parsed = _parse_stat(text)
    assert parsed == {"btime": 1000}


def test_parse_empty() -> None:
    assert _parse_stat("") == {}


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.ctxt == -1
    assert snap.procs_blocked == -1


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "stat"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert snap.btime == 1693847562
    assert snap.processes == 23456
    assert snap.ctxt == 13458
    assert snap.procs_running == 1
    assert snap.procs_blocked == 0


def test_capture_partial_keeps_sentinel(tmp_path: Path) -> None:
    """A kernel that omits e.g. ``processes`` must surface
    ``-1`` for that field so render distinguishes 'kernel didn't
    tell us' from a real zero."""
    p = tmp_path / "stat"
    p.write_text("btime 1000\nctxt 5\n")
    snap = _capture(path=p)
    assert snap.available
    assert snap.processes == -1
    assert snap.procs_running == -1


# --- snapshot threshold ----------------------------------------------------


def test_blocked_warn_below_threshold() -> None:
    snap = _StatSnapshot(
        ctxt=1,
        btime=1,
        processes=1,
        procs_running=1,
        procs_blocked=_BLOCKED_WARN_THRESHOLD - 1,
        available=True,
    )
    assert snap.blocked_warn is False


def test_blocked_warn_at_threshold() -> None:
    snap = _StatSnapshot(
        ctxt=1,
        btime=1,
        processes=1,
        procs_running=1,
        procs_blocked=_BLOCKED_WARN_THRESHOLD,
        available=True,
    )
    assert snap.blocked_warn is True


def test_blocked_warn_sentinel_does_not_fire() -> None:
    """Sentinel ``-1`` (key absent) must NOT trigger ⚠ — a
    missing field is not evidence of a stuck I/O subsystem."""
    snap = _StatSnapshot(
        ctxt=-1,
        btime=-1,
        processes=-1,
        procs_running=-1,
        procs_blocked=-1,
        available=True,
    )
    assert snap.blocked_warn is False


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _StatSnapshot(
        ctxt=-1,
        btime=-1,
        processes=-1,
        procs_running=-1,
        procs_blocked=-1,
        available=False,
    )
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy(tmp_path: Path) -> None:
    """Cry-wolf pin: ⚠ MUST NOT appear when procs_blocked is
    below threshold. The canonical sample has procs_blocked=0
    which is the healthy baseline; surfacing ⚠ here would burn
    the operator every invocation."""
    p = tmp_path / "stat"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_blocked(tmp_path: Path) -> None:
    p = tmp_path / "stat"
    p.write_text(_SAMPLE.replace("procs_blocked 0", "procs_blocked 7"))
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "D state" in text


def test_render_sentinel_unknown(tmp_path: Path) -> None:
    """Absent key renders as 'unknown' not '0' — preserves the
    'kernel didn't tell us' distinction."""
    p = tmp_path / "stat"
    p.write_text("btime 1000\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "unknown" in text


# --- helpers ---------------------------------------------------------------


def test_fmt_thousands_separator() -> None:
    assert _fmt(1234567) == "1,234,567"


def test_fmt_sentinel() -> None:
    assert _fmt(-1) == "unknown"


def test_fmt_zero() -> None:
    assert _fmt(0) == "0"
