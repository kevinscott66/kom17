"""``/admin_engines`` — per-engine connection-pool snapshot.

Sibling to /admin_pragmas (configuration) and /admin_integrity (data
health). This card surfaces the **runtime** state of each engine:
how many connections are checked out right now, how many are sitting
idle, and whether the configured ceiling has been breached.

Why an operator wants this:

* Pool exhaustion is the classic silent stall — a handler that
  ``await``-s a session and never returns it (missed ``async with``
  scope, errored partway with no cleanup) drains the pool one slot
  at a time. The symptom is "the bot feels sluggish, but no errors
  in logs"; the diagnosis is ``checked_out == pool_size +
  max_overflow``. There's no other place this becomes visible
  without dropping into a Python REPL on the host.
* After a deploy that introduced a new long-running handler, the
  operator wants a one-shot check that no engine is sitting at the
  ceiling. Before this card the only signal would be users
  reporting slow commands — too slow.

We render: pool class, configured ``pool_size``, currently checked-
out (in use right now), checked-in (idle, ready for reuse),
overflow (connections opened beyond ``pool_size`` — a positive
number means we're in burst mode). The ``max_overflow`` cap is
read off the pool when available so an operator can compare
"checked-out / (size + max_overflow)" without re-deriving it from
``db.engines``.

The numbers are sampled *at the moment the card renders*. Pool state
moves fast under load; a single number is enough to spot the stuck-
high case but not to debug a transient spike. For that, run the
card twice 5 seconds apart — the deltas tell the story.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router level
(per-engine resource state would be operator-only context in a
shared admin group).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.db.names import ALL_DBS, DBName

if TYPE_CHECKING:
    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncEngine

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.engines")


class _PoolSnapshot:
    """One engine's pool sample.

    Plain attribute container — same rationale as
    :class:`admin.pragmas._PragmaSnapshot`. ``max_overflow`` is
    optional because not every Pool subclass exposes it
    (``NullPool`` for instance) — when absent we render an em-dash
    rather than fabricate a number.
    """

    __slots__ = (
        "checked_in",
        "checked_out",
        "db",
        "max_overflow",
        "overflow",
        "pool_class",
        "pool_size",
    )

    def __init__(
        self,
        *,
        db: DBName,
        pool_class: str,
        pool_size: int,
        checked_in: int,
        checked_out: int,
        overflow: int,
        max_overflow: int | None,
    ) -> None:
        self.db = db
        self.pool_class = pool_class
        self.pool_size = pool_size
        self.checked_in = checked_in
        self.checked_out = checked_out
        self.overflow = overflow
        self.max_overflow = max_overflow

    @property
    def at_ceiling(self) -> bool:
        """True iff every configured slot is currently in use.

        The card flags this with ⚠ because it's the precondition for
        a new ``session()`` to block. Compares against the explicit
        ceiling when known; falls back to ``checked_out >= pool_size``
        when ``max_overflow`` is unavailable (NullPool etc.)."""
        if self.max_overflow is None:
            return self.checked_out >= self.pool_size
        return self.checked_out >= self.pool_size + self.max_overflow


def _sample(engine: AsyncEngine, db: DBName) -> _PoolSnapshot:
    """Read pool counters off the sync pool.

    SQLAlchemy's async engine wraps a sync pool; counters are accessed
    via the same methods. ``max_overflow`` is exposed as the (private)
    ``_max_overflow`` attribute on :class:`QueuePool` — we read it
    defensively via ``getattr`` so non-Queue pools (NullPool, the
    aiosqlite default in some configurations) don't crash the card.
    """
    pool = engine.pool

    # ``size``/``checkedin``/``checkedout``/``overflow`` live on
    # :class:`QueuePool` (and its async-adapted variant) but NOT on
    # the abstract ``Pool`` base — every non-Queue pool subclass
    # (NullPool, StaticPool) silently no-ops them or omits them
    # entirely. Read defensively via ``getattr`` returning a zero
    # callable so the card stays renderable for any pool class. The
    # cost is one branch's worth of mypy-friendly indirection; the
    # alternative — ``cast(QueuePool, pool)`` — would crash hard if
    # an operator ever swapped the pool class behind our backs.
    def _read(name: str) -> int:
        fn = getattr(pool, name, None)
        if fn is None or not callable(fn):
            return 0
        try:
            return int(fn())
        except Exception:  # noqa: BLE001 - diagnostic, never propagate
            return 0

    # ``_max_overflow`` is private API, but it's the only programmatic
    # surface SQLAlchemy gives for the configured ceiling. The
    # alternative — re-reading it from settings — would couple the
    # card to ``build_registry``'s literal arguments, which is a
    # tighter coupling than tolerating a private attribute read.
    max_overflow_raw = getattr(pool, "_max_overflow", None)
    max_overflow = int(max_overflow_raw) if isinstance(max_overflow_raw, int) else None
    return _PoolSnapshot(
        db=db,
        pool_class=type(pool).__name__,
        pool_size=_read("size"),
        checked_in=_read("checkedin"),
        checked_out=_read("checkedout"),
        overflow=_read("overflow"),
        max_overflow=max_overflow,
    )


def _render(snaps: list[_PoolSnapshot]) -> str:
    lines = ["🔌 <b>Engine connection-pool snapshot</b>", ""]
    any_ceiling = False
    for s in snaps:
        if s.at_ceiling:
            any_ceiling = True
        flag = " ⚠" if s.at_ceiling else ""
        lines.append(f"<b>{s.db.value}</b>{flag}")
        lines.append(f"  • pool_class: <code>{s.pool_class}</code>")
        max_ovr = str(s.max_overflow) if s.max_overflow is not None else "—"
        lines.append(
            f"  • pool_size / max_overflow: <code>{s.pool_size}</code> / <code>{max_ovr}</code>"
        )
        lines.append(f"  • checked_out: <code>{s.checked_out}</code> (in use right now)")
        lines.append(f"  • checked_in: <code>{s.checked_in}</code> (idle)")
        # ``overflow()`` returns a value relative to the configured
        # pool_size: it starts at ``-pool_size`` when no connection has
        # ever been opened, rises to 0 once the pool is fully
        # initialised, and goes positive only when we burst past
        # pool_size. The negative-when-idle convention is a
        # SQLAlchemy quirk; render it as-is so an operator looking
        # at the docs sees the same number.
        lines.append(f"  • overflow: <code>{s.overflow}</code>")
        lines.append("")
    if any_ceiling:
        lines.append(
            "<i>⚠ At least one engine is at its connection ceiling. "
            "New session() calls will block until a connection is "
            "returned. Likely a handler missed an "
            "<code>async with</code> scope and is leaking sessions — "
            "grep for ``session()`` calls without surrounding ``async "
            "with``.</i>"
        )
    else:
        lines.append("<i>All engines within configured pool limits.</i>")
    return "\n".join(lines)


async def handle_admin_engines(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_engines; silently dropped"
        )
        return
    snaps = [_sample(registry.engine(db), db) for db in ALL_DBS]
    await message.answer(_render(snaps))
    log.bind(user_id=user.id).info("/admin_engines rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.engines")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_engines(message, settings, registry)

    router.message.register(_entry, Command("admin_engines", ignore_case=True))
    return router
