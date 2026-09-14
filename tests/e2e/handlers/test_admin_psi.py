"""End-to-end ``/admin_psi``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux 4.20+ w/ CONFIG_PSI) OR explicit unavailable
  note (macOS dev, pre-4.20 kernels, CONFIG_PSI=n).
* PSI line parser tolerates kernel field-reordering (key=value).
* cpu has only ``some`` by kernel design — ``full`` is intentionally
  not rendered for cpu rather than emitting a misleading "?".
* Single ⚠ fires only on memory.full.avg60 or io.full.avg60 > 10%.
* Cry-wolf prevention: cpu.some at 99% must NOT ⚠.
* Per-resource availability: one missing file doesn't sink the card.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.psi import (
    _capture,
    _high_pressure,
    _parse_psi_line,
    _PsiLine,
    _PsiResource,
    _PsiSnapshot,
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
        bot, make_message_update("/admin_psi", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_psi", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Pressure Stall Information" in text


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
        make_message_update("/admin_psi", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


def test_parse_some_line() -> None:
    line = _parse_psi_line("some avg10=1.25 avg60=2.50 avg300=3.75 total=12345")
    assert line is not None
    assert line.kind == "some"
    assert line.avg10 == 1.25
    assert line.avg60 == 2.50
    assert line.avg300 == 3.75
    assert line.total_us == 12345


def test_parse_full_line() -> None:
    line = _parse_psi_line("full avg10=0.00 avg60=0.00 avg300=0.00 total=0")
    assert line is not None
    assert line.kind == "full"


def test_parse_unknown_kind_returns_none() -> None:
    """A line starting with something other than some/full → None.

    Forward-compat: if a future kernel adds an ``aggregate`` line,
    it's silently dropped rather than mis-bucketed into some/full."""
    assert _parse_psi_line("aggregate avg10=0.00") is None


def test_parse_reordered_fields() -> None:
    """key=value parsing means kernel reordering doesn't shift indices.

    A positional parser would silently mis-attribute fields here; the
    key=value parser pins this regression."""
    line = _parse_psi_line("some total=99 avg300=3.0 avg10=1.0 avg60=2.0")
    assert line is not None
    assert line.avg10 == 1.0
    assert line.avg60 == 2.0
    assert line.avg300 == 3.0
    assert line.total_us == 99


def test_parse_missing_fields_yields_none() -> None:
    """A truncated line gets None for missing averages — None vs 0.0
    distinguishes 'kernel didn't expose' from 'kernel exposed zero'."""
    line = _parse_psi_line("some avg10=1.0")
    assert line is not None
    assert line.avg10 == 1.0
    assert line.avg60 is None
    assert line.avg300 is None
    assert line.total_us is None


def test_parse_bad_number_keeps_other_fields() -> None:
    line = _parse_psi_line("some avg10=garbage avg60=2.0 total=10")
    assert line is not None
    assert line.avg10 is None
    assert line.avg60 == 2.0
    assert line.total_us == 10


# --- capture ---------------------------------------------------------------


def _write_psi(base: Path, cpu: str | None, memory: str | None, io: str | None) -> None:
    base.mkdir(parents=True, exist_ok=True)
    if cpu is not None:
        (base / "cpu").write_text(cpu)
    if memory is not None:
        (base / "memory").write_text(memory)
    if io is not None:
        (base / "io").write_text(io)


def test_capture_all_missing(tmp_path: Path) -> None:
    snap = _capture(base=tmp_path / "pressure")
    assert not snap.available
    assert not snap.cpu.available


def test_capture_all_three(tmp_path: Path) -> None:
    base = tmp_path / "pressure"
    _write_psi(
        base,
        cpu="some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n",
        memory=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
        io=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
    )
    snap = _capture(base=base)
    assert snap.available
    assert snap.cpu.some is not None
    assert snap.memory.full is not None
    assert snap.io.full is not None
    # cpu deliberately has no full line in the kernel-emitted file.
    assert snap.cpu.full is None


def test_capture_partial_one_missing(tmp_path: Path) -> None:
    """If only /proc/pressure/cpu exists, the card still renders cpu
    and explicitly marks memory + io unavailable."""
    base = tmp_path / "pressure"
    _write_psi(
        base,
        cpu="some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n",
        memory=None,
        io=None,
    )
    snap = _capture(base=base)
    assert snap.available
    assert snap.cpu.available
    assert not snap.memory.available
    assert not snap.io.available


# --- ⚠ predicate ---------------------------------------------------------


