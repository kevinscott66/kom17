"""End-to-end tests for T-020 moderation commands.

/ban, /kick, /mute, /warn, /unwarn, /warnings, /pin, /unpin, /fine

Strategy
--------
Moderation handlers make real Bot API calls (``get_chat_member``,
``ban_chat_member``, ``restrict_chat_member``, …). We monkey-patch
``bot.session.make_request`` to:

1. Answer ``GetChatMember`` — return "member" for targets, "administrator"
   for callers who need admin rights, "creator" for the bot developer check.
2. Swallow ``BanChatMember``, ``UnbanChatMember``, ``RestrictChatMember``,
   ``PinChatMessage``, ``UnpinChatMessage``, ``GetMe`` — return ``True``.
3. Record ``SendMessage`` into a sink so assertions can inspect replies.

Admin-check flow
----------------
``_require_admin`` calls ``bot.get_chat_member(chat_id, caller_id)``.
We return status "administrator" for callers with user_id=1 (the default
test admin), and status "member" for everyone else (so permission denials
work for user_id=2).

Scenarios covered
-----------------
* /ban group → happy path (reply)
* /ban group → permission denied (non-admin caller)
* /ban group → target is bot
* /ban group → target is self
* /ban group → target is admin
* /ban private → falls through (UNHANDLED)
* /kick → happy path
* /mute no duration → per-group default duration (L-43, cfg.mute_minutes)
* /mute with duration → timed
* /mute with invalid duration → error message
* /warn → happy path, count increases
* /warn → at threshold, auto-ban fires
* /warn → L-43 config variants (custom max_warns, autoban disabled)
* /unwarn → happy path
* /unwarn → no warnings → error
* /warnings → zero warnings → empty-state message
* /warnings → lists warnings
* /pin → no reply → error
* /pin → happy path
* /unpin → happy path (no reply, pins latest)
* /fine → non-developer → permission denied
* /fine → developer, reply, valid amount → success + DB row
* /fine → no reason → error
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.moderation import ModerationLog, Warning

# R4: importing the rank tables registers them on ModerationBase.metadata
# so make_wired's create_all builds rank_permissions/command_rank_overrides.
from telegram_invite_bot.db.models.rank_tables import (  # noqa: F401
    CommandRankOverride,
    RankPermissionOverride,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers import moderation as moderation_handlers
from telegram_invite_bot.handlers.moderation import _format_duration
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.services.rank_service import clear_rank_caches
from telegram_invite_bot.utils.telegram_kick import KickOutcome
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.fixture(autouse=True)
def _isolate_rank_caches() -> None:
    """R4: rank/creator/matrix caches are module-level (300/600s TTL);
    tests reuse the same small user ids against fresh tmp DBs, so a
    stale cache entry from a previous test would leak ranks across
    tests. Same contract as the language-cache isolation fixture."""
    clear_rank_caches()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_ADMIN_USER_ID = 1  # treated as group admin in fake_make_request
_NON_ADMIN_USER_ID = 2  # treated as member
_TARGET_USER_ID = 20  # target of moderation actions
_DEVELOPER_USER_ID = 99  # bot developer (set via BotConfig)
_CHAT_ID = -100


def _group_msg(
    text: str,
    *,
    user_id: int = _ADMIN_USER_ID,
    first_name: str = "Admin",
    chat_id: int = _CHAT_ID,
    reply_to_user_id: int | None = None,
    reply_to_first_name: str = "Target",
    reply_to_is_bot: bool = False,
    update_id: int = 1,
    as_caption: bool = False,
) -> Any:
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type="supergroup",
        user_id=user_id,
        first_name=first_name,
        reply_to_user_id=reply_to_user_id,
        reply_to_first_name=reply_to_first_name,
        reply_to_is_bot=reply_to_is_bot,
        update_id=update_id,
        as_caption=as_caption,
    )


def _private_msg(text: str, *, user_id: int = _ADMIN_USER_ID, update_id: int = 1) -> Any:
    return make_message_update(
        text,
        user_id=user_id,
        chat_type="private",
        update_id=update_id,
    )


def _attach_fake_api(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    admin_user_ids: set[int] | None = None,
    titular_admin_user_ids: set[int] | None = None,
    anonymous_admin_user_ids: set[int] | None = None,
    bot_user_ids: set[int] | None = None,
    creator_user_id: int | None = None,
    banned_user_ids: set[int] | None = None,
    chat_title: str = "Тестовая группа",
    chat_username: str | None = None,
    chat_invite_link: str | None = None,
) -> None:
    """Monkey-patch bot.session.make_request to simulate Bot API responses.

    ``admin_user_ids``: user IDs that ``GetChatMember`` should report as
    ``administrator``.  Defaults to ``{_ADMIN_USER_ID}``.

    ``titular_admin_user_ids``: user IDs reported as ``administrator``
    with every moderation right OFF — promoted for a title only (#337).
    They are the boundary case: refused as an actor (legacy dropped
    them into the rank branch), still protected as a target.

    ``anonymous_admin_user_ids``: user IDs whose ``GetChatAdministrators``
    row carries ``is_anonymous=True`` — the "Remain anonymous" switch.
    Everyone else is reported with it OFF (#883). Telegram never names
    the acting anonymous admin, so this set is the only thing the gate
    has to reason over.

    ``bot_user_ids``: user IDs that ``GetChatMember`` should report as
    bots (``user.is_bot=True``). Used by the M-M-3 regression to prove
    that ``/ban @SomeBot`` via the username/numeric path is refused
    even though the users-table row lacks an is_bot flag.

    ``creator_user_id``: user ID reported as the chat CREATOR — status
    ``creator`` from ``GetChatMember`` AND a ``ChatMemberOwner`` row in
    ``GetChatAdministrators`` (R4: ``RankService.can_moderate`` resolves
    the creator via the admin list).

    ``banned_user_ids``: user IDs ``GetChatMember`` reports as ``kicked``
    (#252). Only /unban reads that status, and it is the difference
    between lifting a real ban and no-opping on someone who was never
    banned — the two outcomes now send different things.

    ``chat_title`` / ``chat_username`` / ``chat_invite_link``: what
    ``GetChat`` answers. The unban notice is built from that one read and
    is skipped entirely when neither link half is present, so all three
    have to be settable per test.
    """
    if admin_user_ids is None:
        admin_user_ids = {_ADMIN_USER_ID}
    if titular_admin_user_ids is None:
        titular_admin_user_ids = set()
    if anonymous_admin_user_ids is None:
        anonymous_admin_user_ids = set()
    if bot_user_ids is None:
        bot_user_ids = set()
    if banned_user_ids is None:
        banned_user_ids = set()

    from datetime import datetime

    from aiogram.types import Chat, Message, User

    def _synth_msg(chat_id: int, text: str) -> Message:
        return Message(
            message_id=100,
            date=datetime(2024, 1, 1),
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=0, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__

        if name == "GetChatAdministrators":
            # R-FIX-011-fp: anonymous-admin verification path.
            # R4: also serves RankService.can_moderate's creator lookup.
            from aiogram.types import (
                ChatMemberAdministrator as _Adm,
            )
            from aiogram.types import (
                ChatMemberOwner as _Owner,
            )
            from aiogram.types import (
                User as _TGUser,
            )

            admins: list[Any] = [
                _Adm(
                    user=_TGUser(id=uid, is_bot=False, first_name="A"),
                    can_be_edited=False,
                    is_anonymous=uid in anonymous_admin_user_ids,
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
                for uid in admin_user_ids
            ]
            # #883: a title-only administrator sits in the chat's admin
            # list too, with every moderation right OFF. Leaving them out
            # made this fake unable to express the one case the anonymous
            # gate has to narrow over, which is how the hole survived.
            admins += [
                _Adm(
                    user=_TGUser(id=uid, is_bot=False, first_name="T"),
                    can_be_edited=False,
                    is_anonymous=uid in anonymous_admin_user_ids,
                    can_manage_chat=False,
                    can_delete_messages=False,
                    can_manage_video_chats=False,
                    can_restrict_members=False,
                    can_promote_members=False,
                    can_change_info=True,
                    can_invite_users=True,
                    can_post_stories=False,
                    can_edit_stories=False,
                    can_delete_stories=False,
                )
                for uid in titular_admin_user_ids
            ]
            if creator_user_id is not None:
                admins.append(
                    _Owner(
                        user=_TGUser(id=creator_user_id, is_bot=False, first_name="C"),
                        is_anonymous=creator_user_id in anonymous_admin_user_ids,
                    )
                )
            return admins

        if name == "GetChatMember":
            from aiogram.types import (
                ChatMemberAdministrator,
                ChatMemberBanned,
                ChatMemberMember,
                ChatMemberOwner,
            )
            from aiogram.types import (
                User as TGUser,
            )

            uid = method.user_id
            fake_user = TGUser(id=uid, is_bot=uid in bot_user_ids, first_name="U")
            if uid in titular_admin_user_ids:
                # #337: administrator with every moderation right OFF.
                return ChatMemberAdministrator(
                    user=fake_user,
                    can_be_edited=False,
                    is_anonymous=False,
                    can_manage_chat=False,
                    can_delete_messages=False,
                    can_manage_video_chats=False,
                    can_restrict_members=False,
                    can_promote_members=False,
                    can_change_info=True,
                    can_invite_users=True,
                    can_post_stories=False,
                    can_edit_stories=False,
                    can_delete_stories=False,
                )
            if uid in banned_user_ids:
                return ChatMemberBanned(user=fake_user, until_date=datetime(2099, 1, 1))
            if creator_user_id is not None and uid == creator_user_id:
                return ChatMemberOwner(user=fake_user, is_anonymous=False)
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

        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="TestBot", username="testbot")

        if name == "SendMessage":
            sink.append({"kind": "text", "chat_id": method.chat_id, "text": method.text})
            return _synth_msg(method.chat_id, method.text)

        if name == "BanChatMember":
            # RR-4 #39: ``until_date`` is the whole point of a temp ban,
            # so it has to survive into the sink — ``None`` means the
            # ban is permanent, which is a distinct outcome worth
            # asserting rather than an absence.
            sink.append(
                {
                    "kind": "banchatmember",
                    "user_id": getattr(method, "user_id", None),
                    "until_date": getattr(method, "until_date", None),
                }
            )
            return True

        if name == "GetChat":
            return Chat(
                id=method.chat_id,
                type="supergroup",
                title=chat_title,
                username=chat_username,
                invite_link=chat_invite_link,
            )

        if name in {
            "UnbanChatMember",
            "PinChatMessage",
            "UnpinChatMessage",
        }:
            sink.append({"kind": name.lower()})
            return True

        if name == "RestrictChatMember":
            sink.append({"kind": "restrict", "until_date": getattr(method, "until_date", None)})
            return True

        raise AssertionError(f"unexpected Telegram call in test: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


def _dev_bot_config() -> BotConfig:
    return BotConfig(
        BOT_TOKEN=SecretStr("123:abc"),
        DEVELOPER_ID_1=_DEVELOPER_USER_ID,
    )


# ---------------------------------------------------------------------------
# /ban
# ---------------------------------------------------------------------------


async def test_ban_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase],
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert len(ban_calls) == 1

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    # RR-4 #39: a bare /ban is still permanent, and the card now says so
    # out loud rather than leaving the admin to guess.
    reply = text_calls[-1]["text"]
    assert t("h_mod_ban_forever", "ru") in reply
    assert str(_TARGET_USER_ID) in reply
    # No reason was typed, so the reason line must be absent entirely —
    # not rendered empty, and certainly not left as a raw placeholder.
    assert "📝" not in reply
    assert "{" not in reply

    await bot.session.close()
    await registry.dispose()


async def test_ban_permission_denied(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    # non-admin caller: admin_user_ids does NOT include _NON_ADMIN_USER_ID
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    update = _group_msg("/ban", user_id=_NON_ADMIN_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply_text = text_calls[0]["text"].lower()
    # Handler denial (h_mod_no_permission) pre-R4-attach; once
    # ``CommandAccessMiddleware`` is wired, the denial can come
    # from the command-rank gate instead (h_cmdaccess_denied).
    assert (
        "прав" in reply_text
        or "permission" in reply_text
        or "ранга" in reply_text
        or "rank" in reply_text
    )

    await bot.session.close()
    await registry.dispose()


async def test_ban_by_titular_admin_is_refused(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#337: an administrator with no moderation right cannot ban.

    Legacy resolved the actor gate through
    ``telegram_admin_has_mod_rights`` (bot.py:7455-7476) on the way to
    ``require_group_moderation`` (bot.py:7568-7577, used at bot.py:31841
    for ``/ban``), so a member promoted purely for a title fell into the
    rank branch — rank 0, nothing granted. The port checked bare status
    and handed them the whole moderation surface.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        admin_user_ids=set(),
        titular_admin_user_ids={_NON_ADMIN_USER_ID},
    )

    update = _group_msg("/ban", user_id=_NON_ADMIN_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply_text = text_calls[0]["text"].lower()
    assert (
        "прав" in reply_text
        or "permission" in reply_text
        or "ранга" in reply_text
        or "rank" in reply_text
    )

    await bot.session.close()
    await registry.dispose()


async def test_titular_admin_is_still_protected_as_a_target(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of #337: narrowing the ACTOR gate, not the target guard.

    ``_check_target_ok`` deliberately refuses every admin status — wider
    than legacy, which protected only the creator (bot.py:7601). The two
    checks fail in opposite directions on purpose, so a title-only
    administrator loses the power to ban while keeping the protection
    from being banned.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, titular_admin_user_ids={_TARGET_USER_ID})

    update = _group_msg("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert (
        "администрат" in text_calls[0]["text"].lower()
        or "administrator" in text_calls[0]["text"].lower()
    )

    await bot.session.close()
    await registry.dispose()


async def test_ban_target_is_bot(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/ban", reply_to_user_id=_TARGET_USER_ID, reply_to_is_bot=True)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "бот" in text_calls[0]["text"].lower() or "bot" in text_calls[0]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_ban_target_is_self(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # reply_to_user_id == caller (user_id=1)
    update = _group_msg("/ban", user_id=_ADMIN_USER_ID, reply_to_user_id=_ADMIN_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "себ" in text_calls[0]["text"].lower() or "yourself" in text_calls[0]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_ban_target_is_admin(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    # Both caller AND target are admins
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_ADMIN_USER_ID, _TARGET_USER_ID})

    update = _group_msg("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert (
        "администрат" in text_calls[0]["text"].lower()
        or "administrator" in text_calls[0]["text"].lower()
    )

    await bot.session.close()
    await registry.dispose()


async def test_ban_reason_captured_in_audit_log(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-M-4 regression: trailing free-form text after the target is
    captured as the audit-log ``reason``. Previously ban/kick/mute/warn
    /unwarn all discarded everything past the target argument and wrote
    ``reason=""``.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # /ban via reply: every token after the command is the reason.
    update = _group_msg("/ban spamming hard", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (
            (await s.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )

    assert len(rows) == 1
    assert rows[0].reason == "spamming hard", (
        f"M-M-4: expected reason captured, got {rows[0].reason!r}"
    )

    await bot.session.close()
    await registry.dispose()


def test_the_caption_fixture_really_builds_a_caption() -> None:
    """Pin the fixture the two caption tests below stand on.

    ``as_caption`` has to move the body out of ``text`` entirely, not
    merely add a caption: a fixture that kept writing ``text`` would
    leave both tests passing against the very bug they exist to catch,
    because the handler reads text first.
    """
    message = _group_msg("/ban 7d спам", as_caption=True).message
    assert message is not None
    assert message.text is None, "as_caption still delivers a text body"
    assert message.caption == "/ban 7d спам"
    assert message.photo, "Telegram never sends a caption without media"


async def test_ban_duration_and_reason_survive_a_photo_caption(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/ban 7d спам`` typed under a screenshot must still be 7 days.

    aiogram's ``Command`` filter matches ``text or caption``, so the
    command routes here either way — but ``handle_ban`` used to split
    ``message.text``, which a caption message leaves ``None``. Zero
    arguments parsed means no duration token and no reason, and
    ``/ban`` with no duration is deliberately PERMANENT: the admin
    asked for a week and silently got forever, with an empty reason in
    the audit log. Attaching the offending screenshot is the normal way
    to ban someone, so this was the common path.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC)
    update = _group_msg("/ban 7d спам", reply_to_user_id=_TARGET_USER_ID, as_caption=True)
    await dispatcher.feed_update(bot, update)

    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert len(ban_calls) == 1
    until = ban_calls[0]["until_date"]
    assert until is not None, "the caption's 7d was dropped — this ban is permanent"
    # Seven days from now, with slack for the round-trip.
    assert timedelta(days=6) < until - before < timedelta(days=8)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (
            (await s.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].reason == "спам"

    await bot.session.close()
    await registry.dispose()


async def test_mute_duration_survives_a_photo_caption(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same failure, milder outcome: a caption ``/mute 10m`` fell back to
    the group's default mute length instead of the ten minutes asked for.
    """
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC)
    update = _group_msg("/mute 10m", reply_to_user_id=_TARGET_USER_ID, as_caption=True)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert len(restrict_calls) == 1
    until = restrict_calls[0]["until_date"]
    assert until is not None
    # Ten minutes, not the config default — the two are far enough
    # apart that a window this tight can only hold the parsed token.
    assert timedelta(minutes=9) < until - before < timedelta(minutes=11)

    await bot.session.close()
    await registry.dispose()


async def test_warn_reason_captured_in_audit_log(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-M-4 regression for /warn: reason should appear in both the
    warnings row and the audit-log row.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/warn off-topic posting", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        warn_rows = (
            (await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID)))
            .scalars()
            .all()
        )
        log_rows = (
            (await s.execute(select(ModerationLog).where(ModerationLog.action == "warn")))
            .scalars()
            .all()
        )

    assert len(warn_rows) == 1
    assert warn_rows[0].reason == "off-topic posting"
    assert len(log_rows) == 1
    assert log_rows[0].reason == "off-topic posting"

    await bot.session.close()
    await registry.dispose()


