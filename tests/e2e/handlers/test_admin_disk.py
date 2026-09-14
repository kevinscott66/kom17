"""End-to-end ``/admin_disk``.

Pins:

* Non-developer → silent drop.
* Card lists at least ``database_dir`` and ``logs_dir`` from the
  resolved Settings paths.
* Healthy footer when every volume has headroom.
* Group invocation → router-level private filter rejects.
* Unit pin on dedup: in the default config ``message_stats_dir``
  resolves to the same path as ``database_dir`` — the card must
  emit one row, not two (otherwise an operator scanning two
  identical rows learns to skip them).
* Unit pin on ``_fmt_bytes`` covering the B/KiB/MiB boundaries.
* Unit pin on the low-space warning branch (the whole point of the
  card is the ⚠ glyph firing below ``_LOW_FREE_PERCENT``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.disk import (
    _LOW_FREE_PERCENT,
    _DiskSnapshot,
    _fmt_bytes,
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
        make_message_update("/admin_disk", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_per_configured_dir(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_disk", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Disk usage" in text
    # database_dir is the primary disk concern — it has to render.
    assert "database_dir" in text
    # logs_dir is the secondary surface (volume may be separate).
    assert "logs_dir" in text
    # Either the no-warn footer (free space) or a low-space warning
    # body. We don't pin which because the CI/dev runner's free space
    # is environment-dependent — both branches are valid renders.
    assert ("All configured dirs have headroom" in text) or ("free" in text.lower())


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
            "/admin_disk",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_fmt_bytes_covers_unit_boundaries() -> None:
    """Boundary-cross between units is the most likely formatter
    bug. Pin each so a future refactor that swaps the loop can't
    silently mis-render."""
    assert _fmt_bytes(0) == "0 B"
    assert _fmt_bytes(512) == "512 B"
    assert _fmt_bytes(1024) == "1.00 KiB"
    assert _fmt_bytes(1024 * 1024) == "1.00 MiB"
    assert _fmt_bytes(1024 * 1024 * 1024) == "1.00 GiB"
    # Mid-range value renders with two-decimal precision.
    assert _fmt_bytes(int(1.5 * 1024 * 1024)) == "1.50 MiB"


def test_render_flags_low_space_engine_with_warning_footer() -> None:
    """The ⚠ glyph and the explanatory footer are the load-bearing
    surface — the card has to make a low-space state visible at a
    glance, otherwise the operator misses the precondition that
    makes a routine checkpoint dangerous."""
    snaps = [
        _DiskSnapshot(
            label="database_dir",
            path=Path("/var/lib/telegram-bot"),
            total=100 * 1024 * 1024,
            used=99 * 1024 * 1024,
            free=1 * 1024 * 1024,  # 1% free
        ),
    ]
    rendered = _render(snaps, missing=[])
    # Header gets the ⚠ glyph.
    assert "<b>database_dir</b> ⚠" in rendered
    # Footer flips to warning copy and mentions the WAL-checkpoint
    # remediation context — the load-bearing operator hint.
    assert (
        f"below {int(_LOW_FREE_PERCENT)}% free" in rendered
        or f"below {_LOW_FREE_PERCENT:.0f}% free" in rendered
    )
    assert "WAL checkpoint" in rendered
    # Healthy footer must NOT appear when any volume is low.
    assert "All configured dirs have headroom" not in rendered


def test_render_includes_missing_dirs_explicitly() -> None:
    """A path that doesn't exist on disk (fresh container, not-yet-
    mounted volume) should render as an explicit ``missing:`` line —
    NOT silently dropped. The operator's first question on an
    incident is "are all my dirs there?", and absence-evidence is
    the answer to that."""
    rendered = _render(snaps=[], missing=["logs_dir (/nope/logs)"])
    assert "missing: logs_dir (/nope/logs)" in rendered


@pytest.mark.asyncio
async def test_dedup_collapses_duplicate_resolved_paths(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """When ``message_stats_dir`` falls back to ``database_dir`` (the
    default), both candidate fields resolve to the same Path. The
    card must emit one row to avoid training the operator to
    skip-read duplicate-looking rows."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_disk", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # In the default config message_stats_dir falls back; only the
    # first label (database_dir) wins, so the symbolic name
    # ``message_stats_dir`` must NOT appear in the rendered card.
    # ``database_dir`` row carries the disk usage for that mount.
    assert "message_stats_dir" not in text
    assert "database_dir" in text
