"""End-to-end ``/admin_capabilities``.

Pins:

* Non-developer → silent drop.
* Live card renders (the running process has SOME capability
  posture, even on macOS where the file is absent).
* CapEff=0 → "no effective capabilities", zero ⚠ (healthy bot).
* CapEff with one narrow cap → bare row, NO ⚠ (operator intent).
* CapEff with CAP_SYS_ADMIN only → ⚠ on the catch-all (the
  drift-toward-easiest-fix pattern the card is built to catch).
* CapEff with full bitmap → root-equivalent ⚠.
* root-equivalent supersedes CAP_SYS_ADMIN ⚠ — single ⚠ per
  posture row, no double-marking.
* Missing /proc/self/status (non-Linux) → informational, NO ⚠.
* ``_parse_status`` extracts all five Cap* fields; unparseable
  hex yields None for that field (not a crash).
* ``_decode_bitmap`` decodes well-known caps + buckets out-of-
  table bits under UNKNOWN_<n>.
* ``_is_root_equivalent`` matches the full-cap bitmap.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.capabilities import (
    _CAP_SYS_ADMIN_BIT,
    _CAPABILITY_NAMES,
    _FULL_CAPABILITY_BITMAP,
    _CapsSnapshot,
    _capture,
    _decode_bitmap,
    _has_sys_admin,
    _is_root_equivalent,
    _parse_status,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    cap_inh: int | None = 0,
    cap_prm: int | None = 0,
    cap_eff: int | None = 0,
    cap_bnd: int | None = 0,
    cap_amb: int | None = 0,
    status_present: bool = True,
) -> _CapsSnapshot:
    return _CapsSnapshot(
        sets={
            "CapInh": cap_inh,
            "CapPrm": cap_prm,
            "CapEff": cap_eff,
            "CapBnd": cap_bnd,
            "CapAmb": cap_amb,
        },
        status_present=status_present,
    )


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_capabilities", user_id=42, chat_type="private"),
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
        make_message_update("/admin_capabilities", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Linux capabilities" in text


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
            "/admin_capabilities",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_healthy_cap_eff_zero_no_warn() -> None:
    """CapEff=0 is the healthy posture for this bot. Zero ⚠."""
    rendered = _render(_snap())
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0
    assert "no effective capabilities" in head


def test_narrow_single_cap_no_warn() -> None:
    """A single narrow cap (CAP_NET_BIND_SERVICE for a low-port
    bind, say) is operator intent — bare row, no cry-wolf."""
    bind_bit = _CAPABILITY_NAMES.index("CAP_NET_BIND_SERVICE")
    rendered = _render(_snap(cap_eff=1 << bind_bit))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0
    assert "CAP_NET_BIND_SERVICE" in head


def test_sys_admin_alone_marks_posture() -> None:
    """CAP_SYS_ADMIN in isolation is the catch-all bypass — ⚠ on
    the posture row, name surfaced in the CapEff decode."""
    rendered = _render(_snap(cap_eff=1 << _CAP_SYS_ADMIN_BIT))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    assert "CAP_SYS_ADMIN" in head


def test_root_equivalent_marks_posture() -> None:
    """Full capability bitmap → root-equivalent ⚠. The fix path
    (User=root in systemd unit) is in the footer."""
    rendered = _render(_snap(cap_eff=_FULL_CAPABILITY_BITMAP))
    head, _, _ = rendered.partition("<i>⚠")
    # Single posture-row ⚠ — must NOT also mark CAP_SYS_ADMIN
    # separately (root-equivalent supersedes; we'd otherwise
    # double-count).
    assert head.count("⚠") == 1
    assert "root-equivalent" in head


def test_non_linux_no_warn() -> None:
    """Missing /proc/self/status → bare informational note, zero
    ⚠. The card explicitly states it has no actionable signal."""
    rendered = _render(_snap(status_present=False))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0
    assert "non-Linux host" in head


def test_parse_status_extracts_all_fields() -> None:
    """Pin the field extraction. The kernel emits each Cap* on
    its own tab-separated line; our parser must handle all five."""
    text = (
        "Name:\ttest\n"
        "Uid:\t1000\t1000\t1000\t1000\n"
        "CapInh:\t0000000000000000\n"
        "CapPrm:\t000000ffffffffff\n"
        "CapEff:\t000000ffffffffff\n"
        "CapBnd:\t000000ffffffffff\n"
        "CapAmb:\t0000000000000000\n"
    )
    fields = _parse_status(text)
    assert fields["CapInh"] == 0
    assert fields["CapPrm"] == 0x000000FFFFFFFFFF
    assert fields["CapEff"] == 0x000000FFFFFFFFFF
    assert fields["CapBnd"] == 0x000000FFFFFFFFFF
    assert fields["CapAmb"] == 0


def test_parse_status_missing_field_yields_none() -> None:
    """Pre-CAP_AMB kernels don't have CapAmb. The parser must not
    crash — that field is reported None, others extract fine."""
    text = "CapEff:\t0000000000000400\n"
    fields = _parse_status(text)
    assert fields["CapEff"] == 0x400
    assert fields["CapAmb"] is None


def test_parse_status_unparseable_hex_yields_none() -> None:
    """A future kernel format change must degrade gracefully —
    unparseable hex on a field yields None, not an exception."""
    text = "CapEff:\tnotahexstring\n"
    fields = _parse_status(text)
    assert fields["CapEff"] is None


def test_decode_bitmap_known_caps() -> None:
    """Two specific caps set → both named in the output, in
    kernel-canonical bit order (not alphabetical)."""
    chown_bit = _CAPABILITY_NAMES.index("CAP_CHOWN")
    sys_admin_bit = _CAPABILITY_NAMES.index("CAP_SYS_ADMIN")
    bitmap = (1 << chown_bit) | (1 << sys_admin_bit)
    decoded = _decode_bitmap(bitmap)
    assert decoded == ["CAP_CHOWN", "CAP_SYS_ADMIN"]


def test_decode_bitmap_unknown_bit_bucketed() -> None:
    """A bit beyond the name table → UNKNOWN_<n>. Forward-compat
    guardrail so a kernel addition surfaces visibly."""
    out_of_table_bit = len(_CAPABILITY_NAMES) + 5
    decoded = _decode_bitmap(1 << out_of_table_bit)
    assert decoded == [f"UNKNOWN_{out_of_table_bit}"]


def test_is_root_equivalent_boundary() -> None:
    """Boundary pin. Exactly the full bitmap → root-equivalent.
    One bit short → not. Zero → not."""
    assert _is_root_equivalent(_FULL_CAPABILITY_BITMAP)
    assert not _is_root_equivalent(_FULL_CAPABILITY_BITMAP - 1)
    assert not _is_root_equivalent(0)


def test_has_sys_admin() -> None:
    assert _has_sys_admin(1 << _CAP_SYS_ADMIN_BIT)
    assert _has_sys_admin(_FULL_CAPABILITY_BITMAP)
    assert not _has_sys_admin(0)
    # CAP_CHOWN alone should not trigger
    chown_only = 1 << _CAPABILITY_NAMES.index("CAP_CHOWN")
    assert not _has_sys_admin(chown_only)


def test_capture_with_fixture(tmp_path: Path) -> None:
    """Real _capture against a fixture file mimicking
    /proc/self/status — same parsing path, no /proc dependency."""
    status = tmp_path / "status"
    status.write_text(
        "Name:\ttest\n"
        "CapInh:\t0000000000000000\n"
        "CapPrm:\t0000000000000400\n"
        "CapEff:\t0000000000000400\n"
        "CapBnd:\t000000ffffffffff\n"
        "CapAmb:\t0000000000000000\n"
    )
    snap = _capture(status_path=status)
    assert snap.status_present
    assert snap.sets["CapEff"] == 0x400


def test_capture_missing_file(tmp_path: Path) -> None:
    """Absent /proc/self/status → status_present=False, all fields
    None. The non-Linux signal."""
    snap = _capture(status_path=tmp_path / "does_not_exist")
    assert not snap.status_present
    for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert snap.sets[field] is None
