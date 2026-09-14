"""End-to-end ``/admin_locks``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux + /proc/locks present) OR unavailable note
  (macOS dev / non-procfs container).
* Parser distinguishes holders (8 tokens) from waiters (9 tokens,
  ``->`` between pid and file triple).
* Lines with wrong token count are dropped.
* Lines whose file triple doesn't have exactly 2 colons are dropped.
* ⚠ on blocked count > threshold; below = no marker.
* Single ⚠ predicate — no per-lock markers (cry-wolf prevention
  pinned: a long-held WRITE on SQLite is routine).
* Truncation note appears when entries > _RENDER_ROW_CAP.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.locks import (
    _BLOCKED_WARN_THRESHOLD,
    _RENDER_ROW_CAP,
    _capture,
    _LockEntry,
    _LocksSnapshot,
    _parse_locks,
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
        bot, make_message_update("/admin_locks", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_locks", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Kernel file locks" in text


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
        make_message_update("/admin_locks", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE_HOLDERS = (
    "1: POSIX  ADVISORY  WRITE 1234 08:01:1234567 0 EOF\n"
    "2: FLOCK  ADVISORY  WRITE 5678 08:01:7654321 0 EOF\n"
    "3: POSIX  ADVISORY  READ  9999 08:01:1111111 0 EOF\n"
)

_SAMPLE_WAITERS = (
    "4: POSIX  ADVISORY  WRITE 4444 -> 08:01:1234567 0 EOF\n"
    "5: POSIX  ADVISORY  WRITE 5555 -> 08:01:1234567 0 EOF\n"
)


def test_parse_holders_only() -> None:
    entries = _parse_locks(_SAMPLE_HOLDERS)
    assert len(entries) == 3
    assert all(not e.blocked for e in entries)
    by_id = {e.lock_id: e for e in entries}
    assert by_id["1"].type == "POSIX"
    assert by_id["1"].pid == "1234"
    assert by_id["1"].major_minor == "08:01"
    assert by_id["1"].inode == "1234567"
    assert by_id["2"].type == "FLOCK"
    assert by_id["3"].access == "READ"


def test_parse_waiter_arrow_recognised() -> None:
    """The ``->`` between pid and file triple marks a BLOCKED waiter.
    Pinned because losing the discriminator silently turns waiters
    into holders — which is exactly the wrong way to misread a
    contention situation."""
    entries = _parse_locks(_SAMPLE_HOLDERS + _SAMPLE_WAITERS)
    assert len(entries) == 5
    blocked = [e for e in entries if e.blocked]
    assert len(blocked) == 2
    assert {e.lock_id for e in blocked} == {"4", "5"}
    # Waiter file triple must parse correctly past the arrow.
    assert blocked[0].major_minor == "08:01"
    assert blocked[0].inode == "1234567"


def test_parse_wrong_token_count_dropped() -> None:
    """Mid-update reads or future format extensions may emit lines
    we can't parse — drop, don't crash."""
    text = (
        "1: POSIX ADVISORY WRITE 1234 08:01:1234567\n"  # 6 tokens
        "2: POSIX ADVISORY WRITE 5678 08:01:7654321 0 EOF extra\n"  # 9 + non-arrow
        "3: POSIX ADVISORY WRITE 9999 08:01:1111111 0 EOF\n"  # valid
    )
    entries = _parse_locks(text)
    assert [e.lock_id for e in entries] == ["3"]


def test_parse_bad_file_triple_dropped() -> None:
    """File triple must be exactly maj:min:inode (two colons).
    Anything else means the row isn't a lock row."""
    text = (
        "1: POSIX ADVISORY WRITE 1234 not-a-triple 0 EOF\n"
        "2: POSIX ADVISORY WRITE 5678 08:01:1111111 0 EOF\n"
    )
    entries = _parse_locks(text)
    assert [e.lock_id for e in entries] == ["2"]


