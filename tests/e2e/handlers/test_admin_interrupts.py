"""End-to-end ``/admin_interrupts``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR explicit unavailable note (macOS dev).
* Parser detects CPU count from the header row.
* Rows whose post-colon int prefix is shorter than cpu_count are
  dropped (mid-update read protection).
* Device label after the count columns is preserved verbatim — that's
  what tells the operator "which queue / which controller".
* Zero-total rows → max_share=0.0, not ZeroDivisionError.
* Pseudo-IRQs (NMI, LOC, RES, CAL, TLB) routed into their own block.
* Hardware IRQs below the floor are dropped from display but kept on
  the snapshot — full counts remain accessible to drill-down callers.
* ZERO ⚠ markers anywhere in the rendered card body regardless of
  IRQ skew. Skew is workload- and topology-dependent (pinned
  single-IRQ NIC is configured skew, not problem skew) — operator
  policy, not card policy. Pinned so a future refactor doesn't
  accidentally add a marker.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.interrupts import (
    _DISPLAY_TOTAL_FLOOR,
    _capture,
    _InterruptsSnapshot,
    _is_pseudo,
    _parse_interrupts,
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
        bot, make_message_update("/admin_interrupts", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_interrupts", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Hardware IRQ distribution" in text


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
            "/admin_interrupts", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "           CPU0       CPU1       CPU2       CPU3\n"
    "  0:        42          0          0          0   IO-APIC   2-edge      timer\n"
    "  1:       456        123         98         87   IO-APIC   1-edge      i8042\n"
    " 24:   9500000     200000     150000     150000   PCI-MSI 524288-edge   nvme0q1\n"
    " 25:        50         60         70         80   PCI-MSI 524289-edge   nvme0q2\n"
    "NMI:         0          1          0          0   Non-maskable interrupts\n"
    "LOC:    100000     100001     100002     100003   Local timer interrupts\n"
    "RES:      5000       5100       5200       5300   Rescheduling interrupts\n"
    "CAL:       100        110        120        130   Function call interrupts\n"
    "TLB:       200        210        220        230   TLB shootdowns\n"
    "MCE:         0          0          0          0   Machine check exceptions\n"
)


def test_parse_canonical() -> None:
    rows, cpu_count = _parse_interrupts(_SAMPLE)
    assert cpu_count == 4
    # Every row parsed.
    assert len(rows) == 10
    nvme = next(r for r in rows if r.irq_id == "24")
    assert nvme.per_cpu == (9_500_000, 200_000, 150_000, 150_000)
    assert nvme.total == 10_000_000
    assert "nvme0q1" in nvme.device
    assert "PCI-MSI" in nvme.device
    assert abs(nvme.max_share - 0.95) < 1e-9


def test_parse_pseudo_id_kept() -> None:
    rows, _ = _parse_interrupts(_SAMPLE)
    nmi = next(r for r in rows if r.irq_id == "NMI")
    assert nmi.device == "Non-maskable interrupts"


def test_is_pseudo() -> None:
    assert _is_pseudo("NMI")
    assert _is_pseudo("LOC")
    assert not _is_pseudo("0")
    assert not _is_pseudo("24")


def test_parse_short_row_dropped() -> None:
    """Row with fewer count columns than cpu_count → dropped (file
    mid-update or kernel oddity). Prevents per_cpu tuple-length
    mismatch downstream."""
    text = "CPU0 CPU1 CPU2\nA: 1 2 3 dev\nB: 1 2 dev\n"
    rows, _ = _parse_interrupts(text)
    assert [r.irq_id for r in rows] == ["A"]


def test_parse_zero_total_no_zero_division() -> None:
    """Uncalled IRQ line (all zeros) → max_share=0.0, not a crash."""
    text = "CPU0 CPU1\nX: 0 0 unused\n"
    rows, _ = _parse_interrupts(text)
    assert rows[0].total == 0
    assert rows[0].max_share == 0.0


def test_parse_empty_text() -> None:
    rows, cpu_count = _parse_interrupts("")
    assert rows == ()
    assert cpu_count == 0


def test_parse_header_without_cpu_tokens() -> None:
    rows, cpu_count = _parse_interrupts("junk junk\nA: 1 2 dev\n")
    assert cpu_count == 0
    assert rows == ()


def test_parse_device_label_preserved_with_spaces() -> None:
    """Device label is everything after the count columns — kernel
    annotations like ``IR-IO-APIC 2-edge timer`` are multi-token."""
    text = "CPU0 CPU1\n0: 1 2 IR-IO-APIC 2-edge timer\n"
    rows, _ = _parse_interrupts(text)
    assert rows[0].device == "IR-IO-APIC 2-edge timer"


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.cpu_count == 0


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "interrupts"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert snap.cpu_count == 4


# --- rendering -------------------------------------------------------------


def test_render_unavailable_explains() -> None:
    snap = _InterruptsSnapshot(rows=(), cpu_count=0, available=False)
    rendered = _render(snap)
    assert "unavailable" in rendered
    assert "⚠" not in rendered


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "interrupts"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    rendered = _render(snap)
    # CPU count surfaced.
    assert "CPU count" in rendered
    # Top hardware IRQ (nvme0q1, the 9.5M one) appears.
    assert "nvme0q1" in rendered
    # Pseudo-IRQ block exists and labelled IRQs render.
    assert "Pseudo-IRQs" in rendered
    assert "LOC" in rendered
    assert "NMI" in rendered


def test_render_below_floor_dropped(tmp_path: Path) -> None:
    """IRQs with total <= floor are excluded from the active list —
    unused PCI slots would otherwise crowd the display. They're
    still on the snapshot for drill-down callers."""
    p = tmp_path / "interrupts"
    # IRQ 25 in _SAMPLE has total = 50+60+70+80 = 260, well under the
    # default floor of 1000.
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    rendered = _render(snap)
    # IRQ 25 dropped from display.
    assert "nvme0q2" not in rendered
    # But still on snapshot.
    assert any(r.irq_id == "25" for r in snap.rows)
    # Confirm the floor we tested against matches code's constant.
    assert _DISPLAY_TOTAL_FLOOR >= 260


def test_render_missing_pseudo_kind_note(tmp_path: Path) -> None:
    """Curated pseudo-IRQ absent (some kernels don't expose TLB) →
    'not exposed' label, not silent drop."""
    p = tmp_path / "interrupts"
    # NMI present but no LOC/RES/CAL/TLB.
    p.write_text("CPU0 CPU1\n0: 1000 2000 IO-APIC 2-edge timer\nNMI: 1 2 Non-maskable interrupts\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "not exposed" in rendered


def test_render_parse_failed_explains(tmp_path: Path) -> None:
    """Available file but unparseable → explicit note, no ⚠."""
    p = tmp_path / "interrupts"
    p.write_text("garbage\n")
    snap = _capture(path=p)
    assert snap.available
    rendered = _render(snap)
    assert "parse failed" in rendered or "empty" in rendered
    assert "⚠" not in rendered


def test_no_warnings_anywhere_extreme_skew(tmp_path: Path) -> None:
    """Cry-wolf prevention pin: extreme single-CPU IRQ pinning must
    NOT produce ⚠ anywhere. A NIC IRQ pinned by smp_affinity is the
    configured behavior — operator policy, not card policy."""
    p = tmp_path / "interrupts"
    p.write_text(
        "CPU0 CPU1 CPU2 CPU3\n 24: 99999999 0 0 0 PCI-MSI nic0-rx\nLOC: 1 1 1 1 Local timer\n"
    )
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_cap_truncates(tmp_path: Path) -> None:
    """When there are more active IRQs than the cap, render truncates
    with an explicit "more not shown" note — operator sees the limit
    rather than wondering whether something silently vanished."""
    lines = ["CPU0 CPU1"]
    # 40 active IRQs all above the floor.
    for i in range(40):
        lines.append(f"{i}: 5000 5000 PCI-MSI dev{i}")
    p = tmp_path / "interrupts"
    p.write_text("\n".join(lines) + "\n")
    snap = _capture(path=p)
    rendered = _render(snap)
    assert "more active IRQs not shown" in rendered
