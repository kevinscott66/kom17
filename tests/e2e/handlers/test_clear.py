"""End-to-end tests for /clear bulk message cleanup (L-47).

Strategy mirrors ``test_moderation.py``: monkey-patch
``bot.session.make_request`` to answer ``GetChatMember`` (admin gate),
record ``DeleteMessage`` calls, and capture ``SendMessage`` replies into
a sink. A ``DeleteMessages`` (bulk) call would fall through to the
"unexpected Telegram call" guard — since #253 there must not be one, and
that is deliberate rather than incidental: the bulk endpoint answers
``True`` for a batch it only partly deleted, which is what made the
confirmed count a fiction.

Scenarios covered
------------------
* /clear N group → deletes the last N message ids, confirms count.
* /clear group → permission denied (non-admin caller), no deletes.
* /clear private → refused with the group-only twin, nothing deleted.
* /clear by reply → deletes the replied-to user's recent id window.
* /clear N over a range that is partly undeletable → the confirmation
  counts what really went away, not what was attempted.
* /clear by a plain admin who is not the chat creator → refused with
  ``h_clear_owner_only``, nothing deleted (#339).
* /clear by a developer who is neither admin nor creator → allowed.
* /clear when ``getChatAdministrators`` fails → fail-closed refusal.

The default fake makes ``_ADMIN_USER_ID`` the chat creator, because the
owner gate ported in #339 sits behind the admin gate and every
pre-existing scenario here is written from the owner's seat.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from pydantic import SecretStr

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
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.clear import build_router
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from pathlib import Path

    from telegram_invite_bot.db import EngineRegistry


async def _wire(
    tmp_path: Path, *, developer_id: int | None = None
) -> tuple[Bot, Dispatcher, EngineRegistry]:
    """Build (Bot, Dispatcher, EngineRegistry) wired with ONLY /clear.

    Self-contained on purpose: the shared ``make_wired`` factory builds
    the whole ``main_router``, so this test owns its narrow wiring to
    exercise the /clear handler in isolation. Production registration of
    ``clear.build_router`` lives in ``main_router`` as usual.
    """
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=developer_id),
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
    registry = build_registry(settings)
    engine = registry.engine(DBName.USERS)
    async with engine.begin() as conn:
        await conn.run_sync(UsersBase.metadata.create_all)

    bot = Bot(
        token=settings.bot.token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router(registry, settings))
    return bot, dispatcher, registry


_ADMIN_USER_ID = 1
_NON_ADMIN_USER_ID = 2
_TARGET_USER_ID = 20
_CHAT_ID = -100


def _group_msg(
    text: str,
    *,
    user_id: int = _ADMIN_USER_ID,
    message_id: int = 500,
    reply_to_user_id: int | None = None,
    update_id: int = 1,
) -> Any:
    return make_message_update(
        text,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        user_id=user_id,
        first_name="Admin",
        message_id=message_id,
        reply_to_user_id=reply_to_user_id,
        update_id=update_id,
    )


def _attach_fake_api(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    admin_user_ids: set[int] | None = None,
    missing_ids: set[int] | None = None,
    creator_id: int | None = _ADMIN_USER_ID,
    admins_error: bool = False,
) -> None:
    """Wire the fake Telegram API.

    ``missing_ids`` are message ids ``deleteMessage`` refuses — the
    normal case for a range this handler reconstructs arithmetically
    (gaps, service messages, anything older than 48h).

    ``creator_id`` answers ``getChatAdministrators`` for the #339 owner
    gate: ``None`` means the chat reports no creator at all. Set
    ``admins_error`` to make that call raise, which is the other way
    ``chat_creator_id`` returns ``None``.
    """
    if admin_user_ids is None:
        admin_user_ids = {_ADMIN_USER_ID}
    if missing_ids is None:
        missing_ids = set()

    from datetime import datetime

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import Chat, Message, User

    def _synth_msg(chat_id: int, text: str) -> Message:
        return Message(
            message_id=100,
            date=datetime(2024, 1, 1),
            chat=Chat(id=chat_id, type="supergroup"),
            from_user=User(id=0, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__

        if name == "GetChatMember":
            from aiogram.types import (
                ChatMemberAdministrator,
                ChatMemberMember,
            )
            from aiogram.types import User as TGUser

            uid = method.user_id
            fake_user = TGUser(id=uid, is_bot=False, first_name="U")
            if uid in admin_user_ids:
                return ChatMemberAdministrator(
                    user=fake_user,
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
            return ChatMemberMember(user=fake_user)

        if name == "GetChatAdministrators":
            from aiogram.types import ChatMemberOwner
            from aiogram.types import User as TGUser

            if admins_error:
                raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
            if creator_id is None:
                return []
            return [
                ChatMemberOwner(
                    user=TGUser(id=creator_id, is_bot=False, first_name="Owner"),
                    is_anonymous=False,
                )
            ]

        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="TestBot", username="testbot")

        if name == "DeleteMessage":
            if method.message_id in missing_ids:
                raise TelegramBadRequest(
                    method=method, message="Bad Request: message to delete not found"
                )
            sink.append({"kind": "delete_message", "id": method.message_id})
            return True

        if name == "SendMessage":
            sink.append({"kind": "text", "chat_id": method.chat_id, "text": method.text})
            return _synth_msg(method.chat_id, method.text)

        raise AssertionError(f"unexpected Telegram call in test: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def test_clear_count_happy_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/clear 5", message_id=500)
    await dispatcher.feed_update(bot, update)

    deleted = [e["id"] for e in sink if e["kind"] == "delete_message"]
    # last 5 ids before the command (495..499), one call each
    assert deleted[:5] == [495, 496, 497, 498, 499]

    # The /clear command message itself is also deleted (best-effort).
    assert 500 in deleted

    # Confirmation is sent (not reply()'d — the command message is gone).
    # The h_clear_success key renders the deleted count (5 here: ids
    # 495..499). Assert the count surfaces in the rendered confirmation.
    text = [e for e in sink if e["kind"] == "text"]
    assert text
    assert "5" in text[-1]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_clear_permission_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    update = _group_msg("/clear 5", user_id=_NON_ADMIN_USER_ID, message_id=500)
    await dispatcher.feed_update(bot, update)

    assert not [e for e in sink if e["kind"] in ("delete_messages", "delete_message")]
    text = [e for e in sink if e["kind"] == "text"]
    assert text
    assert "прав" in text[0]["text"].lower() or "permission" in text[0]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_clear_private_is_refused_not_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A DM ``/clear`` gets an answer, and deletes nothing (#123).

    It used to fall through to ``UNHANDLED`` — the module's chat gate
    sits on the router, so a private invocation simply matched nothing
    and the user heard silence. ``with_chat_type_refusal`` now pairs the
    router with the twin that speaks; what must never change is the
    second half of this test: the refusal path must not touch a single
    message.

    ``lang`` is passed into ``feed_update`` because this file wires the
    ``clear`` router alone, without the language middleware that
    supplies it in production.
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = make_message_update("/clear 5", user_id=_ADMIN_USER_ID, chat_type="private")
    result = await dispatcher.feed_update(bot, update, lang="ru")

    assert result is not UNHANDLED
    assert [e["text"] for e in sink if e["kind"] == "text"] == [
        t("h_group_only_command", "ru", command="clear")
    ]
    assert not [e for e in sink if e["kind"].startswith("delete")]

    await bot.session.close()
    await registry.dispose()


async def test_clear_by_reply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # reply envelope message_id = message_id - 1 = 499 (see conftest)
    update = _group_msg("/clear", message_id=500, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    deleted = [e["id"] for e in sink if e["kind"] == "delete_message"]
    # window: from replied-to (499) up to but excluding cmd (500) -> [499]
    assert deleted[0] == 499

    text = [e for e in sink if e["kind"] == "text"]
    assert text

    await bot.session.close()
    await registry.dispose()


async def test_clear_counts_only_the_messages_that_really_went_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#253(17): the confirmation reports deletions, not attempts.

    The id range is reconstructed arithmetically, so most of a wide
    ``/clear`` is normally undeletable. Until #253 a successful bulk
    ``deleteMessages`` was counted as ``len(batch)`` — the Bot API skips
    what it cannot find and still answers ``True``, so "удалено 100"
    was printed after twelve messages had actually gone.
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    # 497 and 498 do not exist; 495, 496 and 499 do.
    _attach_fake_api(bot, monkeypatch, sink, missing_ids={497, 498})

    update = _group_msg("/clear 5", message_id=500)
    await dispatcher.feed_update(bot, update)

    # The fake only records a delete it actually performed, so 497/498
    # are absent from the sink even though the handler asked for them.
    gone = {e["id"] for e in sink if e["kind"] == "delete_message"}
    assert gone == {495, 496, 499, 500}

    text = [e for e in sink if e["kind"] == "text"]
    assert text
    assert text[-1]["text"] == t("h_clear_success_partial", "ru", count=3, skipped=2)

    await bot.session.close()
    await registry.dispose()


async def test_clear_refused_for_an_admin_who_is_not_the_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#339: the legacy ``clear_command_only_owner`` gate is back.

    The caller is a full Telegram administrator with
    ``can_delete_messages`` — enough for every other moderation command
    — but not the chat creator, so /clear refuses and deletes nothing.
    Legacy did the same by default (bot.py:32175-32180 over a column
    that defaults to 1, bot.py:5732).
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, creator_id=999)

    update = _group_msg("/clear 5", message_id=500)
    await dispatcher.feed_update(bot, update)

    assert not [e for e in sink if e["kind"].startswith("delete")]
    assert [e["text"] for e in sink if e["kind"] == "text"] == [t("h_clear_owner_only", "ru")]

    await bot.session.close()
    await registry.dispose()


async def test_clear_allowed_for_a_developer_who_is_neither_admin_nor_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The developer arm of the legacy gate (bot.py:32178) is preserved."""
    bot, dispatcher, registry = await _wire(tmp_path, developer_id=_NON_ADMIN_USER_ID)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set(), creator_id=999)

    update = _group_msg("/clear 3", user_id=_NON_ADMIN_USER_ID, message_id=500)
    await dispatcher.feed_update(bot, update)

    deleted = [e["id"] for e in sink if e["kind"] == "delete_message"]
    assert deleted[:3] == [497, 498, 499]

    await bot.session.close()
    await registry.dispose()


async def test_clear_fails_closed_when_the_creator_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown creator is not evidence of ownership — refuse.

    ``chat_creator_id`` answers ``None`` for an API error, which is the
    fail-open shape R-FIX-007 exists to prevent.
    """
    bot, dispatcher, registry = await _wire(tmp_path)
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admins_error=True)

    update = _group_msg("/clear 5", message_id=500)
    await dispatcher.feed_update(bot, update)

    assert not [e for e in sink if e["kind"].startswith("delete")]
    assert [e["text"] for e in sink if e["kind"] == "text"] == [t("h_clear_owner_only", "ru")]

    await bot.session.close()
    await registry.dispose()
