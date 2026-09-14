"""End-to-end ``/admin_file_nr``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note.
* Parser: 3 space/tab-separated ints; short input → all -1.
* Defensive: non-integer fields → -1 sentinel, no crash.
* Ratio guards: maximum=0 → ratio 0.0 (no spurious ⚠).
* Cry-wolf must-not-fire on canonical-healthy sample.
* ⚠ fires when allocated/maximum >= threshold.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.file_nr import (
    _FILE_NR_WARN_RATIO,
    _capture,
    _FileNrSnapshot,
    _fmt,
    _fmt_pct,
    _parse_file_nr,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


# Canonical-healthy: 1024 allocated of 9.2e18 max — typical
# 64-bit default fs.file-max where the ceiling is effectively
# unlimited. The ratio is microscopic; ⚠ MUST NOT fire.
_SAMPLE_HEALTHY = "1024\t0\t9223372036854775807\n"


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_file_nr", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_file_nr", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "System-wide file handles" in text


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
        make_message_update("/admin_file_nr", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


def test_parse_canonical() -> None:
    assert _parse_file_nr(_SAMPLE_HEALTHY) == (1024, 0, 9223372036854775807)


def test_parse_space_separated() -> None:
    """The kernel emits tabs, but operators who cat-and-paste
    into a test get spaces. Both must parse identically."""
    assert _parse_file_nr("100 0 1000\n") == (100, 0, 1000)


def test_parse_short_returns_sentinels() -> None:
    """Fewer than 3 tokens → all -1. Render then surfaces
    'unknown' rather than crashing on the missing field."""
    assert _parse_file_nr("100 200\n") == (-1, -1, -1)


def test_parse_non_integer_field() -> None:
    """One bad token per-field becomes -1 (we don't drop the
    whole line — the other two fields are still useful signal)."""
    assert _parse_file_nr("100 nope 1000\n") == (100, -1, 1000)


def test_parse_empty() -> None:
    assert _parse_file_nr("") == (-1, -1, -1)


# --- ratio + warn ---------------------------------------------------------


def test_usage_ratio_basic() -> None:
    snap = _FileNrSnapshot(allocated=800, unused=0, maximum=1000, available=True)
    assert snap.usage_ratio == 0.8
    assert snap.under_pressure is True


def test_usage_ratio_below_threshold() -> None:
    snap = _FileNrSnapshot(allocated=799, unused=0, maximum=1000, available=True)
    assert snap.under_pressure is False


def test_maximum_zero_no_crash() -> None:
    """A kernel emitting maximum=0 (or our -1 parse-failure
    sentinel) must NOT crash with DivisionByZero. We return
    ratio 0.0 — 'we don't know, so don't warn' beats 'everything
    is full' for a sentinel that means parse failure."""
    snap_zero = _FileNrSnapshot(allocated=100, unused=0, maximum=0, available=True)
    snap_sentinel = _FileNrSnapshot(allocated=100, unused=0, maximum=-1, available=True)
    assert snap_zero.usage_ratio == 0.0
    assert snap_sentinel.usage_ratio == 0.0
    assert snap_zero.under_pressure is False
    assert snap_sentinel.under_pressure is False


def test_allocated_sentinel_no_crash() -> None:
    snap = _FileNrSnapshot(allocated=-1, unused=0, maximum=1000, available=True)
    assert snap.usage_ratio == 0.0
    assert snap.under_pressure is False


def test_threshold_constant_sane() -> None:
    assert 0.5 < _FILE_NR_WARN_RATIO < 1.0


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.allocated == -1
    assert snap.maximum == -1


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "file-nr"
    p.write_text(_SAMPLE_HEALTHY)
    snap = _capture(path=p)
    assert snap.available
    assert snap.allocated == 1024
    assert snap.maximum == 9223372036854775807


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _FileNrSnapshot(allocated=-1, unused=-1, maximum=-1, available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_no_warning_on_healthy(tmp_path: Path) -> None:
    """Cry-wolf pin: 1024 / 9.2e18 is essentially 0% — ⚠ MUST
    NOT appear. A false positive on every healthy host would
    burn the operator every invocation."""
    p = tmp_path / "file-nr"
    p.write_text(_SAMPLE_HEALTHY)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_pressure(tmp_path: Path) -> None:
    p = tmp_path / "file-nr"
    p.write_text("900 0 1000\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "ENFILE" in text


def test_render_unknown_when_maximum_zero(tmp_path: Path) -> None:
    """When maximum is 0 or sentinel, we can't compute a
    meaningful ratio — surface 'unknown' instead of a
    misleading 0.0%."""
    p = tmp_path / "file-nr"
    p.write_text("100 0 0\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "usage=<code>unknown</code>" in text
    assert "⚠" not in text


# --- helpers ---------------------------------------------------------------


def test_fmt_thousands() -> None:
    assert _fmt(9_223_372_036_854_775_807) == "9,223,372,036,854,775,807"


def test_fmt_sentinel() -> None:
    assert _fmt(-1) == "unknown"


def test_fmt_pct() -> None:
    assert _fmt_pct(0.0) == "0.0%"
    assert _fmt_pct(0.8) == "80.0%"
