"""Async repository for the per-group welcome template (L-57).

Backs the ``welcome_config`` table in ``moderation.db``. Three operations,
each session-scoped and ORM-only (no inline SQL):

* :meth:`get` — load the row for a group (``None`` if never configured).
* :meth:`set_template` — upsert the template text and (re)enable it.
* :meth:`set_enabled` — flip the on/off toggle without touching the text.
* :meth:`clear` — drop the stored template entirely.

The repo trusts its arguments; placeholder/HTML-escaping policy lives in
the handler (the stored template is the operator's literal text).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update

from telegram_invite_bot.db.models.welcome_config import WelcomeConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class WelcomeConfigRow:
    """Domain view of a single ``welcome_config`` row."""

    group_id: int
    template: str | None
    enabled: bool


class WelcomeConfigRepo:
    """``welcome_config`` access. Constructed per request with an open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, group_id: int) -> WelcomeConfigRow | None:
        """Return the config row for ``group_id`` or ``None`` if unset."""
        stmt = select(WelcomeConfig).where(WelcomeConfig.group_id == group_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return WelcomeConfigRow(
            group_id=row.group_id,
            template=row.template,
            enabled=bool(row.enabled),
        )

    async def set_template(self, group_id: int, template: str) -> None:
        """Upsert ``template`` for ``group_id`` and (re)enable it.

        Setting a template implies the admin wants it live, so ``enabled``
        is forced ``True`` here — a prior ``/welcome_off`` does not silently
        suppress a freshly-set template.
        """
        existing = (
            await self._session.execute(
                select(WelcomeConfig).where(WelcomeConfig.group_id == group_id)
            )
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(WelcomeConfig(group_id=group_id, template=template, enabled=True))
        else:
            await self._session.execute(
                update(WelcomeConfig)
                .where(WelcomeConfig.group_id == group_id)
                .values(template=template, enabled=True)
            )

    async def set_enabled(self, group_id: int, enabled: bool) -> None:
        """Toggle the welcome template on/off for ``group_id``.

        Creates a row (with no template) if none exists yet, so that
        ``/welcome_off`` before any ``/setwelcome`` is recorded rather than
        being a silent no-op — the toggle then governs a later template.
        """
        existing = (
            await self._session.execute(
                select(WelcomeConfig).where(WelcomeConfig.group_id == group_id)
            )
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(WelcomeConfig(group_id=group_id, template=None, enabled=enabled))
        else:
            await self._session.execute(
                update(WelcomeConfig)
                .where(WelcomeConfig.group_id == group_id)
                .values(enabled=enabled)
            )

    async def clear(self, group_id: int) -> None:
        """Delete any stored config for ``group_id`` (back to default card)."""
        await self._session.execute(delete(WelcomeConfig).where(WelcomeConfig.group_id == group_id))
