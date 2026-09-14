"""End-to-end ``/admin_misc``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs).
* Parser: ``<int minor> <name>`` per line; malformed lines
  dropped defensively (non-int minor, single-token line).
* Card sorts entries by name for scannability.
* No ⚠ predicate — informational. Cry-wolf guard: ⚠ never
  appears regardless of input.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.misc import (
    _capture,
    _MiscEntry,
    _MiscSnapshot,
    _parse_misc,
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
        bot,
        make_message_update("/admin_misc", user_id=42, chat_type="private"),
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
        make_message_update("/admin_misc", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Misc char devices" in text


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
            "/admin_misc",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = " 60 cpu_dma_latency\n130 watchdog\n237 loop-control\n228 hwrng\n229 fuse\n232 kvm\n"


def test_parse_canonical() -> None:
    entries = _parse_misc(_SAMPLE)
    by_name = {e.name: e for e in entries}
    assert by_name["kvm"].minor == 232
    assert by_name["fuse"].minor == 229
    assert by_name["hwrng"].minor == 228
    assert len(entries) == 6


def test_parse_non_integer_minor_dropped() -> None:
    """Malformed first-token (non-int) is dropped rather than
    crashing render. Pinned because silent acceptance would
    let a corrupt read synthesise a phantom registration."""
    text = "abc broken\n232 kvm\n"
    entries = _parse_misc(text)
    assert [e.name for e in entries] == ["kvm"]


def test_parse_single_token_line_dropped() -> None:
    """A line with only one token (no name) is dropped — there's
    nothing operationally useful to surface."""
    text = "232\n232 kvm\n"
    entries = _parse_misc(text)
    assert [e.name for e in entries] == ["kvm"]


def test_parse_empty() -> None:
    assert _parse_misc("") == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.entries == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "misc"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.entries) == 6


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _MiscSnapshot(entries=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    snap = _MiscSnapshot(entries=(), available=True)
    text = _render(snap)
    assert "No misc drivers registered" in text
    assert "⚠" not in text


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "misc"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "kvm" in text
    assert "fuse" in text
    assert "hwrng" in text
    # Cry-wolf: informational card, must NEVER emit ⚠.
    assert "⚠" not in text


def test_render_sorts_by_name(tmp_path: Path) -> None:
    """Operator usually looks up by name (kvm? fuse? tun?), not
    by minor. Sort by name pins the rendered order so the
    operator can scan alphabetically."""
    p = tmp_path / "misc"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    # 'cpu_dma_latency' < 'fuse' < 'hwrng' < 'kvm' alphabetically
    assert text.index("cpu_dma_latency") < text.index("fuse")
    assert text.index("fuse") < text.index("hwrng")
    assert text.index("hwrng") < text.index("kvm")


def test_misc_entry_fields() -> None:
    e = _MiscEntry(minor=232, name="kvm")
    assert e.minor == 232
    assert e.name == "kvm"
