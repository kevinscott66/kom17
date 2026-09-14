"""End-to-end ``/admin_io``.

Pins:

* Non-developer → silent drop.
* Live card renders.
* /proc/self/io absent → informational, NO ⚠.
* Healthy I/O numbers → all fields shown, zero ⚠.
* Large but normal cumulative bytes → NO ⚠ (cumulative-since-start
  is not a problem; the cry-wolf prevention this card was built
  around).
* cancelled_write_bytes above 1 MiB → ⚠ on that row only.
* cancelled_write_bytes under threshold → NO ⚠ (benign rotation).
* ``_parse_io`` extracts all seven canonical fields.
* ``_parse_io`` bucketing: unknown future fields preserved + None
  on unparseable values (forward-compat).
* ``_capture`` against a fixture file — hermetic.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.io import (
    _CANCELLED_WARN_BYTES,
    _IO_FIELDS,
    _cancelled_concerning,
    _capture,
    _IoSnapshot,
    _parse_io,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    status_present: bool = True,
    **fields: int | None,
) -> _IoSnapshot:
    base: dict[str, int | None] = dict.fromkeys(_IO_FIELDS, 0)
    base.update(fields)
    return _IoSnapshot(fields=base, status_present=status_present)


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_io", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_io", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Disk I/O counters" in text


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
        make_message_update("/admin_io", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_non_linux_no_warn() -> None:
    """Absent /proc/self/io → informational, zero ⚠. Same posture
    as every other Linux-only admin card."""
    rendered = _render(_snap(status_present=False))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "non-Linux host" in head


def test_healthy_zero_no_warn() -> None:
    """All counters at zero (fresh process) → zero ⚠. Sanity."""
    rendered = _render(_snap())
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0


def test_large_cumulative_bytes_no_warn() -> None:
    """A 30-day-old bot will have terabytes of cumulative I/O —
    that's NOT a problem. The card's cry-wolf-prevention story
    hinges on this assertion. Zero ⚠ regardless of absolute
    byte values."""
    huge = 1024 * 1024 * 1024 * 1024  # 1 TiB
    rendered = _render(
        _snap(
            rchar=huge,
            wchar=huge,
            syscr=10_000_000,
            syscw=10_000_000,
            read_bytes=huge,
            write_bytes=huge,
        )
    )
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0


def test_cancelled_above_threshold_warn() -> None:
    """cancelled_write_bytes ≥ 1 MiB → ⚠ on that row only. The
    single field with a cry-wolf-safe absolute threshold."""
    rendered = _render(_snap(cancelled_write_bytes=2 * 1024 * 1024))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 1
    # The ⚠ must specifically be on the cancelled_write_bytes row.
    for line in head.splitlines():
        if "⚠" in line:
            assert "cancelled_write_bytes" in line


def test_cancelled_below_threshold_no_warn() -> None:
    """Small nonzero cancelled (typical log-rotation churn) →
    NO ⚠. Just under the 1 MiB floor is the boundary."""
    rendered = _render(_snap(cancelled_write_bytes=_CANCELLED_WARN_BYTES - 1))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0


def test_parse_io_extracts_all_fields() -> None:
    """Pin the field extraction. The kernel emits one
    ``name: value`` line per field; the parser must yield ints
    for all seven canonical fields."""
    text = (
        "rchar: 1024\n"
        "wchar: 2048\n"
        "syscr: 100\n"
        "syscw: 200\n"
        "read_bytes: 4096\n"
        "write_bytes: 8192\n"
        "cancelled_write_bytes: 0\n"
    )
    fields = _parse_io(text)
    assert fields["rchar"] == 1024
    assert fields["wchar"] == 2048
    assert fields["syscr"] == 100
    assert fields["syscw"] == 200
    assert fields["read_bytes"] == 4096
    assert fields["write_bytes"] == 8192
    assert fields["cancelled_write_bytes"] == 0


def test_parse_io_missing_field_is_none() -> None:
    """A field we expect that the kernel didn't emit → None.
    Forward-compat: a future kernel removing a field must not
    crash."""
    text = "rchar: 1024\n"
    fields = _parse_io(text)
    assert fields["rchar"] == 1024
    assert fields["wchar"] is None
    assert fields["cancelled_write_bytes"] is None


def test_parse_io_unknown_field_preserved() -> None:
    """A field the kernel added that we don't know about must be
    preserved in the dict so the render can surface it under
    'additional fields'."""
    text = "rchar: 1\nfuture_field_xyz: 999\n"
    fields = _parse_io(text)
    assert fields["future_field_xyz"] == 999


def test_parse_io_unparseable_yields_none() -> None:
    """Non-integer value → None for that field. Degrade rather
    than crash on a hypothetical kernel format change."""
    text = "rchar: notanumber\n"
    fields = _parse_io(text)
    assert fields["rchar"] is None


def test_cancelled_concerning_boundary() -> None:
    """Boundary pin. Exactly 1 MiB → ⚠. One byte under → no ⚠.
    None → never ⚠ (we don't claim 'bad' without data)."""
    assert _cancelled_concerning(_CANCELLED_WARN_BYTES)
    assert not _cancelled_concerning(_CANCELLED_WARN_BYTES - 1)
    assert not _cancelled_concerning(0)
    assert not _cancelled_concerning(None)


def test_capture_with_fixture(tmp_path: Path) -> None:
    """Real _capture against a tmp_path file — same parsing path,
    no /proc dependency on the test host."""
    io_file = tmp_path / "io"
    io_file.write_text(
        "rchar: 12345\n"
        "wchar: 67890\n"
        "syscr: 11\n"
        "syscw: 22\n"
        "read_bytes: 100\n"
        "write_bytes: 200\n"
        "cancelled_write_bytes: 0\n"
    )
    snap = _capture(io_path=io_file)
    assert snap.status_present
    assert snap.fields["rchar"] == 12345
    assert snap.fields["write_bytes"] == 200


def test_capture_missing_file(tmp_path: Path) -> None:
    """Absent /proc/self/io → status_present=False, all canonical
    fields None. The non-Linux signal."""
    snap = _capture(io_path=tmp_path / "does_not_exist")
    assert not snap.status_present
    for field in _IO_FIELDS:
        assert snap.fields[field] is None


def test_render_surfaces_unknown_field(tmp_path: Path) -> None:
    """A kernel-side addition must reach the rendered card under
    the 'additional fields' footer."""
    snap = _IoSnapshot(
        fields={**dict.fromkeys(_IO_FIELDS, 0), "future_field_xyz": 42},
        status_present=True,
    )
    rendered = _render(snap)
    assert "future_field_xyz" in rendered
    assert "additional fields" in rendered
