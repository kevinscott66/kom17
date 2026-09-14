"""``/admin_psi`` — Pressure Stall Information from /proc/pressure/*.

PSI (Linux 4.20+, kernel config CONFIG_PSI=y) is the modern
canonical answer to "is this host under sustained resource
pressure?". Existing cards see point-in-time snapshots:
/admin_meminfo reads MemAvailable right now, /admin_loadavg reads
the legacy 1/5/15-min averaged run-queue depth. Neither tells you
whether a process actually **stalled** on a missing resource and
for how much of the window.

PSI counts wall-clock time tasks spent waiting on a contended
resource. ``some`` = at least one task stalled. ``full`` = ALL
non-idle tasks stalled (no useful work happening). Kernel
maintains 10s / 60s / 300s exponentially-weighted averages plus a
total μs counter. That makes PSI the right input to autoscalers
and the right signal for an operator's "is this host healthy?"
question.

Card layout:

* /proc/pressure/cpu — ``some`` line only (CPU "full" is
  meaningless: if everything stalls on CPU, nothing was running
  to detect the stall).
* /proc/pressure/memory — ``some`` + ``full``.
* /proc/pressure/io — ``some`` + ``full``.

⚠ predicate: ``full`` 60-second average > 10% on either memory or
io. That's the boundary where the host genuinely lost throughput,
not just felt slow. CPU is intentionally NOT ⚠'d — high cpu.some
is the normal state of any busy host and would be classic
cry-wolf. The threshold is conservative on purpose: PSI lines
flicker high under any momentary contention; sustaining 10% of
wall-clock with EVERY task stalled is the unambiguous "this host
is in real trouble" signal.

Forward-compat: PSI line format is ``some avg10=X.XX avg60=X.XX
avg300=X.XX total=N``. We parse by tokenising and reading key=value
pairs, so a kernel that adds avg-windows (or reorders) keeps
working. Missing files (4.x kernels without CONFIG_PSI) render an
explanatory note rather than crash — same degrade-don't-crash
posture as every other card.

Same posture as every other admin card — silent-drop, private-only,
pure stdlib, hermetic via keyword-only base path injection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.psi")


_PSI_BASE = Path("/proc/pressure")


# ⚠ threshold — 60-second average percentage at which ``full``
# pressure on memory or io is treated as a real signal rather than
# the noise floor. 10% means 6 seconds out of every minute every
# non-idle task was stalled. Below that, transient contention is
# expected on any active host; mark it and we'd cry wolf hourly.
_FULL_AVG60_WARN_PCT = 10.0


class _PsiLine:
    """One ``some``/``full`` line from a PSI file.

    Fields kept as floats for the averages and int for total
    (microseconds since boot). Missing fields → None; downstream
    rendering distinguishes None from 0.0 so a malformed line
    shows ``?`` rather than a misleading zero.
    """

    __slots__ = ("avg10", "avg300", "avg60", "kind", "total_us")

    def __init__(
        self,
        *,
        kind: str,
        avg10: float | None,
        avg60: float | None,
        avg300: float | None,
        total_us: int | None,
    ) -> None:
        self.kind = kind
        self.avg10 = avg10
        self.avg60 = avg60
        self.avg300 = avg300
        self.total_us = total_us


class _PsiResource:
    """One resource (cpu / memory / io). ``some`` is always present
    when the file exists; ``full`` is None for cpu by kernel design
    and may be None for memory/io on very old PSI kernels.
    """

    __slots__ = ("available", "full", "name", "some")

    def __init__(
        self,
        *,
        name: str,
        some: _PsiLine | None,
        full: _PsiLine | None,
        available: bool,
    ) -> None:
        self.name = name
        self.some = some
        self.full = full
        self.available = available


class _PsiSnapshot:
    """Captured PSI for the three resources we care about.

    ``available`` is True iff at least one of the three files was
    readable — operator sees per-resource availability via the
    embedded _PsiResource.available flag, not a global gate.
    """

    __slots__ = ("available", "cpu", "io", "memory")

    def __init__(
        self,
        *,
        cpu: _PsiResource,
        memory: _PsiResource,
        io: _PsiResource,
        available: bool,
    ) -> None:
        self.cpu = cpu
        self.memory = memory
        self.io = io
        self.available = available


def _parse_psi_line(line: str) -> _PsiLine | None:
    """Parse one PSI line. Returns None for unrecognised lines.

    Format: ``<kind> avg10=X.XX avg60=X.XX avg300=X.XX total=N``.
    We tokenise and read ``k=v`` pairs by name so future kernel
    additions don't shift positional indices and silently
    mis-attribute fields."""
    parts = line.strip().split()
    if not parts:
        return None
    kind = parts[0]
    if kind not in {"some", "full"}:
        return None
    avg10: float | None = None
    avg60: float | None = None
    avg300: float | None = None
    total_us: int | None = None
    for token in parts[1:]:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        try:
            if key == "avg10":
                avg10 = float(value)
            elif key == "avg60":
                avg60 = float(value)
            elif key == "avg300":
                avg300 = float(value)
            elif key == "total":
                total_us = int(value)
        except ValueError:
            # Per-field degrade — keep parsing the rest of the line.
            continue
    return _PsiLine(kind=kind, avg10=avg10, avg60=avg60, avg300=avg300, total_us=total_us)


def _read_resource(name: str, path: Path) -> _PsiResource:
    """Read one PSI file (cpu / memory / io)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _PsiResource(name=name, some=None, full=None, available=False)
    some: _PsiLine | None = None
    full: _PsiLine | None = None
    for raw_line in text.splitlines():
        parsed = _parse_psi_line(raw_line)
        if parsed is None:
            continue
        if parsed.kind == "some":
            some = parsed
        elif parsed.kind == "full":
            full = parsed
    return _PsiResource(name=name, some=some, full=full, available=True)


