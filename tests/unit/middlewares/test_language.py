"""Unit tests for the effective-language resolver + middleware.

Locks the precedence contract the whole cluster depends on:

    stored ``user.language`` > Telegram ``language_code`` > ``"ru"``

plus the per-user TTL cache and its ``/lang`` invalidation hook, and the
``resolve_lang`` fallback helper handlers use when the middleware may not
have run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_invite_bot.core.entities.user import User as UserEntity
from telegram_invite_bot.middlewares import language as lang_mw
from telegram_invite_bot.middlewares.language import (
    LanguageMiddleware,
    best_effort_language_for_user,
    invalidate_language_cache,
    language_for_user,
)
from telegram_invite_bot.utils.language import lang_from_code, resolve_lang


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    lang_mw._CACHE.clear()


def _entity(*, language_code: str | None, override: str | None) -> UserEntity:
    return UserEntity(
        user_id=1,
        username=None,
        first_name=None,
        last_name=None,
        language_code=language_code,
        is_premium=False,
        joined_date=None,
        last_seen=None,
        last_active=None,
        is_new=False,
        language_override=override,
    )


def _registry_returning(user: UserEntity | None) -> MagicMock:
    """Mock EngineRegistry whose users session yields a repo returning ``user``."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    sessionmaker = MagicMock(return_value=session)
    registry = MagicMock()
    registry.session.return_value = sessionmaker
    # UsersRepo(session).get(uid) -> user. Patch the class call site below.
    return registry, session  # type: ignore[return-value]


def _settings_repo_for(user: UserEntity | None) -> MagicMock:
    """Mock UserSettingsRepo: get_language returns the entity's override.

    Mirrors prod: the explicit /lang choice lives in user_settings, so the
    middleware queries it FIRST; the UsersRepo entity is only the fallback.
    """
    sr = MagicMock()
    sr.get_language = AsyncMock(return_value=(user.language_override if user is not None else None))
    return sr


def _tg_user(uid: int = 1, language_code: str | None = None) -> MagicMock:
    u = MagicMock()
    u.id = uid
    u.language_code = language_code
    return u


# --- lang_from_code -------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("en", "en"),
        ("en-US", "en"),
        ("EN", "en"),
        ("ru", "ru"),
        ("uk", "ru"),
        ("de", "ru"),
        ("", "ru"),
        (None, "ru"),
    ],
)
def test_lang_from_code(code: str | None, expected: str) -> None:
    assert lang_from_code(code) == expected


# --- precedence (stored > code > default) ---------------------------------


@pytest.mark.asyncio
async def test_stored_override_wins_over_language_code(monkeypatch: Any) -> None:
    # Stored override "ru" must beat an English client locale.
    user = _entity(language_code="en-US", override="ru")
    registry, _session = _registry_returning(user)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=user)
    monkeypatch.setattr(lang_mw, "UsersRepo", lambda _s: repo)
    monkeypatch.setattr(lang_mw, "UserSettingsRepo", lambda _s: _settings_repo_for(user))

    mw = LanguageMiddleware(registry)
    assert await mw._resolve(_tg_user(language_code="en-US")) == "ru"


@pytest.mark.asyncio
async def test_falls_back_to_language_code_when_no_stored_row(monkeypatch: Any) -> None:
    registry, _session = _registry_returning(None)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    monkeypatch.setattr(lang_mw, "UsersRepo", lambda _s: repo)
    monkeypatch.setattr(lang_mw, "UserSettingsRepo", lambda _s: _settings_repo_for(None))

    mw = LanguageMiddleware(registry)
    assert await mw._resolve(_tg_user(language_code="en")) == "en"
    # ...and a non-English code → ru default.
    lang_mw._CACHE.clear()
    assert await mw._resolve(_tg_user(uid=2, language_code="fr")) == "ru"


@pytest.mark.asyncio
async def test_no_author_defaults_to_ru() -> None:
    mw = LanguageMiddleware(MagicMock())
    assert await mw._resolve(None) == "ru"


