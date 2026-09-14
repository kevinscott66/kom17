"""Async repository for ``economy.runtime_secrets`` — runtime-settable
provider credentials (T-027).

One row per key (e.g. ``CRYPTO_PAY_TOKEN``). Minimal CRUD: read a value
(for the payment secret resolver), upsert a value (the admin
``/set_crypto_token`` panel action), and clear it (revert to the
``.env`` fallback). The repo trusts its arguments — dev-only gating and
token-shape validation live in the handler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.economy import RuntimeSecret

if TYPE_CHECKING:
    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


class RuntimeSecretsRepo:
    """``economy.runtime_secrets`` access — get / upsert / clear."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> str | None:
        """Return the stored value for ``key``, or ``None`` if unset."""
        result = await self._session.execute(
            select(RuntimeSecret.value).where(RuntimeSecret.key == key)
        )
        return result.scalar_one_or_none()

    async def upsert(self, key: str, value: str, *, updated_by: int | None = None) -> None:
        """Insert or replace the value for ``key``.

        ``INSERT ... ON CONFLICT(key) DO UPDATE`` on the primary key —
        one statement, no read-then-write race. ``updated_at`` is a
        Python naive-UTC timestamp (same convention as the rest of the
        economy writes) so the admin status card can show recency.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        stmt = sqlite_insert(RuntimeSecret).values(
            key=key, value=value, updated_at=now, updated_by=updated_by
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[RuntimeSecret.key],
            set_={"value": value, "updated_at": now, "updated_by": updated_by},
        )
        await self._session.execute(stmt)

    async def clear(self, key: str) -> bool:
        """Delete the row for ``key``. Returns ``True`` if a row existed."""
        result = await self._session.execute(delete(RuntimeSecret).where(RuntimeSecret.key == key))
        return cast("CursorResult[Any]", result).rowcount > 0
