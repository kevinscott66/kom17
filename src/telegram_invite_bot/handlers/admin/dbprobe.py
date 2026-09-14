"""``/admin_dbprobe`` — per-engine liveness probe (``SELECT 1``).

Complements the existing DB-side cards:

* :mod:`admin.engines` — pool checked_in/out snapshot (static view).
* :mod:`admin.integrity` — full-scan corruption + FK-orphan check
  (expensive; on-demand).
* :mod:`admin.pragmas` — verifies the connection-level configuration.
* :mod:`admin.db_sizes` — disk-footprint accounting.

What none of those answer: **can each engine actually serve a query
RIGHT NOW, and how long does the round trip take?**

Why an operator wants this:

* "Is the bot stuck on a DB lock?" — SQLite's writer is exclusive;
  a long-running write transaction (legacy bot.py occasionally
  holds one open while doing an aiohttp call — yes, really) makes
  every other connection block on ``busy_timeout``. A latency of
  4500 ms here ≈ blocked on busy_timeout=5000; a clean SELECT 1
  comes back in single-digit ms on a healthy host.
* "Did the WAL get wedged?" — a corrupted WAL or an interrupted
  checkpoint can make ``BEGIN`` itself slow. SELECT 1 on a fresh
  connection forces the engine through its normal acquire-+-pragma
  path; latency anomalies here surface that.
* "Did the disk fall off?" — a FUSE mount disappearing, an EBS
  volume in degraded state, an LXC filesystem freezer event. Any
  of these turns SELECT 1 into either a timeout or an OSError
  through aiosqlite. The card surfaces both via the error-class
  routing hint (same posture as /admin_dns and /admin_tempdir).

Concurrent across all 5 engines via :func:`asyncio.gather` — unlike
``integrity_check`` (which holds a SHARED lock for a full page
walk and competes with traffic), SELECT 1 is sub-millisecond on a
healthy engine and contention is not a concern. Hard 2 s per-engine
timeout caps the worst case.

Silent-drop for non-devs, private-only at the router level.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import text

from telegram_invite_bot.db.names import ALL_DBS, DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.dbprobe")


# Threshold past which a per-engine SELECT 1 carries ⚠. 100 ms is
# generous for a local SQLite open + acquire + SELECT 1 + close
# (typical is 1-10 ms). 100 ms past that usually means the writer
# is held by another connection and we're sitting on busy_timeout.
_LATENCY_CONCERNING_MS = 100.0


# Hard timeout per engine probe. SQLite's default busy_timeout=5000
# would otherwise dominate the wait on a wedged engine. 2 s is
# decisive without being so short it false-positives on a host
# under genuine but recoverable load.
_PROBE_TIMEOUT_S = 2.0


class _ProbeResult:
    """Captured result of a single SELECT 1 probe.

    ``error`` is the exception class name on failure (routing-hint
    posture mirrors /admin_dns and /admin_tempdir), ``None`` on
    success. ``latency_ms`` is wall-clock for the whole
    connect-+-query-+-close sequence so the operator sees the
    end-to-end cost an actual handler would pay.
    """

    __slots__ = ("db", "error", "latency_ms")

    def __init__(self, *, db: DBName, latency_ms: float, error: str | None) -> None:
        self.db = db
        self.latency_ms = latency_ms
        self.error = error


async def _probe_one(
    registry: EngineRegistry, db: DBName, timeout_s: float = _PROBE_TIMEOUT_S
) -> _ProbeResult:
    """Run ``SELECT 1`` against one engine with a hard timeout.

    Uses a fresh connection rather than borrowing one from the pool
    so the probe measures the worst-case path an incoming handler
    would hit on a cold acquire. The latency includes connection
    open + SELECT 1 + close — the operator-visible cost is the full
    round trip, not just the query.
    """
    engine = registry.engine(db)
    start = time.monotonic()
    try:

        async def _do() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        await asyncio.wait_for(_do(), timeout=timeout_s)
    except TimeoutError:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return _ProbeResult(db=db, latency_ms=elapsed_ms, error="TimeoutError")
    except Exception as exc:  # noqa: BLE001 - classify any DB-side failure
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return _ProbeResult(db=db, latency_ms=elapsed_ms, error=type(exc).__name__)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    return _ProbeResult(db=db, latency_ms=elapsed_ms, error=None)


async def _probe_all(registry: EngineRegistry) -> list[_ProbeResult]:
    """Probe all engines concurrently.

    SELECT 1 is cheap and the engines back distinct files — no
    contention, no reason to serialise. The order of the returned
    list matches :data:`ALL_DBS` so the renderer can iterate
    positionally without re-sorting.
    """
    coros = [_probe_one(registry, db) for db in ALL_DBS]
    return await asyncio.gather(*coros)


def _latency_concerning(result: _ProbeResult) -> bool:
    """Slow-but-successful → ⚠; failed → the error is the ⚠.

    A failed probe already carries its own marker (error class).
    Flagging slow-latency on top of that would double-count the
    same engine in the operator's eye; same posture as /admin_dns.
    """
    if result.error is not None:
        return False
    return result.latency_ms > _LATENCY_CONCERNING_MS


def _render(results: list[_ProbeResult]) -> str:
    lines = ["🩻 <b>Engine liveness probe</b>", ""]
    any_warn = False
    for r in results:
        if r.error is not None:
            any_warn = True
            lines.append(
                f"<b>{r.db.value}</b> ⚠ <code>{r.error}</code> <i>({r.latency_ms:.1f} ms)</i>"
            )
        elif _latency_concerning(r):
            any_warn = True
            lines.append(f"<b>{r.db.value}</b> — <code>ok</code> <i>({r.latency_ms:.1f} ms)</i> ⚠")
        else:
            lines.append(f"<b>{r.db.value}</b> — <code>ok</code> <i>({r.latency_ms:.1f} ms)</i>")
    lines.append("")
    if any_warn:
        lines.append(
            f"<i>⚠ markers: SELECT 1 failed (timeout / OperationalError "
            f"— check /admin_engines for pool state, /admin_disk for "
            f"FS state), or latency above {_LATENCY_CONCERNING_MS:.0f} "
            f"ms (typically a writer holding the busy_timeout — "
            f"cross-check /admin_tasks for a stuck coroutine).</i>"
        )
    else:
        lines.append("<i>All engines responsive.</i>")
    return "\n".join(lines)


async def handle_admin_dbprobe(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_dbprobe; silently dropped"
        )
        return
    results = await _probe_all(registry)
    await message.answer(_render(results))
    log.bind(
        user_id=user.id,
        failures=[r.db.value for r in results if r.error is not None],
    ).info("/admin_dbprobe rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.dbprobe")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_dbprobe(message, settings, registry)

    router.message.register(_entry, Command("admin_dbprobe", ignore_case=True))
    return router
