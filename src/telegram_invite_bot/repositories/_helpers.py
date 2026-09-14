"""Shared low-level repo helpers.

Cross-repo SQL utilities that don't justify a base class yet. Two
repos (:class:`UsersRepo`, :class:`EconomyRepo`) duplicated the same
identity-map-aware existence-probe + reload pattern with identical
WHY comments; this module centralises the trick so the next repo that
needs UPSERT-with-reload can pick the helpers up instead of
re-deriving them.

If a third UPSERT-with-reload site lands we should consider promoting
this to a ``BaseRepo`` mixin, but as long as the call shape stays
"existence probe + UPSERT + reload" against a single PK column,
module-level helpers keep mypy honest without inheritance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql.elements import ColumnElement


def legacy_status_active(
    status_col: InstrumentedAttribute[str | None],
) -> ColumnElement[bool]:
    """Predicate for "row is active" under legacy NULL-is-active semantics.

    Both ``users.marriages.status`` and ``users.relationships.status`` were
    added by a later migration; rows written by the original schema have
    ``status IS NULL`` and must be treated as ``'active'`` (see
    ``bot.py:22991`` and ``bot.py:23489``). New rows get an explicit
    ``'active'`` / ``'divorced'`` / ``'pending_restore'`` value.

    Two repos repeated the same ``or_(col.is_(None), col == "active")``
    inline; centralising it here means the eventual NULL→explicit
    backfill only has to touch one place to flip the semantics.
    """
    return or_(status_col.is_(None), status_col == "active")


async def row_exists(
    session: AsyncSession,
    pk_column: InstrumentedAttribute[Any],
    pk_value: Any,
) -> bool:
    """Identity-map-bypassing existence probe.

    Why ``func.count()`` instead of ``session.get(Model, pk_value)``:
    ``session.get`` populates the identity map with the pre-UPSERT row,
    which then *shadows* the row a subsequent UPSERT writes. The later
    re-select returns the stale cached values (``last_seen`` from
    before the touch, etc.) — silently breaking the "advance
    timestamps on every touch" contract. Counting from the underlying
    table sidesteps the identity map entirely.

    Returns ``True`` when at least one row matches ``pk_column == pk_value``.
    """
    stmt = select(func.count()).select_from(pk_column.parent).where(pk_column == pk_value)
    result = await session.execute(stmt)
    return (result.scalar() or 0) > 0


async def reload_after_upsert(
    session: AsyncSession,
    pk_column: InstrumentedAttribute[Any],
    pk_value: Any,
) -> Any:
    """Drop identity-map cache and re-SELECT the row by PK.

    Pair with an UPSERT against the same table: after ``flush()`` the
    DB has the new row, but any prior ``session.get`` / ORM-attribute
    access has cached the *old* values. ``expire_all`` invalidates the
    cache; the SELECT then fetches the fresh row. Caller maps it to an
    entity (we return the raw ORM row because each repo has its own
    ``_to_entity`` shape).
    """
    await session.flush()
    session.expire_all()
    model = pk_column.parent.entity
    result = await session.execute(select(model).where(pk_column == pk_value))
    return result.scalar_one()
