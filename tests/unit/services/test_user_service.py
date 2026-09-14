"""``UserService.touch`` delegates to the repo with Telegram fields."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from aiogram.types import User as TelegramUser

from telegram_invite_bot.core.entities.user import User as UserEntity
from telegram_invite_bot.services.user_service import UserService


class _RecordingRepo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def upsert_from_telegram(self, **kwargs: Any) -> UserEntity:
        self.calls.append(kwargs)
        return UserEntity(
            user_id=kwargs["user_id"],
            username=kwargs["username"],
            first_name=kwargs["first_name"],
            last_name=kwargs["last_name"],
            language_code=kwargs["language_code"],
            is_premium=kwargs["is_premium"],
            joined_date=datetime(2024, 1, 1),
            last_seen=datetime(2024, 1, 1),
            last_active=datetime(2024, 1, 1),
            is_new=True,
        )


async def test_touch_forwards_telegram_fields() -> None:
    repo = _RecordingRepo()
    service = UserService(repo)  # type: ignore[arg-type]
    tg_user = TelegramUser(
        id=42,
        is_bot=False,
        first_name="Alice",
        last_name="Smith",
        username="alice",
        language_code="en",
        is_premium=True,
    )
    user = await service.touch(tg_user)
    assert user.user_id == 42
    assert user.language == "en"
    assert repo.calls == [
        {
            "user_id": 42,
            "username": "alice",
            "first_name": "Alice",
            "last_name": "Smith",
            "language_code": "en",
            "is_premium": True,
        }
    ]


async def test_touch_treats_missing_premium_as_false() -> None:
    repo = _RecordingRepo()
    service = UserService(repo)  # type: ignore[arg-type]
    tg_user = TelegramUser(id=1, is_bot=False, first_name="Bob")
    await service.touch(tg_user)
    assert repo.calls[0]["is_premium"] is False
    assert repo.calls[0]["language_code"] is None


# ── No-settings-repo branches ────────────────────────────────────────────
#
# Production always wires both repos via SessionMiddleware, but the
# ``settings_repo`` parameter is optional so single-table test fixtures
# (the ones above) don't have to construct one. That optionality means
# three guard branches exist on the service: ``set_language`` raises,
# ``get_timezone`` returns None, ``set_timezone`` raises. The
# loud-vs-silent split is intentional — writes that lose data must
# scream, reads that have no data can answer "no data". The tests below
# lock both halves of that contract.


async def test_set_language_without_repo_raises_runtime_error() -> None:
    service = UserService(_RecordingRepo())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="UserSettingsRepo"):
        await service.set_language(user_id=1, language="ru")


async def test_get_timezone_without_repo_returns_none() -> None:
    """Read path: missing settings repo ⇒ "no timezone known", not a
    crash. Callers (``/time``, ``/timezone status``) treat ``None`` as
    "user hasn't picked one" and render the no-tz prompt.
    """
    service = UserService(_RecordingRepo())  # type: ignore[arg-type]
    assert await service.get_timezone(user_id=1) is None


async def test_set_timezone_without_repo_raises_runtime_error() -> None:
    service = UserService(_RecordingRepo())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="UserSettingsRepo"):
        await service.set_timezone(user_id=1, tz="Europe/Moscow")
