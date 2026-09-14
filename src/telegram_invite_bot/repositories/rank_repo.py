"""Async repository for the moderation.db rank tables (ranks epic R1).

Two delta-only override stores over the in-code defaults declared in
:mod:`telegram_invite_bot.core.ranks`:

* permission matrix — :meth:`RankRepo.merged_matrix` returns
  ``DEFAULT_RANK_PERMISSIONS`` overlaid with ``rank_permissions`` rows;
  :meth:`RankRepo.set_permission` upserts one (rank, permission) cell.
* command access — :meth:`RankRepo.command_overrides` returns the
  ``command_rank_overrides`` rows as a dict; set/reset mutate them.
  Defaults stay in :data:`~telegram_invite_bot.core.ranks
  .COMMAND_CATALOG` and are merged at the call site
  (:func:`~telegram_invite_bot.core.ranks.default_min_rank`).

Both reads sit on the hot path of EVERY gated command, so they are
cached module-level with a 300s TTL — the same pattern (module dict +
``time.monotonic`` expiry + an explicit ``clear``/invalidate hook for
writers and tests) as :mod:`telegram_invite_bot.middlewares.language`.

**Invalidation is the CALLER's job, after the transaction commits.**
The writers below deliberately do not clear the caches themselves: a
clear issued inside an open transaction opens a window in which the row
is not yet visible to anybody else, so a concurrent update — updates run
concurrently under both runners — misses the emptied cache, reads the
PRE-write row through its own session and re-fills the cache with the
stale value plus a fresh 300s TTL. ``/cmdcfg set <cmd> 6`` (the kill
switch) would then report success while the command kept running for up
to five more minutes. Call ``clear_rank_matrix_cache`` /
``clear_command_override_cache`` after the ``session_for`` block exits —
the pattern ``RankService.set_rank`` already uses for the per-user rank
cache (services/rank_service.py:210-212).

The repo stays dumb: no permission-name or rank-range validation here —
that lives in the R2 handlers (and ``core.ranks`` exposes
``ALL_PERMISSION_KEYS`` / ``MIN/MAX_SETTABLE_RANK`` for it).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.core.ranks import DEFAULT_RANK_PERMISSIONS
from telegram_invite_bot.db.models.rank_tables import (
    CommandRankOverride,
    RankPermissionOverride,
)
from telegram_invite_bot.utils.cache_generation import CacheGeneration

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

CACHE_TTL_SECONDS = 300.0

# Module-level caches (one process, one logical matrix) — see the
# language-middleware rationale: module scope lets handlers/tests
# invalidate without threading repo instances through DI.
_MATRIX_CACHE: dict[None, tuple[dict[int, dict[str, bool]], float]] = {}
_COMMAND_CACHE: dict[None, tuple[dict[str, int], float]] = {}

# #1942: both caches are filled after an ``await``, and both writers
# invalidate right after they commit — ``/perm`` through
# ``clear_rank_matrix_cache``, ``/cmdcfg`` through
# ``clear_command_override_cache``. An invalidation landing inside that
# window used to be undone by the fill it raced, restoring the OLD
# matrix for the full TTL. See ``utils.cache_generation``.
_MATRIX_GENERATION = CacheGeneration()
_COMMAND_GENERATION = CacheGeneration()


def clear_rank_matrix_cache() -> None:
    """Drop the cached merged permission matrix (writers + tests)."""
    _MATRIX_CACHE.clear()
    _MATRIX_GENERATION.bump()


def clear_command_override_cache() -> None:
    """Drop the cached command-override map (writers + tests)."""
    _COMMAND_CACHE.clear()
    _COMMAND_GENERATION.bump()


def command_override_generation() -> CacheGeneration:
    """The counter :func:`clear_command_override_cache` bumps.

    Exposed because the override map is cached in TWO places, not one.
    ``handlers/command_access.py`` keeps its own copy of the very same
    dict — in memory and on disk — as the fallback its kill switch is
    enforced from when ``moderation.db`` is unreadable, and it fills
    that copy after an ``await`` exactly like the cache above. It
    therefore needs the same before/after check (#1977); handing it the
    counter is cheaper than giving it a second one to keep in step.
    """
    return _COMMAND_GENERATION


class RankRepo:
    """moderation.db rank tables. Constructed per request with an open
    session (same contract as every other repo in this package)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- permission matrix -------------------------------------------------

    async def permission_overrides(self) -> dict[int, dict[str, bool]]:
        """Raw override rows, ``{rank: {permission: allowed}}`` (uncached)."""
        rows = (await self._session.execute(select(RankPermissionOverride))).scalars()
        out: dict[int, dict[str, bool]] = {}
        for row in rows:
            out.setdefault(row.rank, {})[row.permission] = bool(row.allowed)
        return out

    async def merged_matrix(self) -> dict[int, dict[str, bool]]:
        """Defaults overlaid with DB overrides; cached for 300s.

        Returns a deep copy per cache fill so callers can't mutate the
        in-code defaults through the result. Ranks with neither
        defaults nor overrides (0, -1) are simply absent — the lookup
        contract is ``matrix.get(rank, {}).get(perm, False)``, matching
        legacy ``rank_permissions.get(str(level), {})`` (bot.py:6618).
        """
        now = time.monotonic()
        cached = _MATRIX_CACHE.get(None)
        if cached is not None and cached[1] > now:
            return cached[0]
        generation = _MATRIX_GENERATION.snapshot()
        merged = {rank: dict(perms) for rank, perms in DEFAULT_RANK_PERMISSIONS.items()}
        for rank, perms in (await self.permission_overrides()).items():
            merged.setdefault(rank, {}).update(perms)
        if _MATRIX_GENERATION.unchanged(generation):
            _MATRIX_CACHE[None] = (merged, now + CACHE_TTL_SECONDS)
        return merged

    async def set_permission(self, rank: int, permission: str, allowed: bool) -> None:
        """Upsert one matrix cell.

        The caller invalidates with :func:`clear_rank_matrix_cache`
        after committing — see the module docstring for why not here.
        """
        stmt = sqlite_insert(RankPermissionOverride).values(
            rank=rank, permission=permission, allowed=int(allowed)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["rank", "permission"],
            set_={"allowed": stmt.excluded.allowed},
        )
        await self._session.execute(stmt)

    # -- command access ----------------------------------------------------

    async def command_overrides(self) -> dict[str, int]:
        """All command min-rank overrides, ``{command_key: min_rank}``;
        cached for 300s."""
        now = time.monotonic()
        cached = _COMMAND_CACHE.get(None)
        if cached is not None and cached[1] > now:
            return cached[0]
        generation = _COMMAND_GENERATION.snapshot()
        rows = (await self._session.execute(select(CommandRankOverride))).scalars()
        overrides = {row.command_key: row.min_rank for row in rows}
        if _COMMAND_GENERATION.unchanged(generation):
            _COMMAND_CACHE[None] = (overrides, now + CACHE_TTL_SECONDS)
        return overrides

    async def set_command_override(self, command_key: str, min_rank: int) -> None:
        """Upsert one command's minimum rank.

        The caller invalidates with
        :func:`clear_command_override_cache` after committing — see the
        module docstring for why not here.
        """
        stmt = sqlite_insert(CommandRankOverride).values(command_key=command_key, min_rank=min_rank)
        stmt = stmt.on_conflict_do_update(
            index_elements=["command_key"],
            set_={"min_rank": stmt.excluded.min_rank},
        )
        await self._session.execute(stmt)

    async def reset_command_override(self, command_key: str) -> bool:
        """Drop one command's override (back to the catalog default).

        Returns True when a row was actually deleted. The caller
        invalidates the cache after committing.
        """
        result = await self._session.execute(
            delete(CommandRankOverride).where(CommandRankOverride.command_key == command_key)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def reset_all_command_overrides(self) -> int:
        """Drop every command override; returns the number deleted.

        The caller invalidates the cache after committing.

        ``command_key`` is the primary key, so ``is_not(None)`` matches
        every row — the predicate exists purely to give the statement a
        WHERE clause. Without one this is an unbounded DELETE, which
        :func:`telegram_invite_bot.db.safety.install` raises
        ``UnboundedWriteError`` on under ``APP_ENV=prod``; the
        ``/cmdcfg reset all`` handler catches that broadly and replies
        with a bland save-failure, so the developer's only escape from a
        wedged override table could never succeed in production.
        ``allow_unbounded_writes()`` would silence the guard instead of
        satisfying it, and its own docstring asks new code to spell the
        bound out here.
        """
        result = await self._session.execute(
            delete(CommandRankOverride).where(CommandRankOverride.command_key.is_not(None))
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)
