"""End-to-end ``/admin_smaps``.

Pins:

* Non-developer → silent drop.
* Live card renders.
* /proc/self/smaps_rollup absent (non-Linux / pre-4.14) →
  informational, NO ⚠.
* Healthy snapshot, Swap=0 → zero ⚠.
* Huge absolute sizes (4 GiB Rss etc.) → still zero ⚠ — absolute
  values are NEVER ⚠'d (cry-wolf prevention).
* Swap ≥ 1 MiB → ⚠ on Swap row only.
* Swap below threshold → NO ⚠ (idle-page demotion noise).
* PSS/RSS ratio surfaced when both fields present; absent when
  either missing.
* ``_parse_smaps_rollup`` converts kB → bytes and handles header
  + unparseable values + future fields.
* ``_capture`` against fixture file — hermetic.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.smaps import (
    _SMAPS_FIELDS,
    _SWAP_WARN_BYTES,
    _capture,
    _parse_smaps_rollup,
    _pss_rss_ratio,
    _render,
    _SmapsSnapshot,
    _swap_concerning,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    status_present: bool = True,
    **fields: int | None,
) -> _SmapsSnapshot:
    base: dict[str, int | None] = dict.fromkeys(_SMAPS_FIELDS, 0)
    base.update(fields)
    return _SmapsSnapshot(fields=base, status_present=status_present)


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_smaps", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_smaps", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Proportional Set Size" in text


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
        make_message_update("/admin_smaps", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_non_linux_no_warn() -> None:
    """No rollup file → informational, zero ⚠. Same posture as
    every other Linux-only admin card."""
    rendered = _render(_snap(status_present=False))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "non-Linux host" in head


def test_healthy_zero_swap_no_warn() -> None:
    """Snapshot with Swap=0 → zero ⚠. The healthy default."""
    rendered = _render(_snap(Rss=100 * 1024 * 1024, Pss=80 * 1024 * 1024))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0


def test_huge_sizes_no_warn() -> None:
    """A 4 GiB bot is NOT a problem in itself. The cry-wolf
    prevention story this card hinges on: absolute sizes are
    never ⚠'d, only Swap is."""
    huge = 4 * 1024 * 1024 * 1024
    rendered = _render(_snap(Rss=huge, Pss=huge, Private_Dirty=huge))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0


def test_swap_above_threshold_warn() -> None:
    """Swap ≥ 1 MiB → ⚠ on the Swap row specifically."""
    rendered = _render(_snap(Rss=100 * 1024 * 1024, Swap=10 * 1024 * 1024))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 1
    for line in head.splitlines():
        if "⚠" in line:
            assert "Swap" in line


def test_swap_below_threshold_no_warn() -> None:
    """Tiny Swap is benign idle-page demotion noise. NO ⚠."""
    rendered = _render(_snap(Swap=_SWAP_WARN_BYTES - 1))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0


def test_pss_rss_ratio_surfaced() -> None:
    """PSS/RSS ratio shown when both fields present — it's the
    'honest cost' summary line that justifies the card."""
    rendered = _render(_snap(Rss=200 * 1024 * 1024, Pss=100 * 1024 * 1024))
    assert "PSS / RSS ratio" in rendered
    assert "0.50" in rendered


def test_pss_rss_ratio_absent_when_field_missing() -> None:
    """If either Pss or Rss is unreadable, ratio line is
    suppressed — we don't fake numbers."""
    snap = _SmapsSnapshot(
        fields={**dict.fromkeys(_SMAPS_FIELDS), "Rss": 100 * 1024 * 1024},
        status_present=True,
    )
    rendered = _render(snap)
    assert "PSS / RSS ratio" not in rendered


