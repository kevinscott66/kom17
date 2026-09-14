"""End-to-end ``/admin_loadavg``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* 1-min load > 2 × cpu_count triggers ⚠ on that field only.
* 5-min/15-min loads NEVER ⚠ even when high (anti-cry-wolf:
  yesterday's spike isn't today's signal).
* Boundary at exactly ratio=2.0 does NOT ⚠ (strict greater-than).
* Missing CPU count → no ⚠ (absence-of-data anti-cry-wolf).
* Running/total task counts surfaced.
* last_pid surfaced (operator can sample twice for fork-rate).
* Group invocation → router-level private filter rejects.

Parser unit tests use tmp_path so the suite runs on macOS too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.loadavg import (
    _capture,
    _LoadSnapshot,
    _overloaded,
    _parse_loadavg,
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
        bot, make_message_update("/admin_loadavg", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_loadavg", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Load average" in text


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
        make_message_update("/admin_loadavg", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_parse_basic() -> None:
    """Standard /proc/loadavg line — 5 fields."""
    text = "0.42 0.55 0.70 3/512 12345\n"
    l1, l5, l15, running, total, last = _parse_loadavg(text)
    assert l1 == 0.42
    assert l5 == 0.55
    assert l15 == 0.70
    assert running == 3
    assert total == 512
    assert last == 12345


def test_parse_degrades_on_garbage() -> None:
    """Malformed fields → None; we don't want one bad line to
    nuke the whole snapshot."""
    text = "foo bar baz 0/0 not_a_pid\n"
    l1, l5, l15, running, total, last = _parse_loadavg(text)
    assert l1 is None
    assert l5 is None
    assert l15 is None
    assert running == 0
    assert total == 0
    assert last is None


def test_parse_empty() -> None:
    """Empty input → all None."""
    l1, _, _, _, _, _ = _parse_loadavg("")
    assert l1 is None


def test_capture_unavailable_when_path_missing(tmp_path: Path) -> None:
    """macOS dev path."""
    snap = _capture(path=tmp_path / "does_not_exist", cpu_count=4)
    assert not snap.available
    assert snap.load_1 is None
    assert not _overloaded(snap)


def test_overloaded_predicate_boundary(tmp_path: Path) -> None:
    """ratio = exactly 2.0 must NOT ⚠ (strict greater-than).
    ratio = 2.01 must ⚠. Boundary pin prevents accidental
    >= vs > drift."""
    p = tmp_path / "loadavg"
    p.write_text("8.0 4.0 3.0 1/100 1234\n")
    snap = _capture(path=p, cpu_count=4)  # ratio = 2.0 exactly
    assert not _overloaded(snap)

    p.write_text("8.04 4.0 3.0 1/100 1234\n")
    snap = _capture(path=p, cpu_count=4)  # ratio = 2.01
    assert _overloaded(snap)


def test_overloaded_triggers_warning_in_render(tmp_path: Path) -> None:
    """Overloaded snapshot → ⚠ in body. On the 1-min line only."""
    p = tmp_path / "loadavg"
    p.write_text("16.0 4.0 3.0 8/100 1234\n")
    snap = _capture(path=p, cpu_count=4)  # ratio = 4.0
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" in head


def test_healthy_load_no_warning(tmp_path: Path) -> None:
    """ratio = 0.1 — no ⚠ anywhere in body."""
    p = tmp_path / "loadavg"
    p.write_text("0.4 0.5 0.6 1/100 1234\n")
    snap = _capture(path=p, cpu_count=4)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_only_one_min_load_warns(tmp_path: Path) -> None:
    """Anti-cry-wolf pin: a high 5-min or 15-min load with a
    healthy 1-min must NOT ⚠. Yesterday's spike isn't today's
    signal."""
    p = tmp_path / "loadavg"
    # 1-min healthy (0.5), 5/15-min spiked (16.0 each)
    p.write_text("0.5 16.0 16.0 1/100 1234\n")
    snap = _capture(path=p, cpu_count=4)
    assert not _overloaded(snap)
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_missing_cpu_count_no_warning() -> None:
    """If cpu_count is None (exotic platform), we can't compute
    the ratio — must NOT ⚠. Absence of data is not a warning."""
    snap = _LoadSnapshot(
        load_1=99.0,
        load_5=99.0,
        load_15=99.0,
        running=10,
        total=100,
        last_pid=12345,
        cpu_count=None,
        available=True,
    )
    assert not _overloaded(snap)


def test_running_total_surfaced(tmp_path: Path) -> None:
    """Operator-visible: R/total task counts. Critical for I/O-
    vs-CPU-bound distinction."""
    p = tmp_path / "loadavg"
    p.write_text("4.0 4.0 4.0 7/250 5555\n")
    snap = _capture(path=p, cpu_count=4)
    rendered = _render(snap)
    assert "7" in rendered
    assert "250" in rendered


def test_last_pid_surfaced(tmp_path: Path) -> None:
    """Operator-visible: last_pid for fork-rate observation."""
    p = tmp_path / "loadavg"
    p.write_text("0.1 0.1 0.1 1/10 99999\n")
    snap = _capture(path=p, cpu_count=4)
    rendered = _render(snap)
    assert "99999" in rendered


def test_render_unavailable_explains() -> None:
    """macOS dev → explicit unavailable note."""
    snap = _LoadSnapshot(
        load_1=None,
        load_5=None,
        load_15=None,
        running=None,
        total=None,
        last_pid=None,
        cpu_count=4,
        available=False,
    )
    rendered = _render(snap)
    assert "unavailable" in rendered