async def test_ban_uses_persisted_user_settings_language(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-M-5 regression: moderation replies must use the caller's
    persisted ``user_settings.language`` (the explicit /lang choice),
    not the Telegram client locale.

    Pre-fix: ``_lang(message)`` consulted only
    ``message.from_user.language_code`` — a user who set EN via /lang
    but kept their Telegram client on RU would see RU moderation
    replies. The new pipeline routes every other handler through
    ``UserSettingsRepo.get_language`` (Stage 26); moderation was the
    last inconsistency.

    Setup: caller has Telegram ``language_code="ru"`` but seeded with
    ``user_settings.language="en"``. The reply must be the EN variant.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.db.models.user_settings import UserSetting
    from telegram_invite_bot.db.models.users import User

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # Seed: admin user has explicit EN via /lang despite RU Telegram locale.
    engine = registry.engine(DBName.USERS)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(User(user_id=_ADMIN_USER_ID, first_name="Admin"))
        await s.flush()
        s.add(UserSetting(user_id=_ADMIN_USER_ID, language="en"))
        await s.commit()

    update = make_message_update(
        "/ban",
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        user_id=_ADMIN_USER_ID,
        first_name="Admin",
        language_code="ru",
        reply_to_user_id=_TARGET_USER_ID,
        reply_to_first_name="Target",
    )
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls, "expected a reply"
    reply_text = text_calls[-1]["text"]
    # Compare against the rendered keys rather than a hand-copied phrase,
    # so a copy edit never fails this test for the wrong reason.
    assert t("h_mod_ban_forever", "en") in reply_text, (
        f"M-M-5: expected EN reply (persisted user_settings.language='en'), got: {reply_text!r}"
    )
    assert t("h_mod_ban_forever", "ru") not in reply_text

    await bot.session.close()
    await registry.dispose()


async def test_ban_username_target_bot_refused(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-M-3 regression: ``/ban <bot_id>`` via numeric/@username target
    must be refused. The resolve path can't read is_bot from the users
    table, so the handler must re-probe via ``get_chat_member`` and
    treat the target as a bot when Telegram says so.

    Pre-fix: ``is_bot`` was hardcoded to ``False`` on the username/id
    path, letting ``/ban @SomeBot`` proceed to the actual ban call.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    # Target id 55555 is reported as a bot by get_chat_member
    _attach_fake_api(bot, monkeypatch, sink, bot_user_ids={55555})

    update = _group_msg("/ban 55555")  # numeric-id form, no reply
    await dispatcher.feed_update(bot, update)

    # Must NOT have called ban_chat_member
    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert ban_calls == [], "M-M-3: bot target via numeric id must not be banned"

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    # Refusal references bot
    assert "бот" in text_calls[-1]["text"].lower() or "bot" in text_calls[-1]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_ban_private_chat_falls_through(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _private_msg("/ban")
    result = await dispatcher.feed_update(bot, update)
    # Pre-attach: UNHANDLED (falls through to legacy). Post-attach the
    # R4 command-access middleware consumes the sub-rank private command
    # with a denial — either way nothing here moderates anything.
    if result is not UNHANDLED:
        assert [e for e in sink if e["kind"] == "text"]
    assert [e for e in sink if e["kind"] != "text"] == []

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /kick
# ---------------------------------------------------------------------------


async def test_kick_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/kick", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    # kick = ban + unban
    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    unban_calls = [e for e in sink if e["kind"] == "unbanchatmember"]
    assert len(ban_calls) >= 1
    assert len(unban_calls) >= 1

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "исключ" in text_calls[-1]["text"].lower() or "kicked" in text_calls[-1]["text"].lower()
    # #343: a clean kick must not borrow the LEFT_BANNED caveat line.
    assert "снять бан не удалось" not in text_calls[-1]["text"]

    await bot.session.close()
    await registry.dispose()


def _force_kick_outcome(monkeypatch: pytest.MonkeyPatch, outcome: KickOutcome) -> None:
    """Pin what the ban/unban pair reports back, without a fake API.

    ``kick_member`` has its own suite over the Telegram calls
    (``tests/regression/test_captcha_kick_leaves_no_ban.py``); what these
    two tests are about is the handler's reaction to each verdict, which
    the fake API cannot produce on demand.
    """

    async def _fake(*_a: Any, **_kw: Any) -> KickOutcome:
        return outcome

    monkeypatch.setattr(moderation_handlers, "kick_member", _fake)


async def _kick_log_rows(registry: Any) -> list[ModerationLog]:
    from sqlalchemy import select

    async with registry.session(DBName.MODERATION)() as session:
        rows = await session.execute(select(ModerationLog).where(ModerationLog.action == "kick"))
        return list(rows.scalars().all())


async def test_kick_reports_failure_and_audits_nothing_when_the_ban_never_landed(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#269: a kick that did not happen must not be logged as one.

    The old code could only tell success from failure for the pair as a
    whole. Now the ban failing on its own is the one case where nothing
    changed at all — the target is still in the chat — so the audit row
    would be a false record of a removal.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)
    _force_kick_outcome(monkeypatch, KickOutcome.FAILED)

    await dispatcher.feed_update(bot, _group_msg("/kick", reply_to_user_id=_TARGET_USER_ID))

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert text_calls[-1]["text"] == t("h_mod_kick_fail", "ru")
    assert await _kick_log_rows(registry) == []

    await bot.session.close()
    await registry.dispose()


async def test_kick_refuses_a_target_who_is_already_banned(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2031: ``/kick`` must not be a way to reach ``/unban``.

    A kick is a ban plus an unban, and the rank that reaches this handler
    holds ``can_kick`` while ``/unban`` is gated on ``can_ban``
    (``core/ranks.py``). Run over a standing ban, the pair's second half
    is that unban — so the command has to stop before it, and it must not
    leave an ``action="kick"`` row claiming a removal that never happened.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, banned_user_ids={_TARGET_USER_ID})

    await dispatcher.feed_update(bot, _group_msg("/kick", reply_to_user_id=_TARGET_USER_ID))

    assert [e for e in sink if e["kind"] == "unbanchatmember"] == []
    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "уже забанен" in text_calls[-1]["text"]
    assert await _kick_log_rows(registry) == []

    await bot.session.close()
    await registry.dispose()


async def test_kick_still_succeeds_when_only_the_unban_was_lost(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user IS out — the residual ban expires on its own.

    Reporting failure here would be as wrong as the old silence was:
    the admin's command took effect. But reporting the plain success line
    would be wrong too (#343) — the leftover ban is exactly what
    ``utils.telegram_kick``'s third defence promises to surface, so the
    admin gets its own line and the audit row carries the outcome.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)
    _force_kick_outcome(monkeypatch, KickOutcome.LEFT_BANNED)

    await dispatcher.feed_update(bot, _group_msg("/kick", reply_to_user_id=_TARGET_USER_ID))

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "исключ" in text_calls[-1]["text"].lower()
    assert text_calls[-1]["text"] != t("h_mod_kick_success", "ru", mention="")
    assert "снять бан не удалось" in text_calls[-1]["text"]
    rows = await _kick_log_rows(registry)
    assert len(rows) == 1
    assert rows[0].details == KickOutcome.LEFT_BANNED.value

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /mute
# ---------------------------------------------------------------------------


async def test_mute_no_duration_uses_config_default(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-43: /mute with no duration token applies the per-group default
    (``group_mod_config.mute_minutes``, defaults view = 1440 min = 24h,
    mirroring legacy MUTE_DURATION), NOT an indefinite restriction.
    """
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC)
    update = _group_msg("/mute", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert len(restrict_calls) == 1
    until = restrict_calls[0]["until_date"]
    assert until is not None, "L-43: default mute must be finite (cfg.mute_minutes)"
    # Defaults view: 1440 minutes = 24h.
    expected = before + timedelta(minutes=1440)
    assert abs((until - expected).total_seconds()) < 10

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    last = text_calls[-1]["text"].lower()
    # 86400s renders as "1д"/"1d" via _format_duration.
    assert "1д" in last or "1d" in last

    await bot.session.close()
    await registry.dispose()


async def test_mute_no_duration_uses_seeded_mute_minutes(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-43: a group with ``mute_minutes=30`` configured via /modcfg gets
    a 30-minute default mute when the admin gives no duration token."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.db.models.group_mod_config import GroupModConfig

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(GroupModConfig(group_id=_CHAT_ID, mute_minutes=30))
        await s.commit()

    before = datetime.now(UTC)
    update = _group_msg("/mute", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert len(restrict_calls) == 1
    until = restrict_calls[0]["until_date"]
    assert until is not None
    expected = before + timedelta(minutes=30)
    assert abs((until - expected).total_seconds()) < 10

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "30" in text_calls[-1]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_mute_with_duration(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/mute 10m", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert len(restrict_calls) == 1
    # until_date should be set (non-None)
    assert restrict_calls[0]["until_date"] is not None

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "10" in text_calls[-1]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_mute_duration_clamped_to_telegram_max(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request beyond Telegram's 366-day ceiling (``/mute 52w`` = 364d
    is under; ``/mute 99999d`` is over) must be clamped so the API call
    doesn't fail wholesale. The recorded ``until_date`` must sit at or
    below now + 366 days."""
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC)
    update = _group_msg("/mute 99999d", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert len(restrict_calls) == 1
    until = restrict_calls[0]["until_date"]
    assert until is not None
    # Clamp ceiling: now + 366 days (allow a small scheduling slack).
    ceiling = before + timedelta(days=366) + timedelta(seconds=5)
    assert until <= ceiling

    await bot.session.close()
    await registry.dispose()


@pytest.mark.parametrize("token", ["10s", "0s", "29s", "0m", "0h"])
async def test_mute_duration_clamped_to_telegram_min(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """#1734: the other edge, and the dangerous one.

    Telegram reads an ``until_date`` less than 30 seconds out as a
    PERMANENT restriction. ``/mute 10s`` therefore silenced a member
    forever while the reply, the audit row and the log line all said ten
    seconds — the mute the admins can see is over, so nobody ever lifts
    it. ``/ban`` has rounded up to a minute since RR-4 #39 for exactly
    this reason; the floor belongs on both.
    """
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC)
    update = _group_msg(f"/mute {token}", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert len(restrict_calls) == 1
    until = restrict_calls[0]["until_date"]
    assert until is not None
    # Clamp floor: at least a minute out, so Telegram honours it as timed.
    assert until >= before + timedelta(seconds=60)

    # And the reply must report the clamped length, not the asked-for
    # one — a mute that says "10s" and lasts a minute is a smaller lie
    # than one that says "10s" and lasts forever, but still a lie.
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "10с" not in text_calls[-1]["text"]
    assert "0с" not in text_calls[-1]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_mute_invalid_duration(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-parseable duration token should NOT block the mute — it falls through as no-duration."""
    # The handler treats an invalid duration token as "no duration", which
    # since L-43 means the group-config default duration applies. We just
    # check the mute happens.
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # "badtoken" is not a duration — handler should still mute (default duration)
    update = _group_msg("/mute badtoken", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    # With a badtoken as 2nd part and a reply, it should still mute
    assert len(restrict_calls) == 1

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /unmute
# ---------------------------------------------------------------------------


async def test_unmute_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """/unmute restores send permissions (a RestrictChatMember with no
    until_date) and confirms. Reversal of /mute — would be a silent
    dead-end without this handler now the legacy bridge is gone.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/unmute", reply_to_user_id=_TARGET_USER_ID)
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert len(restrict_calls) == 1
    # Lifting a restriction carries no until_date.
    assert restrict_calls[0]["until_date"] is None

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "мут" in text_calls[-1]["text"].lower() or "unmut" in text_calls[-1]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_unmute_permission_denied(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    update = _group_msg("/unmute", user_id=_NON_ADMIN_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    restrict_calls = [e for e in sink if e["kind"] == "restrict"]
    assert not restrict_calls  # never reached the API
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply_text = text_calls[0]["text"].lower()
    # See test_ban_permission_denied: handler OR command-rank denial.
    assert (
        "прав" in reply_text
        or "permission" in reply_text
        or "ранга" in reply_text
        or "rank" in reply_text
    )

    await bot.session.close()
    await registry.dispose()


async def test_unmute_private_chat_falls_through(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    result = await dispatcher.feed_update(bot, _private_msg("/unmute"))
    # Pre-attach: UNHANDLED (falls through to legacy). Post-attach the
    # R4 command-access middleware consumes the sub-rank private command
    # with a denial — either way nothing here moderates anything.
    if result is not UNHANDLED:
        assert [e for e in sink if e["kind"] == "text"]
    assert [e for e in sink if e["kind"] != "text"] == []

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /unban
# ---------------------------------------------------------------------------


async def test_unban_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """/unban lifts a ban (UnbanChatMember) and confirms. The target is
    resolved by numeric id since a banned user can't be replied to.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg(f"/unban {_TARGET_USER_ID}")
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED

    unban_calls = [e for e in sink if e["kind"] == "unbanchatmember"]
    assert len(unban_calls) == 1

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "разбан" in text_calls[-1]["text"].lower() or "unban" in text_calls[-1]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_unban_permission_denied(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    update = _group_msg(f"/unban {_TARGET_USER_ID}", user_id=_NON_ADMIN_USER_ID)
    await dispatcher.feed_update(bot, update)

    unban_calls = [e for e in sink if e["kind"] == "unbanchatmember"]
    assert not unban_calls
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply_text = text_calls[0]["text"].lower()
    # See test_ban_permission_denied: handler OR command-rank denial.
    assert (
        "прав" in reply_text
        or "permission" in reply_text
        or "ранга" in reply_text
        or "rank" in reply_text
    )

    await bot.session.close()
    await registry.dispose()


async def test_unban_private_chat_falls_through(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    result = await dispatcher.feed_update(bot, _private_msg("/unban 20"))
    # Pre-attach: UNHANDLED (falls through to legacy). Post-attach the
    # R4 command-access middleware consumes the sub-rank private command
    # with a denial — either way nothing here moderates anything.
    if result is not UNHANDLED:
        assert [e for e in sink if e["kind"] == "text"]
    assert [e for e in sink if e["kind"] != "text"] == []

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# #252(15): the courtesy DM. Legacy ``remove_ban`` (bot.py:9040-9068) told
# the target where to rejoin; the port dropped it, so a lifted ban was
# invisible to the one person it was lifted for. It is back, with two
# gates legacy had only one of.
# ---------------------------------------------------------------------------


async def test_unban_notifies_the_target_with_a_join_link(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unbanned user is DM'd the group's public link.

    Two sends, and which is which matters: the confirmation goes to the
    group, the notice to the target's own chat.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        banned_user_ids={_TARGET_USER_ID},
        chat_username="testgroup",
    )

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    dms = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _TARGET_USER_ID]
    assert len(dms) == 1
    assert "https://t.me/testgroup" in dms[0]["text"]
    assert [e for e in sink if e["kind"] == "text" and e["chat_id"] == _CHAT_ID]

    await bot.session.close()
    await registry.dispose()


async def test_unban_falls_back_to_the_primary_invite_link(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private group has no ``@username``, so the notice uses the
    primary invite link the group's own admins already created.

    It is *read*, never minted: ``export_chat_invite_link`` revokes the
    primary link and hands back a replacement, which would break every
    copy already pasted elsewhere. The fake raises on any Telegram call
    it does not know, so a mint attempt fails this test rather than
    passing it quietly.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        banned_user_ids={_TARGET_USER_ID},
        chat_invite_link="https://t.me/+abcdef",
    )

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    dms = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _TARGET_USER_ID]
    assert len(dms) == 1
    assert "https://t.me/+abcdef" in dms[0]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_unban_without_a_link_sends_no_notice(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No username and no invite link means no way back in, and legacy's
    link gate is kept: "you were unbanned" with nowhere to go is an
    announcement the reader can do nothing with.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, banned_user_ids={_TARGET_USER_ID})

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    assert [e for e in sink if e["kind"] == "unbanchatmember"]
    assert not [e for e in sink if e["kind"] == "text" and e["chat_id"] == _TARGET_USER_ID]

    await bot.session.close()
    await registry.dispose()


async def test_unban_of_a_user_who_was_not_banned_sends_no_notice(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate legacy did not have (bot.py:9040 notifies regardless).

    ``only_if_banned`` already makes the API call a no-op here, so
    without this check ``/unban <any id>`` would be a way for an admin
    to make the bot deliver the group's invite link to any user id they
    care to name. The unban itself still runs — only the DM is skipped.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, chat_username="testgroup")

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    assert [e for e in sink if e["kind"] == "unbanchatmember"]
    assert not [e for e in sink if e["kind"] == "text" and e["chat_id"] == _TARGET_USER_ID]

    await bot.session.close()
    await registry.dispose()


async def test_unban_notice_escapes_the_group_title(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The title is written by the group's own admins and the bot's
    default parse mode is HTML, so a ``<`` in it would otherwise take
    the join link down with it.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        banned_user_ids={_TARGET_USER_ID},
        chat_title="<b>Клуб</b>",
        chat_username="testgroup",
    )

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    dms = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _TARGET_USER_ID]
    assert len(dms) == 1
    assert "&lt;b&gt;Клуб&lt;/b&gt;" in dms[0]["text"]
    assert "<b>" not in dms[0]["text"]
    assert "https://t.me/testgroup" in dms[0]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_unban_survives_a_failing_courtesy_dm(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target who never started the bot cannot be DM'd. That is the
    normal case, not an error: the ban is lifted either way and the
    moderator still gets their confirmation.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        banned_user_ids={_TARGET_USER_ID},
        chat_username="testgroup",
    )
    inner = bot.session.make_request

    async def blocked_dm(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "SendMessage" and method.chat_id == _TARGET_USER_ID:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        return await inner(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", blocked_dm)

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    assert [e for e in sink if e["kind"] == "unbanchatmember"]
    group_texts = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _CHAT_ID]
    assert group_texts
    assert "разбан" in group_texts[-1]["text"].lower()

    await bot.session.close()
    await registry.dispose()


# ── /unban clears the warning slate (#1780) ──────────────────────────────────


async def _active_warns(registry: EngineRegistry, *, chat_id: int = _CHAT_ID) -> list[Warning]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        rows = await s.execute(
            select(Warning).where(
                Warning.user_id == _TARGET_USER_ID,
                Warning.chat_id == chat_id,
                Warning.active.is_(True),
            )
        )
        return list(rows.scalars().all())


async def _unban_log_rows(registry: EngineRegistry) -> list[ModerationLog]:
    """Every ``unban`` audit row, in insertion order (#1780)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        stmt = (
            select(ModerationLog).where(ModerationLog.action == "unban").order_by(ModerationLog.id)
        )
        return list((await s.execute(stmt)).scalars().all())


async def test_unban_clears_the_targets_warnings_in_that_chat(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1780 — otherwise /unban is a no-op with a delayed fuse.

    The automod escalation re-bans on ``count >= max_warns``
    (``wordfilter.py`` ``_escalate``), and it reaches that branch without
    writing a new warning. So a target unbanned while still sitting at the
    cap is permanently re-banned by their very next filtered message, with
    no warnings of grace and nothing in the DB explaining why. The
    moderator has no reason to look: they just "cleared" the ban.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, banned_user_ids={_TARGET_USER_ID})

    await _seed_warnings(registry, 3)
    assert len(await _active_warns(registry)) == 3

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    assert not await _active_warns(registry)
    group_texts = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _CHAT_ID]
    assert group_texts
    assert "3" in group_texts[-1]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_unban_records_the_clear_as_one_unwarn_row(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One audit row for the sweep, not one per warning.

    Three rows would read as three separate moderator decisions; the
    sweep was a single act, and ``details`` carries how wide it was.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, banned_user_ids={_TARGET_USER_ID})

    await _seed_warnings(registry, 3)
    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    unwarn = await _unwarn_log_rows(registry)
    assert len(unwarn) == 1
    assert unwarn[0].details == "cleared=3"
    assert unwarn[0].admin_id == _ADMIN_USER_ID
    assert len(await _unban_log_rows(registry)) == 1

    await bot.session.close()
    await registry.dispose()


async def test_unban_of_a_user_with_no_warnings_writes_no_unwarn_row(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to sweep must leave the audit trail alone — and must not
    claim in the reply that anything was cleared."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, banned_user_ids={_TARGET_USER_ID})

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    assert not await _unwarn_log_rows(registry)
    group_texts = [e for e in sink if e["kind"] == "text" and e["chat_id"] == _CHAT_ID]
    assert group_texts
    assert "предупрежден" not in group_texts[-1]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_unban_audit_and_sweep_survive_a_failing_confirmation(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1866: the Telegram unban already happened — the DB must follow.

    ``BaseSessionMiddleware`` commits on handler return and rolls back
    on any raise (``middlewares/base.py:131-132``), so before the
    checkpoint a failed group confirmation unwound both writes /unban
    makes: the ``unban`` audit row and the #1780 warning sweep. Nothing
    put the ban back — ``UnbanChatMember`` had already been accepted —
    so the target rejoined still sitting at the cap, and the automod
    escalation re-bans on ``count >= max_warns`` without writing a new
    warning. Their next filtered message was a permanent re-ban with no
    grace, and ``moderation_log`` held no record of who had lifted the
    first one. The moderator had no reason to look: from the chat's
    point of view the unban simply produced no reply.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, banned_user_ids={_TARGET_USER_ID})

    # Wrap the fake API rather than replacing it: the unban call itself
    # still has to succeed, because a rollback that follows a *failed*
    # unban would be the correct behaviour and would prove nothing.
    faked = bot.session.make_request

    async def failing_confirmation(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "SendMessage" and method.chat_id == _CHAT_ID:
            raise RuntimeError("the confirmation never left the building")
        return await faked(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", failing_confirmation)

    await _seed_warnings(registry, 3)

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    assert [e for e in sink if e["kind"] == "unbanchatmember"], (
        "the irreversible half this test is about never ran"
    )
    assert not await _active_warns(registry)
    assert len(await _unban_log_rows(registry)) == 1
    unwarn = await _unwarn_log_rows(registry)
    assert len(unwarn) == 1
    assert unwarn[0].details == "cleared=3"
    # The confirmation really did die, so the durability above comes
    # from the checkpoint and not from a reply that got through.
    assert not [e for e in sink if e["kind"] == "text" and e["chat_id"] == _CHAT_ID]

    await bot.session.close()
    await registry.dispose()


async def test_unban_leaves_another_chats_warnings_alone(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The (user, chat) scope is a security property, not an optimisation
    — the same one ``remove_warning_by_id`` preserves verbatim from
    legacy. An operator in chat A must not be able to wipe chat B's
    record by unbanning someone in their own.
    """
    other_chat = _CHAT_ID - 1
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, banned_user_ids={_TARGET_USER_ID})

    await _seed_warnings(registry, 2)
    await _seed_warnings(registry, 2, chat_id=other_chat)

    await dispatcher.feed_update(bot, _group_msg(f"/unban {_TARGET_USER_ID}"))

    assert not await _active_warns(registry)
    assert len(await _active_warns(registry, chat_id=other_chat)) == 2

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /warn
# ---------------------------------------------------------------------------


async def test_warn_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "1/" in text_calls[-1]["text"]  # count/max

    # Verify DB row was created
    engine = registry.engine(DBName.MODERATION)
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        result = await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].active is True

    await bot.session.close()
    await registry.dispose()


async def _warn_row(registry: EngineRegistry, user_id: int = _TARGET_USER_ID) -> Warning:
    """Return the single warning row for ``user_id`` (#252)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(select(Warning).where(Warning.user_id == user_id))).scalars().all()
    assert len(rows) == 1
    return rows[0]


async def test_warn_reply_form_takes_a_leading_duration(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252: ``/warn 7d <reason>`` sets the warning's lifetime.

    Pre-fix the port had no duration argument at all: every warning
    expired on the repository's 30-day default and the token the admin
    typed was swallowed into the reason ("Причина: 7d"). Legacy read it
    from exactly this position (bot.py:31474-31487).
    """
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC).replace(tzinfo=None)
    await dispatcher.feed_update(bot, _group_msg("/warn 7d спам", reply_to_user_id=_TARGET_USER_ID))

    row = await _warn_row(registry)
    assert row.reason == "спам"  # the duration token is consumed, not stored
    assert row.expires is not None
    assert timedelta(days=6) < row.expires - before < timedelta(days=8)

    await bot.session.close()
    await registry.dispose()


async def test_warn_arg_form_takes_a_trailing_duration(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252: ``/warn <target> 7d <reason>`` — the same courtesy /ban and
    /mute extend to the args form. A numeric target must not be mistaken
    for a duration: ``allow_bare_number`` is False once a target argument
    competes for the slot.
    """
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC).replace(tzinfo=None)
    await dispatcher.feed_update(bot, _group_msg(f"/warn {_TARGET_USER_ID} 7d спам"))

    row = await _warn_row(registry)
    assert row.reason == "спам"
    assert row.expires is not None
    assert timedelta(days=6) < row.expires - before < timedelta(days=8)

    await bot.session.close()
    await registry.dispose()


async def test_warn_zero_never_expires(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252: ``/warn 0`` is legacy's permanent warning — ``expires_days=0``
    is the value ``add_warning`` turns into a NULL ``expires`` column
    (moderation_repo.py:127-129).
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/warn 0 спам", reply_to_user_id=_TARGET_USER_ID))

    row = await _warn_row(registry)
    assert row.reason == "спам"
    assert row.expires is None

    await bot.session.close()
    await registry.dispose()


async def test_warn_sub_day_duration_falls_back_to_the_default(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252: a request shorter than a day is legacy's ``or 30`` fallback,
    NOT permanence (bot.py:31484).

    A warning's lifetime is stored in days, so "/warn 12h" has no
    representation; legacy rounded it down to 0 days and then let the
    ``or 30`` turn that into the default. Reading it as "never expires"
    instead would be the wrong surprise of the two.
    """
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC).replace(tzinfo=None)
    await dispatcher.feed_update(
        bot, _group_msg("/warn 12h спам", reply_to_user_id=_TARGET_USER_ID)
    )

    row = await _warn_row(registry)
    assert row.reason == "спам"
    assert row.expires is not None
    assert timedelta(days=29) < row.expires - before < timedelta(days=31)

    await bot.session.close()
    await registry.dispose()


async def test_warn_without_a_duration_keeps_the_whole_reason(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252 non-regression: a reason that is not a duration stays whole
    and the warning keeps the 30-day default — the behaviour every
    pre-#252 caller relied on.
    """
    from datetime import UTC, datetime, timedelta

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    before = datetime.now(UTC).replace(tzinfo=None)
    await dispatcher.feed_update(
        bot, _group_msg("/warn спам в чате", reply_to_user_id=_TARGET_USER_ID)
    )

    row = await _warn_row(registry)
    assert row.reason == "спам в чате"
    assert row.expires is not None
    assert timedelta(days=29) < row.expires - before < timedelta(days=31)

    await bot.session.close()
    await registry.dispose()


async def test_warn_auto_ban_at_threshold(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Third warning triggers auto-ban — and records that it did.

    #272.1: this used to assert only ``len(ban_calls) >= 1`` plus a
    substring of the reply, so three separate defects were invisible to
    it — a missing ``until_date``, a missing ``moderation_log`` row, and
    a success reply sent even when ``ban_chat_member`` raised. The first
    two are asserted here; the third has its own test below.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.handlers.moderation import WARNING_THRESHOLD

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # Issue WARNING_THRESHOLD - 1 warnings manually via repo
    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for _ in range(WARNING_THRESHOLD - 1):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="pre-seeded",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    # Issue the threshold-breaking warn via handler
    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=10)
    await dispatcher.feed_update(bot, update)

    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert len(ban_calls) == 1
    assert ban_calls[0]["user_id"] == _TARGET_USER_ID
    # The auto-ban is permanent, on purpose. Legacy passed
    # ``duration_minutes=24 * 7 * 60`` but ``add_ban`` never forwarded it
    # to Telegram (bot.py:8970) and ``cleanup_expired_bans`` (bot.py:9150)
    # was never called, so legacy's week was fiction. Pinned so a future
    # #282 decision is a deliberate edit, not a silent drift.
    assert ban_calls[0]["until_date"] is None

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    last = text_calls[-1]["text"].lower()
    assert "заблок" in last or "banned" in last

    # #272.1: the sanction has to reach the audit ledger like every
    # other one does. Read through a FRESH session so a row that was
    # only ever in the handler's identity map cannot satisfy this.
    sm2 = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm2() as s2:
        ban_rows = (
            (await s2.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )
    assert len(ban_rows) == 1
    assert ban_rows[0].user_id == _TARGET_USER_ID
    assert ban_rows[0].chat_id == _CHAT_ID
    assert ban_rows[0].details == f"auto_ban_after_warns={WARNING_THRESHOLD}"
    # The ledger has to name the ADMIN who issued the warn, not the
    # target — swapping the two is invisible to a row-count assertion
    # and would quietly make the audit trail useless.
    assert ban_rows[0].admin_id == _ADMIN_USER_ID
    # ``reason`` round-trips: the auto-ban inherits the warn's reason
    # (empty for a bare /warn, per ``_extract_reason``); the
    # reason-carrying case is pinned by
    # ``test_warn_auto_ban_ledger_carries_the_reason`` below.
    assert not ban_rows[0].reason

    await bot.session.close()
    await registry.dispose()


async def test_warn_reports_a_failed_auto_ban_instead_of_claiming_it(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#272.1: a bot without ban rights must not be told the ban worked.

    Pre-fix the success reply sat outside the ``try``, so the only
    visible difference between "banned" and "the API refused" was a
    WARNING in the journal. The warn itself still counts — that part
    already succeeded — but the admin has to learn the sanction did not
    land, and no ``ban`` audit row may be written for a ban that never
    happened.
    """
    from datetime import datetime

    from aiogram.exceptions import TelegramBadRequest
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.handlers.moderation import WARNING_THRESHOLD

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    passthrough = bot.session.make_request

    async def refuse_bans(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "BanChatMember":
            raise TelegramBadRequest(method=method, message="not enough rights")
        return await passthrough(_bot, method, timeout=timeout)

    monkeypatch.setattr(bot.session, "make_request", refuse_bans)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for _ in range(WARNING_THRESHOLD - 1):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="seed",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=11)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    last = text_calls[-1]["text"]
    # The reply must NOT claim the ban landed...
    assert "заблокирован." not in last
    # ...and must say the ban failed, while still confirming the warn.
    assert "не удалось" in last.lower()
    assert f"{WARNING_THRESHOLD}/{WARNING_THRESHOLD}" in last

    async with sm() as s:
        warn_rows = (
            (await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID)))
            .scalars()
            .all()
        )
        ban_rows = (
            (await s.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )
    # The warning was recorded — only the sanction failed.
    assert len(warn_rows) == WARNING_THRESHOLD
    # No audit row for a ban that never happened.
    assert ban_rows == []

    await bot.session.close()
    await registry.dispose()


async def test_warn_at_limit_no_double_warn(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When already at threshold, /warn is rejected, not added.

    #280: the assertion here used to be a substring check that accepted
    ``"лимит"`` *or* ``"limit"`` *or* the bare threshold digit — and
    every branch of the handler satisfies at least one of those. Delete
    the guard at ``handlers/moderation.py:1787-1789`` and /warn writes a
    fourth row and bans the target; the reply then reads «Лимит
    достигнут — пользователь заблокирован» (``h_mod_warn_auto_ban``),
    which the old check accepted just as happily. The test also never
    looked at the table, though "not added" is a claim about the table.

    So: the exact copy, the row count, and the absence of a sanction.
    """
    from telegram_invite_bot.handlers.moderation import WARNING_THRESHOLD

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for _ in range(WARNING_THRESHOLD):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="pre-seeded",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=11)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert len(text_calls) == 1
    assert text_calls[0]["text"] == t("h_mod_warn_at_limit", "ru", max=WARNING_THRESHOLD)

    # "Not added" is a statement about the table.
    from sqlalchemy import select

    async with sm() as s:
        warn_rows = (
            (await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID)))
            .scalars()
            .all()
        )
    assert len(warn_rows) == WARNING_THRESHOLD
    # And the refusal must not have applied the sanction on the way out.
    assert [e for e in sink if e["kind"] == "banchatmember"] == []

    await bot.session.close()
    await registry.dispose()


async def test_warn_auto_ban_ledger_carries_the_reason(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#272.1: the auto-ban ledger row inherits the warn's reason.

    ``record_action(reason=reason)`` is easy to mutate to
    ``reason=None`` without any row-count assertion noticing, which
    would leave the operator an audit trail that records *that* a ban
    happened but never *why*.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.handlers.moderation import WARNING_THRESHOLD

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    from datetime import datetime

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        for _ in range(WARNING_THRESHOLD - 1):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="seed",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    update = _group_msg("/warn злостный спам", reply_to_user_id=_TARGET_USER_ID, update_id=220)
    await dispatcher.feed_update(bot, update)

    assert len([e for e in sink if e["kind"] == "banchatmember"]) == 1

    sm2 = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm2() as s2:
        ban_rows = (
            (await s2.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )
    assert len(ban_rows) == 1
    assert ban_rows[0].reason == "злостный спам"

    await bot.session.close()
    await registry.dispose()


async def test_warn_past_threshold_skips_the_ban_and_the_ledger(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-M-1 racer branch: ``new_count > threshold`` reuses the crossing
    call's outcome — no second ``ban_chat_member``, no second audit row.

    That branch carries an eight-line comment and, until this test, no
    coverage: it is only reachable when a second admin's ``/warn`` slips
    between the at-limit gate's ``get_warning_count`` read and the
    crossing call's ``add_warning`` commit. The concurrency test below
    cannot open that window — aiogram feeds both updates on one event
    loop and aiosqlite serialises the writes, so the second update just
    hits the at-limit gate instead. So open the window explicitly:
    freeze the *gate's* read one below the threshold while the real
    rows sit at it, which is exactly the state a losing racer observes.
    Without this, relaxing ``new_count == threshold`` to ``>=`` passes
    the whole file while double-banning and double-logging in prod.
    """
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.handlers.moderation import WARNING_THRESHOLD
    from telegram_invite_bot.repositories.moderation_repo import ModerationRepo

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        for _ in range(WARNING_THRESHOLD):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="seed",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    async def _stale_count(self: ModerationRepo, *, user_id: int, chat_id: int) -> int:
        return WARNING_THRESHOLD - 1

    monkeypatch.setattr(ModerationRepo, "get_warning_count", _stale_count)

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=230)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []

    sm2 = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm2() as s2:
        ban_rows = (
            (await s2.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )
    assert ban_rows == []

    # The racer still reports the crossing call's intent — knowingly.
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    last = text_calls[-1]["text"].lower()
    assert "заблок" in last or "banned" in last

    await bot.session.close()
    await registry.dispose()


async def test_warn_concurrent_race_single_auto_ban(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-M-1 regression: two concurrent /warn calls at count=THRESHOLD-1
    must result in exactly one ``ban_chat_member`` call (the warn that
    *crossed* the threshold), not two.

    Pre-fix: both calls passed ``current < THRESHOLD`` and both ran the
    auto-ban branch, leaving the count above the threshold and emitting
    two redundant ban API calls.
    """
    import asyncio

    from telegram_invite_bot.handlers.moderation import WARNING_THRESHOLD

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # Seed THRESHOLD - 1 warnings so the next /warn would cross the line.
    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for _ in range(WARNING_THRESHOLD - 1):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="seed",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    # Fire two concurrent /warn calls (distinct update_ids so aiogram
    # doesn't dedupe). Even if SQLite serialises them at the write level,
    # the post-insert count is now the canonical "did this warn cross
    # the threshold" signal — only the one that lands on
    # ``new_count == WARNING_THRESHOLD`` fires auto-ban.
    upd_a = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=200)
    upd_b = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=201)
    await asyncio.gather(
        dispatcher.feed_update(bot, upd_a),
        dispatcher.feed_update(bot, upd_b),
    )

    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert len(ban_calls) == 1, (
        f"M-M-1: expected exactly one ban (threshold-cross), got {len(ban_calls)}"
    )

    # #272.1: the racer must not write a second audit row either. The
    # API call and the ledger row sit under the same ``new_count ==
    # threshold`` guard, but only the call was pinned — relaxing that
    # guard to ``>=`` would double the ledger silently. Both admins
    # still get the "banned" reply; that is the documented trade.
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker as _sm

    sm2 = _sm(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm2() as s2:
        ban_rows = (
            (await s2.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )
    assert len(ban_rows) == 1, f"M-M-1: expected one audit row, got {len(ban_rows)}"

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert len(text_calls) == 2
    for call in text_calls:
        low = call["text"].lower()
        assert "заблок" in low or "banned" in low

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# L-43: /warn consumes group_mod_config (max_warns / autoban_enabled)
# ---------------------------------------------------------------------------


async def test_warn_custom_max_warns_threshold(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-43: a group configured with ``max_warns=2`` auto-bans on the
    SECOND warning, not the hardcoded default of 3."""
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.db.models.group_mod_config import GroupModConfig

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(GroupModConfig(group_id=_CHAT_ID, max_warns=2))
        # One pre-existing warning — the next one crosses the lowered threshold.
        s.add(
            Warning(
                user_id=_TARGET_USER_ID,
                chat_id=_CHAT_ID,
                admin_id=_ADMIN_USER_ID,
                reason="seed",
                date=datetime(2024, 1, 1),
                active=True,
            )
        )
        await s.commit()

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=210)
    await dispatcher.feed_update(bot, update)

    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert len(ban_calls) == 1, "L-43: auto-ban must fire at the configured max_warns=2"

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    last = text_calls[-1]["text"].lower()
    assert "заблок" in last or "banned" in last

    await bot.session.close()
    await registry.dispose()


async def test_warn_autoban_disabled_records_without_ban(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-43: with ``autoban_enabled=False`` the threshold-reaching warn is
    still recorded and reported with the plain warn_success copy, but NO
    ban is issued — mirroring legacy cmd_warn (bot.py:31498: the
    ``AUTO_BAN_ON_MAX_WARNINGS and count >= max_w`` guard falls through
    to ``warn_success`` when the toggle is off)."""
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.db.models.group_mod_config import GroupModConfig

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(GroupModConfig(group_id=_CHAT_ID, autoban_enabled=False))
        # Two pre-seeded warnings: the next one reaches the default
        # threshold of 3 — which would auto-ban if the toggle were on.
        for _ in range(2):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="seed",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=211)
    await dispatcher.feed_update(bot, update)

    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert ban_calls == [], "L-43: autoban off — no ban at threshold"

    # The warn itself was recorded (3 active rows now).
    async with sm() as s:
        rows = (
            (await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID)))
            .scalars()
            .all()
        )
    assert len(rows) == 3

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    last = text_calls[-1]["text"]
    # warn_success copy with count/max — not the auto-ban copy.
    assert "3/3" in last
    assert "заблок" not in last.lower() and "banned" not in last.lower()

    await bot.session.close()
    await registry.dispose()


async def test_warn_at_limit_uses_config_max(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-43: the at-limit early refusal compares against the group's
    configured ``max_warns``, so with ``max_warns=2`` a target holding 2
    warnings cannot receive a third (legacy bot.py:31470 — the
    ``get_warnings_count >= max_w`` gate applies regardless of autoban)."""
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.db.models.group_mod_config import GroupModConfig

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(GroupModConfig(group_id=_CHAT_ID, max_warns=2, autoban_enabled=False))
        for _ in range(2):
            s.add(
                Warning(
                    user_id=_TARGET_USER_ID,
                    chat_id=_CHAT_ID,
                    admin_id=_ADMIN_USER_ID,
                    reason="seed",
                    date=datetime(2024, 1, 1),
                    active=True,
                )
            )
        await s.commit()

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID, update_id=212)
    await dispatcher.feed_update(bot, update)

    # No new warning row.
    async with sm() as s:
        rows = (
            (await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID)))
            .scalars()
            .all()
        )
    assert len(rows) == 2

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply_text = text_calls[0]["text"].lower()
    assert "лимит" in reply_text or "limit" in reply_text or "2" in reply_text

    await bot.session.close()
    await registry.dispose()


async def test_warnings_header_uses_config_max(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-43: /warnings shows count against the configured threshold."""
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from telegram_invite_bot.db.models.group_mod_config import GroupModConfig

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(GroupModConfig(group_id=_CHAT_ID, max_warns=5))
        s.add(
            Warning(
                user_id=_TARGET_USER_ID,
                chat_id=_CHAT_ID,
                admin_id=_ADMIN_USER_ID,
                reason="spam",
                date=datetime(2024, 3, 1),
                active=True,
            )
        )
        await s.commit()

    update = _group_msg("/warnings", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "1/5" in text_calls[-1]["text"]

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /unwarn
# ---------------------------------------------------------------------------


async def test_unwarn_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # Seed a warning
    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            Warning(
                user_id=_TARGET_USER_ID,
                chat_id=_CHAT_ID,
                admin_id=_ADMIN_USER_ID,
                reason="test",
                date=datetime(2024, 1, 1),
                active=True,
            )
        )
        await s.commit()

    update = _group_msg("/unwarn", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "снят" in text_calls[-1]["text"].lower() or "removed" in text_calls[-1]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def _seed_warnings(
    registry: EngineRegistry,
    count: int,
    *,
    user_id: int = _TARGET_USER_ID,
    chat_id: int = _CHAT_ID,
    reason_prefix: str = "w",
) -> list[int]:
    """Seed ``count`` active warnings for ``user_id``, oldest first (#252).

    Returns their row ids in insertion order, so a test can address the
    *first* one — the row ``remove_last_warning`` would never touch.

    ``user_id`` defaults to the moderation target; the /warnings
    self-service tests seed the *caller* too, and give those rows a
    distinct ``reason_prefix`` so a reply can be attributed to one owner
    or the other. ``chat_id`` defaults to the test group; #1780 seeds a
    second chat to pin that the /unban sweep does not reach across.
    """
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    ids: list[int] = []
    async with sm() as s:
        for i in range(count):
            row = Warning(
                user_id=user_id,
                chat_id=chat_id,
                admin_id=_ADMIN_USER_ID,
                reason=f"{reason_prefix}{i}",
                date=datetime(2024, 1, 1 + i),
                active=True,
            )
            s.add(row)
            await s.flush()
            ids.append(row.id)
        await s.commit()
    return ids


async def _active_reasons(registry: EngineRegistry) -> list[str]:
    """Reasons of the still-active warnings, in row order (#252)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        stmt = select(Warning).where(Warning.active.is_(True)).order_by(Warning.id)
        return [r.reason for r in (await s.execute(stmt)).scalars().all()]


async def _unwarn_log_rows(registry: EngineRegistry) -> list[ModerationLog]:
    """Every ``unwarn`` audit row, in insertion order (#252)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        stmt = (
            select(ModerationLog).where(ModerationLog.action == "unwarn").order_by(ModerationLog.id)
        )
        return list((await s.execute(stmt)).scalars().all())


async def test_unwarn_reply_form_lifts_the_warning_named_by_id(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252(7): ``/unwarn <id> <reason>`` in reply form lifts THAT row.

    ``/warnings`` prints ``#<id>`` for every warning, so the UI has always
    advertised id-addressing; before this change the id was swallowed into
    the reason and the *most recent* warning was lifted instead. Legacy
    read the id from exactly this position (bot.py:31601-31608).
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    ids = await _seed_warnings(registry, 3)

    update = _group_msg(f"/unwarn {ids[0]} извинился", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    # The OLDEST row went, not the newest — that is the whole point.
    assert await _active_reasons(registry) == ["w1", "w2"]

    log_rows = await _unwarn_log_rows(registry)
    assert len(log_rows) == 1
    assert log_rows[0].reason == "извинился"
    assert log_rows[0].details == f"warning_id={ids[0]}"

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_arg_form_lifts_the_warning_named_by_id(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252(7): ``/unwarn <target> <id> <reason>`` — the argument form.

    parts[1] is the target and is consumed by ``_resolve_target``, so the
    id can only be parts[2] (bot.py:31622-31631).
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    ids = await _seed_warnings(registry, 3)

    update = _group_msg(f"/unwarn {_TARGET_USER_ID} {ids[0]} извинился")
    await dispatcher.feed_update(bot, update)

    assert await _active_reasons(registry) == ["w1", "w2"]

    log_rows = await _unwarn_log_rows(registry)
    assert len(log_rows) == 1
    assert log_rows[0].reason == "извинился"
    assert log_rows[0].details == f"warning_id={ids[0]}"

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_survives_a_failed_confirmation(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1875: the lifted warning stays lifted when the reply is refused.

    ``remove_last_warning`` flips ``active`` to 0 and writes the
    ``unwarn`` audit row; everything after it is two reads and an
    unwrapped ``message.reply``. ``BaseSessionMiddleware`` rolls back on
    any raise, so before the checkpoint a rejected confirmation put the
    warning back while the moderator was told nothing at all — and the
    count they read next still carried the warning they had just lifted.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await _seed_warnings(registry, 2)

    # Wrapped AFTER the fake API is installed, so every non-text call
    # the handler makes (admin lookups, chat reads) still answers.
    original = bot.session.make_request

    async def refuse(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            raise TelegramForbiddenError(method=method, message="Forbidden: blocked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = refuse  # type: ignore[method-assign,assignment]
    with contextlib.suppress(TelegramForbiddenError):
        await dispatcher.feed_update(bot, _group_msg("/unwarn", reply_to_user_id=_TARGET_USER_ID))

    # The newest warning is gone for good, and so is its audit row.
    assert await _active_reasons(registry) == ["w0"]
    assert len(await _unwarn_log_rows(registry)) == 1

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_with_an_unknown_id_changes_nothing(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252(7): a mistyped id must not report success.

    This is a deliberate divergence from legacy: ``remove_warning`` set
    ``removed_id = warning_id`` and returned True without ever checking
    that the UPDATE matched a row (bot.py:8599, :8635), so a typo both
    claimed success AND wrote a bogus ``unwarn`` row into the audit log.
    Here the row is looked up first: nothing is written, no warning is
    lifted, and the operator is told the id does not exist rather than
    that the user is clean.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await _seed_warnings(registry, 2)

    update = _group_msg("/unwarn 987654 опечатка", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert await _active_reasons(registry) == ["w0", "w1"]
    assert await _unwarn_log_rows(registry) == []

    reply = [e for e in sink if e["kind"] == "text"][-1]["text"]
    assert "ID" in reply
    # ...and NOT the "user is clean" copy, which would be a lie here.
    assert reply != t("h_mod_unwarn_none", "ru")

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_id_is_scoped_to_the_user_and_chat(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252(7) security: the id lookup is scoped to (id, user_id, chat_id).

    Legacy scoped its UPDATE the same way (bot.py:8596). Without that
    scope an operator in chat A could lift a warning issued in chat B
    just by guessing its row id, since ids are a single global sequence.
    """
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        foreign = Warning(
            user_id=_TARGET_USER_ID,
            chat_id=_CHAT_ID + 1,  # another chat
            admin_id=_ADMIN_USER_ID,
            reason="elsewhere",
            date=datetime(2024, 1, 1),
            active=True,
        )
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    update = _group_msg(f"/unwarn {foreign_id} чужой чат", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert await _active_reasons(registry) == ["elsewhere"]
    assert await _unwarn_log_rows(registry) == []

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_keeps_a_non_numeric_first_word_in_the_reason(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252(7) non-regression: plain ``/unwarn <reason>`` still lifts the
    LAST warning and keeps the reason whole.

    Deliberate divergence from legacy, which did ``reason = parts[2:]``
    unconditionally (bot.py:31608) and therefore ate the first word
    whether or not it was an id — ``/unwarn извинился он`` logged
    "он". Here the token is consumed only when it parses as an id.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await _seed_warnings(registry, 3)

    update = _group_msg("/unwarn извинился он", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    # Last-warning semantics untouched when no id is given.
    assert await _active_reasons(registry) == ["w0", "w1"]

    log_rows = await _unwarn_log_rows(registry)
    assert len(log_rows) == 1
    assert log_rows[0].reason == "извинился он"

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_unicode_digit_is_not_an_id(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252(7) x #102: "²" passes str.isdigit() but int() rejects it.

    ``parse_warning_id`` goes through ``is_int_token``, so a Unicode
    decimal stays in the reason instead of reaching ``int()``. Without
    that the command would raise ValueError before the repo is touched.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await _seed_warnings(registry, 2)

    update = _group_msg("/unwarn \u00b2 повод", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    # Fell through to last-warning semantics, reason kept whole.
    assert await _active_reasons(registry) == ["w0"]
    log_rows = await _unwarn_log_rows(registry)
    assert len(log_rows) == 1
    assert log_rows[0].reason == "\u00b2 повод"

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_no_warnings(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/unwarn", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply_text = text_calls[0]["text"].lower()
    assert "нет" in reply_text or "no" in reply_text or "none" in reply_text

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /warnings
# ---------------------------------------------------------------------------


async def test_warnings_zero_shows_empty_state(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/warnings", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    # Should say "no warnings" (empty state), not blank
    assert text_calls[0]["text"].strip() != ""

    await bot.session.close()
    await registry.dispose()


async def test_warnings_lists_rows(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            Warning(
                user_id=_TARGET_USER_ID,
                chat_id=_CHAT_ID,
                admin_id=_ADMIN_USER_ID,
                reason="spam",
                date=datetime(2024, 3, 1),
                active=True,
            )
        )
        await s.commit()

    update = _group_msg("/warnings", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "spam" in text_calls[-1]["text"]

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /warnings — the bare self-service form (#252, commit 3)
# ---------------------------------------------------------------------------

_MEMBER_USER_ID = 30  # plain member: not a Telegram admin, no rank row


async def test_warnings_bare_form_lists_the_callers_own_warnings(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy ``cmd_warnings`` took no target at all — it read
    ``message.from_user.id`` (bot.py:31657-31677), so ``/warnings`` meant
    "my own record". The port demanded a reply or an @username and
    answered the bare command with ``h_mod_no_reply``."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)
    await _seed_warnings(registry, 2, user_id=_ADMIN_USER_ID)

    update = _group_msg("/warnings")
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"]
    assert "w0" in reply
    assert "w1" in reply
    assert "@username" not in reply  # not the h_mod_no_reply error

    await bot.session.close()
    await registry.dispose()


async def test_warnings_bare_form_shows_only_the_caller(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare form is scoped to the caller: warnings belonging to
    somebody else in the same chat must not appear in it."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)
    await _seed_warnings(registry, 2)  # _TARGET_USER_ID, not the caller
    await _seed_warnings(registry, 1, user_id=_ADMIN_USER_ID, reason_prefix="mine")

    update = _group_msg("/warnings")
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"]
    assert "mine0" in reply
    assert "w0" not in reply
    assert "w1" not in reply

    await bot.session.close()
    await registry.dispose()


async def test_warnings_empty_state_matches_who_was_asked_about(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``h_mod_warnings_none`` is third-person ("у пользователя"), which
    reads as a bug when you asked about yourself — the bare form gets its
    own second-person string."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/warnings")
    await dispatcher.feed_update(bot, update)
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "У вас" in text_calls[0]["text"]

    sink.clear()
    update = _group_msg("/warnings", reply_to_user_id=_TARGET_USER_ID, update_id=2)
    await dispatcher.feed_update(bot, update)
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "У пользователя" in text_calls[0]["text"]

    await bot.session.close()
    await registry.dispose()


async def test_warnings_bare_form_still_requires_the_rank(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-service is not ungated: the legacy catalog filed ``warnings``
    at ``default_rank: 2`` (bot.py:42402) and ``check_command_access``
    (bot.py:42858-42894) enforced it on every decorated handler
    (bot.py:1270). A plain member gets a refusal, not a list."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _seed_warnings(registry, 1, user_id=_MEMBER_USER_ID)

    update = _group_msg("/warnings", user_id=_MEMBER_USER_ID, first_name="Member")
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"]
    assert "w0" not in reply
    assert "ранга" in reply or "прав" in reply.lower()

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /pin
# ---------------------------------------------------------------------------


async def test_pin_no_reply(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/pin")  # no reply_to_user_id → no reply_to_message
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "ответь" in text_calls[0]["text"].lower() or "reply" in text_calls[0]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_pin_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/pin", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    pin_calls = [e for e in sink if e["kind"] == "pinchatmessage"]
    assert len(pin_calls) == 1

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "закреп" in text_calls[-1]["text"].lower() or "pinned" in text_calls[-1]["text"].lower()

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /unpin
# ---------------------------------------------------------------------------


async def test_unpin_happy_path(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    update = _group_msg("/unpin")
    await dispatcher.feed_update(bot, update)

    unpin_calls = [e for e in sink if e["kind"] == "unpinchatmessage"]
    assert len(unpin_calls) == 1

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert (
        "откреп" in text_calls[-1]["text"].lower() or "unpinned" in text_calls[-1]["text"].lower()
    )

    await bot.session.close()
    await registry.dispose()


async def test_pin_and_unpin_record_null_user_id(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-M-2 regression: /pin and /unpin write ``user_id=NULL`` in
    ``moderation_log``, not the previous ``user_id=0`` sentinel that
    collided across the two action types.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    pin_upd = _group_msg("/pin", reply_to_user_id=_TARGET_USER_ID, update_id=300)
    unpin_upd = _group_msg("/unpin", update_id=301)
    await dispatcher.feed_update(bot, pin_upd)
    await dispatcher.feed_update(bot, unpin_upd)

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    select(ModerationLog).where(ModerationLog.action.in_(["pin", "unpin"]))
                )
            )
            .scalars()
            .all()
        )

    pin_rows = [r for r in rows if r.action == "pin"]
    unpin_rows = [r for r in rows if r.action == "unpin"]
    assert len(pin_rows) == 1
    assert len(unpin_rows) == 1
    assert pin_rows[0].user_id is None, "pin must record user_id=NULL"
    assert unpin_rows[0].user_id is None, "unpin must record user_id=NULL"

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# /fine
# ---------------------------------------------------------------------------


async def test_fine_non_developer_denied(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-developer user is denied even if group admin."""
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase],
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_ADMIN_USER_ID})

    update = _group_msg(
        "/fine 100 reason", user_id=_ADMIN_USER_ID, reply_to_user_id=_TARGET_USER_ID
    )
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "прав" in text_calls[0]["text"].lower() or "permission" in text_calls[0]["text"].lower()

    await bot.session.close()
    await registry.dispose()


async def test_fine_developer_happy_path(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Developer can fine; debit is applied and audit row written."""
    bot_config = _dev_bot_config()
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase],
        bot_config=bot_config,
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_DEVELOPER_USER_ID})

    # Seed target wallet with 500 coins
    engine = registry.engine(DBName.ECONOMY)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            EconomyUser(
                user_id=_TARGET_USER_ID,
                balance=500,
                total_earned=500,
                total_spent=0,
                games_played=0,
                games_won=0,
                daily_streak=0,
                language="ru",
            )
        )
        await s.commit()

    update = _group_msg(
        "/fine 100 spam behaviour",
        user_id=_DEVELOPER_USER_ID,
        reply_to_user_id=_TARGET_USER_ID,
    )
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    last = text_calls[-1]["text"].lower()
    assert "штраф" in last or "fine" in last or "100" in last

    # Verify wallet was debited
    async with sm() as s:
        from sqlalchemy import select

        result = await s.execute(select(EconomyUser).where(EconomyUser.user_id == _TARGET_USER_ID))
        wallet = result.scalar_one()
    assert wallet.balance == 400  # 500 - 100

    # Verify audit log
    engine_mod = registry.engine(DBName.MODERATION)
    sm_mod = async_sessionmaker(engine_mod, expire_on_commit=False)
    from sqlalchemy import select

    async with sm_mod() as s:
        result = await s.execute(select(ModerationLog).where(ModerationLog.action == "fine"))
        logs = result.scalars().all()
    assert len(logs) == 1

    # Ledger row. A fine BURNS the coins, so ``to_id`` must stay NULL:
    # naming the admin who typed the command made the fine read as
    # income of theirs on the /profile finances panel and in the
    # weekly "получено" total, neither of which filters on ``type``.
    async with sm() as s:
        rows = (
            (await s.execute(select(Transaction).where(Transaction.type == "fine"))).scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].from_id == _TARGET_USER_ID
    assert rows[0].to_id is None
    assert rows[0].amount == 100

    await bot.session.close()
    await registry.dispose()


async def test_fine_clamped_to_balance_records_requested_amount(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the wallet can't cover the fine the deduction is clamped to
    the balance — the audit row must still record the admin's requested
    amount, so 'fine 100000, only 50 taken' is distinguishable from a
    deliberate 50-coin fine."""
    bot_config = _dev_bot_config()
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase],
        bot_config=bot_config,
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_DEVELOPER_USER_ID})

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = registry.engine(DBName.ECONOMY)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            EconomyUser(
                user_id=_TARGET_USER_ID,
                balance=50,
                total_earned=50,
                total_spent=0,
                games_played=0,
                games_won=0,
                daily_streak=0,
                language="ru",
            )
        )
        await s.commit()

    # Request far more than the 50-coin balance.
    update = _group_msg(
        "/fine 100000 spam behaviour",
        user_id=_DEVELOPER_USER_ID,
        reply_to_user_id=_TARGET_USER_ID,
    )
    await dispatcher.feed_update(bot, update)

    # Wallet drained to zero (only 50 could be taken).
    async with sm() as s:
        wallet = (
            await s.execute(select(EconomyUser).where(EconomyUser.user_id == _TARGET_USER_ID))
        ).scalar_one()
    assert wallet.balance == 0

    # Audit row preserves both deducted + requested.
    engine_mod = registry.engine(DBName.MODERATION)
    sm_mod = async_sessionmaker(engine_mod, expire_on_commit=False)
    async with sm_mod() as s:
        log_row = (
            await s.execute(select(ModerationLog).where(ModerationLog.action == "fine"))
        ).scalar_one()
    assert log_row.details is not None
    assert "amount=50" in log_row.details
    assert "requested=100000" in log_row.details
    assert "clamped_to_balance" in log_row.details

    await bot.session.close()
    await registry.dispose()


async def test_fine_no_reason(make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot_config = _dev_bot_config()
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase],
        bot_config=bot_config,
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_DEVELOPER_USER_ID})

    # Seed target wallet
    engine = registry.engine(DBName.ECONOMY)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            EconomyUser(
                user_id=_TARGET_USER_ID,
                balance=500,
                total_earned=500,
                total_spent=0,
                games_played=0,
                games_won=0,
                daily_streak=0,
                language="ru",
            )
        )
        await s.commit()

    # /fine <amount> with NO reason
    update = _group_msg(
        "/fine 100",
        user_id=_DEVELOPER_USER_ID,
        reply_to_user_id=_TARGET_USER_ID,
    )
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply_text = text_calls[0]["text"].lower()
    assert "причин" in reply_text or "reason" in reply_text

    await bot.session.close()
    await registry.dispose()


async def _seed_wallet(registry: Any, user_id: int, balance: int) -> Any:
    """Give ``user_id`` a wallet and hand back the sessionmaker."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.ECONOMY), expire_on_commit=False)
    async with sm() as s:
        s.add(
            EconomyUser(
                user_id=user_id,
                balance=balance,
                total_earned=balance,
                total_spent=0,
                games_played=0,
                games_won=0,
                daily_streak=0,
                language="ru",
            )
        )
        await s.commit()
    return sm


async def test_fine_in_a_dm_debits_the_wallet(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#252(16): ``/fine`` works in a DM, because legacy's did.

    This test used to assert the #123 group-only refusal. That contract
    was wrong: legacy registered the command with no chat filter at all
    (bot.py:3672) and ``cmd_fine`` never reads ``message.chat`` — it
    takes a user id, moves coins in the global economy wallet and DMs
    the target. Requiring a developer to walk into a group to fine
    someone by id is a demand the legacy contract never made.

    The debit is the assertion that matters: a private-chat refusal
    shadowing the handler would leave the balance untouched.
    """
    bot_config = _dev_bot_config()
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase],
        bot_config=bot_config,
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_DEVELOPER_USER_ID})
    sm = await _seed_wallet(registry, _TARGET_USER_ID, 500)

    update = _private_msg(f"/fine {_TARGET_USER_ID} 100 spam", user_id=_DEVELOPER_USER_ID)
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED

    from sqlalchemy import select

    async with sm() as s:
        wallet = (
            await s.execute(select(EconomyUser).where(EconomyUser.user_id == _TARGET_USER_ID))
        ).scalar_one()
    assert wallet.balance == 400

    texts = [e["text"] for e in sink if e["kind"] == "text"]
    assert texts
    assert t("h_group_only_command", "ru", command="fine") not in texts

    await bot.session.close()
    await registry.dispose()


async def test_fine_penalty_alias_is_registered(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/penalty`` is legacy's third spelling (bot.py:3672), dropped in
    the port and restored by #252(16). An alias nobody registers is an
    alias that answers nobody.
    """
    bot_config = _dev_bot_config()
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase],
        bot_config=bot_config,
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_DEVELOPER_USER_ID})
    sm = await _seed_wallet(registry, _TARGET_USER_ID, 500)

    update = _group_msg(
        "/penalty 100 spam behaviour",
        user_id=_DEVELOPER_USER_ID,
        reply_to_user_id=_TARGET_USER_ID,
    )
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED

    from sqlalchemy import select

    async with sm() as s:
        wallet = (
            await s.execute(select(EconomyUser).where(EconomyUser.user_id == _TARGET_USER_ID))
        ).scalar_one()
    assert wallet.balance == 400

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# R-FIX-006: /unwarn must run the same admin/self/bot target validation
# as /warn. Previously /unwarn only checked ``is_bot`` from the reply path
# and skipped ``_check_target_ok`` (which owns the admin/self guard),
# letting admins flip ``active=False`` on a peer admin's row or unwarn
# themselves.
# ---------------------------------------------------------------------------


async def test_unwarn_target_is_bot_rejected(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-006: /unwarn against a bot reply must be refused (no DB mutation)."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    # Seed a (stale) warning row for the bot id so we can assert it is
    # NOT cleared by the refused command.
    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            Warning(
                user_id=_TARGET_USER_ID,
                chat_id=_CHAT_ID,
                admin_id=_ADMIN_USER_ID,
                reason="seed",
                date=datetime(2024, 1, 1),
                active=True,
            )
        )
        await s.commit()

    update = _group_msg(
        "/unwarn",
        reply_to_user_id=_TARGET_USER_ID,
        reply_to_is_bot=True,
    )
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "бот" in text_calls[0]["text"].lower() or "bot" in text_calls[0]["text"].lower()

    # Row remains active — the refused /unwarn did not flip it.
    from sqlalchemy import select

    async with sm() as s:
        result = await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].active is True

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_target_is_self_rejected(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-006: admin cannot /unwarn themselves."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            Warning(
                user_id=_ADMIN_USER_ID,
                chat_id=_CHAT_ID,
                admin_id=_ADMIN_USER_ID,
                reason="seed-self",
                date=datetime(2024, 1, 1),
                active=True,
            )
        )
        await s.commit()

    # Admin replies to their own message.
    update = _group_msg("/unwarn", reply_to_user_id=_ADMIN_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "себ" in text_calls[0]["text"].lower() or "yourself" in text_calls[0]["text"].lower()

    from sqlalchemy import select

    async with sm() as s:
        result = await s.execute(select(Warning).where(Warning.user_id == _ADMIN_USER_ID))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].active is True

    await bot.session.close()
    await registry.dispose()


async def test_unwarn_target_is_admin_rejected(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-006: admin A cannot /unwarn admin B."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    # Both caller and target are admins.
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_ADMIN_USER_ID, _TARGET_USER_ID})

    engine = registry.engine(DBName.MODERATION)
    from datetime import datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        s.add(
            Warning(
                user_id=_TARGET_USER_ID,
                chat_id=_CHAT_ID,
                admin_id=_ADMIN_USER_ID,
                reason="seed-peer",
                date=datetime(2024, 1, 1),
                active=True,
            )
        )
        await s.commit()

    update = _group_msg("/unwarn", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert (
        "администрат" in text_calls[0]["text"].lower()
        or "administrator" in text_calls[0]["text"].lower()
    )

    from sqlalchemy import select

    async with sm() as s:
        result = await s.execute(select(Warning).where(Warning.user_id == _TARGET_USER_ID))
        rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].active is True

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# R-FIX-007: ``_is_user_admin`` returns None on Telegram-API error; callers
# distinguish actor-path (reject with retry message) from target-path
# (treat target as protected — refuse the action). Previously a bare
# ``except Exception → False`` made the target-path fail OPEN, letting a
# 429 storm classify a real admin as a non-admin and allow the ban.
# ---------------------------------------------------------------------------


def _attach_api_with_failure(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    fail_for_user_ids: set[int],
    admin_user_ids: set[int] | None = None,
) -> None:
    """Like ``_attach_fake_api`` but raises on ``GetChatMember`` for the
    given user ids (simulating a Telegram 429/network error).
    """
    if admin_user_ids is None:
        admin_user_ids = {_ADMIN_USER_ID}

    from datetime import datetime as _dt

    from aiogram.types import Chat as _Chat
    from aiogram.types import Message as _Message
    from aiogram.types import User as _User

    def _synth_msg(chat_id: int, text: str) -> _Message:
        return _Message(
            message_id=100,
            date=_dt(2024, 1, 1),
            chat=_Chat(id=chat_id, type="private"),
            from_user=_User(id=0, is_bot=True, first_name="bot"),
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
            sink.append({"kind": "getchatmember", "user_id": uid})
            if uid in fail_for_user_ids:
                # Simulate a Telegram API error (429-equivalent for tests).
                raise RuntimeError("simulated TelegramRetryAfter")

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

        if name == "GetMe":
            from aiogram.types import User as TGUser

            return TGUser(id=777, is_bot=True, first_name="TestBot", username="testbot")

        if name == "SendMessage":
            sink.append({"kind": "text", "chat_id": method.chat_id, "text": method.text})
            return _synth_msg(method.chat_id, method.text)

        if name in {"BanChatMember", "UnbanChatMember"}:
            sink.append({"kind": name.lower()})
            return True

        raise AssertionError(f"unexpected Telegram call in test: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def test_ban_actor_telegram_error_rejected(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-007 actor path: Telegram error during caller-admin check →
    refuse the action with a retry-later message (fail-closed)."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_api_with_failure(
        bot,
        monkeypatch,
        sink,
        fail_for_user_ids={_ADMIN_USER_ID},  # caller lookup fails
        admin_user_ids=set(),
    )

    update = _group_msg("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    # No ban was executed.
    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert ban_calls == []

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    # Either the retry-later string or its RU equivalent — or, once
    # ``CommandAccessMiddleware`` is attached, the command-rank
    # denial (the admin probe error never GRANTS; rank 0 < 2 denies).
    assert (
        "retry" in reply
        or "повтори" in reply
        or "не удалось проверить" in reply
        or "could not verify" in reply
        or "ранга" in reply
        or "rank" in reply
    )

    await bot.session.close()
    await registry.dispose()


async def test_ban_target_telegram_error_asks_for_retry(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#249 target path: a Telegram error during the target probe must
    refuse the action **and say so honestly**.

    Two properties are pinned here, and they are independent:

    * the ban must not proceed (the R-FIX-007 fail-open regression —
      an API blip must never let a real admin be banned);
    * the reply must be the retry-later copy, not "that user is an
      administrator". The old wording stated a fact the bot had never
      established: after a 429 it does not know the target's status,
      and the issuer could not tell a genuine admin from an outage.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    # Caller lookup succeeds (admin); target lookup raises.
    _attach_api_with_failure(
        bot,
        monkeypatch,
        sink,
        fail_for_user_ids={_TARGET_USER_ID},
        admin_user_ids={_ADMIN_USER_ID},
    )

    update = _group_msg("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    # No ban was executed — an unknown status is never a licence to act.
    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert ban_calls == []

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    assert "не удалось проверить права" in reply or "could not verify" in reply
    # And explicitly NOT the fabricated verdict the port used to send.
    assert "администрат" not in reply
    assert "administrator" not in reply

    await bot.session.close()
    await registry.dispose()


async def test_ban_probes_the_target_membership_only_once(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#249: one ``getChatMember`` per target, not two.

    The target guard used to run two independent round-trips for the
    same (chat, user) — one asking "is it a bot", one asking "is it an
    admin". Besides doubling API traffic on all seven moderation
    commands, the two probes observed the chat at two different moments
    and could disagree. ``_probe_chat_member`` answers both questions
    from one snapshot.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_api_with_failure(
        bot,
        monkeypatch,
        sink,
        fail_for_user_ids=set(),
        admin_user_ids={_ADMIN_USER_ID},
    )

    update = _group_msg("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    target_probes = [
        e for e in sink if e["kind"] == "getchatmember" and e["user_id"] == _TARGET_USER_ID
    ]
    assert len(target_probes) == 1, f"expected a single target probe, saw {len(target_probes)}"
    # Sanity: the command really ran end-to-end, so the count above is
    # not "one" merely because the handler bailed out early.
    assert [e for e in sink if e["kind"] == "banchatmember"]

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# R-FIX-011: Anonymous admin (GroupAnonymousBot id=1087968824) auth bypass.
# Moderation by ``sender_chat`` posts or by the GroupAnonymousBot
# placeholder is refused — the audit log cannot attribute the action to
# a real human admin id.
# ---------------------------------------------------------------------------


def _anonymous_admin_update(
    text: str,
    *,
    chat_id: int = _CHAT_ID,
    reply_to_user_id: int | None = None,
    reply_to_is_bot: bool = False,
    update_id: int = 1,
) -> Any:
    """Build a Telegram-shaped Update where the actor is the
    GroupAnonymousBot acting on behalf of an anonymous admin.

    Telegram sets ``from_user`` to the GroupAnonymousBot placeholder
    (id=1087968824, is_bot=True) AND ``sender_chat`` to the chat itself
    when an admin has "Remain anonymous" enabled.
    """
    from aiogram.types import Update as _Update

    chat = {"id": chat_id, "type": "supergroup", "title": "T"}
    payload: dict[str, Any] = {
        "message_id": 1,
        "date": 1_700_000_000,
        "chat": chat,
        "from": {
            "id": 1087968824,  # GroupAnonymousBot
            "is_bot": True,
            "first_name": "Group",
            "username": "GroupAnonymousBot",
        },
        "sender_chat": chat,
        "text": text,
    }
    if reply_to_user_id is not None:
        payload["reply_to_message"] = {
            "message_id": 999,
            "date": 1_699_999_999,
            "chat": chat,
            "from": {
                "id": reply_to_user_id,
                "is_bot": reply_to_is_bot,
                "first_name": "Target",
            },
            "text": "(replied)",
        }
    return _Update.model_validate({"update_id": update_id, "message": payload})


async def test_anonymous_admin_ban_allowed_by_default(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-011-fp: with the default ``allow_anonymous_admin=True``,
    an anonymous-admin /ban (sender_chat set) is allowed when the chat
    has at least one admin per ``getChatAdministrators``. The audit
    log records ``actor_id=sender_chat.id`` with an ``anonymous=True``
    marker. Pinning the legacy posture that iter-1 over-locked."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    # ``admin_user_ids`` populates both GetChatMember (used elsewhere)
    # AND GetChatAdministrators (used by the new anonymous-verify path).
    # One real admin user (id=42) so the chat has at least one admin,
    # and #883: that admin is the one acting, so "Remain anonymous" is
    # ON for them — the fake used to report every admin as named, a
    # shape Telegram cannot produce alongside a sender_chat update.
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        admin_user_ids={42},
        anonymous_admin_user_ids={42},
    )

    update = _anonymous_admin_update("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    # Ban proceeds.
    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert len(ban_calls) == 1

    # Audit row is written with sender_chat.id as the actor.
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = registry.engine(DBName.MODERATION)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(select(ModerationLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].admin_id == _CHAT_ID  # sender_chat.id

    await bot.session.close()
    await registry.dispose()


async def test_anonymous_admin_ban_refused_when_flag_disabled(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-011-fp: with ``ALLOW_ANONYMOUS_ADMIN=false`` the locked-down
    iter-1 posture comes back — anonymous admin's /ban is refused."""
    bot_cfg = BotConfig(
        BOT_TOKEN=SecretStr("123:abc"),
        ALLOW_ANONYMOUS_ADMIN=False,
    )
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase],
        bot_config=bot_cfg,
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={42})

    update = _anonymous_admin_update("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    ban_calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert ban_calls == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    assert "anonymous" in reply or "анонимн" in reply or "remain anonymous" in reply

    await bot.session.close()
    await registry.dispose()


async def test_anonymous_admin_warn_records_chat_id_actor(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-FIX-011-fp: anonymous-admin /warn proceeds and the warning row
    records ``admin_id=sender_chat.id`` (Telegram intentionally hides
    which human admin acted, so the chat itself is the audit-trail
    attribution)."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        admin_user_ids={42},
        anonymous_admin_user_ids={42},
    )

    update = _anonymous_admin_update("/warn", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    engine = registry.engine(DBName.MODERATION)
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(select(Warning))).scalars().all()
    assert len(rows) == 1
    assert rows[0].admin_id == _CHAT_ID

    await bot.session.close()
    await registry.dispose()


async def test_anonymous_title_only_admin_ban_is_refused(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#883: "Remain anonymous" must not buy back what #337 took away.

    Telegram hides WHICH admin acted, so the gate asks the only sound
    question available: does any anonymous administrator of this chat
    hold a moderation right? Here the chat's single anonymous admin was
    promoted for a title — every moderation right OFF — so the actor
    provably holds none either and /ban is refused.

    Before #883 the branch stopped at "the chat has at least one
    admin", which is true in every chat, so this ban went through and
    the audit row was attributed to the chat.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        admin_user_ids=set(),
        titular_admin_user_ids={42},
        anonymous_admin_user_ids={42},
    )

    update = _anonymous_admin_update("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    assert "anonymous" in reply or "анонимн" in reply

    await bot.session.close()
    await registry.dispose()


async def test_anonymous_admin_with_rights_allowed_beside_a_title_only_one(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#883, the other direction, stated as its own claim.

    The narrowing is over the SET of possible actors, never over one
    identity Telegram does not send. A chat holding both a title-only
    anonymous admin and a real one therefore keeps working — which is
    what pins the refusal above to the missing rights rather than to
    the mere presence of a title-only row.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        admin_user_ids={42},
        titular_admin_user_ids={43},
        anonymous_admin_user_ids={42, 43},
    )

    update = _anonymous_admin_update("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert len([e for e in sink if e["kind"] == "banchatmember"]) == 1

    await bot.session.close()
    await registry.dispose()


async def test_anonymous_actor_refused_when_no_admin_is_anonymous(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#883: ``is_anonymous`` is the half of the filter that scopes the
    set to actors who could actually have sent this update.

    Every admin here has "Remain anonymous" OFF, so none of them can be
    behind a ``sender_chat`` message and the set of possible actors is
    empty — refuse. Without that half, the rights-bearing named admin
    (id=42) would vouch for an actor who is demonstrably not them, and
    the check would collapse back into "does the chat have admins".
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={42})

    update = _anonymous_admin_update("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    assert "anonymous" in reply or "анонимн" in reply

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# #246: ``sender_chat`` is not evidence of adminship on its own.
# Telegram sets it for an anonymous admin of THIS chat *and* for any
# member posting as a channel they own. Only the first is an admin.
# ---------------------------------------------------------------------------

_FOREIGN_CHANNEL_ID = -1002222222222


def _channel_actor_update(
    text: str,
    *,
    chat_id: int = _CHAT_ID,
    sender_chat_id: int = _FOREIGN_CHANNEL_ID,
    reply_to_user_id: int | None = None,
    update_id: int = 1,
) -> Any:
    """Build an Update sent on behalf of a *foreign* channel.

    Most supergroups let any member pick one of their own channels in
    the "send as" chooser. Telegram then sets ``sender_chat`` to that
    channel and fills ``from`` with the ``Channel_Bot`` placeholder
    (id=136817688) — the Bot API always supplies a fake sender user for
    on-behalf-of-a-chat messages in non-channel chats, which is why an
    ``assert message.from_user is not None`` never caught this shape.

    Owning a channel is the only thing this proves. It is deliberately
    NOT the anonymous-admin shape: ``sender_chat.id != chat.id``.
    """
    from aiogram.types import Update as _Update

    chat = {"id": chat_id, "type": "supergroup", "title": "T"}
    payload: dict[str, Any] = {
        "message_id": 1,
        "date": 1_700_000_000,
        "chat": chat,
        "from": {
            "id": 136817688,  # Channel_Bot
            "is_bot": True,
            "first_name": "Channel",
            "username": "Channel_Bot",
        },
        "sender_chat": {
            "id": sender_chat_id,
            "type": "channel",
            "title": "Somebody else's channel",
        },
        "text": text,
    }
    if reply_to_user_id is not None:
        payload["reply_to_message"] = {
            "message_id": 999,
            "date": 1_699_999_999,
            "chat": chat,
            "from": {"id": reply_to_user_id, "is_bot": False, "first_name": "Target"},
            "text": "(replied)",
        }
    return _Update.model_validate({"update_id": update_id, "message": payload})


async def test_ban_from_a_foreign_channel_actor_is_refused(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#246: posting as a channel you own does not make you an admin.

    The old predicate accepted any ``sender_chat`` and then "verified"
    the actor by asking whether the chat has *any* admins — a question
    that is true in every chat, so a regular member could pick their
    channel in the "send as" chooser and issue /ban in reply to anyone.

    Note the fake is given a populated admin set (id=42), exactly the
    condition that used to wave this through: the refusal must come
    from who the actor *is*, not from the chat happening to be
    admin-less.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={42})

    update = _channel_actor_update("/ban", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    # The copy must not tell a channel actor to switch off "Remain
    # anonymous" — they never turned it on; that advice is the
    # anonymous-admin refusal and would send them chasing a setting
    # that does not apply.
    assert "канал" in reply or "channel" in reply

    await bot.session.close()
    await registry.dispose()


async def test_a_genuine_anonymous_admin_is_still_allowed(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of #246, stated as its own claim.

    Tightening the predicate to ``sender_chat.id == chat.id`` had to
    leave the "Remain anonymous" posture working — it is the default in
    most channel-style groups, and refusing it was the iter-1 regression
    R-FIX-011-fp exists to prevent. Distinguishing the two cases is the
    whole point of the fix, so both directions are pinned.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(
        bot,
        monkeypatch,
        sink,
        admin_user_ids={42},
        anonymous_admin_user_ids={42},
    )

    # Same builder as the anonymous-admin tests: sender_chat IS the chat.
    update = _channel_actor_update(
        "/ban", sender_chat_id=_CHAT_ID, reply_to_user_id=_TARGET_USER_ID
    )
    await dispatcher.feed_update(bot, update)

    assert len([e for e in sink if e["kind"] == "banchatmember"]) == 1

    await bot.session.close()
    await registry.dispose()


# ---------------------------------------------------------------------------
# R4 (ranks epic): widened gate — TG admin OR ranked-with-permission.
# DESIGN_RANKS.md §2.2. Every widening below is paired with a proof that
# the OLD TG-admin behaviour is intact (the happy-path tests above all
# run with a TG-admin caller and pass unchanged).
# ---------------------------------------------------------------------------

_RANKED_USER_ID = 7  # never in the fake's admin set; rank set per-test


async def _set_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    """Write a global rank directly (users.db upsert) and drop caches."""
    async with session_for(registry, DBName.USERS) as session:
        await UsersRepo(session).set_rank(user_id, rank)
    clear_rank_caches()


async def test_warn_ranked_rank2_non_tg_admin_allowed(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KEY POWER (legacy bot.py:7555-7577 + 31437): a rank-2 moderator
    WITHOUT Telegram adminship can /warn — the bot acts with its own
    rights. The warning row is recorded and the success copy is sent."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_rank(registry, _RANKED_USER_ID, 2)

    update = _group_msg("/warn spam", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED

    engine = registry.engine(DBName.MODERATION)
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(select(Warning))).scalars().all()
    assert len(rows) == 1
    assert rows[0].admin_id == _RANKED_USER_ID

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[-1]["text"].lower()
    assert "предупрежд" in reply or "warn" in reply


async def test_ban_ranked_rank2_denied_by_matrix(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rank 2 holds can_warn but NOT can_ban (default matrix,
    bot.py:2611-2712) — /ban from a rank-2 non-TG-admin is refused and
    no BanChatMember is issued."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_rank(registry, _RANKED_USER_ID, 2)

    update = _group_msg("/ban", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    assert "прав" in reply or "permission" in reply


async def test_unwarn_ranked_rank3_denied_rank4_allowed(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/unwarn maps to can_remove_warn (the matrix spelling): False up
    to rank 3, True from rank 4 (bot.py:2611-2712)."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await _set_rank(registry, _RANKED_USER_ID, 3)
    update = _group_msg("/unwarn", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    assert "прав" in reply or "permission" in reply

    sink.clear()
    await _set_rank(registry, _RANKED_USER_ID, 4)
    update = _group_msg(
        "/unwarn", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID, update_id=2
    )
    await dispatcher.feed_update(bot, update)
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    # No warning exists for the target → the gate PASSED and the
    # handler reached its "no warnings" reply (not a permission denial).
    reply = text_calls[0]["text"].lower()
    assert "прав" not in reply
    assert "permission" not in reply


async def test_warnings_and_pin_ranked_rank2_allowed(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening coverage for the read-only companion (/warnings →
    can_warn) and /pin (can_pin) at rank 2."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_rank(registry, _RANKED_USER_ID, 2)

    update = _group_msg("/warnings", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    assert "прав" not in text_calls[0]["text"].lower()
    assert "permission" not in text_calls[0]["text"].lower()

    sink.clear()
    update = _group_msg(
        "/pin", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID, update_id=2
    )
    await dispatcher.feed_update(bot, update)
    assert [e for e in sink if e["kind"] == "pinchatmessage"]


async def test_warn_ranked_cannot_target_equal_rank(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """can_moderate target-guard (legacy bot.py:7609-7620): a rank-2
    actor cannot warn a rank-2 target (target_rank >= actor_rank)."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())
    await _set_rank(registry, _RANKED_USER_ID, 2)
    await _set_rank(registry, _TARGET_USER_ID, 2)

    update = _group_msg("/warn", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    engine = registry.engine(DBName.MODERATION)
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(select(Warning))).scalars().all()
    assert rows == []

    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    # can_moderate_higher copy ("Нельзя модерировать пользователя с рангом …")
    assert "модерир" in reply or "moderate" in reply


async def test_warn_nobody_moderates_the_creator(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat creator is untouchable for ranked actors AND TG admins
    (legacy creator guard, bot.py:7597-7603; in the new pipeline the
    creator's "creator" member status also trips the target-is-admin
    refusal)."""
    creator_id = 50
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set(), creator_user_id=creator_id)
    await _set_rank(registry, _RANKED_USER_ID, 5)  # even a rank-5 owner

    update = _group_msg("/warn", user_id=_RANKED_USER_ID, reply_to_user_id=creator_id)
    await dispatcher.feed_update(bot, update)

    engine = registry.engine(DBName.MODERATION)
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(select(Warning))).scalars().all()
    assert rows == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls  # a refusal was sent


async def test_warn_tg_admin_can_target_higher_rank(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NO NARROWING for TG admins — a DELIBERATE divergence, not parity.

    Legacy's *permission* gate did wave a live Telegram admin through
    (``require_group_moderation``, bot.py:7568-7577), but the *target*
    guard right after it did not: ``can_moderate`` (bot.py:7580-7622)
    ran for every actor and exempted only ``DEVELOPER_IDS``
    (bot.py:7608-7609), so a synced admin was still rank-compared.
    We exempt them anyway because our staff-sync is lazy and main-chat
    only — see ``handlers.moderation._check_rank_target_ok`` (#338) for
    the full reasoning and the accepted residual. An unranked TG admin
    can therefore warn a rank-5 (non-admin) target."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)  # user 1 is TG admin
    await _set_rank(registry, _TARGET_USER_ID, 5)

    update = _group_msg("/warn", reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)

    engine = registry.engine(DBName.MODERATION)
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        rows = (await s.execute(select(Warning))).scalars().all()
    assert len(rows) == 1


async def test_mute_ranked_rank2_allowed_kick_rank1_denied(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix spot-checks: rank 2 holds can_mute (True) while rank 1
    lacks can_kick (bot.py:2611-2712)."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids=set())

    await _set_rank(registry, _RANKED_USER_ID, 2)
    update = _group_msg("/mute 10m", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID)
    await dispatcher.feed_update(bot, update)
    assert [e for e in sink if e["kind"] == "restrict"]

    sink.clear()
    await _set_rank(registry, _RANKED_USER_ID, 1)
    update = _group_msg(
        "/kick", user_id=_RANKED_USER_ID, reply_to_user_id=_TARGET_USER_ID, update_id=2
    )
    await dispatcher.feed_update(bot, update)
    assert [e for e in sink if e["kind"] == "banchatmember"] == []
    text_calls = [e for e in sink if e["kind"] == "text"]
    assert text_calls
    reply = text_calls[0]["text"].lower()
    # Handler matrix denial — or the command-rank gate (min 2 > rank 1)
    # once ``CommandAccessMiddleware`` is attached.
    assert "прав" in reply or "permission" in reply or "ранга" in reply or "rank" in reply


# ---------------------------------------------------------------------------
# /ban [duration] — RR-4 #39
# ---------------------------------------------------------------------------


def _ban_call(sink: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [e for e in sink if e["kind"] == "banchatmember"]
    assert len(calls) == 1, f"expected exactly one ban, got {calls!r}"
    return calls[0]


def _seconds_out(until: Any) -> float:
    """How far into the future the ban's ``until_date`` sits."""
    from datetime import UTC, datetime

    assert until is not None, "expected a finite ban, got a permanent one"
    return (until - datetime.now(UTC)).total_seconds()


async def test_ban_reply_bare_number_is_hours(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/ban 24`` in reply form means a day, not 24 minutes.

    Legacy's parser read a unitless token as minutes while legacy's own
    help and FAQ promised hours (ru.yaml:871). The copy is what an admin
    reads, so the copy wins.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/ban 24", reply_to_user_id=_TARGET_USER_ID))

    assert _seconds_out(_ban_call(sink)["until_date"]) == pytest.approx(24 * 3600, abs=30)
    # And the card states the term, so a misread token is visible at
    # once. It normalises to the largest fitting unit — 24 hours reads
    # back as "1д", which is the same span said better.
    reply = [e for e in sink if e["kind"] == "text"][-1]["text"]
    assert t("h_mod_ban_forever", "ru") not in reply
    assert _format_duration(24 * 3600, "ru") in reply
    # The consumed token is not also the reason.
    assert "📝" not in reply

    await bot.session.close()
    await registry.dispose()


async def test_ban_reply_duration_and_reason_both_land(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this item names: duration AND reason in the reply.

    Legacy printed the duration and swallowed the reason; the port
    printed neither. Both now show, and both reach the audit row.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(
        bot, _group_msg("/ban 7d злостный спам", reply_to_user_id=_TARGET_USER_ID)
    )

    assert _seconds_out(_ban_call(sink)["until_date"]) == pytest.approx(7 * 86400, abs=30)

    reply = [e for e in sink if e["kind"] == "text"][-1]["text"]
    assert "злостный спам" in reply
    assert "{" not in reply, "every placeholder must be filled"

    sm = async_sessionmaker(registry.engine(DBName.MODERATION), expire_on_commit=False)
    async with sm() as s:
        rows = (
            (await s.execute(select(ModerationLog).where(ModerationLog.action == "ban")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].reason == "злостный спам"
    # The term is auditable too — "why is this person still banned" is a
    # question the log should answer without replaying the chat.
    assert rows[0].details == f"duration_seconds={7 * 86400}"

    await bot.session.close()
    await registry.dispose()


async def test_ban_arg_form_bare_number_is_a_user_id(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision that makes ``allow_bare_number`` necessary.

    ``/ban 999999`` with no reply is a ban of user 999999 — NOT a
    999999-hour ban of nobody. Reading it as a duration would leave the
    real target untouched and the command silently useless.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/ban 999999"))

    call = _ban_call(sink)
    assert call["user_id"] == 999999
    assert call["until_date"] is None, "no duration token was given → permanent"

    await bot.session.close()
    await registry.dispose()


@pytest.mark.parametrize(
    "text",
    ["/ban 7d 999999 спам", "/ban 999999 7d спам"],
    ids=["leading", "trailing"],
)
async def test_ban_arg_form_accepts_the_duration_on_either_side(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Both orders work, the way /mute already accepts both."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg(text))

    call = _ban_call(sink)
    assert call["user_id"] == 999999
    assert _seconds_out(call["until_date"]) == pytest.approx(7 * 86400, abs=30)
    # Whichever side the duration sat on, it must not be eaten as reason.
    reply = [e for e in sink if e["kind"] == "text"][-1]["text"]
    assert "спам" in reply
    assert "7d" not in reply

    await bot.session.close()
    await registry.dispose()


async def test_ban_arg_form_does_not_eat_a_numeric_reason(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/ban 999999 3 раза спамил`` — the 3 is prose, not three hours.

    A reason may open with a number, and consuming it would both shorten
    the ban and truncate the record. Outside reply form a duration must
    carry its unit.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg("/ban 999999 3 раза спамил"))

    assert _ban_call(sink)["until_date"] is None
    reply = [e for e in sink if e["kind"] == "text"][-1]["text"]
    assert "3 раза спамил" in reply

    await bot.session.close()
    await registry.dispose()


@pytest.mark.parametrize("token", ["навсегда", "forever", "0"])
async def test_ban_permanent_words(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg(f"/ban {token}", reply_to_user_id=_TARGET_USER_ID))

    assert _ban_call(sink)["until_date"] is None
    reply = [e for e in sink if e["kind"] == "text"][-1]["text"]
    assert t("h_mod_ban_forever", "ru") in reply
    # The word itself must not survive into the reason line.
    assert "📝" not in reply

    await bot.session.close()
    await registry.dispose()


@pytest.mark.parametrize(
    ("text", "expect_permanent", "expect_seconds"),
    [
        # Past Telegram's 366-day ceiling the ban IS permanent, and the
        # card says permanent rather than promising an expiry that the
        # API silently discards.
        ("/ban 999w", True, 0),
        # Under Telegram's 30s floor a ban would ALSO become permanent —
        # the surprise nobody wants from "/ban 5s". Round up to a minute.
        ("/ban 5s", False, 60),
    ],
    ids=["over-ceiling", "under-floor"],
)
async def test_ban_clamps_at_telegrams_edges(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expect_permanent: bool,
    expect_seconds: int,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(bot, _group_msg(text, reply_to_user_id=_TARGET_USER_ID))

    until = _ban_call(sink)["until_date"]
    if expect_permanent:
        assert until is None
    else:
        assert _seconds_out(until) == pytest.approx(expect_seconds, abs=30)

    await bot.session.close()
    await registry.dispose()


async def test_ban_reason_is_html_escaped(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason is admin-typed free text landing in an HTML message.

    Unescaped, a reason of ``<b>`` would corrupt the card at best and be
    rejected by Telegram at worst — the ban would already have happened,
    so the admin would see a failure for an action that succeeded.
    """
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase, UsersBase])
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink)

    await dispatcher.feed_update(
        bot, _group_msg("/ban 1h <b>oops</b>", reply_to_user_id=_TARGET_USER_ID)
    )

    reply = [e for e in sink if e["kind"] == "text"][-1]["text"]
    assert "&lt;b&gt;oops&lt;/b&gt;" in reply
    assert "<b>oops</b>" not in reply

    await bot.session.close()
    await registry.dispose()


async def test_fine_commits_before_the_notifications_go_out(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#493: the debit and the audit row must be committed BEFORE ``/fine``
    starts talking to Telegram.

    Both writes open ``BEGIN IMMEDIATE`` (``db/engines.py``), and the
    handler then makes two network calls — the reply and the target's DM.
    Without the mid-handler checkpoint those write locks are held across
    both, so one blocked target or FloodWait stalls every other user's
    economy write until ``busy_timeout`` expires.

    The probe reads the balance from a *fresh* session at the moment the
    reply is dispatched. The DB file is per-test on disk, so a still-open
    transaction is invisible to that connection: seeing 400 proves the
    commit already happened.
    """
    bot_config = _dev_bot_config()
    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase],
        bot_config=bot_config,
    )
    sink: list[dict[str, Any]] = []
    _attach_fake_api(bot, monkeypatch, sink, admin_user_ids={_DEVELOPER_USER_ID})

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(registry.engine(DBName.ECONOMY), expire_on_commit=False)
    async with sm() as s:
        s.add(EconomyUser(user_id=_TARGET_USER_ID, balance=500, language="ru"))
        await s.commit()

    balance_when_replying: list[int | None] = []
    inner = bot.session.make_request

    async def probing_make_request(*args: Any, **kwargs: Any) -> Any:
        method = args[1] if len(args) > 1 else kwargs["method"]
        if type(method).__name__ == "SendMessage" and not balance_when_replying:
            async with sm() as probe:
                balance_when_replying.append(
                    (
                        await probe.execute(
                            select(EconomyUser.balance).where(
                                EconomyUser.user_id == _TARGET_USER_ID
                            )
                        )
                    ).scalar_one_or_none()
                )
        return await inner(*args, **kwargs)

    monkeypatch.setattr(bot.session, "make_request", probing_make_request)

    await dispatcher.feed_update(
        bot,
        _group_msg(
            "/fine 100 spam behaviour",
            user_id=_DEVELOPER_USER_ID,
            reply_to_user_id=_TARGET_USER_ID,
        ),
    )

    assert balance_when_replying == [400]

    await bot.session.close()
    await registry.dispose()