def test_parse_smaps_rollup_extracts_all_fields() -> None:
    """Pin the kB → bytes conversion + field extraction."""
    text = (
        "00400000-7fff00000000 ---p 00000000 00:00 0  [rollup]\n"
        "Rss:              204800 kB\n"
        "Pss:              102400 kB\n"
        "Pss_Anon:          51200 kB\n"
        "Pss_File:          51200 kB\n"
        "Pss_Shmem:             0 kB\n"
        "Shared_Clean:     102400 kB\n"
        "Shared_Dirty:          0 kB\n"
        "Private_Clean:         0 kB\n"
        "Private_Dirty:    102400 kB\n"
        "Referenced:       204800 kB\n"
        "Anonymous:        102400 kB\n"
        "Swap:                  0 kB\n"
        "SwapPss:               0 kB\n"
    )
    fields = _parse_smaps_rollup(text)
    # 204800 kB = 200 MiB = 209715200 bytes
    assert fields["Rss"] == 204800 * 1024
    assert fields["Pss"] == 102400 * 1024
    assert fields["Private_Dirty"] == 102400 * 1024
    assert fields["Swap"] == 0


def test_parse_smaps_rollup_skips_header() -> None:
    """The first line of smaps_rollup is the address range +
    [rollup] tag — no colon, must not crash parser."""
    text = "00400000-7fff00000000 ---p 00000000 00:00 0  [rollup]\nRss: 100 kB\n"
    fields = _parse_smaps_rollup(text)
    assert fields["Rss"] == 100 * 1024


def test_parse_smaps_rollup_unparseable_yields_none() -> None:
    """A non-integer kB value → None for that field. Future-
    kernel format change degrades rather than crashes."""
    text = "Rss: garbage kB\n"
    fields = _parse_smaps_rollup(text)
    assert fields["Rss"] is None


def test_parse_smaps_rollup_unknown_field_preserved() -> None:
    """Future-kernel addition is preserved in the dict so render
    can surface it under 'additional fields'."""
    text = "FutureField: 42 kB\n"
    fields = _parse_smaps_rollup(text)
    assert fields["FutureField"] == 42 * 1024


def test_swap_concerning_boundary() -> None:
    """Boundary pin: exactly 1 MiB → ⚠; one byte under → no ⚠."""
    assert _swap_concerning(_SWAP_WARN_BYTES)
    assert not _swap_concerning(_SWAP_WARN_BYTES - 1)
    assert not _swap_concerning(0)
    assert not _swap_concerning(None)


def test_pss_rss_ratio_handles_zero_rss() -> None:
    """Zero Rss → None ratio (not a div-by-zero crash). Should
    not happen on a real process but defends against fixture
    misconfig."""
    snap = _SmapsSnapshot(
        fields={**dict.fromkeys(_SMAPS_FIELDS), "Rss": 0, "Pss": 0},
        status_present=True,
    )
    assert _pss_rss_ratio(snap) is None


def test_capture_with_fixture(tmp_path: Path) -> None:
    """Real _capture against a tmp_path file — same parsing path,
    no /proc dependency on the test host."""
    rollup = tmp_path / "smaps_rollup"
    rollup.write_text(
        "00400000-7fff00000000 ---p 00000000 00:00 0  [rollup]\n"
        "Rss: 100 kB\n"
        "Pss: 50 kB\n"
        "Swap: 0 kB\n"
    )
    snap = _capture(smaps_path=rollup)
    assert snap.status_present
    assert snap.fields["Rss"] == 100 * 1024
    assert snap.fields["Pss"] == 50 * 1024


def test_capture_missing_file(tmp_path: Path) -> None:
    """Absent file → status_present=False, every canonical
    field None. The non-Linux / pre-4.14 signal."""
    snap = _capture(smaps_path=tmp_path / "does_not_exist")
    assert not snap.status_present
    for name in _SMAPS_FIELDS:
        assert snap.fields[name] is None


def test_render_surfaces_unknown_field() -> None:
    """A kernel-side addition must reach the rendered card under
    the 'additional fields' footer."""
    snap = _SmapsSnapshot(
        fields={**dict.fromkeys(_SMAPS_FIELDS, 0), "FutureField": 42 * 1024},
        status_present=True,
    )
    rendered = _render(snap)
    assert "FutureField" in rendered
    assert "additional fields" in rendered
