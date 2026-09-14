"""Async repository for ``users.user_settings`` (Stage 26+).

One row per user; one column per preference. Each handler that needs a
column adds a focused ``get_<col>`` / ``set_<col>`` pair here — keeping
all writes against ``user_settings`` in a single class so no two call
sites can disagree on row-shape conventions (e.g. empty string vs NULL
for "cleared").

Those conventions were originally chosen to match the legacy telebot
monolith, which read the same table through raw ``sqlite3``. That
process is no longer running (``telegram-bot.service`` is disabled and
stopped on prod; this service is the sole writer), so the legacy
compatibility notes below are history, not a live constraint — they
explain why a convention looks the way it does, and are not a reason to
keep a convention that turns out to be wrong. See :meth:`set_city` for
one that was.

Stage 26: ``language``.
Stage 27: ``timezone``.
RR-6 #74: ``city`` — the one column with no legacy counterpart here
(legacy kept it in a JSON file), so it answers only to us.
#1926: ``current_group_id`` — the column legacy created and only ever
read from its DM group-management panel. It is the DM side of the
``grp_`` deep link (:mod:`~telegram_invite_bot.core.deep_links`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.user_settings import UserSetting

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class UserSettingsRepo:
    """``users.user_settings`` access. Constructed per request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_preferences(self, user_id: int) -> tuple[str | None, str | None]:
        """Return ``(language, timezone)`` for ``user_id`` in one query.

        Stage 31: batched read for the ``UserService.touch`` hot path —
        every command that calls ``touch`` previously paid two
        round-trips (``get_language`` + ``get_timezone``) against the
        same row. Folding them into a single ``SELECT`` halves the
        per-update SQL cost without coupling the writer side, which
        keeps its per-column ``set_language`` / ``set_timezone`` split
        (each column has different on-conflict semantics — see those
        methods for why).

        Single-column callers (e.g. ``UserService.get_timezone`` for
        ``/time``) keep using the focused getters below; this method
        is for the touch-time join where both fields are wanted at
        once.

        Empty-string ``timezone`` (legacy "cleared" wire format) is
        normalised to ``None`` here, same as :meth:`get_timezone`.
        """
        stmt = select(UserSetting.language, UserSetting.timezone).where(
            UserSetting.user_id == user_id
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return (None, None)
        lang, tz = row
        if tz is not None:
            tz = tz.strip() or None
        return (lang, tz)

    async def get_language(self, user_id: int) -> str | None:
        """Return the user's explicit language choice, or ``None`` if
        they never set one. Callers fall back to Telegram's
        ``language_code`` themselves — keeps the layering honest (this
        repo speaks only about ``user_settings``).
        """
        stmt = select(UserSetting.language).where(UserSetting.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_language(self, user_id: int, language: str) -> None:
        """UPSERT ``language``. Mirrors legacy ``set_user_language``
        (bot.py:41816) — same INSERT…ON CONFLICT DO UPDATE shape so an
        in-flight legacy reader sees identical row layout.

        Caller is responsible for validating ``language`` is ``ru`` or
        ``en``; we don't enforce here so future locales can land
        without touching this method.
        """
        stmt = sqlite_insert(UserSetting).values(user_id=user_id, language=language)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={"language": stmt.excluded.language},
        )
        await self._session.execute(stmt)
        # Deliberately NO ``session.flush()`` here — the
        # :class:`SessionMiddleware` owns the transaction boundary and
        # commits (which implies a flush) on handler success. Repos
        # that flush themselves leak the commit semantic and confuse
        # the cross-repo invariant ("either all writes in this update
        # land, or none do"). Compare with :class:`UsersRepo.upsert_from_telegram`,
        # which DOES flush — but only because the next line re-SELECTs
        # the row through the identity map and needs the INSERT visible.
        # We don't re-SELECT here, so the flush is pure overhead.

    async def get_timezone(self, user_id: int) -> str | None:
        """Return the user's stored IANA timezone (e.g. ``Europe/Moscow``)
        or ``None`` if unset / cleared.

        Legacy ``get_user_timezone`` (bot.py:41844) treated empty string
        as "no value" and wrote rows on that understanding. Those rows
        are still here, so the rule is mirrored: without it a cleared
        timezone would read back as the empty-string IANA name and every
        conversion built on it would fail.
        """
        stmt = select(UserSetting.timezone).where(UserSetting.user_id == user_id)
        result = await self._session.execute(stmt)
        tz = result.scalar_one_or_none()
        if tz is None:
            return None
        tz = tz.strip()
        return tz or None

    async def set_timezone(self, user_id: int, tz: str | None) -> None:
        """UPSERT ``timezone``. ``None`` writes the empty string, matching
        legacy ``set_user_timezone`` (bot.py:41859) — that's the wire
        format ``get_timezone`` (above) and the legacy reader both treat
        as "cleared". Switching to NULL here would silently desync the
        two readers for any in-flight legacy ``user_settings`` row.

        The INSERT branch writes ``language=NULL`` — see
        :meth:`set_city` for why that ``None`` is load-bearing and not
        an omission.
        """
        stmt = sqlite_insert(UserSetting).values(
            user_id=user_id,
            language=None,
            timezone=tz or "",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={"timezone": stmt.excluded.timezone},
        )
        await self._session.execute(stmt)
        # No ``flush()`` — same reasoning as :meth:`set_language`.

    async def get_city(self, user_id: int) -> str | None:
        """Return the user's saved home city, or ``None`` if unset (RR-6 #74).

        Both ``NULL`` and the empty string read as "no city", mirroring
        :meth:`get_timezone`. Two spellings for one state is not ideal,
        but it's the cheaper half of the trade: ``set_city(None)`` can
        then clear the value through the same UPSERT the setter uses,
        with no second statement and no row-existence branch.
        """
        stmt = select(UserSetting.city).where(UserSetting.user_id == user_id)
        city = (await self._session.execute(stmt)).scalar_one_or_none()
        if city is None:
            return None
        return city.strip() or None

    async def set_city(self, user_id: int, city: str | None) -> None:
        """UPSERT ``city``. ``None`` writes the empty string ("cleared").

        The caller validates: this method will happily store whatever
        string it is handed, including one long enough to blow up a
        message render. :func:`handlers.city.normalize_city` is the
        gate, and it runs before every call site. Keeping the check
        there rather than here means the *user-facing error text* and
        the rule that produced it sit in the same file.

        ``language=None`` on the INSERT branch is deliberate and must
        stay explicit. A row created by *this* method is a row for a
        user who has never touched ``/lang``, so its ``language`` has to
        read as "no choice made". Two ways to get that wrong:

        * Writing ``'ru'`` as a placeholder — which both this method and
          :meth:`set_timezone` used to do — makes
          :class:`LanguageMiddleware` see an explicit ``ru`` override
          (it accepts any value in ``("ru", "en")``) and switch an
          English user to Russian the moment its 5-minute cache lapses.
          The user never asked for that and has no idea which command
          did it.
        * *Omitting* the key is not the same as passing ``None``: the
          mapped column carries ``default="ru"``, which SQLAlchemy Core
          applies to any column absent from ``.values()``. Passing
          ``None`` explicitly is what actually suppresses it.

        The column is nullable in both the ORM model and the deployed
        schema (``language TEXT DEFAULT 'ru'`` — a default, not a
        constraint), so NULL is representable, and ``get_language``
        already returns ``None`` for it.
        """
        stmt = sqlite_insert(UserSetting).values(
            user_id=user_id,
            language=None,
            city=city or "",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={"city": stmt.excluded.city},
        )
        await self._session.execute(stmt)
        # No ``flush()`` — same reasoning as :meth:`set_language`.

    async def get_current_group(self, user_id: int) -> int | None:
        """The group this user's DM is currently "about", if any (#1926).

        Written by the ``grp_`` deep link — the group→DM buttons carry
        the chat they were tapped in, so anything group-scoped the user
        does next in the DM has a chat to attribute it to. ``None`` is
        the honest and common answer: someone who opened the DM directly
        never named a group, and the value is a convenience, never an
        authorisation. Whoever acts on it re-checks the caller against
        ``bot_groups`` — this column says *which* group, never *may
        they*.

        ``0`` reads as ``None``: no Telegram chat has that id, and it is
        what a row written by a path that stored a default rather than a
        real id would hold.
        """
        stmt = select(UserSetting.current_group_id).where(UserSetting.user_id == user_id)
        chat_id = (await self._session.execute(stmt)).scalar_one_or_none()
        return chat_id or None

    async def set_current_group(self, user_id: int, chat_id: int | None) -> None:
        """UPSERT ``current_group_id``; ``None`` clears it.

        NULL rather than the empty-string dance :meth:`set_timezone`
        performs — this column is an INTEGER, and legacy's own reader
        (``get_user_current_group``, bot.py:41872) already treats NULL
        as "no group".

        The caller validates that ``chat_id`` is a group the bot is
        actually in; this method stores what it is handed. See
        :meth:`set_city` for why ``language=None`` on the INSERT branch
        is load-bearing rather than an omission.
        """
        stmt = sqlite_insert(UserSetting).values(
            user_id=user_id,
            language=None,
            current_group_id=chat_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={"current_group_id": stmt.excluded.current_group_id},
        )
        await self._session.execute(stmt)
        # No ``flush()`` — same reasoning as :meth:`set_language`.