def _capture(*, base: Path = _PSI_BASE) -> _PsiSnapshot:
    """Read /proc/pressure/{cpu,memory,io} + build a snapshot.

    Keyword-only base path so tests can inject a tmp directory —
    same hermetic-fixture pattern as every other card."""
    cpu = _read_resource("cpu", base / "cpu")
    memory = _read_resource("memory", base / "memory")
    io = _read_resource("io", base / "io")
    available = cpu.available or memory.available or io.available
    return _PsiSnapshot(cpu=cpu, memory=memory, io=io, available=available)


def _high_pressure(snap: _PsiSnapshot) -> tuple[str, ...]:
    """Names of resources where ``full.avg60`` exceeds the warn pct.

    CPU is never returned (no ``full`` line by kernel design). An
    unavailable resource returns no entry — absence of data is NOT
    a warning."""
    triggered: list[str] = []
    for res in (snap.memory, snap.io):
        if not res.available or res.full is None or res.full.avg60 is None:
            continue
        if res.full.avg60 > _FULL_AVG60_WARN_PCT:
            triggered.append(res.name)
    return tuple(triggered)


def _fmt_avg(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.2f}%"


def _fmt_total(value: int | None) -> str:
    """Render total μs as a human-readable seconds value when
    large, μs otherwise. Total is cumulative stall time since boot
    so on long-running hosts it'll easily be in the seconds range."""
    if value is None:
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}s"
    return f"{value}μs"


def _render_line(label: str, line: _PsiLine | None) -> str:
    if line is None:
        return f"    • <i>{label}: not exposed by this kernel</i>"
    return (
        f"    • <b>{label}</b>: "
        f"avg10=<code>{_fmt_avg(line.avg10)}</code> "
        f"avg60=<code>{_fmt_avg(line.avg60)}</code> "
        f"avg300=<code>{_fmt_avg(line.avg300)}</code> "
        f"total=<code>{_fmt_total(line.total_us)}</code>"
    )


def _render_resource(res: _PsiResource) -> list[str]:
    lines = [f"  <b>{res.name}</b>:"]
    if not res.available:
        lines.append(
            "    <i>file unavailable — kernel built without CONFIG_PSI, or pre-4.20 kernel.</i>"
        )
        return lines
    lines.append(_render_line("some", res.some))
    if res.name == "cpu":
        # Kernel doesn't expose ``full`` for cpu — skip rather than
        # render a misleading "?" line.
        return lines
    lines.append(_render_line("full", res.full))
    return lines


def _render(snap: _PsiSnapshot) -> str:
    lines = ["📈 <b>PSI — Pressure Stall Information</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/pressure unavailable on this host — Linux 4.20+ "
            "with CONFIG_PSI required. macOS dev + older kernels see "
            "this.</i>"
        )
        return "\n".join(lines)

    triggered = _high_pressure(snap)
    if triggered:
        joined = ", ".join(f"<code>{r}</code>" for r in triggered)
        lines.append(
            f"  ⚠ <b>sustained full pressure on:</b> {joined} — every "
            f"non-idle task stalled for &gt;{_FULL_AVG60_WARN_PCT:.0f}% "
            f"of the last 60s on these resources. Host has actually "
            f"lost throughput, not just felt slow."
        )
        lines.append("")

    lines.extend(_render_resource(snap.cpu))
    lines.append("")
    lines.extend(_render_resource(snap.memory))
    lines.append("")
    lines.extend(_render_resource(snap.io))
    lines.append("")
    lines.append(
        "<i>some = at least one task stalled waiting for the "
        "resource. full = ALL non-idle tasks stalled (no useful "
        "work). cpu has no full by kernel design. Averages are "
        "exponentially-weighted percentages over the window.</i>"
    )
    lines.append("")
    lines.append(
        f"<i>⚠ markers: memory.full.avg60 or io.full.avg60 above "
        f"{_FULL_AVG60_WARN_PCT:.0f}% — sustained pressure where "
        f"throughput was actually lost. cpu.some is NOT marked "
        f"(high values are normal on any busy host; marking would "
        f"be cry-wolf). See /admin_loadavg for run-queue, "
        f"/admin_meminfo for point-in-time memory, /admin_diskstats "
        f"for per-device I/O.</i>"
    )
    return "\n".join(lines)


async def handle_admin_psi(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_psi; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    triggered = _high_pressure(snap)
    log.bind(
        user_id=user.id,
        available=snap.available,
        cpu_available=snap.cpu.available,
        memory_available=snap.memory.available,
        io_available=snap.io.available,
        high_pressure_resources=list(triggered),
    ).info("/admin_psi rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.psi")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_psi(message, settings)

    router.message.register(_entry, Command("admin_psi", ignore_case=True))
    return router