@pytest.mark.asyncio
async def test_db_failure_degrades_to_language_code(monkeypatch: Any) -> None:
    registry = MagicMock()
    registry.session.side_effect = RuntimeError("db down")
    mw = LanguageMiddleware(registry)
    assert await mw._resolve(_tg_user(language_code="en")) == "en"


# --- cache + invalidation -------------------------------------------------


@pytest.mark.asyncio
async def test_cache_avoids_second_db_hit(monkeypatch: Any) -> None:
    user = _entity(language_code="en", override="ru")
    registry, _session = _registry_returning(user)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=user)
    sr = _settings_repo_for(user)
    monkeypatch.setattr(lang_mw, "UsersRepo", lambda _s: repo)
    monkeypatch.setattr(lang_mw, "UserSettingsRepo", lambda _s: sr)

    mw = LanguageMiddleware(registry)
    tg = _tg_user(language_code="en")
    assert await mw._resolve(tg) == "ru"
    assert await mw._resolve(tg) == "ru"
    # The override lookup is the DB hit now; the second call is cached.
    sr.get_language.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalidate_forces_reread(monkeypatch: Any) -> None:
    user_ru = _entity(language_code="en", override="ru")
    user_en = _entity(language_code="en", override="en")
    registry, _session = _registry_returning(user_ru)
    repo = MagicMock()
    repo.get = AsyncMock(side_effect=[user_ru, user_en])
    sr = MagicMock()
    sr.get_language = AsyncMock(side_effect=["ru", "en"])
    monkeypatch.setattr(lang_mw, "UsersRepo", lambda _s: repo)
    monkeypatch.setattr(lang_mw, "UserSettingsRepo", lambda _s: sr)

    mw = LanguageMiddleware(registry)
    tg = _tg_user(language_code="en")
    assert await mw._resolve(tg) == "ru"
    invalidate_language_cache(tg.id)
    assert await mw._resolve(tg) == "en"
    # The override lookup is the DB hit now; invalidation forces a re-read.
    assert sr.get_language.await_count == 2


@pytest.mark.asyncio
async def test_call_stamps_data_lang(monkeypatch: Any) -> None:
    user = _entity(language_code="en", override="en")
    registry, _session = _registry_returning(user)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=user)
    monkeypatch.setattr(lang_mw, "UsersRepo", lambda _s: repo)
    monkeypatch.setattr(lang_mw, "UserSettingsRepo", lambda _s: _settings_repo_for(user))

    mw = LanguageMiddleware(registry)
    seen: dict[str, Any] = {}

    async def handler(_event: Any, data: dict[str, Any]) -> str:
        seen.update(data)
        return "ok"

    data = {"event_from_user": _tg_user(language_code="en")}
    result = await mw(handler, MagicMock(), data)
    assert result == "ok"
    assert seen["lang"] == "en"


@pytest.mark.asyncio
async def test_cache_is_lru_capped(monkeypatch: Any) -> None:
    """The cache must not keep an entry per user_id seen, forever.

    The TTL alone does not bound it — expiry is only checked when an
    entry is READ, so a user who never sends a second message leaves
    their tuple behind for the life of the process. This middleware is
    attached at the root on message AND callback_query, so its key space
    is every member of every group the bot is in.
    """
    user = _entity(language_code="en", override="en")
    registry, _session = _registry_returning(user)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=user)
    monkeypatch.setattr(lang_mw, "UsersRepo", lambda _s: repo)
    monkeypatch.setattr(lang_mw, "UserSettingsRepo", lambda _s: _settings_repo_for(user))
    monkeypatch.setattr(lang_mw, "_MAX_CACHED_USERS", 3)

    mw = LanguageMiddleware(registry)
    for uid in range(1, 11):
        assert await mw._resolve(_tg_user(uid=uid, language_code="en")) == "en"

    assert len(lang_mw._CACHE) == 3
    # The survivors are the most recently seen, not the first ones in.
    assert set(lang_mw._CACHE) == {8, 9, 10}


