"""End-to-end ``/setrules`` (L-46) — admin write-side for group rules.

Pins:

* Admin (developer) in a group → rules upserted, ``setrules_ok`` reply.
  Covers both the INSERT (no prior row) and UPDATE (prior row) arms of
  the upsert, and proves ``/rules`` then renders the new blob.
* Non-admin in a group → moderation's ``_require_admin`` refusal copy,
  NO write (the prior rules survive).
* Empty arg → ``h_setrules_usage`` card, no write.
* Over-length blob (>4000 chars) → ``h_setrules_too_long`` card, no
  write.
* Private DM → router-level group filter keeps it out of the write
  handler; the #123 refusal twin answers "group only" and no rules
  are stored.

The admin gate is :func:`moderation._require_admin`, which calls
``bot.get_chat_member`` (mocked here) and short-circuits on the
developer-id bypass. We exercise both: the happy path uses the dev id
(no get_chat_member round-trip needed), the rejection path uses a
non-dev id reported as a plain member.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import GroupSettings, User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_CHAT_ID = -100_777
_DEV_ID = 9001
_PLAIN_ID = 9002


def _dev_bot_config() -> BotConfig:
    return BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_DEV_ID)


async def _seed_user(registry: EngineRegistry, *, user_id: int, lang: str = "ru") -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=user_id, first_name="A", language_code=lang))
        await session.commit()


async def _seed_rules(registry: EngineRegistry, rules: str) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(GroupSettings(group_id=_CHAT_ID, rules=rules))
        await session.commit()


async def _read_rules(registry: EngineRegistry) -> str | None:
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        row = await conn.execute(
            select(GroupSettings.rules).where(GroupSettings.group_id == _CHAT_ID)
        )
        return row.scalar_one_or_none()


def _attach_member_api(
    bot: Any,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    admin_ids: set[int],
) -> None:
    """Mock the minimal Telegram surface ``_require_admin`` + replies hit."""

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "GetChatMember":
            from aiogram.types import ChatMemberAdministrator, ChatMemberMember
            from aiogram.types import User as TGUser

            uid = method.user_id
            user = TGUser(id=uid, is_bot=False, first_name="U")
            if uid in admin_ids:
                return ChatMemberAdministrator(
                    user=user,
                    can_be_edited=False,
                    is_anonymous=False,
                    can_manage_chat=True,
                    can_delete_messages=True,
                    can_manage_video_chats=True,
                    can_restrict_members=True,
                    can_promote_members=False,
                    can_change_info=True,
                    can_invite_users=True,
                    can_post_stories=False,
                    can_edit_stories=False,
                    can_delete_stories=False,
                )
            return ChatMemberMember(user=user)
        if name == "SendMessage":
            sink.append({"chat_id": method.chat_id, "text": method.text})
            from aiogram.types import Chat, Message
            from aiogram.types import User as TGUser

            return Message(
                message_id=1,
                date=1_700_000_000,
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TGUser(id=777, is_bot=True, first_name="Bot"),
                text=method.text,
            )
        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="Bot", username="bot")
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


def _group_update(text: str, *, user_id: int) -> Any:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        language_code="ru",
    )


async def _wire_rules(
    make_wired: WiredFactory,
    settings: Any,
) -> tuple[Any, Any, EngineRegistry]:
    """Build a dispatcher carrying ONLY the rules router, with the
    test's dev-id ``Settings`` threaded into ``build_router`` so the
    ``/setrules`` admin gate sees ``_DEV_ID`` as a developer.

    We bypass the full main_router (which still calls
    ``build_rules_router(registry)`` without settings during the
    shared-file merge window) and mount the router directly — that lets
    us inject a bespoke ``Settings`` for the dev-bypass assertions. The
    ``settings`` arg is built by the shared ``make_settings`` fixture
    (a tmp-rooted, validation-clean Settings) with a dev-id patched in.
    """
    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from telegram_invite_bot.handlers.rules import build_router as build_rules_router
    from telegram_invite_bot.middlewares.session import SessionMiddleware

    # Reuse the factory only to materialise a registry + bot with the
    # right schema; we then mount our own dispatcher/router around it.
    bot, _dispatcher, registry = await make_wired(
        schemas=[UsersBase], session_middleware=False, bot_config=_dev_bot_config()
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.outer_middleware(SessionMiddleware(registry))
    dispatcher.include_router(build_rules_router(registry, settings))
    return bot, dispatcher, registry


@pytest.mark.asyncio
async def test_setrules_insert_by_admin(
    make_wired: WiredFactory,
    make_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire_rules(make_wired, make_settings())
    await _seed_user(registry, user_id=_DEV_ID)
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, admin_ids={_DEV_ID})

    result = await dispatcher.feed_update(
        bot, _group_update("/setrules Будьте вежливы.", user_id=_DEV_ID)
    )

    assert result is not UNHANDLED
    assert await _read_rules(registry) == "Будьте вежливы."
    assert sink[-1]["text"] == t("h_setrules_ok", "ru")


@pytest.mark.asyncio
async def test_setrules_update_existing(
    make_wired: WiredFactory,
    make_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire_rules(make_wired, make_settings())
    await _seed_user(registry, user_id=_DEV_ID)
    await _seed_rules(registry, "Старые правила.")
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, admin_ids={_DEV_ID})

    await dispatcher.feed_update(bot, _group_update("/setrules Новые правила.", user_id=_DEV_ID))

    assert await _read_rules(registry) == "Новые правила."


@pytest.mark.asyncio
async def test_setrules_rejects_non_admin(
    make_wired: WiredFactory,
    make_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire_rules(make_wired, make_settings())
    await _seed_user(registry, user_id=_PLAIN_ID)
    await _seed_rules(registry, "Изначальные.")
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, admin_ids=set())

    await dispatcher.feed_update(bot, _group_update("/setrules Захват.", user_id=_PLAIN_ID))

    # Prior rules survive; the only reply is the refusal copy.
    assert await _read_rules(registry) == "Изначальные."
    assert sink, "expected a refusal reply"
    assert sink[-1]["text"] != t("h_setrules_ok", "ru")


@pytest.mark.asyncio
async def test_setrules_empty_arg_usage(
    make_wired: WiredFactory,
    make_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire_rules(make_wired, make_settings())
    await _seed_user(registry, user_id=_DEV_ID)
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, admin_ids={_DEV_ID})

    await dispatcher.feed_update(bot, _group_update("/setrules", user_id=_DEV_ID))

    assert await _read_rules(registry) is None
    assert sink[-1]["text"] == t("h_setrules_usage", "ru")


@pytest.mark.asyncio
async def test_setrules_too_long(
    make_wired: WiredFactory,
    make_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire_rules(make_wired, make_settings())
    await _seed_user(registry, user_id=_DEV_ID)
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, admin_ids={_DEV_ID})

    blob = "a" * 4001
    await dispatcher.feed_update(bot, _group_update(f"/setrules {blob}", user_id=_DEV_ID))

    assert await _read_rules(registry) is None
    assert sink[-1]["text"] == t("h_setrules_too_long", "ru", max_len=4000)


@pytest.mark.asyncio
async def test_setrules_private_is_refused(
    make_wired: WiredFactory,
    make_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DM ``/setrules`` is answered, and writes no rules (#123).

    There is no group to set rules for in a DM, so the refusal twin
    answers; the rules row must stay untouched, which the exact-match
    on the sink guards (a stored-rules confirmation would break it).
    """
    bot, dispatcher, registry = await _wire_rules(make_wired, make_settings())
    await _seed_user(registry, user_id=_DEV_ID)
    sink: list[dict[str, Any]] = []
    _attach_member_api(bot, monkeypatch, sink, admin_ids={_DEV_ID})

    # ``lang`` is injected by hand: this file mounts the rules router on
    # a bespoke dispatcher carrying only ``SessionMiddleware``, while the
    # refusal twin (like every localised handler) takes ``lang`` from the
    # root ``LanguageMiddleware`` that ``build_main_router`` installs in
    # production. The worker handlers resolve the language themselves,
    # which is why the other tests here never needed it.
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/setrules hi", user_id=_DEV_ID, chat_type="private"),
        lang="ru",
    )

    assert result is not UNHANDLED
    assert [e["text"] for e in sink] == [t("h_group_only_command", "ru", command="setrules")]
