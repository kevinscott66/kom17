"""End-to-end ``/admin_random``.

Pins:

* Non-developer → silent drop.
* Live card renders with all three sections (source probes,
  /dev/urandom, kernel entropy).
* Healthy snapshot → zero ⚠ on data rows (cry-wolf pin).
* os.urandom probe failure → ⚠ on that row + error class surfaced.
* /dev/urandom missing → ⚠ on the device row.
* Low entropy reading → ⚠ on the entropy row.
* Non-Linux host (no entropy file) → bare informational row, NO ⚠
  — missing-because-impossible is not concerning.
* ``_looks_like_csprng_output`` rejects empty / wrong-length /
  all-zero; accepts genuine random bytes.
* ``_read_entropy_avail`` parses an integer file; returns
  (True, None) on a present-but-unparseable file; (False, None)
  on a missing file (the non-Linux signal).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.random_info import (
    _ENTROPY_CONCERNING_FLOOR,
    _capture,
    _entropy_concerning,
    _looks_like_csprng_output,
    _RandomSnapshot,
    _read_entropy_avail,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    os_urandom_ok: bool = True,
    os_urandom_error: str | None = None,
    secrets_ok: bool = True,
    secrets_error: str | None = None,
    device_present: bool = True,
    device_readable: bool = True,
    entropy_file_present: bool = True,
    entropy_avail: int | None = 256,
) -> _RandomSnapshot:
    return _RandomSnapshot(
        os_urandom_ok=os_urandom_ok,
        os_urandom_error=os_urandom_error,
        secrets_ok=secrets_ok,
        secrets_error=secrets_error,
        device_present=device_present,
        device_readable=device_readable,
        entropy_file_present=entropy_file_present,
        entropy_avail=entropy_avail,
    )


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_random", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_random", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "CSPRNG posture" in text
    assert "source probes" in text
    assert "/dev/urandom" in text
    assert "kernel entropy" in text


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
        make_message_update("/admin_random", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_healthy_snapshot_no_warn() -> None:
    rendered = _render(_snap())
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0


def test_os_urandom_failure_marks_row() -> None:
    """OSError from os.urandom is the chroot-without-/dev/urandom
    signature; the error class must be surfaced so the operator
    knows it's not a logic bug."""
    rendered = _render(_snap(os_urandom_ok=False, os_urandom_error="OSError"))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    assert "OSError" in head
    urandom_line = next(line for line in head.splitlines() if "os.urandom" in line)
    assert "⚠" in urandom_line


def test_missing_device_marks_row() -> None:
    rendered = _render(_snap(device_present=False, device_readable=False))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    assert "not present" in head


def test_unreadable_device_marks_row() -> None:
    """Present-but-unreadable is a distinct signal from missing —
    points at sandbox / namespace mounting rather than chroot."""
    rendered = _render(_snap(device_present=True, device_readable=False))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    assert "not readable" in head


def test_low_entropy_marks_row() -> None:
    rendered = _render(_snap(entropy_avail=10))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    entropy_line = next(line for line in head.splitlines() if "entropy_avail" in line)
    assert "⚠" in entropy_line


def test_non_linux_no_warn() -> None:
    """Missing entropy file on a non-Linux host is informational —
    we can't measure what doesn't exist. Cry-wolf prevention pin."""
    rendered = _render(_snap(entropy_file_present=False, entropy_avail=None))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0
    assert "non-Linux host" in head


def test_looks_like_csprng_output() -> None:
    """The shape check. Wrong length / all-zero / empty rejected.
    Genuine CSPRNG output (sampled from os.urandom) accepted."""
    import os

    assert _looks_like_csprng_output(os.urandom(32))
    assert not _looks_like_csprng_output(b"")
    assert not _looks_like_csprng_output(b"\x00" * 32)
    assert not _looks_like_csprng_output(b"abc")  # wrong length


def test_read_entropy_avail_present_and_missing(tmp_path: Path) -> None:
    """Three states pinned: parseable int, present-but-unparseable
    (returns ``(True, None)`` so the render distinguishes from
    missing), and absent (``(False, None)`` — non-Linux signal)."""
    good = tmp_path / "entropy_good"
    good.write_text("256\n")
    assert _read_entropy_avail(good) == (True, 256)

    junk = tmp_path / "entropy_junk"
    junk.write_text("not-a-number")
    assert _read_entropy_avail(junk) == (True, None)

    missing = tmp_path / "does_not_exist"
    assert _read_entropy_avail(missing) == (False, None)


def test_entropy_concerning_threshold() -> None:
    """Boundary pin around ``_ENTROPY_CONCERNING_FLOOR``. Missing
    file / unreadable value never concerning."""
    floor = _ENTROPY_CONCERNING_FLOOR
    assert _entropy_concerning(_snap(entropy_avail=floor - 1))
    assert not _entropy_concerning(_snap(entropy_avail=floor + 1))
    # Missing/unreadable path: never ⚠ — we don't claim "low" without data.
    assert not _entropy_concerning(_snap(entropy_file_present=False, entropy_avail=None))
    assert not _entropy_concerning(_snap(entropy_file_present=True, entropy_avail=None))


def test_capture_live(tmp_path: Path) -> None:
    """Real ``_capture`` against the running host's CSPRNGs.
    os.urandom + secrets must succeed on any normal Python; if
    they don't the bot can't ship anyway, so failing here is
    fail-fast rather than skip-the-test."""
    snap = _capture(
        urandom_device=tmp_path / "no_device",
        entropy_file=tmp_path / "no_entropy",
    )
    assert snap.os_urandom_ok
    assert snap.secrets_ok
    # We forced both files to be absent so the device + entropy
    # branches go through the missing-path code.
    assert not snap.device_present
    assert not snap.entropy_file_present