@pytest.mark.asyncio
async def test_cache_hit_refreshes_lru_position(monkeypatch: Any) -> None:
    """A regular is kept by their cache HITS, not just by misses.

    If only the miss path touched the LRU order, an active user whose
    entry never expires would age out behind a stream of one-off
    visitors and pay a users.db read on their next message — the exact
    cost this cache exists to remove.
    """
    user = _entity(language_code="en", override="en")
    registry, _session = _registry_returning(user)
    repo = MagicMock()
    repo.get = AsyncMock(return_value=user)
    sr = _settings_repo_for(user)
    monkeypatch.setattr(lang_mw, "UsersRepo", lambda _s: repo)
    monkeypatch.setattr(lang_mw, "UserSettingsRepo", lambda _s: sr)
    monkeypatch.setattr(lang_mw, "_MAX_CACHED_USERS", 3)

    mw = LanguageMiddleware(registry)
    regular = _tg_user(uid=777, language_code="en")
    assert await mw._resolve(regular) == "en"

    for uid in (1, 2, 3, 4, 5):
        await mw._resolve(_tg_user(uid=uid, language_code="en"))
        assert await mw._resolve(regular) == "en"

    assert 777 in lang_mw._CACHE
    assert len(lang_mw._CACHE) == 3
    # One lookup for the regular + one per newcomer — the regular was
    # served from cache every single time.
    assert sr.get_language.await_count == 6


# --- resolve_lang helper --------------------------------------------------


def test_resolve_lang_prefers_data() -> None:
    assert resolve_lang({"lang": "en"}, _tg_user(language_code="ru")) == "en"


def test_resolve_lang_falls_back_to_user_code() -> None:
    assert resolve_lang({}, _tg_user(language_code="en")) == "en"


def test_resolve_lang_ignores_junk_data_lang() -> None:
    assert resolve_lang({"lang": "fr"}, _tg_user(language_code="en")) == "en"


def test_resolve_lang_default_ru_without_anything() -> None:
    assert resolve_lang({}, None) == "ru"


# --- third-party resolution (strict vs best-effort) -----------------------


def _repos_for(*, override: str | None, entity: UserEntity | None) -> tuple[Any, Any]:
    """A (users_repo, settings_repo) pair for the third-party resolvers."""
    users_repo = MagicMock()
    users_repo.get = AsyncMock(return_value=entity)
    settings_repo = MagicMock()
    settings_repo.get_language = AsyncMock(return_value=override)
    return users_repo, settings_repo


@pytest.mark.asyncio
async def test_best_effort_agrees_with_the_strict_resolver() -> None:
    """The wrapper adds a guard, not a different precedence."""
    users_repo, settings_repo = _repos_for(override="en", entity=None)

    strict = await language_for_user(
        7, users_repo=users_repo, settings_repo=settings_repo, fallback="ru"
    )
    guarded = await best_effort_language_for_user(
        7, users_repo=users_repo, settings_repo=settings_repo, fallback="ru"
    )

    assert strict == guarded == "en"


@pytest.mark.asyncio
async def test_strict_resolver_propagates_a_read_fault() -> None:
    """``language_for_user`` is deliberately strict: callers that resolve
    BEFORE doing the work want the error path, not a silent guess."""
    users_repo, settings_repo = _repos_for(override=None, entity=None)
    settings_repo.get_language = AsyncMock(side_effect=RuntimeError("db is locked"))

    with pytest.raises(RuntimeError):
        await language_for_user(
            7, users_repo=users_repo, settings_repo=settings_repo, fallback="ru"
        )


@pytest.mark.asyncio
async def test_best_effort_swallows_a_read_fault() -> None:
    """The post-commit DM paths (/give, withdrawal approve/reject) call
    this one: the money already moved, so a DB hiccup must degrade the
    DM's wording rather than surface as a failed command the operator
    would retry."""
    users_repo, settings_repo = _repos_for(override=None, entity=None)
    settings_repo.get_language = AsyncMock(side_effect=RuntimeError("db is locked"))

    assert (
        await best_effort_language_for_user(
            7, users_repo=users_repo, settings_repo=settings_repo, fallback="en"
        )
        == "en"
    )
    users_repo.get.assert_not_awaited()
