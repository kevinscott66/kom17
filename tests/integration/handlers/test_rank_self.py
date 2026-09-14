"""Tests for handlers/rank_self.py — /staff_me + bang-commands + lazy sync (R3).

Real two-file registry (users.db + moderation.db), fake aiogram Bot
and Message — same harness as tests/integration/services/
test_rank_service.py. Legacy anchors per the handler docstrings
(/staff_me bot.py:41462-41513, bang parser bot.py:31322-31346,
targets bot.py:31349-31381, staff-sync bot.py:7479-7519).

Security posture covered:

* /staff_me grant is fail-CLOSED: API error on the main-chat admin
  probe denies (legacy ``except: status = ""``);
* bang-commands deny rank-0 non-admin actors (h_mod_no_permission)
  and never touch a developer's rank (rank_cannot_change_dev);
* lazy staff-sync never demotes on API uncertainty and auto-promotes
  ONLY behind RANK_AUTOSYNC.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType
from aiogram.types import Message
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import Settings
from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import ModerationBase, UsersBase

# Imports register the tables on their metadata for create_all.
from telegram_invite_bot.db.models.rank_tables import (  # noqa: F401
    RankPermissionOverride,
)
from telegram_invite_bot.db.models.users import User  # noqa: F401
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers import rank_self
from telegram_invite_bot.handlers.rank_self import (
    clear_staff_sync_cache,
    handle_bang_rank,
    handle_staff_me,
    lazy_staff_sync,
    parse_bang_rank_command,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services.rank_service import RankService, clear_rank_caches

DEV_ID = 999_000
MAIN_CHAT = -1009999999999
CHAT = -1001234567890
ACTOR = 111
TARGET = 222


@pytest.fixture(autouse=True)
def _isolate_caches() -> None:
    clear_rank_caches()
    clear_staff_sync_cache()


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


def make_settings(
    *,
    main_chat_id: int = MAIN_CHAT,
    rank_autosync: bool = False,
    developers: tuple[int, ...] = (DEV_ID,),
) -> Settings:
    """Settings stub: only the surface RankService + rank_self touch.

    ``rank_autosync`` is the only knob the flow reads, so the stub
    doubles as the test bench for both of its states.
    """
    bot_cfg = SimpleNamespace(
        is_developer=lambda uid: uid in developers,
        main_chat_id=main_chat_id,
        rank_autosync=rank_autosync,
    )
    return cast("Settings", SimpleNamespace(bot=bot_cfg))


class FakeBot:
    """get_chat_member stub: per-(chat, user) statuses + failure mode."""

    def __init__(
        self,
        admins: set[tuple[int, int]] | None = None,
        *,
        raise_member: bool = False,
        titular: bool = False,
    ) -> None:
        self.admins = admins or set()
        self.raise_member = raise_member
        # #337: ``titular=True`` makes every admin in ``admins`` an
        # administrator Telegram granted no moderation right to — the
        # case that must NOT count as adminship for rank sync.
        self.titular = titular
        self.member_calls = 0

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.member_calls += 1
        if self.raise_member:
            raise RuntimeError("telegram down")
        is_admin = (chat_id, user_id) in self.admins
        status = ChatMemberStatus.ADMINISTRATOR if is_admin else ChatMemberStatus.MEMBER
        return SimpleNamespace(
            status=status,
            user=SimpleNamespace(id=user_id, is_bot=False),
            can_restrict_members=is_admin and not self.titular,
        )

    async def get_chat_administrators(self, chat_id: int) -> Any:
        return []


def bot(**kwargs: Any) -> Bot:
    return cast("Bot", FakeBot(**kwargs))


class FakeMessage:
    """Just enough Message for the rank_self handlers."""

    def __init__(
        self,
        *,
        text: str | None = None,
        chat_type: ChatType = ChatType.SUPERGROUP,
        chat_id: int = CHAT,
        user_id: int | None = ACTOR,
        reply_to: Any = None,
        entities: list[Any] | None = None,
    ) -> None:
        self.text = text
        self.chat = SimpleNamespace(type=chat_type, id=chat_id)
        self.from_user = SimpleNamespace(id=user_id, is_bot=False) if user_id is not None else None
        self.reply_to_message = reply_to
        self.entities = entities
        self.replies: list[str] = []

    async def reply(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


def msg(**kwargs: Any) -> Message:
    return cast("Message", FakeMessage(**kwargs))


def reply_from(user_id: int, *, is_bot: bool = False) -> Any:
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id, is_bot=is_bot))


async def _get_rank(registry: EngineRegistry, user_id: int) -> int:
    async with session_for(registry, DBName.USERS) as s:
        return await UsersRepo(s).get_rank(user_id)


async def _seed_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    async with session_for(registry, DBName.USERS) as s:
        await UsersRepo(s).set_rank(user_id, rank, by=DEV_ID)


# -- parse_bang_rank_command (legacy bot.py:31322-31346) -----------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!повысить", ("promote", 1)),
        ("!!повысить @user", ("promote", 2)),
        ("!!!!!повысить", ("promote", 5)),
        ("!promote", ("promote", 1)),
        ("!!PROMOTE", ("promote", 2)),
        ("!понизить", ("demote", 0)),  # bangs-1, floor 0
        ("!!!понизить", ("demote", 2)),
        ("!!demote", ("demote", 1)),
        ("!разжаловать", ("strip", 0)),
        ("!!!!!strip", ("strip", 0)),
        ("! повысить", ("promote", 1)),  # \s* between bangs and verb
        ("  !повысить  ", ("promote", 1)),  # surrounding whitespace stripped
    ],
)
def test_parse_bang_command_matches(text: str, expected: tuple[str, int]) -> None:
    assert parse_bang_rank_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "повысить",  # no bang
        "!!!!!!повысить",  # 6 bangs > 5 → not a command (legacy bot.py:31338)
        "!warn",
        "hello !повысить",  # must be anchored at start
    ],
)
def test_parse_bang_command_rejects(text: str | None) -> None:
    assert parse_bang_rank_command(text) is None


# -- /staff_me ------------------------------------------------------------------


async def test_staff_me_group_gets_private_only_notice(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    message = FakeMessage(text="/staff_me", chat_type=ChatType.SUPERGROUP)
    await handle_staff_me(cast("Message", message), bot(), settings, registry, "ru")
    assert message.replies == [t("h_staffme_private_only", "ru")]


async def test_staff_me_developer_short_circuits(registry: EngineRegistry) -> None:
    settings = make_settings()
    fake = FakeBot()
    message = FakeMessage(text="/staff_me", chat_type=ChatType.PRIVATE, user_id=DEV_ID)
    await handle_staff_me(cast("Message", message), cast("Bot", fake), settings, registry, "ru")
    assert message.replies == [t("h_staffme_dev", "ru")]
    assert fake.member_calls == 0  # no admin probe for developers


async def test_staff_me_non_admin_denied(registry: EngineRegistry) -> None:
    settings = make_settings()
    message = FakeMessage(text="/staff_me", chat_type=ChatType.PRIVATE)
    await handle_staff_me(cast("Message", message), bot(), settings, registry, "ru")
    assert message.replies == [t("h_staffme_not_admin", "ru")]
    assert await _get_rank(registry, ACTOR) == 0


async def test_staff_me_api_error_denies_fail_closed(
    registry: EngineRegistry,
) -> None:
    # Legacy coerces a get_chat_member exception to status "" → deny
    # (bot.py:41484-41486). The grant must NEVER pass on API error.
    settings = make_settings()
    message = FakeMessage(text="/staff_me", chat_type=ChatType.PRIVATE)
    await handle_staff_me(
        cast("Message", message), bot(raise_member=True), settings, registry, "ru"
    )
    assert message.replies == [t("h_staffme_not_admin", "ru")]
    assert await _get_rank(registry, ACTOR) == 0


async def test_staff_me_no_main_chat_configured_denies(
    registry: EngineRegistry,
) -> None:
    settings = make_settings(main_chat_id=0)
    fake = FakeBot(admins={(0, ACTOR)})
    message = FakeMessage(text="/staff_me", chat_type=ChatType.PRIVATE)
    await handle_staff_me(cast("Message", message), cast("Bot", fake), settings, registry, "ru")
    assert message.replies == [t("h_staffme_not_admin", "ru")]
    assert fake.member_calls == 0  # no probe against chat_id 0


async def test_staff_me_admin_unranked_gets_moderator(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    message = FakeMessage(text="/staff_me", chat_type=ChatType.PRIVATE)
    await handle_staff_me(
        cast("Message", message),
        bot(admins={(MAIN_CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert message.replies == [t("h_staffme_done", "ru", rank=t("rank_level_2", "ru"))]
    assert await _get_rank(registry, ACTOR) == 2


async def test_staff_me_already_ranked_shows_current(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 3)
    message = FakeMessage(text="/staff_me", chat_type=ChatType.PRIVATE)
    await handle_staff_me(
        cast("Message", message),
        bot(admins={(MAIN_CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert message.replies == [t("h_staffme_already", "ru", rank=t("rank_level_3", "ru"))]
    assert await _get_rank(registry, ACTOR) == 3  # unchanged


# -- bang-commands ----------------------------------------------------------------


async def test_bang_denied_for_rank0_non_admin(registry: EngineRegistry) -> None:
    settings = make_settings()
    message = FakeMessage(text="!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", message), bot(), settings, registry, "ru")
    assert message.replies == [t("h_mod_no_permission", "ru")]
    assert await _get_rank(registry, TARGET) == 0


async def test_bang_promote_by_ranked_actor_via_reply(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 5)
    message = FakeMessage(text="!!!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert await _get_rank(registry, TARGET) == 3
    assert message.replies == [t("rank_set_done", "ru", rank_name=t("rank_level_3", "ru"))]


async def test_bang_promote_refused_when_only_authority_is_tg_adminship(
    registry: EngineRegistry,
) -> None:
    """The escalation this gate exists for.

    Ranks are global, and anybody can create a group, add the bot and be
    its admin. Before the fix that alone authorized ``!повысить``, so a
    stranger could hand out ``can_ban``/``can_mute``/``can_kick`` — good
    in EVERY group the bot serves — from a chat nobody else is in.
    """
    settings = make_settings()
    message = FakeMessage(text="!!!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert await _get_rank(registry, TARGET) == 0
    assert message.replies == [t("h_rank_global_denied", "ru")]


async def test_bang_self_promotion_is_refused(registry: EngineRegistry) -> None:
    """Same trick without an accomplice: reply to your OWN message."""
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 4)
    message = FakeMessage(text="!!!!!повысить", reply_to=reply_from(ACTOR))
    await handle_bang_rank(cast("Message", message), bot(), settings, registry, "ru")
    assert await _get_rank(registry, ACTOR) == 4  # unchanged
    assert message.replies == [t("h_rank_bang_too_high", "ru")]


async def test_bang_cannot_grant_at_or_above_own_rank(registry: EngineRegistry) -> None:
    """A rank-4 admin must not be able to mint a rank-5 owner."""
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 4)
    message = FakeMessage(text="!!!!!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", message), bot(), settings, registry, "ru")
    assert await _get_rank(registry, TARGET) == 0
    assert message.replies == [t("h_rank_bang_too_high", "ru")]


async def test_bang_cannot_touch_a_peer(registry: EngineRegistry) -> None:
    """Equal rank is not below you — no demoting your peers."""
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 4)
    await _seed_rank(registry, TARGET, 4)
    message = FakeMessage(text="!разжаловать", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", message), bot(), settings, registry, "ru")
    assert await _get_rank(registry, TARGET) == 4
    assert message.replies == [t("h_rank_bang_target_denied", "ru")]


async def test_bang_promote_caps_at_five(registry: EngineRegistry) -> None:
    # Level 5 is the parser's ceiling; only a developer outranks it.
    settings = make_settings(developers=(DEV_ID, ACTOR))
    message = FakeMessage(text="!!!!!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert await _get_rank(registry, TARGET) == 5


async def test_bang_demote_and_strip(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 5)
    await _seed_rank(registry, TARGET, 4)
    # Main-chat admin: the post-action staff-sync must leave the actor's
    # own rank alone between the two commands.
    admin = bot(admins={(MAIN_CHAT, ACTOR)})
    demote = FakeMessage(text="!!понизить", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", demote), admin, settings, registry, "ru")
    assert await _get_rank(registry, TARGET) == 1  # bangs-1
    assert demote.replies == [t("rank_demote_done", "ru", rank_name=t("rank_level_1", "ru"))]

    strip = FakeMessage(text="!разжаловать", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", strip), admin, settings, registry, "ru")
    assert await _get_rank(registry, TARGET) == 0
    assert strip.replies == [t("rank_strip_done", "ru")]


async def test_bang_rank4_actor_passes_matrix_without_tg_admin(
    registry: EngineRegistry,
) -> None:
    # Rank 4 holds can_manage_mods in the default matrix (bot.py:2619)
    # — a ranked non-TG-admin CAN manage ranks (the epic's key power).
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 4)
    message = FakeMessage(text="!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", message), bot(), settings, registry, "ru")
    assert await _get_rank(registry, TARGET) == 1
    assert message.replies == [t("rank_set_done", "ru", rank_name=t("rank_level_1", "ru"))]


async def test_bang_rank3_actor_lacks_can_manage_mods(
    registry: EngineRegistry,
) -> None:
    # Rank 3's matrix row has can_manage_mods=False (bot.py:2670).
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 3)
    message = FakeMessage(text="!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", message), bot(), settings, registry, "ru")
    assert message.replies == [t("h_mod_no_permission", "ru")]
    assert await _get_rank(registry, TARGET) == 0


async def test_bang_no_targets(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 5)
    message = FakeMessage(text="!повысить")
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert message.replies == [t("rank_no_targets", "ru")]


async def test_bang_bot_reply_target_excluded(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 5)
    message = FakeMessage(text="!повысить", reply_to=reply_from(TARGET, is_bot=True))
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert message.replies == [t("rank_no_targets", "ru")]
    assert await _get_rank(registry, TARGET) == 0


async def test_bang_developer_target_immutable(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 5)
    message = FakeMessage(text="!!повысить", reply_to=reply_from(DEV_ID))
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert message.replies == [t("rank_cannot_change_dev", "ru")]
    assert await _get_rank(registry, DEV_ID) == 0  # row never written


async def test_bang_mention_targets_resolved_via_users_db(
    registry: EngineRegistry,
) -> None:
    await _seed_rank(registry, ACTOR, 5)
    async with session_for(registry, DBName.USERS) as s:
        await UsersRepo(s).upsert_from_telegram(
            user_id=TARGET,
            username="SomeMod",
            first_name="Some",
            last_name=None,
            language_code="ru",
            is_premium=False,
        )
    text = "!!повысить @somemod"
    entity = SimpleNamespace(
        type=MessageEntityType.MENTION,
        offset=text.index("@"),
        length=len("@somemod"),
        user=None,
    )
    message = FakeMessage(text=text, entities=[entity])
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        make_settings(),
        registry,
        "ru",
    )
    assert await _get_rank(registry, TARGET) == 2
    assert message.replies == [t("rank_set_done", "ru", rank_name=t("rank_level_2", "ru"))]


async def test_bang_text_mention_multi_targets(registry: EngineRegistry) -> None:
    await _seed_rank(registry, ACTOR, 5)
    entities = [
        SimpleNamespace(
            type=MessageEntityType.TEXT_MENTION,
            offset=0,
            length=1,
            user=SimpleNamespace(id=TARGET, is_bot=False),
        ),
        SimpleNamespace(
            type=MessageEntityType.TEXT_MENTION,
            offset=2,
            length=1,
            user=SimpleNamespace(id=TARGET + 1, is_bot=False),
        ),
    ]
    message = FakeMessage(text="!повысить a b", entities=entities)
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(CHAT, ACTOR)}),
        make_settings(),
        registry,
        "ru",
    )
    assert await _get_rank(registry, TARGET) == 1
    assert await _get_rank(registry, TARGET + 1) == 1
    assert message.replies == [t("rank_done_multi", "ru", count=2)]


# -- lazy staff-sync ----------------------------------------------------------------


async def test_sync_demotes_ranked_user_who_lost_adminship(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 2)
    ranks = RankService(registry, settings)
    await lazy_staff_sync(ranks, bot(), settings, ACTOR)
    assert await _get_rank(registry, ACTOR) == 0


async def test_sync_keeps_rank_for_live_admin(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 2)
    ranks = RankService(registry, settings)
    await lazy_staff_sync(ranks, bot(admins={(MAIN_CHAT, ACTOR)}), settings, ACTOR)
    assert await _get_rank(registry, ACTOR) == 2


async def test_sync_demotes_titular_admin(registry: EngineRegistry) -> None:
    """#337: adminship for the sync means moderation rights, not a title.

    Legacy's ``sync_ranks_with_telegram_admins`` filtered the admin list
    through ``telegram_admin_has_mod_rights`` (bot.py:7500), so a
    title-only administrator was demoted like any other non-admin. The
    port checked bare status and kept their rank alive indefinitely.
    """
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 2)
    ranks = RankService(registry, settings)
    await lazy_staff_sync(ranks, bot(admins={(MAIN_CHAT, ACTOR)}, titular=True), settings, ACTOR)
    assert await _get_rank(registry, ACTOR) == 0


async def test_sync_never_demotes_on_api_error(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 2)
    ranks = RankService(registry, settings)
    await lazy_staff_sync(ranks, bot(raise_member=True), settings, ACTOR)
    assert await _get_rank(registry, ACTOR) == 2  # uncertainty → no action


async def test_sync_survives_a_failure_before_the_chat_is_known(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1477 — the promise must hold in the branch that keeps it.

    ``chat_id`` used to be assigned after ``is_developer``, while the
    ``except`` handler names it unconditionally. A failure in that first
    step therefore left the handler raising ``UnboundLocalError``, out
    of a function documented as never raising, into whatever flow
    triggered the sync — so a best-effort background nicety could break
    the command the user actually sent.
    """
    settings = make_settings()
    ranks = RankService(registry, settings)

    def _boom(_user_id: int) -> bool:
        raise RuntimeError("settings blew up before chat_id was known")

    monkeypatch.setattr(settings.bot, "is_developer", _boom)

    # The point of the assertion is that nothing escapes.
    await lazy_staff_sync(ranks, bot(), settings, ACTOR)


