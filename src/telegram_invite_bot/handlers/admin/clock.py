"""``/admin_clock`` — host clock + timezone snapshot.

Complements /admin_uptime (boot-time + elapsed) by surfacing the
**current** clock state the process sees right now: wall-clock UTC,
local-time tzname, monotonic-clock tick, and the perf-counter tick.
Together with /admin_uptime an operator can correlate "did time
jump?" between two snapshots — a healthy host advances monotonic
and wall-clock by the same delta; a slewed or stepped NTP correction
shows as a divergence.

Why an operator wants this:

* Schedule debugging. Cron-style jobs ("nightly cleanup at 03:00")
  rely on the local clock matching the operator's mental model;
  surfacing :data:`time.tzname` lets them confirm the host runs
  in the timezone they think it does (a Docker image with no
  ``TZ`` env defaults to UTC, which silently shifts every
  hour-based assertion).
* Token-bucket / rate-limit triage. Throttling buckets key off
  monotonic time; if monotonic is suspiciously close to wall-clock
  epoch the operator knows the process restarted recently and
  per-user counters were reset.
* Stuck-clock / NTP-step detection. Calling /admin_clock twice
  with an operator-measured delay and comparing the wall-clock
  delta against the monotonic delta surfaces NTP step adjustments
  (wall jumps, monotonic doesn't) — the cheapest detection
  without root SSH.

Pure stdlib (``time`` + ``datetime``) — no psutil dependency, no
DB hit, no IO. Same posture as every other ``/admin_*``: silent-drop
for non-devs (existence must not leak dev IDs), private-only at the
router level.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.clock")


class _ClockSnapshot:
    """One read of the host's clock state.

    Plain attribute container — captured all at once so the wall
    and monotonic reads are as close together as possible. A delay
    between them would inflate the apparent "drift" on subsequent
    diffs.
    """

    __slots__ = ("local_now", "monotonic_s", "perf_counter_s", "tzname", "utc_now")

    def __init__(
        self,
        *,
        utc_now: datetime,
        local_now: datetime,
        monotonic_s: float,
        perf_counter_s: float,
        tzname: tuple[str, str],
    ) -> None:
        self.utc_now = utc_now
        self.local_now = local_now
        self.monotonic_s = monotonic_s
        self.perf_counter_s = perf_counter_s
        self.tzname = tzname


def _capture() -> _ClockSnapshot:
    """Capture all four reads back-to-back.

    Order is intentional: wall-clock first (it's the read the
    operator most cares about), then monotonic + perf_counter as
    close together as possible so any later drift comparison
    between them is dominated by real drift, not capture latency.
    """
    return _ClockSnapshot(
        utc_now=datetime.now(UTC),
        local_now=datetime.now().astimezone(),
        monotonic_s=time.monotonic(),
        perf_counter_s=time.perf_counter(),
        tzname=(time.tzname[0], time.tzname[1]),
    )


def _render(snap: _ClockSnapshot) -> str:
    lines = ["🕐 <b>Host clock</b>", ""]
    lines.append(f"• UTC now: <code>{snap.utc_now.strftime('%Y-%m-%d %H:%M:%S')}</code>")
    # Local time with the offset so the operator sees both the
    # wall reading and which zone produced it — naked local time
    # is ambiguous when the host TZ is itself the bug under
    # investigation.
    lines.append(f"• Local now: <code>{snap.local_now.strftime('%Y-%m-%d %H:%M:%S %z')}</code>")
    std, dst = snap.tzname
    # ``time.tzname`` is ``(standard_name, dst_name)`` — render both
    # so the operator can confirm the host knows about its own DST
    # transition (a misconfigured container has both entries equal
    # to ``UTC`` regardless of the configured offset).
    if std == dst:
        lines.append(f"• Zone: <code>{std}</code>")
    else:
        lines.append(f"• Zone: <code>{std}</code> / <code>{dst}</code> (DST)")
    lines.append("")
    # Monotonic + perf_counter are both since-process-start; we
    # render them with enough precision (3 decimals) that the
    # operator running two snapshots can spot sub-second drift.
    lines.append(f"• Monotonic since boot: <code>{snap.monotonic_s:.3f}s</code>")
    lines.append(f"• Perf counter since boot: <code>{snap.perf_counter_s:.3f}s</code>")
    lines.append("")
    lines.append(
        "<i>Two snapshots a known interval apart: wall delta and "
        "monotonic delta should match. Divergence → NTP step. "
        "Different tzname than expected → host TZ misconfig.</i>"
    )
    return "\n".join(lines)


async def handle_admin_clock(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_clock; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_clock rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.clock")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_clock(message, settings)

    router.message.register(_entry, Command("admin_clock", ignore_case=True))
    return router
