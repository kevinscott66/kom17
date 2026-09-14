"""#2024 — ``/lang`` must drop its cache entry AFTER the commit, not before.

The house rule is written down in three places already:
``handlers/rank_admin.py:389-392`` ("AFTER the session closed, i.e.
after the commit: clearing inside the transaction lets a concurrent
update re-fill the cache from the not-yet-visible pre-write row for
another full TTL"), ``handlers/rank_admin.py:676-678`` ("After the
commit, never inside it") and ``repositories/rank_repo.py:28-29``. Of the
caches guarded by :class:`~telegram_invite_bot.utils.cache_generation
.CacheGeneration`, the language cache was the only one whose writer
broke it.

The window is the ``await`` inside ``checkpoint()``. ``users.db`` runs
at ``synchronous=FULL``, so the commit is a real fsync and a genuine
suspension point; the user's other in-flight update is picked up there,
misses the cache the handler has just cleared, opens its own connection
— which under WAL sees only committed data, i.e. the OLD language —
and stores it.

:class:`CacheGeneration` cannot catch this and is not meant to. It
detects an invalidation landing *during* a read; here the invalidation
already happened, so the racing reader's snapshot is post-bump and
``unchanged()`` is true. Order is the entire fix: bump after the commit
and the same reader's snapshot is pre-bump, so it serves what it read
and declines to speak for the next five minutes on it.

Observable cost of the bug: ``/lang en`` answers «Язык изменён» in
English, and every message for the following five minutes comes back in
Russian. To the user the command silently did not work.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from telegram_invite_bot.config.settings import AppEnv, Settings
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.language import handle_lang_callback
from telegram_invite_bot.middlewares import language as lang_mod
from telegram_invite_bot.middlewares.language import LanguageMiddleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from telegram_invite_bot.db.engines import EngineRegistry

_USER = 8171
_STALE = "ru"
_CHOSEN = "en"


@pytest.fixture(autouse=True)
def _isolate_cache() -> None:
    """Module state; start every case from empty."""
    lang_mod.clear_language_cache()


@pytest.fixture
async def registry(make_settings: Callable[..., Settings]) -> AsyncIterator[EngineRegistry]:
    """A real registry — the middleware under test opens its own session."""
    settings = make_settings(AppEnv.DEV)
    reg = build_registry(settings)
    try:
        async with reg.engine(DBName.USERS).begin() as conn:
            await conn.run_sync(UsersBase.metadata.create_all)
        yield reg
    finally:
        await reg.dispose()


class _UserService:
    """Only the two methods the callback uses; both record the call."""

    def __init__(self) -> None:
        self.touched = 0
        self.stored: str | None = None

    async def touch(self, _tg_user: object) -> None:
        self.touched += 1

    async def set_language(self, _user_id: int, choice: str) -> str:
        self.stored = choice
        return choice


def _callback() -> Any:
    """A ``lang_set_en`` tap.

    ``message`` is deliberately not a real ``Message``: the handler's
    ``isinstance`` guard then skips ``edit_text``, which is the
    ``InaccessibleMessage`` path it already handles, and the test does
    not need aiogram's network types to observe a cache.
    """
    return SimpleNamespace(
        from_user=SimpleNamespace(id=_USER, language_code=_STALE),
        data=f"lang_set_{_CHOSEN}",
        message=None,
        answer=_noop,
    )


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _tg_user() -> Any:
    return SimpleNamespace(id=_USER, language_code=_STALE)


async def test_a_reader_racing_the_commit_does_not_pin_the_old_language(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, at the seam where it happens.

    ``checkpoint`` is where the handler suspends, so that is where the
    other update runs. It resolves through the REAL middleware, with
    only the database read stubbed to the pre-write row — which is what
    a second connection genuinely sees while the commit is in flight.
    """
    service = _UserService()
    middleware = LanguageMiddleware(registry)

    async def pre_write_row(_self: LanguageMiddleware, _user_id: int, _fallback: str) -> str:
        return _STALE

    monkeypatch.setattr(LanguageMiddleware, "_stored_or", pre_write_row)

    async def checkpoint() -> None:
        # The commit's fsync: the loop picks up this user's other
        # in-flight update here, and it can only see committed data.
        assert await middleware._resolve(_tg_user()) == _STALE  # noqa: SLF001

    await handle_lang_callback(_callback(), service, checkpoint)  # type: ignore[arg-type]

    assert service.stored == _CHOSEN
    cached = lang_mod._CACHE.get(_USER)  # noqa: SLF001
    assert cached is None or cached[0] != _STALE, (
        "the language the user just changed away from is pinned in the cache "
        f"for the whole TTL: {cached!r} — /lang answers in English and then "
        "keeps replying in Russian"
    )


async def test_the_entry_is_dropped_even_with_nobody_racing(
    registry: EngineRegistry,
) -> None:
    """The invalidation must still happen; moving it must not lose it."""
    lang_mod._CACHE[_USER] = (_STALE, float("inf"))  # noqa: SLF001

    await handle_lang_callback(_callback(), _UserService(), _noop)  # type: ignore[arg-type]

    assert _USER not in lang_mod._CACHE  # noqa: SLF001


async def test_no_checkpoint_still_invalidates(registry: EngineRegistry) -> None:
    """``checkpoint`` is ``| None`` for the tests that construct no session.

    In production a :class:`~telegram_invite_bot.middlewares.base
    .BaseSessionMiddleware` always puts one in ``data``, so the ``None``
    branch is not a deployment state — but the invalidation sits after
    the ``if`` on purpose, so that it does not depend on that being
    true forever.
    """
    lang_mod._CACHE[_USER] = (_STALE, float("inf"))  # noqa: SLF001

    await handle_lang_callback(_callback(), _UserService(), None)  # type: ignore[arg-type]

    assert _USER not in lang_mod._CACHE  # noqa: SLF001


async def test_the_next_update_sees_the_new_preference(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with the write visible, the next resolve caches it."""
    middleware = LanguageMiddleware(registry)

    async def committed_row(_self: LanguageMiddleware, _user_id: int, _fallback: str) -> str:
        return _CHOSEN

    monkeypatch.setattr(LanguageMiddleware, "_stored_or", committed_row)

    await handle_lang_callback(_callback(), _UserService(), _noop)  # type: ignore[arg-type]

    assert await middleware._resolve(_tg_user()) == _CHOSEN  # noqa: SLF001
    assert lang_mod._CACHE[_USER][0] == _CHOSEN  # noqa: SLF001
