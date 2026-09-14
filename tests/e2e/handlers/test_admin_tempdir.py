"""End-to-end ``/admin_tempdir``.

Pins:

* Non-developer → silent drop.
* Card renders resolved path + writability + free-space + env vars.
* Not-writable → ⚠ + error-class hint.
* Free space below threshold → ⚠.
* Healthy state (writable, plenty of space) → bare. Cry-wolf
  prevention.
* Disk-usage failure → "unavailable" branch, no fake zeros.
* Env vars (TMPDIR/TEMP/TMP) shown explicitly as set-or-unset.
* Capture probes writability with a real syscall — exercised
  with a tmp_path so the test never touches /tmp.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.tempdir import (
    _FREE_BYTES_CONCERNING,
    _capture,
    _render,
    _TempdirSnapshot,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(**overrides: Any) -> _TempdirSnapshot:
    defaults: dict[str, Any] = {
        "resolved": Path("/tmp"),
        "env_vars": {"TMPDIR": None, "TEMP": None, "TMP": None},
        "writable": True,
        "writable_error": None,
        "free_bytes": 10 * 1024 * 1024 * 1024,  # 10 GiB
        "total_bytes": 50 * 1024 * 1024 * 1024,  # 50 GiB
        "usage_available": True,
    }
    defaults.update(overrides)
    return _TempdirSnapshot(**defaults)


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
        make_message_update("/admin_tempdir", user_id=42, chat_type="private"),
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
        make_message_update("/admin_tempdir", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "tempfile.gettempdir" in text
    assert "resolved" in text
    assert "writable" in text


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
            "/admin_tempdir",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_not_writable_surfaces_warning() -> None:
    """The AppArmor/SELinux/ReadOnlyPaths failure mode the module
    docstring documents: probe-write failed, error-class name
    surfaced as routing hint."""
    rendered = _render(_snap(writable=False, writable_error="PermissionError"))
    assert _row_warn_count(rendered) == 1
    assert "PermissionError" in rendered


def test_render_low_free_space_surfaces_warning() -> None:
    """Below the 100 MiB threshold the bot is one bad render away
    from ENOSPC. ⚠ on the row pairs with /admin_disk for the
    full headroom picture."""
    rendered = _render(_snap(free_bytes=_FREE_BYTES_CONCERNING - 1))
    assert _row_warn_count(rendered) == 1


def test_render_healthy_state_no_warnings() -> None:
    """Writable + plenty of free space + env vars unset → bare.
    Cry-wolf prevention; mirrors warnings_view / flags / locale /
    cpu / runtime / memory / rusage."""
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


def test_render_usage_unavailable_no_fake_zero() -> None:
    """FUSE / sandbox where shutil.disk_usage raises → "unavailable"
    branch. Card must NOT print "0 bytes free" which would lie
    and would trip the low-free ⚠ on absent data."""
    rendered = _render(_snap(usage_available=False, free_bytes=0))
    assert "unavailable" in rendered
    assert _row_warn_count(rendered) == 0


def test_render_env_vars_explicitly_listed() -> None:
    """TMPDIR / TEMP / TMP each rendered with "unset" hint when
    absent — the precedence is only visible if all three are
    shown. Hiding the unset ones would leave the operator unsure
    which one is winning."""
    rendered = _render(_snap(env_vars={"TMPDIR": "/var/tmp", "TEMP": None, "TMP": None}))
    assert "TMPDIR" in rendered
    assert "/var/tmp" in rendered
    assert "TEMP" in rendered
    assert "TMP" in rendered
    assert "unset" in rendered


def test_capture_writable_real_directory(tmp_path: Path) -> None:
    """Real probe against a real writable directory must succeed.
    Validates the writability probe end-to-end without touching
    /tmp — uses tmp_path so the test is hermetic."""
    snap = _capture(str(tmp_path))
    assert snap.writable is True
    assert snap.writable_error is None
    # The probe must NOT leave any files behind — the
    # NamedTemporaryFile context closes & deletes.
    leftovers = list(tmp_path.iterdir())
    assert leftovers == []


def test_capture_not_writable_real_failure(tmp_path: Path) -> None:
    """Pointing capture at a read-only directory must flip
    writable=False and surface the error class. We chmod the
    directory rather than mock the probe so the test exercises
    the real syscall path."""
    target = tmp_path / "ro"
    target.mkdir()
    # 0o500 = read+execute, no write — for the dir owner.
    os.chmod(target, 0o500)
    try:
        snap = _capture(str(target))
        assert snap.writable is False
        assert snap.writable_error is not None
    finally:
        # Restore so pytest can clean up tmp_path.
        os.chmod(target, 0o700)