def test_parse_missing_lock_id_colon_dropped() -> None:
    """First token must end with ``:`` — that's the kernel's lock-id
    delimiter. A line that doesn't follow this shape isn't a lock
    row at all."""
    text = (
        "noprefix POSIX ADVISORY WRITE 1234 08:01:1111111 0 EOF\n"
        "1: POSIX ADVISORY WRITE 1234 08:01:2222222 0 EOF\n"
    )
    entries = _parse_locks(text)
    assert [e.lock_id for e in entries] == ["1"]


def test_parse_empty() -> None:
    assert _parse_locks("") == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    """macOS / non-procfs — ENOENT path."""
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.entries == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "locks"
    p.write_text(_SAMPLE_HOLDERS + _SAMPLE_WAITERS)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.entries) == 5
    assert snap.holder_count == 3
    assert snap.blocked_count == 2


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _LocksSnapshot(entries=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "macOS" in text or "non-procfs" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    """Empty Linux /proc/locks is distinct from missing — say so."""
    snap = _LocksSnapshot(entries=(), available=True)
    text = _render(snap)
    assert "No file locks held" in text
    assert "⚠" not in text


def test_render_no_warning_below_threshold(tmp_path: Path) -> None:
    """Cry-wolf pin: a long-held WRITE lock on SQLite is routine.
    Holders alone must NOT trigger ⚠ regardless of count — the
    queue depth is the signal."""
    p = tmp_path / "locks"
    # 100 holders, zero waiters.
    lines = [f"{i}: POSIX ADVISORY WRITE {1000 + i} 08:01:{i} 0 EOF" for i in range(100)]
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warning_above_threshold(tmp_path: Path) -> None:
    """The single ⚠ predicate: blocked count > threshold."""
    p = tmp_path / "locks"
    n_blocked = _BLOCKED_WARN_THRESHOLD + 2
    lines = ["1: POSIX ADVISORY WRITE 1000 08:01:42 0 EOF"]
    for i in range(n_blocked):
        lines.append(f"{i + 2}: POSIX ADVISORY WRITE {2000 + i} -> 08:01:42 0 EOF")
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "blocked waiters" in text


def test_render_warning_strict_boundary(tmp_path: Path) -> None:
    """At exactly the threshold, no ⚠. Pinned because off-by-one
    on a threshold is a silent regression."""
    p = tmp_path / "locks"
    lines = []
    for i in range(_BLOCKED_WARN_THRESHOLD):
        lines.append(f"{i + 1}: POSIX ADVISORY WRITE {2000 + i} -> 08:01:42 0 EOF")
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_truncation_note(tmp_path: Path) -> None:
    p = tmp_path / "locks"
    n = _RENDER_ROW_CAP + 5
    lines = [f"{i}: POSIX ADVISORY WRITE {1000 + i} 08:01:{i} 0 EOF" for i in range(n)]
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    text = _render(snap)
    assert "more entries not shown" in text


def test_render_waiters_listed_first(tmp_path: Path) -> None:
    """Blocked waiters are the operationally interesting rows;
    they must precede holders in the rendered list."""
    p = tmp_path / "locks"
    p.write_text(_SAMPLE_HOLDERS + _SAMPLE_WAITERS)
    snap = _capture(path=p)
    text = _render(snap)
    # Find first BLOCKED and first HELD in the rendered string.
    blocked_pos = text.index("BLOCKED")
    held_pos = text.index("HELD")
    assert blocked_pos < held_pos


def test_lock_entry_fields_preserved() -> None:
    """All fields preserved verbatim — operator wants exact triples,
    not summaries."""
    e = _LockEntry(
        lock_id="42",
        type="OFDLCK",
        kind="ADVISORY",
        access="WRITE",
        pid="1234",
        blocked=True,
        major_minor="00:1f",
        inode="9999",
        start="0",
        end="EOF",
    )
    assert e.blocked is True
    assert e.end == "EOF"
    assert e.major_minor == "00:1f"
