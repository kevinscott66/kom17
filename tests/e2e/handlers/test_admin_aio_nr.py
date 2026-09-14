"""End-to-end ``/admin_aio_nr``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note (only when BOTH sources
  missing).
* Partial availability: one sysctl present, other missing →
  card renders the present one; missing one shows 'unknown'.
* Cry-wolf must-not-fire on canonical-healthy sample
  (aio-nr << aio-max-nr).
* ⚠ fires when usage ratio crosses threshold.
* Defensive: ceiling=0 / sentinel doesn't fire spurious ⚠.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.aio_nr import (
    _AIO_WARN_RATIO,
    _AioNrSnapshot,
    _capture,
    _fmt,
    _fmt_pct,
    _read_int,
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
        bot, make_message_update("/admin_aio_nr", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_aio_nr", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "AIO contexts" in text


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
        make_message_update("/admin_aio_nr", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parsers ---------------------------------------------------------------


def test_read_int_present(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("65536\n")
    assert _read_int(p) == 65536


def test_read_int_missing(tmp_path: Path) -> None:
    assert _read_int(tmp_path / "absent") == -1


def test_read_int_non_numeric(tmp_path: Path) -> None:
    p = tmp_path / "v"
    p.write_text("garbage\n")
    assert _read_int(p) == -1


# --- ratio + warn ---------------------------------------------------------


def test_usage_ratio_basic() -> None:
    snap = _AioNrSnapshot(aio_nr=800, aio_max_nr=1000, available=True)
    assert snap.usage_ratio == 0.8
    assert snap.under_pressure is True


def test_usage_ratio_below_threshold() -> None:
    snap = _AioNrSnapshot(aio_nr=100, aio_max_nr=1_048_576, available=True)
    assert snap.under_pressure is False


def test_ceiling_zero_no_crash() -> None:
    """Defensive — fs.aio-max-nr=0 (exotic but legal) must NOT
    fire ⚠ on a divide-by-zero or "100% of nothing"."""
    snap = _AioNrSnapshot(aio_nr=0, aio_max_nr=0, available=True)
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_aio_nr_sentinel_no_crash() -> None:
    snap = _AioNrSnapshot(aio_nr=-1, aio_max_nr=65536, available=True)
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_threshold_constant_sane() -> None:
    assert 0.5 < _AIO_WARN_RATIO < 1.0


# --- capture ---------------------------------------------------------------


def test_capture_all_missing(tmp_path: Path) -> None:
    """Both sources absent → available=False. CONFIG_AIO=n or
    macOS-dev render path."""
    snap = _capture(
        aio_nr_path=tmp_path / "no1",
        aio_max_nr_path=tmp_path / "no2",
    )
    assert not snap.available


def test_capture_partial_aio_nr_only(tmp_path: Path) -> None:
    nr = tmp_path / "aio_nr"
    nr.write_text("128\n")
    snap = _capture(
        aio_nr_path=nr,
        aio_max_nr_path=tmp_path / "absent",
    )
    assert snap.available
    assert snap.aio_nr == 128
    assert snap.aio_max_nr == -1


def test_capture_all_present(tmp_path: Path) -> None:
    nr = tmp_path / "aio_nr"
    nr.write_text("128\n")
    mx = tmp_path / "aio_max_nr"
    mx.write_text("65536\n")
    snap = _capture(aio_nr_path=nr, aio_max_nr_path=mx)
    assert snap.available
    assert snap.aio_nr == 128
    assert snap.aio_max_nr == 65536


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _AioNrSnapshot(aio_nr=-1, aio_max_nr=-1, available=False)
    text = _render(snap)
    assert "unreadable" in text or "CONFIG_AIO" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy() -> None:
    """Cry-wolf pin: aio-nr=128 of aio-max-nr=65536 — typical
    healthy box. ⚠ MUST NOT appear."""
    snap = _AioNrSnapshot(aio_nr=128, aio_max_nr=65536, available=True)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_pressure() -> None:
    snap = _AioNrSnapshot(aio_nr=900, aio_max_nr=1000, available=True)
    text = _render(snap)
    assert "⚠" in text
    assert "EAGAIN" in text


def test_render_partial_shows_unknown() -> None:
    """One source unreadable → 'unknown' for that field, no
    spurious ⚠ on the -1 sentinel."""
    snap = _AioNrSnapshot(aio_nr=128, aio_max_nr=-1, available=True)
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
