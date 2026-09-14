"""End-to-end ``/admin_devices``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs).
* Parser: two labelled sections separated by blank line; lines
  before any header are dropped; non-int major dropped.
* Char vs block separation respected.
* No ⚠ predicate — informational card. Cry-wolf guard: ⚠ never
  appears regardless of content.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.devices import (
    _capture,
    _Device,
    _DevicesSnapshot,
    _parse_devices,
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
        make_message_update("/admin_devices", user_id=42, chat_type="private"),
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
        make_message_update("/admin_devices", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Registered device drivers" in text


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
            "/admin_devices",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "Character devices:\n"
    "  1 mem\n"
    "  4 tty\n"
    "  5 /dev/tty\n"
    " 10 misc\n"
    "\n"
    "Block devices:\n"
    "259 blkext\n"
    "  7 loop\n"
    "  8 sd\n"
)


def test_parse_canonical() -> None:
    char, block = _parse_devices(_SAMPLE)
    assert [(d.major, d.name) for d in char] == [
        (1, "mem"),
        (4, "tty"),
        (5, "/dev/tty"),
        (10, "misc"),
    ]
    assert [(d.major, d.name) for d in block] == [
        (259, "blkext"),
        (7, "loop"),
        (8, "sd"),
    ]


def test_parse_data_before_header_dropped() -> None:
    """Defensive: lines appearing before any 'Character devices:' /
    'Block devices:' header land in no section and are dropped.
    Pinned because silent acceptance would synthesise phantom
    drivers under an unknown future section header."""
    text = "  99 phantom\nCharacter devices:\n  1 mem\n"
    char, block = _parse_devices(text)
    assert [(d.major, d.name) for d in char] == [(1, "mem")]
    assert block == ()


def test_parse_non_integer_major_dropped() -> None:
    """A line whose first token isn't an int (corrupt read, future
    format) is dropped, not crashed on."""
    text = "Character devices:\n  abc broken\n  4 tty\n"
    char, _ = _parse_devices(text)
    assert [(d.major, d.name) for d in char] == [(4, "tty")]


def test_parse_empty() -> None:
    assert _parse_devices("") == ((), ())


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.char == ()
    assert snap.block == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "devices"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.char) == 4
    assert len(snap.block) == 3


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _DevicesSnapshot(char=(), block=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    snap = _DevicesSnapshot(char=(), block=(), available=True)
    text = _render(snap)
    assert "No drivers registered" in text
    assert "⚠" not in text


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "devices"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "mem" in text
    assert "loop" in text
    assert "tty" in text
    # Cry-wolf: informational card, must NEVER emit ⚠.
    assert "⚠" not in text


def test_render_caps_long_section(tmp_path: Path) -> None:
    """When a section has >40 entries we surface the first 40
    plus a '… N more' note. Pinned so an extremely device-heavy
    host (~80 char drivers on a desktop) doesn't blow the
    Telegram 4096-char message limit."""
    lines = ["Character devices:"]
    lines.extend(f"  {i} drv{i}" for i in range(1, 60))
    p = tmp_path / "devices"
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "drv1 " not in text  # we render via <code> wrap; just check count tail
    assert "19 more" in text


def test_device_fields() -> None:
    d = _Device(major=10, name="misc")
    assert d.major == 10
    assert d.name == "misc"
