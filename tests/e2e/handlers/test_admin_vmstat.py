"""End-to-end ``/admin_vmstat``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Single ⚠ on oom_kill > 0; nothing else triggers.
* Cry-wolf prevention: pgmajfault, pswpout, pswpin non-zero MUST
  NOT ⚠ (normal on any active host).
* Non-integer values skipped per-line; rest of file stays usable.
* Missing curated key renders "n/a", not silent drop.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.vmstat import (
    _capture,
    _parse_vmstat,
    _render,
    _triggered_warns,
    _VmstatSnapshot,
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
        bot, make_message_update("/admin_vmstat", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_vmstat", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "VM activity counters" in text


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
        make_message_update("/admin_vmstat", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


def test_parse_basic() -> None:
    text = "pgfault 100\npgmajfault 5\noom_kill 0\n"
    values = _parse_vmstat(text)
    assert values["pgfault"] == 100
    assert values["pgmajfault"] == 5
    assert values["oom_kill"] == 0


def test_parse_skips_non_integer() -> None:
    """Some kernel versions emit non-int values (e.g. balloon_inflate
    on Xen guests). Skip just that line, keep parsing the rest."""
    text = "pgfault 100\nbalstr 3.14\noom_kill 1\n"
    values = _parse_vmstat(text)
    assert "balstr" not in values
    assert values["pgfault"] == 100
    assert values["oom_kill"] == 1


def test_parse_skips_short_line() -> None:
    text = "pgfault\npgmajfault 7\n"
    values = _parse_vmstat(text)
    assert "pgfault" not in values
    assert values["pgmajfault"] == 7


# --- capture ---------------------------------------------------------------


def test_capture_unavailable_when_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "nope")
    assert not snap.available
    assert snap.values == {}


def test_capture_reads_tmp_file(tmp_path: Path) -> None:
    p = tmp_path / "vmstat"
    p.write_text("oom_kill 0\npgfault 1234\n")
    snap = _capture(path=p)
    assert snap.available
    assert snap.values["pgfault"] == 1234


# --- ⚠ predicate ---------------------------------------------------------


def test_warn_clean() -> None:
    snap = _VmstatSnapshot(values={"oom_kill": 0, "pgmajfault": 100}, available=True)
    assert _triggered_warns(snap) == ()


def test_warn_oom_kill() -> None:
    snap = _VmstatSnapshot(values={"oom_kill": 3}, available=True)
    assert _triggered_warns(snap) == ("oom_kill",)


def test_warn_false_when_unavailable() -> None:
    snap = _VmstatSnapshot(values={}, available=False)
    assert _triggered_warns(snap) == ()


def test_warn_no_cry_wolf_on_swap_or_faults(tmp_path: Path) -> None:
    """Cry-wolf pin: huge pgmajfault / pswpin / pswpout values must
    NOT trigger ⚠. Major faults are normal on any active host
    (every page-cache miss is a fault); swap activity can be
    legitimate pressure recovery."""
    p = tmp_path / "vmstat"
    p.write_text("oom_kill 0\npgmajfault 9999999\npswpin 999999\npswpout 999999\n")
    snap = _capture(path=p)
    assert _triggered_warns(snap) == ()


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _VmstatSnapshot(values={}, available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_clean_no_warning(tmp_path: Path) -> None:
    p = tmp_path / "vmstat"
    p.write_text("oom_kill 0\npgfault 1000\npgmajfault 50\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    # Curated descriptions contain literal ⚠ for the oom_kill row
    # (legend label) — strip those out before asserting no trigger.
    legend_free = head.replace(" ⚠", "")
    assert "⚠" not in legend_free


def test_render_warns_on_oom_kill(tmp_path: Path) -> None:
    p = tmp_path / "vmstat"
    p.write_text("oom_kill 1\npgfault 100\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" in head
    assert "OOM-killer fired" in head


def test_render_missing_key_shows_na(tmp_path: Path) -> None:
    """A curated key absent in /proc/vmstat (older kernel) → 'n/a',
    not silent drop."""
    p = tmp_path / "vmstat"
    # oom_kill key absent from this synthetic file.
    p.write_text("pgfault 100\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "oom_kill" in rendered
    assert "n/a" in rendered
