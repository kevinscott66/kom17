"""End-to-end ``/admin_memory``.

Pins:

* Non-developer → silent drop.
* Card renders every key in ``_KEYS`` even when the value is
  missing (renders as "unavailable" rather than dropped — the
  shape stability is what lets operators compare samples).
* VmSwap > 0 → ⚠. Any swap residency is the latency-killer
  the card exists to spot.
* VmPeak > 1.5× VmSize → ⚠. Transient inflation the allocator
  hasn't returned.
* Healthy state (no swap, peak ~= size) → bare. Cry-wolf
  prevention.
* /proc unavailable (non-Linux) → "unavailable" message.
* Missing VmSwap row (older / no-CONFIG_SWAP kernel) treated as
  non-concerning — we can't claim swap if we can't see the
  number.
* Group invocation → router-level private filter rejects.

Parser tests use a literal /proc/self/status fixture so the
extraction logic is exercised without a live kernel.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.memory import (
    _KEYS,
    _capture,
    _MemSnapshot,
    _parse,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(values: dict[str, int], *, available: bool = True) -> _MemSnapshot:
    return _MemSnapshot(available=available, values=values)


def _row_warn_count(rendered: str) -> int:
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_memory", user_id=42, chat_type="private"),
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
        make_message_update("/admin_memory", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Process memory" in text


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
            "/admin_memory",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_every_key_appears() -> None:
    """Each key in ``_KEYS`` must render even when the value is
    missing. Shape stability is what makes the card diffable
    against an earlier sample."""
    rendered = _render(_snap({}))
    for key in _KEYS:
        assert key in rendered, f"key {key!r} missing from card"


def test_render_swap_residency_surfaces_warning() -> None:
    """VmSwap > 0 is the latency-killer signal — the next request
    that touches a swapped page takes a page-fault detour through
    the swap device. ⚠ is the cue."""
    rendered = _render(_snap({"VmRSS": 100_000, "VmSwap": 1024, "VmSize": 100_000}))
    assert _row_warn_count(rendered) == 1


def test_render_peak_inflation_surfaces_warning() -> None:
    """VmPeak more than 1.5× current VmSize means the allocator
    hasn't returned the transient inflation to the OS. The marker
    is the cue to read the numbers and decide whether to investigate."""
    rendered = _render(_snap({"VmRSS": 100_000, "VmSize": 100_000, "VmPeak": 200_000, "VmSwap": 0}))
    assert _row_warn_count(rendered) == 1


def test_render_healthy_state_no_warnings() -> None:
    """No swap, peak ≈ size, all other rows present. If the
    renderer marked any healthy row, the ⚠ glyph would burn out
    (cry-wolf prevention, same posture as warnings_view / flags /
    locale / cpu / runtime)."""
    rendered = _render(
        _snap(
            {
                "VmRSS": 100_000,
                "VmPeak": 105_000,
                "VmHWM": 102_000,
                "VmSize": 110_000,
                "VmData": 80_000,
                "VmStk": 132,
                "VmSwap": 0,
            }
        )
    )
    assert _row_warn_count(rendered) == 0


def test_render_missing_swap_row_not_concerning() -> None:
    """A kernel without CONFIG_SWAP (or a parser that didn't see
    the row) reports VmSwap as missing. We must NOT mark — we
    can't claim swap if we can't see the number. The row still
    renders, just as "unavailable"."""
    rendered = _render(_snap({"VmRSS": 100_000, "VmSize": 100_000}))
    # No ⚠ on the VmSwap row because the value is unknown, not > 0.
    assert _row_warn_count(rendered) == 0


def test_render_unavailable_branch() -> None:
    """Non-Linux / unreadable /proc → "unavailable" message. Must
    NOT render fake zeros — a running process can't have 0 RSS,
    so a 0 there would be a lie."""
    rendered = _render(_snap({}, available=False))
    assert "unavailable" in rendered
    # The per-row breakdown is skipped on the unavailable branch —
    # we don't want zero-valued rows mistaken for real readings.
    assert "VmRSS" not in rendered


def test_parse_extracts_vm_rows() -> None:
    """Validate the /proc/self/status parser against a realistic
    kernel-format fixture. The format is stable across kernels;
    the test pins the column extraction."""
    text = (
        "Name:\tpython3\n"
        "State:\tR (running)\n"
        "VmPeak:\t  450000 kB\n"
        "VmSize:\t  300000 kB\n"
        "VmRSS:\t   120000 kB\n"
        "VmData:\t  200000 kB\n"
        "VmStk:\t     132 kB\n"
        "VmSwap:\t       0 kB\n"
        "Threads:\t1\n"
    )
    parsed = _parse(text)
    assert parsed["VmRSS"] == 120000
    assert parsed["VmPeak"] == 450000
    assert parsed["VmSwap"] == 0
    # Non-Vm* rows must NOT pollute the dict.
    assert "Name" not in parsed
    assert "Threads" not in parsed


def test_capture_unavailable_when_path_missing(tmp_path: Path) -> None:
    """Pointing capture at a missing path → available=False.
    Validates the macOS / Windows code path without crossing
    platform boundaries in CI."""
    snap = _capture(tmp_path / "no-such-file")
    assert snap.available is False
    assert snap.values == {}