async def test_sync_ttl_cache_skips_repeat_probe(registry: EngineRegistry) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 2)
    ranks = RankService(registry, settings)
    fake = FakeBot(admins={(MAIN_CHAT, ACTOR)})
    await lazy_staff_sync(ranks, cast("Bot", fake), settings, ACTOR)
    await lazy_staff_sync(ranks, cast("Bot", fake), settings, ACTOR)
    assert fake.member_calls == 1  # second call served from the 600s cache


async def test_sync_cache_is_capped(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-verify cache must not keep an entry per user, forever.

    Its TTL is only consulted when an entry is READ, so every user who
    ever triggered a synced flow would otherwise leave a deadline behind
    for the life of the process. Evicting one is harmless — unlike a
    rate-limit bucket it guards no allowance, it only defers a Telegram
    probe — so a plain LRU cap is the right bound here.
    """
    monkeypatch.setattr(rank_self, "_SYNC_CACHE_MAX_ENTRIES", 3)
    settings = make_settings()
    ranks = RankService(registry, settings)
    fake = FakeBot()

    # Unranked + autosync off: the sync returns early, but only AFTER it
    # has stamped the cache — which is the whole point, every user who
    # merely speaks lands in this table.
    for uid in range(1, 11):
        await lazy_staff_sync(ranks, cast("Bot", fake), settings, uid)

    assert len(rank_self._SYNC_CACHE) == 3
    assert set(rank_self._SYNC_CACHE) == {(MAIN_CHAT, 8), (MAIN_CHAT, 9), (MAIN_CHAT, 10)}


async def test_sync_skips_developer_and_unranked_without_autosync(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    ranks = RankService(registry, settings)
    fake = FakeBot()
    await lazy_staff_sync(ranks, cast("Bot", fake), settings, DEV_ID)
    await lazy_staff_sync(ranks, cast("Bot", fake), settings, ACTOR)
    assert fake.member_calls == 0  # no probe: dev short-circuit + rank0/no-autosync


async def test_sync_autopromote_only_with_flag(registry: EngineRegistry) -> None:
    # Default (RANK_AUTOSYNC=0): TG admin at rank 0 is NOT promoted.
    settings_off = make_settings()
    ranks = RankService(registry, settings_off)
    await lazy_staff_sync(ranks, bot(admins={(MAIN_CHAT, ACTOR)}), settings_off, ACTOR)
    assert await _get_rank(registry, ACTOR) == 0

    clear_staff_sync_cache()
    clear_rank_caches()
    # Opt-in (RANK_AUTOSYNC=1): legacy auto-promote applies (bot.py:7513-7519).
    settings_on = make_settings(rank_autosync=True)
    ranks_on = RankService(registry, settings_on)
    await lazy_staff_sync(ranks_on, bot(admins={(MAIN_CHAT, ACTOR)}), settings_on, ACTOR)
    assert await _get_rank(registry, ACTOR) == 2


async def test_bang_flow_demotes_rank_only_actor_after_action(
    registry: EngineRegistry,
) -> None:
    # The documented lazy-sync consequence: a ranked actor who is NOT an
    # admin of the MAIN chat is re-verified afterwards and demoted
    # (legacy staff-sync semantics, bot.py:7505-7511). The target's
    # promotion from this very action still stands.
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 4)
    message = FakeMessage(text="!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", message), bot(), settings, registry, "ru")
    assert await _get_rank(registry, TARGET) == 1
    assert await _get_rank(registry, ACTOR) == 0  # demote-on-loss applied


async def test_bang_flow_sync_ignores_adminship_of_the_incoming_chat(
    registry: EngineRegistry,
) -> None:
    # Being an admin of the group the command was typed in must neither
    # save the actor's rank nor be probed at all: the sync question is
    # "still staff in the MAIN chat?", and CHAT is a group anybody can
    # create and add the bot to.
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 4)
    fake = FakeBot(admins={(CHAT, ACTOR)})
    message = FakeMessage(text="!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(cast("Message", message), cast("Bot", fake), settings, registry, "ru")
    assert await _get_rank(registry, ACTOR) == 0
    assert fake.member_calls == 1  # the one probe went to MAIN_CHAT


async def test_bang_flow_sync_keeps_rank_of_a_main_chat_admin(
    registry: EngineRegistry,
) -> None:
    settings = make_settings()
    await _seed_rank(registry, ACTOR, 4)
    message = FakeMessage(text="!повысить", reply_to=reply_from(TARGET))
    await handle_bang_rank(
        cast("Message", message),
        bot(admins={(MAIN_CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert await _get_rank(registry, TARGET) == 1
    assert await _get_rank(registry, ACTOR) == 4


async def test_sync_is_a_noop_without_a_main_chat(registry: EngineRegistry) -> None:
    # CHAT_ID unset: there is no staff roster to sync against, so the
    # rank stands and no API call is made.
    settings = make_settings(main_chat_id=0)
    await _seed_rank(registry, ACTOR, 2)
    fake = FakeBot()
    await lazy_staff_sync(RankService(registry, settings), cast("Bot", fake), settings, ACTOR)
    assert await _get_rank(registry, ACTOR) == 2
    assert fake.member_calls == 0


async def test_staff_me_done_then_sync_is_noop_for_live_admin(
    registry: EngineRegistry,
) -> None:
    # /staff_me ends with a lazy sync against the main chat; the caller
    # just proved live adminship, so the sync must not undo the grant.
    settings = make_settings()
    message = FakeMessage(text="/staff_me", chat_type=ChatType.PRIVATE)
    await handle_staff_me(
        cast("Message", message),
        bot(admins={(MAIN_CHAT, ACTOR)}),
        settings,
        registry,
        "ru",
    )
    assert await _get_rank(registry, ACTOR) == 2
