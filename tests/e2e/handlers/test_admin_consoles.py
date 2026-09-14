"""End-to-end ``/admin_consoles``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs).
* Parser handles canonical fixed-column line with parenthesised
  flags; lines without parens are dropped defensively.
* Flag decode: E → enabled, C → preferred, B → boot.
* ⚠ fires iff enabled_count == 0; must NOT fire when at least
  one console is enabled (cry-wolf pin).
* No-consoles-at-all also fires ⚠ (degenerate case).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.consoles import (
    _capture,
    _Console,
    _ConsolesSnapshot,
    _parse_consoles,
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
        make_message_update("/admin_consoles", user_id=42, chat_type="private"),
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
        make_message_update("/admin_consoles", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Kernel consoles" in text


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
            "/admin_consoles",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = "tty0                 -WU (EC p  )    4:1\nttyS0                -W- (E  p a)    4:64\n"


def test_parse_canonical() -> None:
    consoles = _parse_consoles(_SAMPLE)
    assert len(consoles) == 2
    by_name = {c.name: c for c in consoles}
    assert by_name["tty0"].enabled is True
    assert by_name["tty0"].is_preferred is True
    assert by_name["tty0"].is_boot is False
    assert by_name["tty0"].device == "4:1"
    assert by_name["ttyS0"].enabled is True
    assert by_name["ttyS0"].is_preferred is False
    assert by_name["ttyS0"].device == "4:64"


def test_parse_disabled_console() -> None:
    """A console registered but not enabled: no 'E' in flags.
    Pinned because this is the input that triggers the ⚠
    predicate — the parser MUST set enabled=False here."""
    text = "ttyS1                -W- ( C p  )    4:65\n"
    consoles = _parse_consoles(text)
    assert len(consoles) == 1
    assert consoles[0].enabled is False
    assert consoles[0].is_preferred is True


def test_parse_boot_console() -> None:
    """B flag — earlycon / boot console. Surfaced separately
    from enabled because a lingering boot console after full
    boot is unusual and worth noting."""
    text = "ttyS0                -W- (EB p  )    4:64\n"
    consoles = _parse_consoles(text)
    assert consoles[0].is_boot is True
    assert consoles[0].enabled is True


def test_parse_line_without_parens_dropped() -> None:
    """Defensive: a line missing the parenthesised flag token
    is dropped, not synthesised with empty flags. Pinned because
    silent acceptance could produce a bogus enabled=False on a
    parser failure and burn the operator with a false ⚠."""
    text = "garbage line no parens here\nttyS0 -W- (E) 4:64\n"
    consoles = _parse_consoles(text)
    assert [c.name for c in consoles] == ["ttyS0"]


def test_parse_empty() -> None:
    assert _parse_consoles("") == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.consoles == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "consoles"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.consoles) == 2
    assert snap.enabled_count == 2


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _ConsolesSnapshot(consoles=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_no_consoles_warns() -> None:
    """Zero registered consoles — kernel printk has nowhere
    to go. Single ⚠ predicate fires."""
    snap = _ConsolesSnapshot(consoles=(), available=True)
    text = _render(snap)
    assert "⚠" in text
    assert "No consoles registered" in text


def test_render_no_warning_when_at_least_one_enabled(tmp_path: Path) -> None:
    """Cry-wolf pin: ⚠ MUST NOT appear when ≥1 console has
    the E flag. Pinned because the entire predicate is
    'zero enabled consoles' — a false positive on a healthy
    host would burn the operator on every invocation."""
    p = tmp_path / "consoles"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text
    assert "tty0" in text
    assert "ttyS0" in text


def test_render_warns_when_all_disabled(tmp_path: Path) -> None:
    """⚠ fires when every registered console lacks E."""
    p = tmp_path / "consoles"
    p.write_text(
        "tty0                 -WU ( C p  )    4:1\nttyS0                -W- (   p a)    4:64\n"
    )
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "DISABLED" in text


def test_console_fields() -> None:
    c = _Console(name="ttyS0", device="4:64", flags_raw="ECB")
    assert c.enabled is True
    assert c.is_preferred is True
    assert c.is_boot is True
