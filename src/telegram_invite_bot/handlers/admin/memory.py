"""``/admin_memory`` — process-memory anatomy via /proc/self/status.

Complements /admin_proc (peak RSS + CPU) with the **breakdown** view:
not just "how much memory", but **what kind** — anonymous heap vs.
file-backed mappings vs. swap-resident vs. stack. Same operator
question /admin_fds answers for descriptors ("how close, what kind"),
applied to memory.

Why an operator wants this:

* "Are we leaking, or just warm?" — VmRSS alone is ambiguous; the
  same number can be a stable warmed-up heap or a slow growth. The
  VmData line (anonymous heap + initialised globals) is the
  load-bearing leak signal — if it climbs sample-over-sample,
  Python object retention is the culprit; if VmRSS climbs but
  VmData doesn't, it's file-backed (mmap of a model file,
  loguru rotation, image cache).
* "Are we swapping?" — VmSwap > 0 is the latency-killer signal.
  Even a few MB of swapped pages means a page-fault-storm on
  the first request that touches them. ⚠ on any non-zero VmSwap
  is the visual cue.
* "Did we just inflate?" — VmPeak / VmHWM (high-water-mark) is
  the historical maximum since process start. A VmPeak far above
  current VmSize means we briefly inflated (image render, JSON
  batch decode) and the allocator hasn't returned the pages to
  the OS. Not always a leak, but always worth knowing.
* "Stack growing?" — VmStk above the default 8 MB usually means
  one of two things: PyInstaller's bootstrap (legitimate), or
  unbounded recursion that didn't quite hit the recursionlimit
  (worrying). Bumping /admin_runtime's recursionlimit alone
  doesn't tell you whether the stack actually grew; this does.

Linux-only at the read layer (parses /proc/self/status). On
macOS / Windows the file doesn't exist; the card surfaces
"unavailable" and skips the breakdown. Mirrors /admin_fds.
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


log = logger.bind(component="handlers.admin.memory")


# Keys we extract from /proc/self/status. Order is the render
# order — most operationally-important first (RSS is the headline
# every monitoring dashboard shows; Peak / HWM contextualise it;
# Data is the leak-signal; Swap is the ⚠ candidate; Stk closes
# the breakdown).
_KEYS: tuple[str, ...] = (
    "VmRSS",
    "VmPeak",
    "VmHWM",
    "VmSize",
    "VmData",
    "VmStk",
    "VmSwap",
)


class _MemSnapshot:
    """Captured /proc/self/status memory rows.

    Values are ints in kilobytes (the unit /proc/self/status uses
    natively — we don't convert at capture time so the test fixtures
    stay readable). The renderer does the kB→MiB conversion. A
    missing key collapses to ``-1`` — surfaced as "unavailable"
    rather than ``0`` (which would be a lie: a running process
    cannot have 0 RSS).
    """

    __slots__ = ("available", "values")

    def __init__(self, *, available: bool, values: dict[str, int]) -> None:
        self.available = available
        self.values = values


def _parse(text: str) -> dict[str, int]:
    """Pull the Vm* lines out of /proc/self/status.

    Format is one ``Key:\twhitespace\tvalue\tunit`` per line. We
    only care about the keys in ``_KEYS`` and assume kB (which is
    what the kernel always emits for Vm*). Anything we can't parse
    is silently dropped — the renderer fills missing keys with
    ``-1`` so a future kernel that renames a row doesn't crash
    the diagnostic.
    """
    out: dict[str, int] = {}
    wanted = set(_KEYS)
    for line in text.splitlines():
        # Lines look like: ``VmRSS:\t   12345 kB``
        key, _, rest = line.partition(":")
        if key not in wanted:
            continue
        # Strip the unit (assume kB) and parse the integer. A
        # malformed line (non-numeric value) is dropped silently —
        # better to lose one row than crash the whole card on a
        # kernel quirk we didn't anticipate.
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            out[key] = int(parts[0])
        except ValueError:
            continue
    return out


def _capture(status_path: Path = Path("/proc/self/status")) -> _MemSnapshot:
    """Read /proc/self/status. Parameterised for the test fixtures.

    A read failure (FileNotFoundError on non-Linux, PermissionError
    in some seccomp profiles) flips ``available=False`` — render
    takes the "unavailable" branch. We do not fake values.
    """
    try:
        text = status_path.read_text()
    except OSError:
        return _MemSnapshot(available=False, values={})
    return _MemSnapshot(available=True, values=_parse(text))


def _fmt_kb(kb: int) -> str:
    """Render kB as MiB with one decimal — the unit operators
    actually scan ("VmRSS: 312.4 MiB" reads faster than
    "VmRSS: 319898 kB"). ``-1`` is the sentinel for missing.
    """
    if kb < 0:
        return "<i>unavailable</i>"
    return f"{kb / 1024:.1f} MiB"


def _swap_concerning(values: dict[str, int]) -> bool:
    """``True`` when VmSwap > 0.

    Any swap residency for a latency-sensitive bot is concerning:
    the next request that touches those pages takes a page-fault
    detour through the swap device. The threshold is therefore
    "any non-zero", not "more than X" — and we treat a missing
    VmSwap row (older kernel, kernel without CONFIG_SWAP) as
    non-concerning, because we can't claim the bot is swapping
    if we can't see the number.
    """
    vmswap = values.get("VmSwap", -1)
    return vmswap > 0


def _peak_inflated(values: dict[str, int]) -> bool:
    """``True`` if VmPeak is more than 1.5× current VmSize.

    A briefly-inflated allocator footprint (peak >> current) means
    the bot transiently allocated a lot and the allocator hasn't
    returned the pages. Not a leak per se — Python's small-object
    allocator famously doesn't release arenas — but the operator
    wants to know **how much** ahead of current the peak is, so
    the marker is the cue to read the numbers.
    """
    vmpeak = values.get("VmPeak", -1)
    vmsize = values.get("VmSize", -1)
    if vmpeak < 0 or vmsize <= 0:
        return False
    return vmpeak > vmsize * 3 // 2


def _render(snap: _MemSnapshot) -> str:
    lines = ["🧠 <b>Process memory</b>", ""]
    if not snap.available:
        lines.append(
            "  <code>unavailable</code> <i>(/proc/self/status not readable — non-Linux host?)</i>"
        )
        lines.append("")
        lines.append(
            "<i>Linux-only diagnostic. On macOS use Activity Monitor "
            "or vmmap; on Windows use Task Manager / Process Explorer.</i>"
        )
        return "\n".join(lines)

    swap_warn = _swap_concerning(snap.values)
    peak_warn = _peak_inflated(snap.values)
    for key in _KEYS:
        value = snap.values.get(key, -1)
        concerning = (key == "VmSwap" and swap_warn) or (key == "VmPeak" and peak_warn)
        marker = " ⚠" if concerning else ""
        lines.append(f"  • <code>{key}</code>: <code>{_fmt_kb(value)}</code>{marker}")
    lines.append("")
    lines.append(
        "<i>⚠ markers: VmSwap &gt; 0 (any swap residency is a "
        "latency-killer for the next request that touches those "
        "pages), VmPeak &gt; 1.5× VmSize (transient inflation the "
        "allocator hasn't returned to the OS). VmData is the leak "
        "signal — diff against an earlier sample to spot heap "
        "growth.</i>"
    )
    return "\n".join(lines)


async def handle_admin_memory(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_memory; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_memory rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.memory")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_memory(message, settings)

    router.message.register(_entry, Command("admin_memory", ignore_case=True))
    return router
