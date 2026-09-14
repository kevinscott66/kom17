"""``/admin_meminfo`` — host-wide memory accounting from /proc/meminfo.

The existing memory cards answer *process-scoped* questions:

* ``/admin_memory`` — VmRSS / VmPeak / VmSwap from /proc/self/status:
  what THIS process consumes.
* ``/admin_smaps`` — PSS / Private / Swap from /proc/self/smaps_rollup:
  the honest per-process cost after share-correction.
* ``/admin_rusage`` — page faults + major/minor: process I/O behaviour.

None of those answer the question an operator actually asks when the
bot starts misbehaving in production: **"is the HOST out of memory?"**.
That's the canonical /proc/meminfo question — MemTotal, MemFree,
MemAvailable, Buffers, Cached, SwapTotal, SwapFree, Dirty, Writeback.

Why MemAvailable specifically:

The naive "free memory" view (MemFree) is wrong on Linux — the
kernel deliberately uses every spare byte for page cache, so on a
healthy box MemFree is near zero and that's *fine*. The kernel
itself computes ``MemAvailable`` (since 3.14, ~2014) as "how much
memory could a new allocation realistically claim without
swapping" — accounting for reclaimable cache + dentries + inodes.
That's the field every modern monitoring tool (free -h, htop,
sysstat) actually reports as "available", and it's the single
canonical "is the host under memory pressure?" signal.

Cry-wolf posture: ⚠ ONLY when MemAvailable < 10% of MemTotal AND
the field exists. Anything above that band is "host is doing fine"
and we render with no marker — even though MemFree might be tiny,
even though Swap might be used. Swap usage on a memory-tight
system is *expected* and is not a fault — the kernel did the right
thing by paging cold pages out. ⚠'ing on Swap usage would be the
exact cry-wolf failure mode this codebase rejects.

Forward-compat: /proc/meminfo gains fields over kernel releases
(HardwareCorrupted, AnonHugePages, KReclaimable, ShmemHugePages,
…). We carry a curated set + bucket the rest under "other fields"
so future kernels don't silently drop information.

Same posture as every other admin card — silent-drop, private-only,
loguru-tagged, dishka-DI-free (pure stdlib).
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


log = logger.bind(component="handlers.admin.meminfo")


_MEMINFO_PATH = Path("/proc/meminfo")


# 10% of MemTotal — kernel docs + every monitoring tool agree this is
# the "starting to be unhappy" band. Lower would miss real pressure
# (a tight box at 7% IS in trouble); higher would cry wolf on healthy
# cache-heavy machines. The single ⚠ trigger of this card.
_AVAILABLE_WARN_FRACTION = 0.10


# Curated meminfo fields with operator-facing notes. Order is
# "headline numbers first, deeper accounting last" — same ergonomic
# principle as /admin_limits. Fields not in this table get bucketed
# under "other fields" so we don't silently drop new kernel fields.
_MEMINFO_FIELDS: tuple[tuple[str, str], ...] = (
    ("MemTotal", "physical RAM visible to the kernel"),
    ("MemFree", "truly idle — usually near 0 on Linux (page cache eats the rest)"),
    ("MemAvailable", "kernel's own estimate of allocatable RAM — the real pressure signal"),
    ("Buffers", "block-device metadata cache"),
    ("Cached", "page cache — reclaimable on demand"),
    ("SwapTotal", "configured swap capacity"),
    ("SwapFree", "unused swap"),
    ("Dirty", "modified pages awaiting writeback — high = I/O backlog"),
    ("Writeback", "pages currently being written to disk"),
    ("Shmem", "shared memory (tmpfs + SysV IPC)"),
    ("Slab", "kernel object cache total"),
    ("SReclaimable", "reclaimable slab — part of MemAvailable headroom"),
    ("AnonPages", "process-owned non-file-backed pages"),
    ("Mapped", "memory-mapped file pages currently faulted in"),
)


class _MemSnapshot:
    """Captured /proc/meminfo reading.

    * ``fields`` — ordered dict-like tuple of (name, bytes-or-None).
      None means the field was in our curated list but absent from
      /proc/meminfo (old kernel, exotic build) — render explicitly
      as "n/a" rather than skip so the operator sees the absence.
    * ``other_fields`` — fields the kernel exposed that aren't in
      our curated table. Surfaced as a footer count so a future
      kernel adding a field doesn't go unnoticed.
    * ``available_fraction`` — MemAvailable / MemTotal as float, or
      None if either field is missing / zero. Single source of
      truth for the ⚠ predicate.
    * ``available`` — False when /proc/meminfo couldn't be read at
      all (macOS, container without /proc, permission error).
    """

    __slots__ = ("available", "available_fraction", "fields", "other_fields")

    def __init__(
        self,
        *,
        fields: tuple[tuple[str, int | None], ...],
        other_fields: tuple[str, ...],
        available_fraction: float | None,
        available: bool,
    ) -> None:
        self.fields = fields
        self.other_fields = other_fields
        self.available_fraction = available_fraction
        self.available = available


def _parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo into a bytes-valued dict.

    Format is ``Name:    <value> kB`` (or rarely no unit for HugePages
    counters). We multiply by 1024 when ``kB`` is present and accept
    the raw integer otherwise — same convention every parser uses.
    Values that don't parse as int are dropped silently; we don't
    want a single malformed line (extremely rare in practice but
    possible on a corrupted /proc) to break the whole snapshot.
    """
    out: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        # The kernel emits ``kB`` (case-sensitive) for memory fields
        # and nothing for HugePages_Total / _Free / _Rsvd counters.
        # Treat ``kB`` as 1024 (the kernel's own convention — yes,
        # despite the lowercase k, it's KiB).
        if len(parts) >= 2 and parts[1] == "kB":
            out[name.strip()] = value * 1024
        else:
            out[name.strip()] = value
    return out


