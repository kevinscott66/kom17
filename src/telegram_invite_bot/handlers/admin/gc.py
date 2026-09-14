"""``/admin_gc`` — Python garbage-collector snapshot.

Complements /admin_proc (peak-RSS + CPU) with the **allocator-cycle**
side of memory health. RSS is the symptom; GC pressure is the
cause that operators chase before RSS climbs into OOM territory.

Why an operator wants this:

* Cycle-leak suspicion. The bot's RSS climbs slowly over hours;
  /admin_proc shows the climb but not the cause. If the GC's gen-2
  collection count is rising fast and gen-2 object count is high,
  reference cycles are the culprit — most often from a handler
  closure capturing the Bot instance and being pinned by an
  aiohttp connector. The card surfaces this before the OOM-killer
  decides for us.
* "Did GC get disabled?" — :func:`gc.disable` is something a
  benchmarking branch might leave behind. ``gc.isenabled() == False``
  in production is the kind of one-line regression that has zero
  runtime symptoms until the first long-running session, by which
  point the symptom is "process slow" without a stack trace.
* Threshold drift. ``gc.set_threshold`` arguments are a known
  performance lever — a refactor that touches the loop's
  initialization should not silently lower gen-0 below 700
  (Python's default). The card pins the current values so a
  regression surfaces here, not as a flame-graph mystery.

We sample :func:`gc.get_count`, :func:`gc.get_stats`,
:func:`gc.get_threshold`, and :func:`gc.isenabled` once at message-
handle time. Cheap (microseconds); no allocation pressure introduced
by the diagnostic itself.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router level
(GC numbers are not secret but the rest of the control plane is
private, so keeping the posture symmetric simplifies the audit).
"""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.gc")


class _GCSnapshot:
    """One-shot GC state.

    Three generations is a CPython invariant (gen-0, gen-1, gen-2).
    Storing per-gen counts + thresholds + collection-counts as
    parallel tuples mirrors what :mod:`gc` itself returns, so the
    renderer doesn't have to dance around mismatched shapes.

    ``collections`` is the cumulative collection count from
    :func:`gc.get_stats` — that's the load-bearing number for
    spotting "GC running far more often than expected" without
    needing a baseline.
    """

    __slots__ = ("collections", "counts", "enabled", "thresholds")

    def __init__(
        self,
        *,
        enabled: bool,
        counts: tuple[int, int, int],
        thresholds: tuple[int, int, int],
        collections: tuple[int, int, int],
    ) -> None:
        self.enabled = enabled
        self.counts = counts
        self.thresholds = thresholds
        self.collections = collections


def _capture() -> _GCSnapshot:
    """Single GC sample.

    Order of calls matters mildly: ``get_count`` first so we read it
    before the diagnostic itself perturbs allocation. In practice
    the perturbation is below the per-call delta so the order is a
    documentation gesture, not a correctness invariant — but cheap
    to honour and avoids "why does the count jump on every call"
    confusion if a future change makes the renderer allocation-
    heavy.
    """
    counts = gc.get_count()
    thresholds = gc.get_threshold()
    stats = gc.get_stats()
    # ``get_stats`` returns a list of per-generation dicts;
    # ``collections`` is the cumulative count. Defensive extraction:
    # if a future CPython adds a generation the snapshot still
    # captures the first three rather than IndexError'ing.
    coll = tuple(int(s.get("collections", 0)) for s in stats[:3])
    while len(coll) < 3:
        coll = (*coll, 0)
    return _GCSnapshot(
        enabled=gc.isenabled(),
        counts=(int(counts[0]), int(counts[1]), int(counts[2])),
        thresholds=(
            int(thresholds[0]),
            int(thresholds[1]),
            int(thresholds[2]),
        ),
        collections=(coll[0], coll[1], coll[2]),
    )


def _render(snap: _GCSnapshot) -> str:
    lines = ["♻️ <b>Garbage collector</b>", ""]
    # The enabled flag is the load-bearing top-line signal. A
    # disabled-in-prod GC is a real regression mode (a refactor or
    # benchmarking branch left ``gc.disable()`` behind), and the
    # rest of the numbers are meaningless when it's off — surface
    # the warning marker so an operator can't miss it.
    if snap.enabled:
        lines.append("<b>Enabled:</b> <code>yes</code>")
    else:
        lines.append(
            "<b>Enabled:</b> <code>no</code> ⚠ (cycles will accumulate until manually collected)"
        )
    lines.append("")
    lines.append("<b>Per-generation:</b>")
    # Three generations is a CPython invariant; loop+enumerate would
    # add noise. Render as a tight aligned table-of-lines (Telegram
    # HTML doesn't have actual tables) so the operator can scan
    # gen-by-gen without parsing column labels.
    for gen in range(3):
        lines.append(
            f"  • gen-{gen}: "
            f"<code>{snap.counts[gen]}</code> objects, "
            f"threshold <code>{snap.thresholds[gen]}</code>, "
            f"collections <code>{snap.collections[gen]}</code>"
        )
    lines.append("")
    lines.append(
        "<i>gen-0 fills fastest (every short-lived alloc); gen-2 "
        "collections climbing fast = reference-cycle pressure. "
        "Thresholds at CPython defaults are <code>700, 10, 10</code>."
        "</i>"
    )
    return "\n".join(lines)


async def handle_admin_gc(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_gc; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_gc rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.gc")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_gc(message, settings)

    router.message.register(_entry, Command("admin_gc", ignore_case=True))
    return router
