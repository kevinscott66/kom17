"""``RuntimeSecretsRepo`` — the runtime-settable secret store (T-027).

Pins the minimal CRUD (get / upsert / clear) the payment secret
resolver and the ``/set_crypto_token`` admin panel action rely on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.repositories.runtime_secrets_repo import RuntimeSecretsRepo
from tests.integration.repositories._session import build_session

_KEY = "CRYPTO_PAY_TOKEN"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


async def test_get_missing_returns_none(session: AsyncSession) -> None:
    assert await RuntimeSecretsRepo(session).get(_KEY) is None


async def test_upsert_then_get(session: AsyncSession) -> None:
    repo = RuntimeSecretsRepo(session)
    await repo.upsert(_KEY, "12345:AAtoken", updated_by=100)
    await session.commit()
    assert await repo.get(_KEY) == "12345:AAtoken"


async def test_upsert_replaces_existing(session: AsyncSession) -> None:
    """Re-set overwrites the single per-key row, not a second insert."""
    repo = RuntimeSecretsRepo(session)
    await repo.upsert(_KEY, "old", updated_by=100)
    await repo.upsert(_KEY, "new", updated_by=200)
    await session.commit()
    assert await repo.get(_KEY) == "new"


async def test_clear_removes_value(session: AsyncSession) -> None:
    repo = RuntimeSecretsRepo(session)
    await repo.upsert(_KEY, "tok", updated_by=100)
    await session.commit()
    existed = await repo.clear(_KEY)
    await session.commit()
    assert existed is True
    assert await repo.get(_KEY) is None


async def test_clear_missing_returns_false(session: AsyncSession) -> None:
    repo = RuntimeSecretsRepo(session)
    existed = await repo.clear(_KEY)
    await session.commit()
    assert existed is False
