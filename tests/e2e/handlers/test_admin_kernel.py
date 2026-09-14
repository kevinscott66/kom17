"""End-to-end ``/admin_kernel``.

Pins:

* Non-developer → silent drop.
* Card renders version + cmdline rows.
* ``mitigations=off`` token in cmdline → ⚠ concern row.
* ``nosmt`` token in cmdline → ⚠ concern row.
* Whole-token match: ``nosmtp_enabled=1`` does NOT trigger
  the ``nosmt`` concern (substring would be a false positive).
* Healthy cmdline (no concerning tokens) → bare. Cry-wolf
  prevention; mirrors flags / cpu / runtime / memory / rusage /
  tempdir.
* Long cmdline → truncated at ``_MAX_CMDLINE_LEN`` with marker.
* /proc unreadable (non-Linux host) → "unavailable" branch,
  no fake version/cmdline data.
* Capture is hermetic — exercised against tmp_path-backed
  files rather than the real /proc.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.kernel import (
    _MAX_CMDLINE_LEN,
    _capture,
    _cmdline_concerns,
    _KernelSnapshot,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(**overrides: Any) -> _KernelSnapshot:
    defaults: dict[str, Any] = {
        "available": True,
        "version": "Linux version 6.5.0-15-generic (build@host) (gcc 13)",
        "cmdline": "BOOT_IMAGE=/vmlinuz root=UUID=abc ro quiet splash",
    }
    defaults.update(overrides)
    return _KernelSnapshot(**defaults)


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
        make_message_update("/admin_kernel", user_id=42, chat_type="private"),
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
        make_message_update("/admin_kernel", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Kernel" in text
    # On a non-Linux host (CI on macOS / Windows) the card surfaces
    # the unavailable branch. Either branch is acceptable here; the
    # render-paths are exercised independently below.
    assert "version" in text or "unavailable" in text


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
            "/admin_kernel",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_mitigations_off_surfaces_warning() -> None:
    """``mitigations=off`` disables Spectre/Meltdown/L1TF — a
    throughput win on trusted-tenant hosts and a security disaster
    on multi-tenant. Either way the operator must see the marker."""
    rendered = _render(_snap(cmdline="BOOT_IMAGE=/vmlinuz mitigations=off quiet"))
    assert "mitigations=off" in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_nosmt_surfaces_warning() -> None:
    """``nosmt`` disables hyperthreading — may be a hardening
    decision or an accidental tuning leftover. The operator
    cross-checks /admin_cpu's affinity for the runtime cost."""
    rendered = _render(_snap(cmdline="BOOT_IMAGE=/vmlinuz nosmt ro"))
    assert "nosmt" in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_healthy_cmdline_no_warnings() -> None:
    """No concerning tokens → bare. Cry-wolf prevention."""
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


def test_render_unavailable_no_fake_data() -> None:
    """Non-Linux host where /proc is absent → "unavailable" branch.
    Card must not pretend it has version/cmdline data."""
    rendered = _render(_KernelSnapshot(available=False))
    assert "unavailable" in rendered
    assert _row_warn_count(rendered) == 0


def test_render_long_cmdline_truncated() -> None:
    """Hardened-kernel / large initramfs cmdlines can run to several
    kB. The render must clip past ``_MAX_CMDLINE_LEN`` with a
    visible marker rather than blow the 4096-char message limit."""
    long_cmdline = "BOOT_IMAGE=/vmlinuz " + ("x" * (_MAX_CMDLINE_LEN * 2))
    rendered = _render(_snap(cmdline=long_cmdline))
    assert "truncated" in rendered
    # The full input was 2× the cap — the rendered cmdline row must
    # not contain the entire string.
    assert long_cmdline not in rendered


def test_cmdline_concerns_whole_token_match() -> None:
    """Whole-token semantics: ``nosmtp_enabled=1`` is NOT a hit on
    ``nosmt``. Substring matching would be a noisy false positive
    on any future kernel param that happens to share a prefix."""
    assert _cmdline_concerns("BOOT_IMAGE=/vmlinuz nosmtp_enabled=1 ro") == []


def test_cmdline_concerns_multiple_tokens() -> None:
    """Both concerning tokens present → both surfaced. Order
    follows ``_CONCERNING_CMDLINE_TOKENS`` (not cmdline order)
    so the operator's eye lands on the same spot every time."""
    found = _cmdline_concerns("ro mitigations=off quiet nosmt splash")
    assert "mitigations=off" in found
    assert "nosmt" in found


def test_capture_reads_real_paths(tmp_path: Path) -> None:
    """End-to-end through ``_capture`` using tmp_path-backed
    version/cmdline files. Validates the file-read codepath
    without touching the real /proc."""
    version_path = tmp_path / "version"
    cmdline_path = tmp_path / "cmdline"
    version_path.write_text("Linux version 6.5.0 fake build\n")
    cmdline_path.write_text("BOOT_IMAGE=/vmlinuz mitigations=off\n")
    snap = _capture(version_path=version_path, cmdline_path=cmdline_path)
    assert snap.available is True
    assert "Linux version 6.5.0" in snap.version
    assert "mitigations=off" in snap.cmdline


def test_capture_missing_paths_unavailable(tmp_path: Path) -> None:
    """Either read failing → the whole snapshot is unavailable.
    A half-card (version known, cmdline missing) would be more
    misleading than a clean "non-Linux" surface."""
    snap = _capture(
        version_path=tmp_path / "nope_version",
        cmdline_path=tmp_path / "nope_cmdline",
    )
    assert snap.available is False