def _capture(*, path: Path = _MEMINFO_PATH) -> _MemSnapshot:
    """Read /proc/meminfo + build a snapshot.

    The path parameter is keyword-only so tests can inject a tmp
    file — /proc isn't writable, and macOS doesn't have /proc at
    all. Same hermetic-fixture pattern as every other diagnostic
    card in this directory.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _MemSnapshot(
            fields=tuple((name, None) for name, _ in _MEMINFO_FIELDS),
            other_fields=(),
            available_fraction=None,
            available=False,
        )

    parsed = _parse_meminfo(text)
    curated_names = {name for name, _ in _MEMINFO_FIELDS}
    fields = tuple((name, parsed.get(name)) for name, _ in _MEMINFO_FIELDS)
    others = tuple(sorted(set(parsed) - curated_names))

    mem_total = parsed.get("MemTotal")
    mem_avail = parsed.get("MemAvailable")
    fraction: float | None
    if mem_total is None or mem_avail is None or mem_total <= 0:
        # MemAvailable predates ~2014 (kernel 3.14). On the unlikely
        # ancient kernel where it's missing, we deliberately do NOT
        # synthesise it from MemFree + Buffers + Cached — that
        # formula is famously wrong on memcg-aware kernels, and a
        # false-confidence number is worse than no number.
        fraction = None
    else:
        fraction = mem_avail / mem_total

    return _MemSnapshot(
        fields=fields,
        other_fields=others,
        available_fraction=fraction,
        available=True,
    )


def _pressure(snap: _MemSnapshot) -> bool:
    """⚠ predicate — MemAvailable below 10% of MemTotal.

    Returns False if the fraction isn't computable (missing fields
    or unreadable /proc) — absence of data is NOT a warning, same
    cry-wolf-prevention posture as every other card.
    """
    if snap.available_fraction is None:
        return False
    return snap.available_fraction < _AVAILABLE_WARN_FRACTION


def _fmt_bytes(value: int | None) -> str:
    """Human-readable bytes: GiB if ≥1 GiB, else MiB, else raw.
    None → "n/a" (field absent on this kernel)."""
    if value is None:
        return "n/a"
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024**3):.2f} GiB"
    if value >= 1024 * 1024:
        return f"{value / (1024**2):.1f} MiB"
    return f"{value:,} B"


def _render(snap: _MemSnapshot) -> str:
    lines = ["🧠 <b>Host memory (/proc/meminfo)</b>", ""]

    if not snap.available:
        # macOS dev / container without /proc — render the absence,
        # don't pretend we have data. Mirrors /admin_smaps + /admin_io
        # behaviour for the same reason.
        lines.append(
            "  <i>/proc/meminfo unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers will see this.</i>"
        )
        return "\n".join(lines)

    notes = dict(_MEMINFO_FIELDS)
    for name, value in snap.fields:
        formatted = _fmt_bytes(value)
        marker = ""
        if name == "MemAvailable" and _pressure(snap):
            marker = " ⚠"
        lines.append(f"  • <b>{name}</b>: <code>{formatted}</code>{marker}")
        lines.append(f"      <i>{notes[name]}</i>")

    if snap.available_fraction is not None:
        lines.append("")
        pct = snap.available_fraction * 100
        lines.append(f"  <b>MemAvailable fraction:</b> <code>{pct:.1f}%</code> of MemTotal")

    if snap.other_fields:
        lines.append("")
        lines.append(
            "  <i>Other /proc/meminfo fields exposed by this kernel "
            f"but not in the curated list ({len(snap.other_fields)}): "
            f"{', '.join(snap.other_fields[:8])}"
            f"{', …' if len(snap.other_fields) > 8 else ''}.</i>"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: only on MemAvailable below 10% of MemTotal — "
        "the kernel's own canonical &quot;memory pressure&quot; "
        "signal. MemFree near zero is normal (page cache uses the "
        "spare); Swap usage on a tight box is the kernel doing the "
        "right thing, not a fault. See /admin_memory + /admin_smaps "
        "for process-scoped accounting.</i>"
    )
    return "\n".join(lines)


async def handle_admin_meminfo(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_meminfo; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        available_fraction=snap.available_fraction,
        other_field_count=len(snap.other_fields),
        pressure=_pressure(snap),
    ).info("/admin_meminfo rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.meminfo")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_meminfo(message, settings)

    router.message.register(_entry, Command("admin_meminfo", ignore_case=True))
    return router
