"""#1942: an invalidation that lands mid-read must not be undone by the fill.

Four process-global caches in this tree share one shape — miss, read,
store — and the read is an ``await``:

.. code-block:: python

    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    value = await <a database read>
    _CACHE.put(key, value)

Every one of them also has a writer that invalidates the moment it has
committed. When those two interleave the invalidation clears an entry
that does not exist yet, and the store afterwards puts back exactly the
value the writer was removing — for the FULL TTL, process-wide, with
nothing left to correct it. Three of the four are permission tables, so
that is a demoted moderator keeping their rights for five more minutes.

The interleaving is imposed, not raced. Each test replaces the read
itself with one that performs the real production invalidation before
returning the value the suspended coroutine would have read — which is
the race stated as an ordering. What is asserted is never "the return
value changed": the caller may serve what it read, since that answer is
as fresh as the read that produced it. What must not happen is the
cache SPEAKING for it afterwards.

Each cache gets its control as well, in the same file: with no
invalidation in flight the fill still happens. Without those a guard
that simply stopped caching would pass everything above, and the
caches exist for a reason.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from telegram_invite_bot.config.settings import AppEnv, Settings
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.middlewares import language as lang_mod
from telegram_invite_bot.middlewares.language import (
    LanguageMiddleware,
    invalidate_language_cache,
)
from telegram_invite_bot.repositories import rank_repo as rank_repo_mod
from telegram_invite_bot.repositories.rank_repo import (
    RankRepo,
    clear_command_override_cache,
    clear_rank_matrix_cache,
)
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services import rank_service as rank_service_mod
from telegram_invite_bot.services.rank_service import (
    RankService,
    clear_rank_caches,
    invalidate_rank_cache,
)

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db.engines import EngineRegistry

_USER = 4242
_CHAT = -1001234567890
_OLD_RANK = 5
_NEW_RANK = 0


@pytest.fixture(autouse=True)
def _isolate_caches() -> None:
    """Every cache here is module state; start each case from empty."""
    clear_rank_caches()
    lang_mod.clear_language_cache()


@pytest.fixture
async def registry(make_settings: Callable[..., Settings]) -> AsyncIterator[EngineRegistry]:
    """A real registry, so ``session_for`` is the production one."""
    settings = make_settings(AppEnv.DEV)
    reg = build_registry(settings)
    try:
        async with reg.engine(DBName.USERS).begin() as conn:
            await conn.run_sync(UsersBase.metadata.create_all)
        yield reg
    finally:
        await reg.dispose()


@pytest.fixture
def service(registry: EngineRegistry, make_settings: Callable[..., Settings]) -> RankService:
    return RankService(registry, make_settings(AppEnv.DEV))


# ── The rank table: /setrank losing to a concurrent gate check ───────────────


async def _seed_rank(registry: EngineRegistry, rank: int) -> None:
    async with session_for(registry, DBName.USERS) as session:
        await UsersRepo(session).set_rank(_USER, rank, by=1)
        await session.commit()


async def test_a_demotion_during_a_rank_read_is_not_undone(
    service: RankService, registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headline defect, at the seam it actually happens.

    The read below returns the pre-demotion rank because that is what
    was on disk when it started — that part is honest. Caching it is
    not: ``/setrank`` has already committed and already invalidated,
    and nothing else will touch this key for five minutes.
    """
    await _seed_rank(registry, _NEW_RANK)  # the demotion is already durable

    async def racing_read(self: UsersRepo, user_id: int) -> int:
        # What /setrank did while this coroutine was suspended.
        invalidate_rank_cache(user_id)
        return _OLD_RANK

    monkeypatch.setattr(UsersRepo, "get_rank", racing_read)
    assert await service.get_rank(_USER) == _OLD_RANK

    monkeypatch.undo()
    # The cache must not be able to answer at all — the next caller
    # goes back to the database and sees the demotion.
    assert rank_service_mod._RANK_CACHE.get(_USER, now=0.0) is None  # noqa: SLF001
    assert await service.get_rank(_USER) == _NEW_RANK


async def test_an_uncontended_rank_read_is_still_cached(
    service: RankService, registry: EngineRegistry
) -> None:
    """The control. A guard that never fills would pass the test above."""
    await _seed_rank(registry, _OLD_RANK)

    assert await service.get_rank(_USER) == _OLD_RANK

    # Served from the cache now: the row is gone and the answer is not.
    async with session_for(registry, DBName.USERS) as session:
        await UsersRepo(session).set_rank(_USER, _NEW_RANK, by=1)
        await session.commit()
    assert await service.get_rank(_USER) == _OLD_RANK