def _mk_snap(
    *, mem_full_avg60: float | None = 0.0, io_full_avg60: float | None = 0.0
) -> _PsiSnapshot:
    cpu = _PsiResource(
        name="cpu",
        some=_PsiLine(kind="some", avg10=0.0, avg60=0.0, avg300=0.0, total_us=0),
        full=None,
        available=True,
    )
    memory = _PsiResource(
        name="memory",
        some=_PsiLine(kind="some", avg10=0.0, avg60=0.0, avg300=0.0, total_us=0),
        full=_PsiLine(kind="full", avg10=0.0, avg60=mem_full_avg60, avg300=0.0, total_us=0),
        available=True,
    )
    io = _PsiResource(
        name="io",
        some=_PsiLine(kind="some", avg10=0.0, avg60=0.0, avg300=0.0, total_us=0),
        full=_PsiLine(kind="full", avg10=0.0, avg60=io_full_avg60, avg300=0.0, total_us=0),
        available=True,
    )
    return _PsiSnapshot(cpu=cpu, memory=memory, io=io, available=True)


def test_warn_clean() -> None:
    assert _high_pressure(_mk_snap()) == ()


def test_warn_memory_above_threshold() -> None:
    assert _high_pressure(_mk_snap(mem_full_avg60=25.0)) == ("memory",)


def test_warn_io_above_threshold() -> None:
    assert _high_pressure(_mk_snap(io_full_avg60=99.0)) == ("io",)


def test_warn_both() -> None:
    triggered = _high_pressure(_mk_snap(mem_full_avg60=15.0, io_full_avg60=20.0))
    assert set(triggered) == {"memory", "io"}


def test_warn_boundary_strict_gt() -> None:
    """Threshold is strict > 10%, not ≥ — sitting exactly at 10% is
    the edge of normal, not a real pressure event."""
    assert _high_pressure(_mk_snap(mem_full_avg60=10.0)) == ()


def test_warn_cpu_some_at_99_does_not_fire(tmp_path: Path) -> None:
    """Cry-wolf pin: cpu.some=99% is normal for any busy host. The
    card must NOT ⚠ on it — only memory/io full pressure triggers."""
    base = tmp_path / "pressure"
    _write_psi(
        base,
        cpu="some avg10=99.0 avg60=99.0 avg300=99.0 total=999999999\n",
        memory=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
        io=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
    )
    snap = _capture(base=base)
    assert _high_pressure(snap) == ()


def test_warn_false_when_unavailable() -> None:
    """Absence of data is NOT a warning — macOS dev must not ⚠."""
    cpu = _PsiResource(name="cpu", some=None, full=None, available=False)
    memory = _PsiResource(name="memory", some=None, full=None, available=False)
    io = _PsiResource(name="io", some=None, full=None, available=False)
    snap = _PsiSnapshot(cpu=cpu, memory=memory, io=io, available=False)
    assert _high_pressure(snap) == ()


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    cpu = _PsiResource(name="cpu", some=None, full=None, available=False)
    memory = _PsiResource(name="memory", some=None, full=None, available=False)
    io = _PsiResource(name="io", some=None, full=None, available=False)
    snap = _PsiSnapshot(cpu=cpu, memory=memory, io=io, available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_clean_no_warning(tmp_path: Path) -> None:
    base = tmp_path / "pressure"
    _write_psi(
        base,
        cpu="some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n",
        memory=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
        io=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
    )
    snap = _capture(base=base)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" not in head


def test_render_warns_on_memory(tmp_path: Path) -> None:
    base = tmp_path / "pressure"
    _write_psi(
        base,
        cpu="some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n",
        memory=(
            "some avg10=50.0 avg60=50.0 avg300=50.0 total=999999\n"
            "full avg10=25.0 avg60=25.0 avg300=25.0 total=999999\n"
        ),
        io=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
    )
    snap = _capture(base=base)
    rendered = _render(snap)
    head = rendered.partition("<i>⚠ markers")[0]
    assert "⚠" in head
    assert "memory" in head
    assert "sustained full pressure" in head


def test_render_cpu_has_no_full_line(tmp_path: Path) -> None:
    """cpu's PSI file has no ``full`` line by kernel design. Render
    must NOT emit a "full: ?" line for cpu — that would be misleading."""
    base = tmp_path / "pressure"
    _write_psi(
        base,
        cpu="some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n",
        memory=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
        io=(
            "some avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
            "full avg10=0.0 avg60=0.0 avg300=0.0 total=0\n"
        ),
    )
    snap = _capture(base=base)
    rendered = _render(snap)
    # Pin: between the "cpu:" header and the next resource header,
    # there must be no "full" label.
    cpu_block, _, rest = rendered.partition("<b>memory</b>")
    cpu_block = cpu_block.split("<b>cpu</b>", 1)[-1]
    assert "full" not in cpu_block
