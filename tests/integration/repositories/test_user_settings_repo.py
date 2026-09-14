"""``UserSettingsRepo`` — focused integration tests.

The handler-level e2e suite (test_language.py, test_timezone.py)
already covers the typical write→read paths transitively, but those
tests can't distinguish "batched read returns the right tuple" from
"two separate reads happen to agree on the same row". This file pins
the per-method contract so a future regression in
:meth:`get_preferences` (Stage 31) — e.g. accidentally returning
``(timezone, language)`` after a refactor — surfaces here loudly
instead of via a half-broken downstream renderer.

What we test:

* ``get_preferences`` on an unknown user → ``(None, None)``.
* Empty-string timezone (legacy "cleared" wire format) normalises to
  ``None`` in the tuple — same contract as the focused
  :meth:`get_timezone` getter.
* Tuple ordering is ``(language, timezone)``. A swap would break every
  handler that calls ``user_service.touch``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.user_settings import UserSetting
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from tests.integration.repositories._session import build_session


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def _ensure_user(session: AsyncSession, user_id: int) -> None:
    """FK target for user_settings — minimal row, no metadata."""
    session.add(User(user_id=user_id))
    await session.flush()


async def test_get_preferences_unknown_user_is_double_none(
    session: AsyncSession,
) -> None:
    repo = UserSettingsRepo(session)
    assert await repo.get_preferences(99999) == (None, None)


async def test_get_preferences_returns_lang_then_tz(
    session: AsyncSession,
) -> None:
    """Ordering matters — ``UserService.touch`` unpacks via
    ``override, tz = ...``. A swap would silently put a tz string into
    ``language_override`` and the next entity render would treat
    ``Europe/Moscow`` as a language code.
    """
    await _ensure_user(session, 1)
    session.add(UserSetting(user_id=1, language="en", timezone="Europe/Berlin"))
    await session.flush()
    repo = UserSettingsRepo(session)
    assert await repo.get_preferences(1) == ("en", "Europe/Berlin")


async def test_get_preferences_normalises_empty_tz_to_none(
    session: AsyncSession,
) -> None:
    """Legacy writes empty string for a cleared tz; the batched getter
    must surface ``None`` so consumers don't have to know that wire
    format. Mirrors :meth:`get_timezone`'s contract.
    """
    await _ensure_user(session, 2)
    session.add(UserSetting(user_id=2, language="ru", timezone=""))
    await session.flush()
    repo = UserSettingsRepo(session)
    lang, tz = await repo.get_preferences(2)
    assert lang == "ru"
    assert tz is None


async def test_get_preferences_no_settings_row_is_double_none(
    session: AsyncSession,
) -> None:
    """User exists in ``users`` but has no ``user_settings`` row yet
    (the common case before they ever run /lang or /timezone).
    Mustn't error — the touch() join needs to tolerate the missing
    row gracefully.
    """
    await _ensure_user(session, 3)
    repo = UserSettingsRepo(session)
    assert await repo.get_preferences(3) == (None, None)


# --- the language placeholder (RR-6 #74) ------------------------------------


@pytest.mark.parametrize("setter", ["timezone", "city", "current_group"])
async def test_creating_a_row_does_not_invent_a_language_choice(
    session: AsyncSession,
    setter: str,
) -> None:
    """REGRESSION PIN. Both setters create the ``user_settings`` row when
    it doesn't exist yet, and both used to seed ``language='ru'`` as a
    placeholder. :class:`LanguageMiddleware` accepts any stored value in
    ``("ru", "en")`` as an explicit ``/lang`` choice — so an English
    user's first ever ``/timezone`` or ``/city`` silently switched them
    to Russian once the middleware's 5-minute cache lapsed.

    A row this method creates must read as "no language chosen".

    Note the failure mode this pins is subtle in *both* directions:
    dropping ``language`` from ``.values()`` would not fix it either,
    because the mapped column carries ``default="ru"`` and SQLAlchemy
    Core applies that to absent columns. Only the explicit ``None``
    works.
    """
    await _ensure_user(session, 4)
    repo = UserSettingsRepo(session)
    if setter == "timezone":
        await repo.set_timezone(4, "Europe/Moscow")
    elif setter == "city":
        await repo.set_city(4, "Krasnodar")
    else:
        await repo.set_current_group(4, -1001)
    await session.flush()

    assert await repo.get_language(4) is None


async def test_setters_never_clobber_an_existing_language(
    session: AsyncSession,
) -> None:
    """The other half of the contract: a user who HAS chosen a language
    keeps it across preference writes. ``on_conflict_do_update`` touches
    only the one column, so the ``None`` above never reaches an existing
    row.
    """
    await _ensure_user(session, 5)
    repo = UserSettingsRepo(session)
    await repo.set_language(5, "en")
    await repo.set_city(5, "London")
    await repo.set_timezone(5, "Europe/London")
    await repo.set_current_group(5, -1005)
    await session.flush()

    assert await repo.get_language(5) == "en"
    assert await repo.get_city(5) == "London"
    assert await repo.get_current_group(5) == -1005
    assert await repo.get_preferences(5) == ("en", "Europe/London")


async def test_city_round_trip_and_clear(session: AsyncSession) -> None:
    """``None`` in, ``None`` out — via the empty-string wire format the
    timezone column already uses."""
    await _ensure_user(session, 6)
    repo = UserSettingsRepo(session)
    assert await repo.get_city(6) is None

    await repo.set_city(6, "Санкт-Петербург")
    await session.flush()
    assert await repo.get_city(6) == "Санкт-Петербург"

    await repo.set_city(6, None)
    await session.flush()
    assert await repo.get_city(6) is None


async def test_current_group_round_trip_and_clear(session: AsyncSession) -> None:
    """#1926: the DM side of the ``grp_`` deep link.

    NULL, not the empty-string dance the text columns use — the column
    is an INTEGER, and legacy's own reader (``get_user_current_group``,
    bot.py:41872) already reads NULL as "no group".
    """
    await _ensure_user(session, 7)
    repo = UserSettingsRepo(session)
    assert await repo.get_current_group(7) is None

    await repo.set_current_group(7, -1001234567890)
    await session.flush()
    assert await repo.get_current_group(7) == -1001234567890

    # Re-tapping a different group's button moves the pointer.
    await repo.set_current_group(7, -1009)
    await session.flush()
    assert await repo.get_current_group(7) == -1009

    await repo.set_current_group(7, None)
    await session.flush()
    assert await repo.get_current_group(7) is None


async def test_current_group_zero_reads_as_no_group(session: AsyncSession) -> None:
    """No Telegram chat has id 0, so a stored zero is a row written by a
    path that saved a default instead of an id. It must not read back as
    a group anything can be credited to."""
    await _ensure_user(session, 8)
    session.add(UserSetting(user_id=8, language=None, current_group_id=0))
    await session.flush()

    assert await UserSettingsRepo(session).get_current_group(8) is None
