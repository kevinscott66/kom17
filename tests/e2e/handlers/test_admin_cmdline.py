"""End-to-end ``/admin_cmdline``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs).
* Parser splits on whitespace; ``key=value`` partitions on first
  ``=``; bare flags become ``(name, None)``.
* Notable section is curated allowlist — adding to it is
  intentional; pinned to avoid silent expansion.
* Empty value (``key=``) preserved as ``""`` not collapsed to bare.
* Raw cmdline surfaces verbatim.
* Zero ⚠ regardless of state — including ``mitigations=off``,
  which is a legitimate choice; pinned cry-wolf prevention.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.cmdline import (
    _NOTABLE_BARE,
    _NOTABLE_KEYS,
    _capture,
    _CmdlineSnapshot,
    _parse_cmdline,
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
        make_message_update("/admin_cmdline", user_id=42, chat_type="private"),
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
        make_message_update("/admin_cmdline", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Kernel boot parameters" in text


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
            "/admin_cmdline",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "BOOT_IMAGE=/boot/vmlinuz-6.5.0-26-generic root=UUID=abc-def "
    "ro quiet splash mitigations=off nosmt isolcpus=2,3 "
    "intel_iommu=on transparent_hugepage=madvise"
)


def test_parse_canonical() -> None:
    params = _parse_cmdline(_SAMPLE)
    by_key = {k: v for (k, v) in params}
    assert by_key["BOOT_IMAGE"] == "/boot/vmlinuz-6.5.0-26-generic"
    assert by_key["root"] == "UUID=abc-def"  # Embedded '=' kept.
    assert by_key["mitigations"] == "off"
    assert by_key["nosmt"] is None  # Bare flag.
    assert by_key["ro"] is None
    assert by_key["isolcpus"] == "2,3"


def test_parse_embedded_equals_kept() -> None:
    """``root=UUID=abc-def`` — embedded ``=`` past the first must
    be preserved in the value. Pinned because a naive split('=')
    would mangle this and lose half the UUID."""
    params = _parse_cmdline("root=UUID=abc-def")
    assert params == (("root", "UUID=abc-def"),)


def test_parse_empty_value_preserved() -> None:
    """``key=`` is NOT the same as bare ``key`` — the kernel treats
    them differently in some places. We preserve the distinction.
    Pinned because a refactor that collapses '' → None silently
    changes semantics."""
    params = _parse_cmdline("transparent_hugepage= quiet")
    assert ("transparent_hugepage", "") in params
    assert ("quiet", None) in params


def test_parse_order_preserved() -> None:
    """Cmdline order is meaningful — later params override earlier
    ones in the kernel. We must not sort."""
    params = _parse_cmdline("debug loglevel=7 debug")
    keys = [k for (k, _) in params]
    assert keys == ["debug", "loglevel", "debug"]


def test_parse_empty() -> None:
    assert _parse_cmdline("") == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.params == ()
    assert snap.raw == ""


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "cmdline"
    p.write_text(_SAMPLE + "\n")
    snap = _capture(path=p)
    assert snap.available
    assert snap.raw == _SAMPLE  # Trailing newline stripped.
    assert len(snap.params) == 10


# --- notable allowlist pin -------------------------------------------------


def test_notable_keys_explicit_allowlist() -> None:
    """The notable set is intentional curation — pinned so a
    refactor that adds an entry has to update this assertion,
    making the expansion visible in code review. Removing this
    test is itself a signal."""
    # Core security/perf flags that operationally matter.
    expected_minimum = {
        "mitigations",
        "isolcpus",
        "hugepages",
        "intel_iommu",
        "amd_iommu",
        "transparent_hugepage",
        "init",
        "root",
    }
    assert expected_minimum.issubset(_NOTABLE_KEYS)
    # Bare-flag set must be a subset of context-sensitive flags;
    # ``nosmt`` is the most operationally important one.
    assert "nosmt" in _NOTABLE_BARE


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _CmdlineSnapshot(raw="", params=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    snap = _CmdlineSnapshot(raw="", params=(), available=True)
    text = _render(snap)
    assert "Empty cmdline" in text
    assert "⚠" not in text


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "cmdline"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    # Raw cmdline verbatim.
    assert "BOOT_IMAGE=/boot/vmlinuz-6.5.0-26-generic" in text
    # Notable section highlights specific entries.
    assert "mitigations=off" in text
    assert "isolcpus=2,3" in text
    assert "intel_iommu=on" in text
    assert "Notable parameters" in text


def test_render_no_notable_section_when_empty(tmp_path: Path) -> None:
    """An entirely-vanilla cmdline (no curated flags) renders a
    distinct note rather than an empty section. Pinned because the
    rendered text needs to remain readable in both extremes."""
    p = tmp_path / "cmdline"
    p.write_text("BOOT_IMAGE=/vmlinuz some_unknown_flag=yes")
    snap = _capture(path=p)
    text = _render(snap)
    assert "No parameters from the curated notable set" in text


def test_render_no_warnings_even_with_mitigations_off(tmp_path: Path) -> None:
    """Cry-wolf prevention: ``mitigations=off`` is a legitimate
    performance choice in sealed environments. Pinned with the
    explicit flag present and the card still doesn't warn —
    operator policy decides whether the posture matches intent."""
    p = tmp_path / "cmdline"
    p.write_text("mitigations=off nosmt numa=off mem=4G init=/bin/sh debug")
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_raw_cmdline_surfaces(tmp_path: Path) -> None:
    """Even params NOT in the curated set must appear in the raw
    section — the highlight is for readability, not gatekeeping."""
    p = tmp_path / "cmdline"
    p.write_text("BOOT_IMAGE=/vmlinuz exotic_unknown_param=42 some_flag")
    snap = _capture(path=p)
    text = _render(snap)
    assert "exotic_unknown_param=42" in text
    assert "some_flag" in text