async def test_an_unrelated_invalidation_only_costs_a_read(
    service: RankService, registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One counter per cache, not per key — this is what that trades.

    A ``/setrank`` on somebody else during our read makes us skip our
    own fill. The cost is one extra database read next time, which is
    the cheap direction to be wrong in; the expensive one is the test
    above.
    """
    await _seed_rank(registry, _OLD_RANK)

    async def racing_read(self: UsersRepo, user_id: int) -> int:
        invalidate_rank_cache(999_999)  # a different user entirely
        return _OLD_RANK

    monkeypatch.setattr(UsersRepo, "get_rank", racing_read)
    assert await service.get_rank(_USER) == _OLD_RANK

    assert rank_service_mod._RANK_CACHE.get(_USER, now=0.0) is None  # noqa: SLF001


# ── The creator table ────────────────────────────────────────────────────────


async def test_a_creator_lookup_racing_a_reset_is_not_cached(
    service: RankService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only tests invalidate this one, and this is the leak they fear.

    A lookup in flight when :func:`clear_rank_caches` runs would carry
    the previous case's creator into the next one — precisely what that
    hook exists to prevent.
    """

    async def racing_lookup(_bot: Bot, _chat_id: int) -> int:
        clear_rank_caches()
        return 777

    monkeypatch.setattr(rank_service_mod, "chat_creator_id", racing_lookup)
    bot = cast("Bot", SimpleNamespace())

    assert await service._creator_id(bot, _CHAT) == 777  # noqa: SLF001

    assert rank_service_mod._CREATOR_CACHE.get(_CHAT, now=0.0) is None  # noqa: SLF001


async def test_an_uncontended_creator_lookup_is_still_cached(
    service: RankService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: one Telegram round trip per chat, as designed."""
    calls = 0

    async def counting_lookup(_bot: Bot, _chat_id: int) -> int:
        nonlocal calls
        calls += 1
        return 777

    monkeypatch.setattr(rank_service_mod, "chat_creator_id", counting_lookup)
    bot = cast("Bot", SimpleNamespace())

    assert await service._creator_id(bot, _CHAT) == 777  # noqa: SLF001
    assert await service._creator_id(bot, _CHAT) == 777  # noqa: SLF001

    assert calls == 1


# ── The permission matrix: /perm ─────────────────────────────────────────────


class _StubSession:
    """Just the ``execute`` the override reads use, nothing else.

    The SQL is pinned in ``tests/integration/repositories/test_rank_repo.py``;
    what is under test here is the caching around it.
    """

    def __init__(self, rows: list[Any], *, on_execute: Callable[[], None] | None = None) -> None:
        self._rows = rows
        self._on_execute = on_execute
        self.calls = 0

    async def execute(self, _statement: Any) -> Any:
        self.calls += 1
        if self._on_execute is not None:
            self._on_execute()
        return SimpleNamespace(scalars=lambda: list(self._rows))


def _repo(session: _StubSession) -> RankRepo:
    return RankRepo(cast("AsyncSession", session))


async def test_a_perm_change_during_a_matrix_read_is_not_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/perm`` invalidates right after it commits; this is that window."""
    repo = _repo(_StubSession([]))

    async def racing_overrides() -> dict[int, dict[str, bool]]:
        clear_rank_matrix_cache()  # what /perm did meanwhile
        return {}

    monkeypatch.setattr(repo, "permission_overrides", racing_overrides)

    await repo.merged_matrix()

    assert rank_repo_mod._MATRIX_CACHE == {}  # noqa: SLF001


async def test_an_uncontended_matrix_read_is_still_cached() -> None:
    """The control: the matrix is read on every gated command."""
    session = _StubSession([])

    await _repo(session).merged_matrix()
    await _repo(session).merged_matrix()

    assert rank_repo_mod._MATRIX_CACHE != {}  # noqa: SLF001
    assert session.calls == 1


# ── The command-override map: /cmdcfg ────────────────────────────────────────


async def test_a_cmdcfg_change_during_an_override_read_is_not_undone() -> None:
    """Same window, on the map that decides a command's minimum rank."""
    session = _StubSession([], on_execute=clear_command_override_cache)

    await _repo(session).command_overrides()

    assert rank_repo_mod._COMMAND_CACHE == {}  # noqa: SLF001


async def test_an_uncontended_override_read_is_still_cached() -> None:
    """The control."""
    row = SimpleNamespace(command_key="balance", min_rank=3)
    session = _StubSession([row])

    assert await _repo(session).command_overrides() == {"balance": 3}
    assert await _repo(session).command_overrides() == {"balance": 3}

    assert session.calls == 1


# ── The language cache: /lang ────────────────────────────────────────────────


def _tg_user(user_id: int = _USER) -> Any:
    return SimpleNamespace(id=user_id, language_code="en")


async def test_a_lang_change_during_a_language_read_is_not_undone(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cosmetic rather than a permission, but the same defect exactly."""
    middleware = LanguageMiddleware(registry)

    async def racing_read(_self: LanguageMiddleware, user_id: int, _fallback: str) -> str:
        invalidate_language_cache(user_id)  # what the /lang callback did
        return "en"

    monkeypatch.setattr(LanguageMiddleware, "_stored_or", racing_read)

    assert await middleware._resolve(_tg_user()) == "en"  # noqa: SLF001

    assert lang_mod._CACHE == {}  # noqa: SLF001


async def test_an_uncontended_language_read_is_still_cached(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: one read per user per TTL, which is the whole point."""
    middleware = LanguageMiddleware(registry)
    calls = 0

    async def counting_read(_self: LanguageMiddleware, _user_id: int, _fallback: str) -> str:
        nonlocal calls
        calls += 1
        return "en"

    monkeypatch.setattr(LanguageMiddleware, "_stored_or", counting_read)

    assert await middleware._resolve(_tg_user()) == "en"  # noqa: SLF001
    assert await middleware._resolve(_tg_user()) == "en"  # noqa: SLF001

    assert calls == 1
