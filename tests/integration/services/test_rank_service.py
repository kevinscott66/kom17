"""End-to-end tests for :class:`RankService` (ranks epic R1).

Real two-file registry (users.db + moderation.db), fake aiogram Bot.
Covers the design's contract points (DESIGN_RANKS.md §2.2):

* verdict precedence: developer → live-TG-admin bypass → rank vs
  merged matrix, with the API-error path falling through WITHOUT a
  grant (fail-closed on grants);
* the override overlay widening a rank's matrix row end-to-end;
* ``can_moderate`` guard order verbatim legacy (self → creator probe →
  developer → target_rank >= actor_rank), incl. the legacy
  ``except: pass`` posture when the creator probe fails;
* rank getter cache + ``invalidate_rank_cache`` on ``set_rank``;
* ``set_rank`` guards: range 0..6 and developer immutability.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import ModerationBase, UsersBase

# Imports register the tables on their metadata for create_all.
from telegram_invite_bot.db.models.rank_tables import RankPermissionOverride  # noqa: F401
from telegram_invite_bot.db.models.users import User  # noqa: F401
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.rank_repo import RankRepo, clear_rank_matrix_cache
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services import rank_service as rank_service_module
from telegram_invite_bot.services.rank_service import (
    REASON_CREATOR,
    REASON_DEVELOPER,
    REASON_HIGHER,
    REASON_NO_PERMISSION,
    REASON_RANK,
    REASON_SELF,
    REASON_TG_ADMIN,
    REASON_TG_ADMIN_ONLY,
    RankService,
    clear_rank_caches,
    invalidate_rank_cache,
)

DEV_ID = 999_000
CHAT = -1001234567890
ACTOR = 111
TARGET = 222
CREATOR_ID = 333


@pytest.fixture(autouse=True)
def _isolate_caches() -> None:
    clear_rank_caches()


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engines = {}
    sessions = {}
    for db, base in ((DBName.USERS, UsersBase), (DBName.MODERATION, ModerationBase)):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{db.value}.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)
        engines[db] = engine
        sessions[db] = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(engines=engines, sessions=sessions)
    try:
        yield reg
    finally:
        await reg.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:test-token-for-tests-only", DEVELOPER_ID_1=DEV_ID),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


@pytest.fixture
def service(registry: EngineRegistry, settings: Settings) -> RankService:
    return RankService(registry, settings)


class FakeBot:
    """Just enough of aiogram Bot for the admin/creator probes.

    ``member_status`` drives get_chat_member; ``raise_member`` /
    ``raise_admins`` simulate Telegram API failures. ``member_rights``
    (#337) says whether an ADMINISTRATOR actually holds a moderation
    right — ``False`` is the title-only administrator, who must not
    bypass the rank matrix.
    """

    def __init__(
        self,
        member_status: ChatMemberStatus = ChatMemberStatus.MEMBER,
        creator_id: int | None = CREATOR_ID,
        *,
        raise_member: bool = False,
        raise_admins: bool = False,
        member_rights: bool = True,
    ) -> None:
        self.member_status = member_status
        self.creator_id = creator_id
        self.raise_member = raise_member
        self.raise_admins = raise_admins
        self.member_rights = member_rights
        self.member_calls = 0
        self.admins_calls = 0

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.member_calls += 1
        if self.raise_member:
            raise RuntimeError("telegram down")
        return SimpleNamespace(
            status=self.member_status,
            user=SimpleNamespace(id=user_id, is_bot=False),
            can_restrict_members=self.member_rights,
        )

    async def get_chat_administrators(self, chat_id: int) -> Any:
        self.admins_calls += 1
        if self.raise_admins:
            raise RuntimeError("telegram down")
        if self.creator_id is None:
            return []
        return [
            SimpleNamespace(
                status=ChatMemberStatus.CREATOR,
                user=SimpleNamespace(id=self.creator_id, is_bot=False),
            )
        ]


def bot(**kwargs: Any) -> Bot:
    return cast("Bot", FakeBot(**kwargs))


async def _seed_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    async with session_for(registry, DBName.USERS) as s:
        await UsersRepo(s).set_rank(user_id, rank, by=DEV_ID)


# -- may_manage_ranks(): rank WRITES ------------------------------------------


async def test_rank_writes_are_not_authorized_by_tg_adminship(
    service: RankService,
) -> None:
    """The one place the TG-admin bypass must NOT apply.

    ``check`` grants a live chat admin ``can_manage_mods``, and the
    ranks this permission writes are GLOBAL — so honouring the bypass
    would let anybody create a group, add the bot, and hand out
    moderation power valid in every other group the bot serves.
    """
    verdict = await service.may_manage_ranks(
        ACTOR, CHAT, bot(member_status=ChatMemberStatus.ADMINISTRATOR)
    )
    assert not verdict.allowed
    assert verdict.reason == REASON_TG_ADMIN_ONLY
    # ...while the same actor still passes the ordinary permission gate.
    allowed = await service.check(
        ACTOR, CHAT, "can_manage_mods", bot(member_status=ChatMemberStatus.ADMINISTRATOR)
    )
    assert allowed.allowed and allowed.reason == REASON_TG_ADMIN


async def test_rank_writes_allowed_for_a_ranked_actor_who_is_also_an_admin(
    service: RankService, registry: EngineRegistry
) -> None:
    """Precedence guard: the bypass must not SHADOW a genuine rank.

    ``check`` returns on adminship before it ever reads the matrix, so
    delegating to it would refuse the bot owner's own rank-4 admins in
    the very groups where they are also Telegram admins.
    """
    await _seed_rank(registry, ACTOR, 4)
    verdict = await service.may_manage_ranks(
        ACTOR, CHAT, bot(member_status=ChatMemberStatus.ADMINISTRATOR)
    )
    assert verdict.allowed and verdict.reason == REASON_RANK
    assert verdict.actor_rank == 4


async def test_rank_writes_allowed_for_developer_without_a_probe(
    service: RankService,
) -> None:
    fake = FakeBot()
    verdict = await service.may_manage_ranks(DEV_ID, CHAT, cast("Bot", fake))
    assert verdict.allowed and verdict.reason == REASON_DEVELOPER
    assert fake.member_calls == 0


async def test_rank_writes_denied_for_a_rank_without_the_permission(
    service: RankService, registry: EngineRegistry
) -> None:
    # Rank 3's matrix row has can_manage_mods=False, and no adminship
    # to explain the refusal — the plain denial reason stands.
    await _seed_rank(registry, ACTOR, 3)
    verdict = await service.may_manage_ranks(ACTOR, CHAT, bot())
    assert not verdict.allowed
    assert verdict.reason == REASON_NO_PERMISSION


# -- check(): precedence ------------------------------------------------------


async def test_developer_always_allowed_without_any_api_call(
    service: RankService,
) -> None:
    fake = FakeBot()
    verdict = await service.check(DEV_ID, CHAT, "can_ban", cast("Bot", fake))
    assert verdict.allowed and verdict.reason == REASON_DEVELOPER
    assert verdict.actor_rank == 6
    assert fake.member_calls == 0  # short-circuits before the probe


async def test_tg_admin_bypasses_ranks(service: RankService) -> None:
    # Rank 0, not in users table at all — live adminship alone passes.
    verdict = await service.check(
        ACTOR, CHAT, "can_ban", bot(member_status=ChatMemberStatus.ADMINISTRATOR)
    )
    assert verdict.allowed and verdict.reason == REASON_TG_ADMIN


async def test_titular_tg_admin_does_not_bypass_ranks(service: RankService) -> None:
    """#337: an administrator with no moderation right is not an admin.

    Legacy reached this decision through ``telegram_admin_has_mod_rights``
    (bot.py:7455-7476), so a member promoted for a title alone landed in
    the rank branch, where rank 0 grants nothing. Before #337 the port
    checked bare status and handed them ``can_ban``.
    """
    verdict = await service.check(
        ACTOR,
        CHAT,
        "can_ban",
        bot(member_status=ChatMemberStatus.ADMINISTRATOR, member_rights=False),
    )
    assert not verdict.allowed
    assert verdict.reason == REASON_NO_PERMISSION


async def test_titular_admin_still_passes_on_rank(
    service: RankService, registry: EngineRegistry
) -> None:
    """The narrowing only removes the free pass, never an earned one."""
    await _seed_rank(registry, ACTOR, 2)
    verdict = await service.check(
        ACTOR,
        CHAT,
        "can_warn",
        bot(member_status=ChatMemberStatus.ADMINISTRATOR, member_rights=False),
    )
    assert verdict.allowed


async def test_creator_bypasses_ranks_without_explicit_rights(service: RankService) -> None:
    """A creator short-circuits on status (bot.py:7457-7458) — no right needed."""
    verdict = await service.check(
        ACTOR,
        CHAT,
        "can_ban",
        bot(member_status=ChatMemberStatus.CREATOR, member_rights=False),
    )
    assert verdict.allowed and verdict.reason == REASON_TG_ADMIN


async def test_api_error_does_not_grant_admin_bypass(service: RankService) -> None:
    # get_chat_member fails → no TG-admin grant; rank 0 → denied.
    verdict = await service.check(ACTOR, CHAT, "can_warn", bot(raise_member=True))
    assert not verdict.allowed
    assert verdict.reason == REASON_NO_PERMISSION


async def test_rank_pass_and_fail_against_default_matrix(
    service: RankService, registry: EngineRegistry
) -> None:
    await _seed_rank(registry, ACTOR, 2)
    allowed = await service.check(ACTOR, CHAT, "can_warn", bot())
    denied = await service.check(ACTOR, CHAT, "can_ban", bot())
    assert allowed.allowed and allowed.reason == REASON_RANK
    assert allowed.actor_rank == 2
    assert not denied.allowed and denied.reason == REASON_NO_PERMISSION


async def test_rank_zero_has_no_permissions(service: RankService) -> None:
    verdict = await service.check(ACTOR, CHAT, "can_warn", bot())
    assert not verdict.allowed and verdict.actor_rank == 0


async def test_matrix_override_widens_rank(service: RankService, registry: EngineRegistry) -> None:
    await _seed_rank(registry, ACTOR, 2)
    assert not (await service.check(ACTOR, CHAT, "can_ban", bot())).allowed
    async with session_for(registry, DBName.MODERATION) as s:
        await RankRepo(s).set_permission(2, "can_ban", True)
    # #577: invalidation is the caller's job, and it belongs AFTER the
    # session closed — clearing inside the transaction would let another
    # reader re-fill the cache from the still-invisible pre-write row.
    clear_rank_matrix_cache()
    assert (await service.check(ACTOR, CHAT, "can_ban", bot())).allowed


# -- can_moderate(): guard order ----------------------------------------------


async def test_can_moderate_blocks_self_even_for_developer(
    service: RankService,
) -> None:
    verdict = await service.can_moderate(DEV_ID, DEV_ID, CHAT, bot())
    assert not verdict.allowed and verdict.reason == REASON_SELF


async def test_can_moderate_blocks_creator(service: RankService, registry: EngineRegistry) -> None:
    await _seed_rank(registry, ACTOR, 5)
    verdict = await service.can_moderate(ACTOR, CREATOR_ID, CHAT, bot())
    assert not verdict.allowed and verdict.reason == REASON_CREATOR


async def test_creator_probe_failure_skips_guard_legacy_posture(
    service: RankService, registry: EngineRegistry
) -> None:
    # Legacy ``except: pass`` (bot.py:7597-7603): if the creator lookup
    # fails the guard is skipped and the rank comparison decides.
    await _seed_rank(registry, ACTOR, 3)
    verdict = await service.can_moderate(ACTOR, TARGET, CHAT, bot(raise_admins=True))
    assert verdict.allowed  # target rank 0 < actor rank 3


async def test_developer_moderates_above_own_seeded_rank(
    service: RankService, registry: EngineRegistry
) -> None:
    await _seed_rank(registry, TARGET, 6)
    verdict = await service.can_moderate(DEV_ID, TARGET, CHAT, bot())
    assert verdict.allowed and verdict.reason == REASON_DEVELOPER


async def test_cannot_moderate_equal_or_higher_rank(
    service: RankService, registry: EngineRegistry
) -> None:
    await _seed_rank(registry, ACTOR, 2)
    await _seed_rank(registry, TARGET, 2)
    verdict = await service.can_moderate(ACTOR, TARGET, CHAT, bot())
    assert not verdict.allowed and verdict.reason == REASON_HIGHER
    assert verdict.actor_rank == 2 and verdict.target_rank == 2


async def test_can_moderate_lower_rank(service: RankService, registry: EngineRegistry) -> None:
    await _seed_rank(registry, ACTOR, 3)
    await _seed_rank(registry, TARGET, 1)
    verdict = await service.can_moderate(ACTOR, TARGET, CHAT, bot())
    assert verdict.allowed and verdict.target_rank == 1


# -- rank getter cache + set_rank guards ---------------------------------------


async def test_get_rank_cached_and_invalidated(
    service: RankService, registry: EngineRegistry
) -> None:
    await _seed_rank(registry, ACTOR, 2)
    assert await service.get_rank(ACTOR) == 2
    # Write behind the cache: still serves 2 until invalidated.
    async with session_for(registry, DBName.USERS) as s:
        await UsersRepo(s).set_rank(ACTOR, 4, by=DEV_ID)
    assert await service.get_rank(ACTOR) == 2
    invalidate_rank_cache(ACTOR)
    assert await service.get_rank(ACTOR) == 4


async def test_get_rank_rereads_the_db_after_eviction(
    service: RankService, registry: EngineRegistry
) -> None:
    """The cost of the LRU ceiling (#74), end to end.

    Enough distinct users push a warm entry out of the bounded table.
    The next read must go back to the DB and answer from it — losing the
    cached rank may cost a query, never a stale grant. Sized off the
    module constant so raising the ceiling cannot silently stop
    exercising the eviction.
    """
    await _seed_rank(registry, ACTOR, 2)
    assert await service.get_rank(ACTOR) == 2  # warm the cache
    async with session_for(registry, DBName.USERS) as s:
        await UsersRepo(s).set_rank(ACTOR, 1, by=DEV_ID)  # demote behind it

    for uid in range(rank_service_module._MAX_CACHED_USERS):
        rank_service_module._RANK_CACHE.put(1_000_000 + uid, 6, now=0.0)

    assert await service.get_rank(ACTOR) == 1


async def test_set_rank_persists_and_invalidates(
    service: RankService, registry: EngineRegistry
) -> None:
    await _seed_rank(registry, ACTOR, 1)
    assert await service.get_rank(ACTOR) == 1  # warm the cache
    assert await service.set_rank(ACTOR, 3, by=DEV_ID) is True
    assert await service.get_rank(ACTOR) == 3  # no stale TTL window
    async with session_for(registry, DBName.USERS) as s:
        assert await UsersRepo(s).get_rank(ACTOR) == 3


async def test_set_rank_rejects_out_of_range(service: RankService) -> None:
    assert await service.set_rank(ACTOR, -1, by=DEV_ID) is False
    assert await service.set_rank(ACTOR, 7, by=DEV_ID) is False


async def test_set_rank_developer_is_immutable(
    service: RankService, registry: EngineRegistry
) -> None:
    assert await service.set_rank(DEV_ID, 2, by=ACTOR) is False
    # Re-affirming 6 is allowed (legacy permits level == 6).
    assert await service.set_rank(DEV_ID, 6, by=DEV_ID) is True
    # And the developer pin wins regardless of the stored value.
    assert await service.get_rank(DEV_ID) == 6


async def test_users_repo_get_rank_defaults(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.USERS) as s:
        repo = UsersRepo(s)
        assert await repo.get_rank(424242) == 0  # no row → 0
        await repo.set_rank(424242, 5, by=DEV_ID)  # upsert creates the row
        assert await repo.get_rank(424242) == 5
