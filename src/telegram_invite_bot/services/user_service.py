"""User-facing application service.

Thin layer over :class:`UsersRepo` — exists so handlers don't construct
repos themselves and so cross-table workflows (e.g. ``user + economy``
on first registration in later stages) have an obvious home.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram.types import User as TelegramUser

    from telegram_invite_bot.core.entities.user import User as UserEntity
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo


class UserService:
    def __init__(
        self,
        repo: UsersRepo,
        settings_repo: UserSettingsRepo | None = None,
    ) -> None:
        self._repo = repo
        # ``settings_repo`` is optional so unit tests that only exercise
        # ``touch`` (e.g. early-stage start/profile fixtures) don't have
        # to wire a second repo. Production always passes both via
        # :class:`SessionMiddleware`. ``set_language`` raises if it's
        # missing — better a loud error than a silent no-op on
        # ``/lang`` clicks.
        self._settings_repo = settings_repo

    async def touch(self, tg_user: TelegramUser) -> UserEntity:
        """Upsert Telegram-supplied user metadata; return the merged entity.

        Called from ``/start`` and any handler that wants to keep
        ``last_seen`` fresh. Legacy equivalent: ``register_user +
        update_user_info`` from ``bot.py``.

        Stage 26: also reads ``user_settings.language`` and stamps it
        onto the returned entity as ``language_override``. Doing the
        join here means every renderer that already calls ``touch``
        starts honouring the user's ``/lang`` choice for free; the
        alternative (each handler asks the settings repo separately)
        would multiply the round-trip and make it easy to forget.
        """
        user = await self._repo.upsert_from_telegram(
            user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            language_code=tg_user.language_code,
            is_premium=bool(tg_user.is_premium),
        )
        if self._settings_repo is None:
            return user
        # Stage 31: one SELECT for both preference columns instead of
        # two. The per-column getters still exist for single-field
        # callers (``/time`` reads only ``timezone``); ``touch`` is the
        # join point where both are wanted at once, so the batched
        # method earns its keep here.
        override, tz = await self._settings_repo.get_preferences(tg_user.id)
        return replace(user, language_override=override, timezone=tz)

    async def set_language(self, user_id: int, language: str) -> str:
        """Persist the user's explicit language choice. Returns the
        normalised value actually stored (``ru`` / ``en``).

        Normalisation mirrors legacy ``set_user_language``
        (bot.py:41818): only ``"en"`` becomes ``en``; everything else
        clamps to ``ru``. Keeps the column from filling with junk if a
        future callback typo slips through.
        """
        if self._settings_repo is None:
            raise RuntimeError(
                "UserService.set_language requires a UserSettingsRepo. "
                "Wire it via SessionMiddleware."
            )
        normalised = "en" if language == "en" else "ru"
        await self._settings_repo.set_language(user_id, normalised)
        return normalised

    async def get_timezone(self, user_id: int) -> str | None:
        """Return the user's stored timezone or ``None`` if unset."""
        if self._settings_repo is None:
            return None
        return await self._settings_repo.get_timezone(user_id)

    async def set_timezone(self, user_id: int, tz: str | None) -> None:
        """Persist the user's timezone. ``None`` clears it.

        Validation (does this IANA name resolve?) is the caller's
        responsibility — :func:`telegram_invite_bot.utils.time.format_local_time`
        is the natural probe and the ``/timezone`` handler runs it
        BEFORE this method. Validating here would force the repo layer
        to import ``zoneinfo`` for one ``except`` clause.
        """
        if self._settings_repo is None:
            raise RuntimeError(
                "UserService.set_timezone requires a UserSettingsRepo. "
                "Wire it via SessionMiddleware."
            )
        await self._settings_repo.set_timezone(user_id, tz)

    async def get_city(self, user_id: int) -> str | None:
        """Return the user's saved home city, or ``None`` (RR-6 #74).

        Degrades to ``None`` without a settings repo, matching
        :meth:`get_timezone` — the readers (``/weather``, ``/forecast``)
        treat "no saved city" as a normal state and prompt for one, so a
        missing repo costs a prompt, not an error.
        """
        if self._settings_repo is None:
            return None
        return await self._settings_repo.get_city(user_id)

    async def set_city(self, user_id: int, city: str | None) -> None:
        """Persist the user's home city. ``None`` clears it.

        Validation belongs to the caller — see
        :func:`telegram_invite_bot.handlers.city.normalize_city`. Unlike
        :meth:`get_city`, a missing repo raises: a setter that silently
        discards the user's input is the worst of both worlds (they see
        a confirmation and lose the value). Same posture as
        :meth:`set_language` / :meth:`set_timezone`.
        """
        if self._settings_repo is None:
            raise RuntimeError(
                "UserService.set_city requires a UserSettingsRepo. Wire it via SessionMiddleware."
            )
        await self._settings_repo.set_city(user_id, city)
