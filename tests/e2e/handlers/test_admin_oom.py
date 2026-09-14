"""End-to-end ``/admin_oom``.

Pins:

* Non-developer → silent drop.
* Live card renders with this-process + system-policy sections.
* Healthy snapshot (adj=0, panic=0) → zero ⚠ on data rows.
* Positive oom_score_adj → ⚠ on the adj row (the stale-test-tweak
  regression class).
* Negative oom_score_adj → bare, NOT ⚠ — operator intent to
  protect; never cry-wolf on intentional configuration.
* panic_on_oom != 0 → ⚠ on the panic row (host-wide blast radius).
* Mode-2 overcommit surfaces the ratio with "(mode 2 only)" note;
  mode 0/1 surfaces "(currently ignored)" so the operator's mental
  model doesn't drift.
* Missing /proc files (non-Linux fake) → informational rows, NO ⚠.
* ``_read_int`` parses single-line ints; returns None on missing/
  unparseable.
* ``_adj_concerning`` boundary: 0 is not concerning, 1 is.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.oom import (
    _adj_concerning,
    _capture,
    _OomSnapshot,
    _panic_concerning,
    _read_int,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    oom_score: int | None = 100,
    oom_score_adj: int | None = 0,
    overcommit_memory: int | None = 0,
    overcommit_ratio: int | None = 50,
    panic_on_oom: int | None = 0,
) -> _OomSnapshot:
    return _OomSnapshot(
        oom_score=oom_score,
        oom_score_adj=oom_score_adj,
        overcommit_memory=overcommit_memory,
        overcommit_ratio=overcommit_ratio,
        panic_on_oom=panic_on_oom,
    )


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_oom", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_oom", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "OOM-killer posture" in text
    assert "this process" in text
    assert "system policy" in text


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
        make_message_update("/admin_oom", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_healthy_snapshot_no_warn() -> None:
    rendered = _render(_snap())
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0


def test_positive_adj_marks_row() -> None:
    """oom_score_adj > 0 is the stale-test-tweak class of regression.
    ⚠ on the adj row, nothing else marked."""
    rendered = _render(_snap(oom_score_adj=500))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    adj_line = next(line for line in head.splitlines() if "oom_score_adj" in line)
    assert "⚠" in adj_line


def test_negative_adj_not_concerning() -> None:
    """Negative adj is operator intent to PROTECT the process. Never
    ⚠ — that would be crying wolf on a deliberate choice."""
    rendered = _render(_snap(oom_score_adj=-500))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0


def test_panic_on_oom_marks_row() -> None:
    """panic_on_oom != 0 is the doomsday-switch signal — host-wide
    reboot on any OOM. ⚠ on the panic row."""
    rendered = _render(_snap(panic_on_oom=1))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    assert "panic_on_oom = 1" in head


def test_overcommit_mode_2_notes_ratio_meaningful() -> None:
    """Mode 2 (strict) is the only mode where overcommit_ratio
    actually bounds anything. The note must change so the
    operator's mental model tracks the kernel's behaviour."""
    rendered = _render(_snap(overcommit_memory=2, overcommit_ratio=80))
    assert "mode 2 only" in rendered
    assert "currently ignored" not in rendered


def test_overcommit_mode_0_notes_ratio_ignored() -> None:
    """Modes 0/1 ignore the ratio entirely. The note must say so —
    seeing ``ratio = 50`` without context implies it's binding."""
    rendered = _render(_snap(overcommit_memory=0, overcommit_ratio=50))
    assert "currently ignored" in rendered


def test_non_linux_no_warn() -> None:
    """All /proc reads failed (non-Linux host) → informational
    rows, zero ⚠. Same cry-wolf posture as every other admin card
    on missing-by-design data."""
    rendered = _render(
        _snap(
            oom_score=None,
            oom_score_adj=None,
            overcommit_memory=None,
            overcommit_ratio=None,
            panic_on_oom=None,
        )
    )
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0
    assert "not readable" in head


def test_adj_concerning_boundary() -> None:
    """Boundary pin around ``_ADJ_CONCERNING_THRESHOLD``. Zero is
    the default and benign; +1 is concerning; negative protects."""
    assert not _adj_concerning(0)
    assert not _adj_concerning(-1)
    assert not _adj_concerning(-1000)
    assert _adj_concerning(1)
    assert _adj_concerning(1000)
    assert not _adj_concerning(None)


def test_panic_concerning_zero_only_ok() -> None:
    """Only 0 is benign — any non-zero value means SOME panic
    behaviour is active (kernel has 0/1/2 modes for this knob)."""
    assert not _panic_concerning(0)
    assert _panic_concerning(1)
    assert _panic_concerning(2)
    assert not _panic_concerning(None)


def test_read_int(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.write_text("42\n")
    assert _read_int(good) == 42

    negative = tmp_path / "negative"
    negative.write_text("-1000\n")
    assert _read_int(negative) == -1000

    junk = tmp_path / "junk"
    junk.write_text("not-a-number")
    assert _read_int(junk) is None

    missing = tmp_path / "nope"
    assert _read_int(missing) is None


def test_capture_with_fixtures(tmp_path: Path) -> None:
    """End-to-end _capture against fixture files. Mixed present /
    missing files so the snapshot exercises both branches."""
    score = tmp_path / "oom_score"
    score.write_text("250")
    adj = tmp_path / "oom_score_adj"
    adj.write_text("0")
    panic = tmp_path / "panic_on_oom"
    panic.write_text("0")
    # overcommit files deliberately missing.
    snap = _capture(
        oom_score_path=score,
        oom_score_adj_path=adj,
        overcommit_memory_path=tmp_path / "missing_om",
        overcommit_ratio_path=tmp_path / "missing_or",
        panic_on_oom_path=panic,
    )
    assert snap.oom_score == 250
    assert snap.oom_score_adj == 0
    assert snap.overcommit_memory is None
    assert snap.overcommit_ratio is None
    assert snap.panic_on_oom == 0
