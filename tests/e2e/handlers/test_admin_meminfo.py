"""End-to-end ``/admin_meminfo``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* MemAvailable below 10% triggers ⚠ on that field — and the body
  has no ⚠ elsewhere.
* Healthy MemAvailable (≥10%) keeps body ⚠-free — cry-wolf
  prevention pin.
* Swap usage alone does NOT ⚠ — explicit anti-cry-wolf pin
  (kernel paging cold pages is correct behaviour, not a fault).
* MemAvailable absent (ancient kernel) → no ⚠, no false-confidence
  synthesis from MemFree+Buffers+Cached.
* Other-fields footer surfaces forward-compat kernel extensions.
* Group invocation → router-level private filter rejects.

Parser unit tests use tmp_path so the suite runs on macOS too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.meminfo import (
    _capture,
    _MemSnapshot,
    _parse_meminfo,
    _pressure,
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
        bot, make_message_update("/admin_meminfo", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_meminfo", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Host memory" in text


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
        make_message_update("/admin_meminfo", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_parse_meminfo_basic(tmp_path: Path) -> None:
    """Standard /proc/meminfo line format with kB suffix → bytes
    (×1024). The kernel calls it kB despite meaning KiB, and every
    tool that interoperates uses the same multiplier."""
    text = "MemTotal:       16384000 kB\nMemFree:          204800 kB\nMemAvailable:   12288000 kB\n"
    parsed = _parse_meminfo(text)
    assert parsed["MemTotal"] == 16384000 * 1024
    assert parsed["MemFree"] == 204800 * 1024
    assert parsed["MemAvailable"] == 12288000 * 1024


def test_parse_meminfo_no_unit() -> None:
    """HugePages counters have no kB suffix — kernel renders raw
    int. Parser must not multiply those."""
    parsed = _parse_meminfo("HugePages_Total:       0\nHugePages_Free:       0\n")
    assert parsed["HugePages_Total"] == 0
    assert parsed["HugePages_Free"] == 0


def test_parse_meminfo_skips_malformed() -> None:
    """A single corrupt line must NOT break the whole snapshot —
    degrade-don't-crash, same posture as every other parser in this
    directory."""
    parsed = _parse_meminfo(
        "MemTotal:    100 kB\nGarbage line without colon\nBogus: not_an_int kB\nMemFree:    50 kB\n"
    )
    assert parsed["MemTotal"] == 100 * 1024
    assert parsed["MemFree"] == 50 * 1024
    assert "Bogus" not in parsed


def test_capture_unavailable_when_path_missing(tmp_path: Path) -> None:
    """macOS dev / non-procfs container path. Must degrade with
    available=False, not raise."""
    snap = _capture(path=tmp_path / "does_not_exist")
    assert not snap.available
    assert snap.available_fraction is None


def test_capture_healthy(tmp_path: Path) -> None:
    """16 GiB total, 12 GiB available → ~75% headroom → no ⚠."""
    p = tmp_path / "meminfo"
    p.write_text(
        "MemTotal:       16777216 kB\n"
        "MemFree:          524288 kB\n"
        "MemAvailable:   12582912 kB\n"
        "Buffers:          262144 kB\n"
        "Cached:          4194304 kB\n"
        "SwapTotal:       2097152 kB\n"
        "SwapFree:        2097152 kB\n"
    )
    snap = _capture(path=p)
    assert snap.available
    assert snap.available_fraction is not None
    assert snap.available_fraction > 0.5
    assert not _pressure(snap)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_capture_pressure_triggers_warning(tmp_path: Path) -> None:
    """5% available → below the 10% threshold → ⚠ on MemAvailable."""
    p = tmp_path / "meminfo"
    p.write_text(
        "MemTotal:       16777216 kB\n"
        "MemFree:           65536 kB\n"
        "MemAvailable:     838860 kB\n"
        "Cached:           131072 kB\n"
    )
    snap = _capture(path=p)
    assert _pressure(snap)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" in head


def test_boundary_above_threshold_does_not_warn(tmp_path: Path) -> None:
    """Right above 10% must NOT ⚠ — anti-flapping pin at the
    boundary. 10.1% is healthy; 9.9% is pressure."""
    p = tmp_path / "meminfo"
    # MemAvailable = 10.5% of MemTotal.
    p.write_text("MemTotal:       10000000 kB\nMemAvailable:    1050000 kB\n")
    snap = _capture(path=p)
    assert not _pressure(snap)


def test_swap_usage_alone_does_not_warn(tmp_path: Path) -> None:
    """⚠'ing on swap usage would be the canonical cry-wolf failure.
    A box paging cold pages out is the kernel doing the right
    thing, not a problem. Pin explicitly so a future refactor
    doesn't accidentally add a swap-pressure ⚠."""
    p = tmp_path / "meminfo"
    p.write_text(
        "MemTotal:       16777216 kB\n"
        "MemFree:          524288 kB\n"
        "MemAvailable:   12582912 kB\n"
        "SwapTotal:       2097152 kB\n"
        "SwapFree:              0 kB\n"  # all swap used — still no ⚠
    )
    snap = _capture(path=p)
    assert not _pressure(snap)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_missing_memavailable_does_not_synthesise(tmp_path: Path) -> None:
    """Ancient kernel without MemAvailable: we must NOT synthesise
    it from MemFree + Buffers + Cached — that formula is wrong on
    modern kernels and a false-confidence number is worse than
    no number."""
    p = tmp_path / "meminfo"
    p.write_text(
        "MemTotal:       1000000 kB\n"
        "MemFree:           5000 kB\n"
        "Buffers:           1000 kB\n"
        "Cached:           10000 kB\n"
    )
    snap = _capture(path=p)
    assert snap.available_fraction is None
    assert not _pressure(snap)


def test_other_fields_bucketed(tmp_path: Path) -> None:
    """Forward-compat: a kernel field we don't curate must surface
    in the other_fields tuple — silent drops would defeat the
    purpose of a diagnostic card."""
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:    100 kB\nHardwareCorrupted: 0 kB\nFutureField: 42 kB\n")
    snap = _capture(path=p)
    assert "HardwareCorrupted" in snap.other_fields
    assert "FutureField" in snap.other_fields


def test_render_unavailable_explains() -> None:
    """When /proc/meminfo isn't readable, the operator must see an
    explicit explanation — not a card with mysterious n/a rows.
    macOS dev experience pin."""
    snap = _MemSnapshot(
        fields=(),
        other_fields=(),
        available_fraction=None,
        available=False,
    )
    rendered = _render(snap)
    assert "unavailable" in rendered
