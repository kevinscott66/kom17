"""End-to-end ``/admin_nr_open``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note (only when ALL sources missing).
* Partial availability: nr_open present, rlimit failing → render
  shows nr_open, 'unknown' for soft/hard.
* Cry-wolf must-not-fire on canonical-healthy sample
  (NOFILE=1024 of nr_open=1048576).
* ⚠ fires when hard/nr_open ratio crosses 80%.
* Defensive: ceiling=0 / sentinel doesn't fire spurious ⚠.
* rlimit reader OSError/ValueError → -1/-1, no crash.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.nr_open import (
    _NR_OPEN_WARN_RATIO,
    _capture,
    _fmt,
    _fmt_pct,
    _NrOpenSnapshot,
    _read_int,
    _read_rlimit_safe,
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
        bot, make_message_update("/admin_nr_open", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_nr_open", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "RLIMIT_NOFILE" in text or "nr_open" in text


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
        make_message_update("/admin_nr_open", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parsers ---------------------------------------------------------------


def test_read_int_present(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("1048576\n")
    assert _read_int(p) == 1048576


def test_read_int_missing(tmp_path: Path) -> None:
    assert _read_int(tmp_path / "absent") == -1


def test_read_int_non_numeric(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("garbage\n")
    assert _read_int(p) == -1


def test_read_rlimit_safe_normal() -> None:
    assert _read_rlimit_safe(lambda: (1024, 4096)) == (1024, 4096)


def test_read_rlimit_safe_oserror() -> None:
    def raiser() -> tuple[int, int]:
        raise OSError("sandbox blocked")

    assert _read_rlimit_safe(raiser) == (-1, -1)


def test_read_rlimit_safe_valueerror() -> None:
    """Pinned: resource.getrlimit can raise ValueError on a
    kernel that doesn't expose the resource id. Theoretical
    for RLIMIT_NOFILE today, but defensive handling means we
    degrade gracefully if it ever changes."""

    def raiser() -> tuple[int, int]:
        raise ValueError("unknown rlimit")

    assert _read_rlimit_safe(raiser) == (-1, -1)


# --- ratio + warn ---------------------------------------------------------


def test_usage_ratio_basic() -> None:
    snap = _NrOpenSnapshot(nr_open=1000, nofile_soft=500, nofile_hard=800, available=True)
    assert snap.usage_ratio == 0.8
    assert snap.under_pressure is True


def test_usage_ratio_below_threshold() -> None:
    snap = _NrOpenSnapshot(nr_open=1_048_576, nofile_soft=1024, nofile_hard=4096, available=True)
    assert snap.under_pressure is False


def test_ceiling_zero_no_crash() -> None:
    snap = _NrOpenSnapshot(nr_open=0, nofile_soft=1024, nofile_hard=4096, available=True)
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_hard_sentinel_no_crash() -> None:
    snap = _NrOpenSnapshot(nr_open=1_048_576, nofile_soft=-1, nofile_hard=-1, available=True)
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_threshold_constant_sane() -> None:
    assert 0.5 < _NR_OPEN_WARN_RATIO < 1.0


# --- capture ---------------------------------------------------------------


def test_capture_all_missing(tmp_path: Path) -> None:
    def failing_reader() -> tuple[int, int]:
        raise OSError("sandbox blocked")

    snap = _capture(nr_open_path=tmp_path / "no", rlimit_reader=failing_reader)
    assert not snap.available


def test_capture_partial_nr_open_only(tmp_path: Path) -> None:
    p = tmp_path / "nr_open"
    p.write_text("1048576\n")

    def failing_reader() -> tuple[int, int]:
        raise OSError("sandbox")

    snap = _capture(nr_open_path=p, rlimit_reader=failing_reader)
    assert snap.available
    assert snap.nr_open == 1048576
    assert snap.nofile_soft == -1
    assert snap.nofile_hard == -1


def test_capture_partial_rlimit_only(tmp_path: Path) -> None:
    snap = _capture(
        nr_open_path=tmp_path / "absent",
        rlimit_reader=lambda: (1024, 4096),
    )
    assert snap.available
    assert snap.nr_open == -1
    assert snap.nofile_soft == 1024
    assert snap.nofile_hard == 4096


def test_capture_all_present(tmp_path: Path) -> None:
    p = tmp_path / "nr_open"
    p.write_text("1048576\n")
    snap = _capture(nr_open_path=p, rlimit_reader=lambda: (1024, 4096))
    assert snap.available
    assert snap.nr_open == 1048576
    assert snap.nofile_soft == 1024
    assert snap.nofile_hard == 4096


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _NrOpenSnapshot(nr_open=-1, nofile_soft=-1, nofile_hard=-1, available=False)
    text = _render(snap)
    assert "readable" in text or "non-procfs" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy() -> None:
    """Cry-wolf pin: typical Linux defaults (NOFILE=1024 soft,
    4096 hard, nr_open=1048576). Ratio ~0.4%. ⚠ MUST NOT appear."""
    snap = _NrOpenSnapshot(nr_open=1_048_576, nofile_soft=1024, nofile_hard=4096, available=True)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_pressure() -> None:
    snap = _NrOpenSnapshot(nr_open=1000, nofile_soft=500, nofile_hard=900, available=True)
    text = _render(snap)
    assert "⚠" in text
    assert "EPERM" in text or "sysctl" in text


def test_render_partial_shows_unknown() -> None:
    """nr_open known, rlimit failed → 'unknown' for the
    soft/hard rows, no spurious ⚠."""
    snap = _NrOpenSnapshot(nr_open=1_048_576, nofile_soft=-1, nofile_hard=-1, available=True)
    text = _render(snap)
    assert "unknown" in text
    assert "⚠" not in text


# --- helpers ---------------------------------------------------------------


def test_fmt_thousands() -> None:
    assert _fmt(1_048_576) == "1,048,576"


def test_fmt_sentinel() -> None:
    assert _fmt(-1) == "unknown"


def test_fmt_pct() -> None:
    assert _fmt_pct(0.8) == "80.0%"
    assert _fmt_pct(0.0) == "0.0%"
