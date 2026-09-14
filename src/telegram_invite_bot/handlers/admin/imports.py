"""``/admin_imports`` — sys.modules census + builtin/frozen split.

Complements /admin_modules (installed dist versions of load-bearing
deps) by surfacing the **actually imported** set: what's resident in
``sys.modules`` right now, broken down by top-level package, with
separate counts for builtin and frozen entries.

Why an operator wants this:

* "Why did startup take so long?" — a 4 k-module ``sys.modules`` is
  a different posture than a 1.5 k-module one. The legacy bot.py
  shotgun-imports everything at module-import time, which is one
  of the reasons cold-start latency is what it is. As we strangler
  more modules out, this number should DROP — and the operator
  wants to see the trend without instrumenting startup.
* "What's the import surface of the new pipeline?" — top-level
  package tally (e.g. ``aiogram: 47``, ``sqlalchemy: 89``,
  ``pydantic: 23``) is the readable form of "where is the import
  weight concentrated". Pairs with /admin_modules — that card shows
  installed versions, this card shows actually-loaded ones.
* Builtin vs frozen vs file-backed split: a frozen-stdlib entry in
  ``sys.modules`` is a 3.11+ optimisation; their presence is a
  cheap confirmation that the runtime is doing the fast-import
  path. A frozen count of zero on Python 3.11+ is a regression
  signal — typically caused by ``-X frozen_modules=off`` or a
  custom builtins-only build.
* Late-import audit: handlers that lazy-import inside a function
  are visible here AFTER they've run at least once. An operator
  doing a smoke pass through the bot can refresh this card to see
  which modules have actually been touched in the live session.

Cross-references: /admin_pythonpath for the import resolution
order, /admin_modules for the dist-version side, /admin_flags for
``-X frozen_modules`` policy.

Silent-drop for non-devs, private-only at the router level. Same
posture as every other ``/admin_*``.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.imports")


# Top-level packages we show explicitly regardless of how big they
# are — the operator cares about the import weight of THESE in
# particular. Order is render order.
_LOAD_BEARING_PACKAGES: tuple[str, ...] = (
    "aiogram",
    "sqlalchemy",
    "aiosqlite",
    "pydantic",
    "dishka",
    "loguru",
    "telegram_invite_bot",
)


# How many of the heaviest non-load-bearing top-level packages to
# surface. 5 is enough to spot a surprise (e.g. ``numpy`` showing
# up because something dragged in pandas via a Pillow plugin)
# without making the card scrollable.
_HEAVY_PACKAGE_LIMIT = 5


# Threshold past which the total sys.modules count warrants a ⚠.
# Pure-stdlib CPython ≈ 150-200 modules. Our full pipeline pulls
# aiogram + sqla + pydantic + dishka + loguru ≈ 800-1200. 3000 is
# the "something dragged in too much" line — typically caused by
# an accidental ``import pandas`` or ``import torch`` in a handler.
_MODULES_TOTAL_CONCERNING = 3000


class _ImportsSnapshot:
    """Captured ``sys.modules`` census.

    ``top_level_counts`` is a Counter mapping the first dotted-name
    component (``aiogram.types`` → ``aiogram``) to the number of
    sub-modules currently imported. ``builtin_count`` / ``frozen_count``
    are computed at capture time by walking ``sys.modules.values()``
    once — re-walking per render branch would be wasteful and would
    make the snapshot non-pure.
    """

    __slots__ = (
        "builtin_count",
        "frozen_count",
        "load_bearing_counts",
        "top_level_counts",
        "total",
    )

    def __init__(
        self,
        *,
        total: int,
        top_level_counts: Counter[str],
        load_bearing_counts: dict[str, int],
        builtin_count: int,
        frozen_count: int,
    ) -> None:
        self.total = total
        self.top_level_counts = top_level_counts
        self.load_bearing_counts = load_bearing_counts
        self.builtin_count = builtin_count
        self.frozen_count = frozen_count


def _classify_module(mod: object) -> tuple[bool, bool]:
    """Return ``(is_builtin, is_frozen)`` for a sys.modules value.

    A module is "builtin" if its ``__spec__.origin`` is the literal
    string ``"built-in"`` (CPython convention) and "frozen" if origin
    is ``"frozen"``. Some sys.modules values are not modules at all
    (PEP 562 lazy attributes, custom sys.modules entries) — those
    classify as neither, which is fine.
    """
    spec = getattr(mod, "__spec__", None)
    if spec is None:
        return False, False
    origin = getattr(spec, "origin", None)
    return origin == "built-in", origin == "frozen"


def _capture() -> _ImportsSnapshot:
    """Sample sys.modules.

    Takes a snapshot of ``list(sys.modules.items())`` so concurrent
    imports during iteration (rare but possible — a handler that
    triggers an import while we render) don't blow the walk with a
    ``RuntimeError: dictionary changed size during iteration``.
    """
    items = list(sys.modules.items())
    top_level: Counter[str] = Counter()
    builtin = 0
    frozen = 0
    for name, mod in items:
        top = name.partition(".")[0]
        if top:
            top_level[top] += 1
        is_b, is_f = _classify_module(mod)
        if is_b:
            builtin += 1
        if is_f:
            frozen += 1

    load_bearing = {pkg: top_level.get(pkg, 0) for pkg in _LOAD_BEARING_PACKAGES}

    return _ImportsSnapshot(
        total=len(items),
        top_level_counts=top_level,
        load_bearing_counts=load_bearing,
        builtin_count=builtin,
        frozen_count=frozen,
    )


def _heavy_packages_excluding_load_bearing(
    snap: _ImportsSnapshot, *, limit: int = _HEAVY_PACKAGE_LIMIT
) -> list[tuple[str, int]]:
    """Top-N top-level packages by sub-module count, excluding the
    explicitly-listed load-bearing ones.

    The load-bearing packages are already rendered with their own
    counts, so re-listing them in the "heaviest" section would just
    push the actual surprises off the bottom of the card.
    """
    load_bearing_set = set(_LOAD_BEARING_PACKAGES)
    ranked = [
        (name, count)
        for name, count in snap.top_level_counts.most_common()
        if name not in load_bearing_set
    ]
    return ranked[:limit]


def _render(snap: _ImportsSnapshot) -> str:
    lines = ["📦 <b>sys.modules census</b>", ""]

    total_marker = " ⚠" if snap.total > _MODULES_TOTAL_CONCERNING else ""
    lines.append(f"  • <b>total:</b> <code>{snap.total}</code>{total_marker}")
    lines.append(f"  • <b>builtin:</b> <code>{snap.builtin_count}</code>")
    lines.append(f"  • <b>frozen:</b> <code>{snap.frozen_count}</code>")

    lines.append("")
    lines.append("  <b>load-bearing packages:</b>")
    for pkg in _LOAD_BEARING_PACKAGES:
        count = snap.load_bearing_counts.get(pkg, 0)
        if count == 0:
            # Not imported (yet) — informational, not a warning.
            # A handler that lazy-imports a dep won't show up here
            # until it runs, which is exactly the point.
            lines.append(f"    • <code>{pkg}</code>: <i>not imported</i>")
        else:
            lines.append(f"    • <code>{pkg}</code>: <code>{count}</code>")

    heavy = _heavy_packages_excluding_load_bearing(snap)
    if heavy:
        lines.append("")
        lines.append("  <b>heaviest other top-level packages:</b>")
        for name, count in heavy:
            lines.append(f"    • <code>{name}</code>: <code>{count}</code>")

    lines.append("")
    lines.append(
        f"<i>⚠ marker on total: above {_MODULES_TOTAL_CONCERNING} modules "
        f"indicates likely accidental heavy import (pandas / torch / "
        f"numpy dragged in via a plugin). Cross-check the heaviest "
        f"list and /admin_modules for the dist-version side. As "
        f"strangler completes the load-bearing count should fall.</i>"
    )
    return "\n".join(lines)


async def handle_admin_imports(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_imports; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_imports rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.imports")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_imports(message, settings)

    router.message.register(_entry, Command("admin_imports", ignore_case=True))
    return router
