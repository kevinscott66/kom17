"""End-to-end ``/admin_thp``.

Pins:

* Non-developer → silent drop.
* Card renders OR unavailable note (only when ALL sources missing).
* Partial availability: one source present, others missing → render
  shows the present one, 'unknown' for the rest.
* Bracketed parsing: ``a [b] c`` → ``b``; no brackets → ``""``.
* Cry-wolf must-not-fire on canonical-healthy sample
  (enabled=madvise).
* ⚠ fires only on enabled=always; madvise/never silent.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.thp import (
    _UNSAFE_MODE,
    _capture,
    _fmt_kb,
    _fmt_str,
    _parse_anon_hugepages,
    _parse_bracketed,
    _render,
    _ThpSnapshot,
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
        bot, make_message_update("/admin_thp", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_thp", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Transparent Huge Pages" in text


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
        make_message_update("/admin_thp", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parsers ---------------------------------------------------------------


def test_parse_bracketed_madvise() -> None:
    assert _parse_bracketed("always [madvise] never\n") == "madvise"


def test_parse_bracketed_always() -> None:
    assert _parse_bracketed("[always] madvise never\n") == "always"


def test_parse_bracketed_never() -> None:
    assert _parse_bracketed("always madvise [never]\n") == "never"


def test_parse_bracketed_empty() -> None:
    assert _parse_bracketed("") == ""


def test_parse_bracketed_no_brackets() -> None:
    """Defensive: a stripped-sysfs container or CONFIG=n kernel
    might emit a plain-text file with no brackets — we return
    empty rather than crash, and render shows 'unknown'."""
    assert _parse_bracketed("always madvise never\n") == ""


def test_parse_anon_hugepages_present() -> None:
    text = "MemTotal: 8000000 kB\nAnonHugePages:    524288 kB\n"
    assert _parse_anon_hugepages(text) == 524288


def test_parse_anon_hugepages_missing() -> None:
    assert _parse_anon_hugepages("MemTotal: 8000000 kB\n") == -1


def test_parse_anon_hugepages_garbage() -> None:
    assert _parse_anon_hugepages("AnonHugePages:    garbage kB\n") == -1


def test_parse_anon_hugepages_empty() -> None:
    assert _parse_anon_hugepages("") == -1


# --- warn predicate -------------------------------------------------------


def test_unsafe_mode_constant() -> None:
    """Pinned: only 'always' fires ⚠. If a future maintainer
    edits _UNSAFE_MODE to e.g. 'madvise' to silence a noisy
    machine, this test catches it — the threshold matters."""
    assert _UNSAFE_MODE == "always"


def test_warn_on_always() -> None:
    snap = _ThpSnapshot(enabled="always", defrag="madvise", anon_hugepages_kb=0, available=True)
    assert snap.under_pressure is True


def test_no_warn_on_madvise() -> None:
    snap = _ThpSnapshot(enabled="madvise", defrag="madvise", anon_hugepages_kb=0, available=True)
    assert snap.under_pressure is False


def test_no_warn_on_never() -> None:
    snap = _ThpSnapshot(enabled="never", defrag="never", anon_hugepages_kb=0, available=True)
    assert snap.under_pressure is False


def test_no_warn_on_unknown() -> None:
    """Stripped-sysfs (enabled='') must NOT fire ⚠ — 'we don't
    know' is the safer posture than 'assume always'."""
    snap = _ThpSnapshot(enabled="", defrag="", anon_hugepages_kb=-1, available=False)
    assert snap.under_pressure is False


# --- capture --------------------------------------------------------------


def test_capture_all_missing(tmp_path: Path) -> None:
    snap = _capture(
        enabled_path=tmp_path / "no1",
        defrag_path=tmp_path / "no2",
        meminfo_path=tmp_path / "no3",
    )
    assert not snap.available


def test_capture_partial_enabled_only(tmp_path: Path) -> None:
    en = tmp_path / "enabled"
    en.write_text("always [madvise] never\n")
    snap = _capture(
        enabled_path=en,
        defrag_path=tmp_path / "absent",
        meminfo_path=tmp_path / "absent2",
    )
    assert snap.available
    assert snap.enabled == "madvise"
    assert snap.defrag == ""
    assert snap.anon_hugepages_kb == -1


def test_capture_all_present(tmp_path: Path) -> None:
    en = tmp_path / "enabled"
    en.write_text("always [madvise] never\n")
    df = tmp_path / "defrag"
    df.write_text("[always] defer defer+madvise madvise never\n")
    mi = tmp_path / "meminfo"
    mi.write_text("MemTotal: 8000000 kB\nAnonHugePages: 524288 kB\n")
    snap = _capture(enabled_path=en, defrag_path=df, meminfo_path=mi)
    assert snap.available
    assert snap.enabled == "madvise"
    assert snap.defrag == "always"
    assert snap.anon_hugepages_kb == 524288


# --- render ---------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _ThpSnapshot(enabled="", defrag="", anon_hugepages_kb=-1, available=False)
    text = _render(snap)
    assert "unreadable" in text or "non-procfs" in text
    assert "⚠" not in text


def test_render_no_warning_on_madvise() -> None:
    """Cry-wolf pin: realistic healthy sample. madvise is the
    database-safe mode; ⚠ MUST NOT appear."""
    snap = _ThpSnapshot(enabled="madvise", defrag="madvise", anon_hugepages_kb=2048, available=True)
    text = _render(snap)
    assert "⚠" not in text


def test_render_no_warning_on_never() -> None:
    snap = _ThpSnapshot(enabled="never", defrag="never", anon_hugepages_kb=0, available=True)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warns_on_always() -> None:
    snap = _ThpSnapshot(enabled="always", defrag="always", anon_hugepages_kb=0, available=True)
    text = _render(snap)
    assert "⚠" in text
    assert "khugepaged" in text or "stall" in text


def test_render_partial_shows_unknown() -> None:
    """One source unreadable → 'unknown' for that field, no
    spurious ⚠ on the empty enabled value."""
    snap = _ThpSnapshot(enabled="", defrag="madvise", anon_hugepages_kb=-1, available=True)
    text = _render(snap)
    assert "unknown" in text
    assert "⚠" not in text


# --- helpers --------------------------------------------------------------


def test_fmt_kb_gib() -> None:
    assert _fmt_kb(4 * 1024 * 1024) == "4.00 GiB"


def test_fmt_kb_mib() -> None:
    assert _fmt_kb(2048) == "2.00 MiB"


def test_fmt_kb_small() -> None:
    assert _fmt_kb(100) == "100 kB"


def test_fmt_kb_sentinel() -> None:
    assert _fmt_kb(-1) == "unknown"


def test_fmt_str_present() -> None:
    assert _fmt_str("madvise") == "madvise"


def test_fmt_str_empty() -> None:
    assert _fmt_str("") == "unknown"
