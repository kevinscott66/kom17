"""End-to-end group-onboarding events: bot-added notice + new-member welcome.

Feeds real ``Update`` objects (a ``my_chat_member`` transition and a
``new_chat_members`` service message) through a real Dispatcher. The only
fake is ``Bot.session.make_request`` (records outgoing sends) plus a
stubbed ``bot.me`` / ``bot.get_me`` so the deep-link button and the
bot-self check are deterministic.

The greeting paths are money-less, so ``make_wired()`` with defaults is
enough for them. The two blocks at the bottom are the exception — both
*write*: ``handle_new_members`` files a ``user_group_joins`` row (RR-1
#3), and ``handle_bot_membership`` owns the ``bot_groups`` row that the
15% group cut is paid against (#111). Those tests ask for
``schemas=[UsersBase]`` — and one of each deliberately does not, to prove
the write stays best-effort when the table isn't there.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import (
    Chat,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    Update,
)
from aiogram.types import User as TgUser
from pydantic import SecretStr
from sqlalchemy import select

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import ModerationBase, UsersBase
from telegram_invite_bot.db.models.users import BotGroup, Marriage, UserGroupJoin
from telegram_invite_bot.db.models.users import User as UserRow
from telegram_invite_bot.db.models.welcome_config import WelcomeConfig
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.group_events import _RECENT_OFFBOARDS, _RECENT_ONBOARDS
from telegram_invite_bot.i18n import t
from telegram_invite_bot.services.rank_service import RankService, clear_rank_caches

if TYPE_CHECKING:
    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_BOT_ID = 999
_BOT_USERNAME = "my_test_bot"


@pytest.fixture(autouse=True)
def _clean_onboard_claims() -> Any:
    """#245(d)'s and #605's dedup maps are module-level state on the handler.

    They exist to make the two updates about one join — or one departure
    — collapse into a single pass, which also means a second test
    reusing the same ``(chat, user)`` pair inside the window would be
    silently skipped.
    """
    _RECENT_ONBOARDS.clear()
    _RECENT_OFFBOARDS.clear()
    yield
    _RECENT_ONBOARDS.clear()
    _RECENT_OFFBOARDS.clear()


# ``getChatMember`` answers are typed per status — pydantic refuses a
# payload whose ``status`` does not match the model it is validated into.
_MEMBER_MODELS: dict[str, Any] = {
    "administrator": ChatMemberAdministrator,
    "member": ChatMemberMember,
    "left": ChatMemberLeft,
}


def _stub_bot_identity(bot: Bot, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``bot.me()`` / ``bot.get_me()`` return a stable identity so
    the bot-self check and the deep-link button URL are deterministic."""

    async def _me(*_a: Any, **_k: Any) -> TgUser:
        return TgUser(id=_BOT_ID, is_bot=True, first_name="Bot", username=_BOT_USERNAME)

    monkeypatch.setattr(bot, "me", _me)
    monkeypatch.setattr(bot, "get_me", _me)


def _capture_sends(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    *,
    caller_status: str | None = None,
    caller_titular: bool = False,
) -> list[dict[str, Any]]:
    """Record outgoing ``SendMessage`` calls (chat_id / text / markup).

    Any other Telegram call is an assertion failure — the passive
    onboarding paths make exactly one kind of call, and a silent extra
    round-trip is worth failing on. ``caller_status`` is the one opt-out:
    the welcome-config commands are admin-gated, so their tests need
    ``getChatMember`` answered. Pass the status the check should see
    (``"administrator"`` to be let through, ``"member"`` to be refused,
    ``"left"`` for #233's "is the other spouse gone too?" probe).

    Since #337 the ``administrator`` status alone is no longer enough:
    ``_require_admin`` reads the rights matrix and refuses a title-only
    administrator. The caller payload therefore carries
    ``can_restrict_members=True`` by default; pass
    ``caller_titular=True`` for the payload that must be refused.
    """
    sink: list[dict[str, Any]] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "GetChatMember" and caller_status is not None:
            model = _MEMBER_MODELS.get(caller_status, ChatMemberMember)
            rights: dict[str, Any] = (
                {}
                if caller_titular or caller_status != "administrator"
                else {"can_restrict_members": True}
            )
            return model.model_validate(
                _member(
                    caller_status,
                    user={"id": method.user_id, "is_bot": False, "first_name": "Admin"},
                    **rights,
                )
            )
        if name == "SendMessage":
            sink.append(
                {
                    "chat_id": method.chat_id,
                    "text": method.text,
                    "markup": method.reply_markup,
                }
            )
            return Message(
                message_id=1,
                date=1_700_000_000,
                chat=Chat(id=method.chat_id, type="supergroup"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return sink


# ``ChatMemberRestricted`` requires the whole permission matrix; none of
# it matters here, only the ``is_member`` flag beside it does.
_RESTRICTED_PERMS: dict[str, Any] = {
    "until_date": 0,
    **{
        field: False
        for field in (
            "can_send_messages",
            "can_send_audios",
            "can_send_documents",
            "can_send_photos",
            "can_send_videos",
            "can_send_video_notes",
            "can_send_voice_notes",
            "can_send_polls",
            "can_send_other_messages",
            "can_add_web_page_previews",
            "can_change_info",
            "can_invite_users",
            "can_pin_messages",
            "can_manage_topics",
            "can_react_to_messages",
            "can_edit_tag",
        )
    },
}


# ``ChatMemberAdministrator``'s own required matrix. For the *bot's* own
# payload only the ``administrator`` status itself is what the handler
# reads; for a *caller* payload ``_capture_sends`` overrides
# ``can_restrict_members`` on top of this, because since #337 the status
# alone no longer clears :func:`handlers.moderation._require_admin`.
_ADMIN_RIGHTS: dict[str, Any] = {
    field: False
    for field in (
        "can_be_edited",
        "is_anonymous",
        "can_manage_chat",
        "can_delete_messages",
        "can_manage_video_chats",
        "can_restrict_members",
        "can_promote_members",
        "can_change_info",
        "can_invite_users",
        "can_post_stories",
        "can_edit_stories",
        "can_delete_stories",
    )
}

_STATUS_EXTRAS: dict[str, dict[str, Any]] = {
    "restricted": _RESTRICTED_PERMS,
    "administrator": _ADMIN_RIGHTS,
    "kicked": {"until_date": 0},
}


def _member(status: str, *, user: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """One ``ChatMember`` payload — about the bot unless told otherwise."""
    payload: dict[str, Any] = {
        "status": status,
        "user": user
        or {"id": _BOT_ID, "is_bot": True, "first_name": "Bot", "username": _BOT_USERNAME},
    }
    payload.update(_STATUS_EXTRAS.get(status, {}))
    payload.update(extra)
    return payload


def _membership_update(
    *,
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    adder_id: int = 7,
    adder_lang: str | None = "ru",
    chat_id: int = -1001,
    chat_title: str = "My Group",
) -> Update:
    """A ``my_chat_member`` transition about the bot. Defaults to a join."""
    adder: dict[str, Any] = {"id": adder_id, "is_bot": False, "first_name": "Adder"}
    if adder_lang is not None:
        adder["language_code"] = adder_lang
    return Update.model_validate(
        {
            "update_id": 10,
            "my_chat_member": {
                "chat": {"id": chat_id, "type": "supergroup", "title": chat_title},
                "from": adder,
                "date": 1_700_000_000,
                "old_chat_member": old or _member("left"),
                "new_chat_member": new or _member("member"),
            },
        }
    )


def _bot_added_update(**kwargs: Any) -> Update:
    """A ``my_chat_member`` transition where the BOT goes left → member."""
    return _membership_update(**kwargs)


def _chat_member_update(
    user: dict[str, Any],
    *,
    chat_id: int = -1002,
    old_status: str = "left",
    new_status: str = "member",
) -> Update:
    """A bare ``chat_member`` transition (#245(d), #605).

    Defaults to the invite-link join. Pass ``old_status``/``new_status``
    for the departure a chat with join/leave notices switched off
    produces instead. In real life no service message accompanies
    either, which is the whole point: this is the only signal the bot
    gets.
    """

    # ``ChatMemberRestricted`` is the one status whose payload needs
    # ``is_member`` to disambiguate "muted but here" from "gone"; without
    # it pydantic falls through to ``ChatMemberBanned`` and refuses.
    def _at(status: str) -> dict[str, Any]:
        extra = {"is_member": True} if status == "restricted" else {}
        return _member(status, user=user, **extra)

    return Update.model_validate(
        {
            "update_id": 12,
            "chat_member": {
                "chat": {"id": chat_id, "type": "supergroup", "title": "G"},
                "from": user,
                "date": 1_700_000_000,
                "old_chat_member": _at(old_status),
                "new_chat_member": _at(new_status),
            },
        }
    )


def _new_members_update(
    members: list[dict[str, Any]],
    *,
    chat_id: int = -1002,
) -> Update:
    """A service message carrying ``new_chat_members``."""
    return Update.model_validate(
        {
            "update_id": 11,
            "message": {
                "message_id": 50,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "supergroup", "title": "G"},
                "from": members[0],
                "new_chat_members": members,
            },
        }
    )


async def test_bot_added_posts_group_notice_with_deeplink(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bot joining a group → ``bot_added_*`` group notice with a deep-link
    button into the private chat (plus a best-effort DM to the adder)."""
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    result = await dispatcher.feed_update(bot, _bot_added_update(chat_id=-1001, adder_id=7))
    assert result is not UNHANDLED

    group = next(m for m in sent if m["chat_id"] == -1001)
    assert group["markup"] is not None
    # #1926: the link carries the chat it was posted in.
    assert (
        group["markup"].inline_keyboard[0][0].url == f"https://t.me/{_BOT_USERNAME}?start=grp_-1001"
    )

    # Adder also got a best-effort DM mentioning the (escaped) group title.
    dm = next(m for m in sent if m["chat_id"] == 7)
    assert "My Group" in dm["text"]


async def test_new_member_welcome_card_mentions_escaped_name(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A human joining → ``group_welcome_*`` card naming the joiner, with
    HTML-escaped display name (parse_mode=HTML injection seam)."""
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {
        "id": 555,
        "is_bot": False,
        "first_name": "<b>Mallory</b>",
        "language_code": "ru",
    }
    result = await dispatcher.feed_update(bot, _new_members_update([member], chat_id=-1002))
    assert result is not UNHANDLED

    assert len(sent) == 1
    card = sent[0]
    assert card["chat_id"] == -1002
    # Escaped, not live markup.
    assert "<b>Mallory</b>" not in card["text"]
    assert "&lt;b&gt;Mallory&lt;/b&gt;" in card["text"]
    # DM deep-link button present.
    assert (
        card["markup"].inline_keyboard[0][0].url == f"https://t.me/{_BOT_USERNAME}?start=grp_-1002"
    )


async def test_bot_only_join_is_skipped(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot joining as a new member → no welcome (bots are skipped)."""
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 4242, "is_bot": True, "first_name": "OtherBot", "username": "otherbot"}
    await dispatcher.feed_update(bot, _new_members_update([member]))

    # No human in the batch → handler returns without posting. The handler
    # still matches the filter (an all-bot batch is indistinguishable at
    # filter time), but the no-send contract is what we lock in here.
    assert sent == []


async def test_new_member_welcome_english_has_no_cyrillic(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``language_code='en'`` → the welcome card renders English (no
    Cyrillic characters leak through the i18n fallback)."""
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 700, "is_bot": False, "first_name": "Bob", "language_code": "en"}
    await dispatcher.feed_update(bot, _new_members_update([member]))

    assert len(sent) == 1
    text = sent[0]["text"]
    assert not any("Ѐ" <= ch <= "ӿ" for ch in text), text


# ---------------------------------------------------------------------------
# RR-1 #3: join bookkeeping (feeds the /profile since-join counter)
# ---------------------------------------------------------------------------


async def _stored_joins(registry: EngineRegistry) -> list[UserGroupJoin]:
    async with session_for(registry, DBName.USERS) as session:
        return list((await session.execute(select(UserGroupJoin))).scalars().all())


async def test_new_members_are_recorded_as_a_membership(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The welcome card is the visible half; this is the other one.

    ``/profile`` counts messages "since joining" from this row, so a
    greeting that posts without writing it leaves the card permanently
    unable to answer the question for everyone who joined after cutover.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)

    members = [
        {"id": 555, "is_bot": False, "first_name": "Ann", "language_code": "ru"},
        {"id": 556, "is_bot": False, "first_name": "Bob", "language_code": "ru"},
    ]
    await dispatcher.feed_update(bot, _new_members_update(members, chat_id=-1002))

    rows = sorted(await _stored_joins(registry), key=lambda r: r.user_id)
    assert [r.user_id for r in rows] == [555, 556]
    assert {r.chat_id for r in rows} == {-1002}
    assert {r.source for r in rows} == {"join_event"}
    assert {r.group_title for r in rows} == {"G"}
    assert all(r.is_active == 1 and r.left_at is None for r in rows)


async def test_an_invite_link_join_is_recorded_and_greeted(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#245(d): the join Telegram announces with no service message.

    Following an invite link, or having a join request approved,
    produces a ``chat_member`` transition and nothing else. Before this
    handler existed those two arrivals — the two most common ways into a
    public group — were the ones that got neither a membership row nor a
    captcha. The update fed here carries no ``message`` at all, so
    nothing but the new registration can be answering it.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    joiner = {"id": 561, "is_bot": False, "first_name": "Dana", "language_code": "ru"}
    await dispatcher.feed_update(bot, _chat_member_update(joiner, chat_id=-1002))

    rows = await _stored_joins(registry)
    assert [(r.user_id, r.chat_id, r.source) for r in rows] == [(561, -1002, "join_event")]
    assert len(sent) == 1
    assert "Dana" in sent[0]["text"]


async def test_one_join_seen_twice_is_onboarded_once(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary "add a friend" join fires BOTH updates (#245(d)).

    Telegram sends the ``new_chat_members`` service message *and* the
    ``chat_member`` transition, in no guaranteed order. The membership
    write is idempotent, so the row count would look fine either way —
    the tell is the welcome card, which would be posted twice for one
    arrival. Both orders are fed here because the dedup claim has to work
    from whichever update happens to land first.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    joiner = {"id": 562, "is_bot": False, "first_name": "Emil", "language_code": "ru"}
    await dispatcher.feed_update(bot, _new_members_update([joiner], chat_id=-1002))
    await dispatcher.feed_update(bot, _chat_member_update(joiner, chat_id=-1002))
    assert len(sent) == 1

    other = {"id": 563, "is_bot": False, "first_name": "Fay", "language_code": "ru"}
    await dispatcher.feed_update(bot, _chat_member_update(other, chat_id=-1002))
    await dispatcher.feed_update(bot, _new_members_update([other], chat_id=-1002))
    assert len(sent) == 2

    rows = sorted(await _stored_joins(registry), key=lambda r: r.user_id)
    assert [r.user_id for r in rows] == [562, 563]


@pytest.mark.parametrize(
    ("old_status", "new_status"),
    [("member", "restricted"), ("restricted", "member"), ("member", "administrator")],
)
async def test_a_within_membership_change_is_not_a_join(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    old_status: str,
    new_status: str,
) -> None:
    """``chat_member`` fires on every status change, not only arrivals.

    Two of those changes are ones this module causes itself: the captcha
    mute (``member`` -> ``restricted``) and the lift back afterwards. If
    "ended up a member" counted as a join, the lift would re-trigger
    onboarding and the loop would be the bot's own. The transition test
    therefore requires the *previous* status to be an explicit exit.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    user = {"id": 564, "is_bot": False, "first_name": "Gus", "language_code": "ru"}
    await dispatcher.feed_update(
        bot,
        _chat_member_update(user, chat_id=-1002, old_status=old_status, new_status=new_status),
    )

    assert sent == []
    assert await _stored_joins(registry) == []


async def test_a_bot_joining_by_link_is_not_onboarded(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``chat_member`` path filters bots like the service-message one.

    A bot added to the group produces a ``chat_member`` update about
    *itself* as the subject; the bot's own membership is a separate
    ``my_chat_member`` update and never reaches here.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    other_bot = {"id": 4243, "is_bot": True, "first_name": "OtherBot"}
    await dispatcher.feed_update(bot, _chat_member_update(other_bot))

    assert sent == []
    assert await _stored_joins(registry) == []


async def test_a_bot_only_join_records_nothing(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bots are skipped before the bookkeeping write, not after it — no
    membership row for an account that will never have a profile."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)

    member = {"id": 4242, "is_bot": True, "first_name": "OtherBot", "username": "otherbot"}
    await dispatcher.feed_update(bot, _new_members_update([member]))

    assert await _stored_joins(registry) == []


async def test_a_failed_join_write_still_lets_the_welcome_through(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bookkeeping is best-effort by design.

    ``make_wired()`` with no schemas leaves ``user_group_joins`` absent,
    so the write raises exactly as it would on a half-migrated deploy.
    The joiner must still be greeted: an accounting problem is not a
    reason to silently ignore a new member.
    """
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 557, "is_bot": False, "first_name": "Cara", "language_code": "ru"}
    result = await dispatcher.feed_update(bot, _new_members_update([member]))

    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Cara" in sent[0]["text"]


# ---------------------------------------------------------------------------
# #244: the departure half — ``left_chat_member``
# ---------------------------------------------------------------------------


def _left_member_update(
    member: dict[str, Any],
    *,
    chat_id: int = -1002,
    actor: dict[str, Any] | None = None,
) -> Update:
    """A service message carrying ``left_chat_member``.

    ``actor`` is the ``from`` field: it equals ``member`` on a voluntary
    leave and is the *admin* on a kick — the distinction the handler has
    to get right.
    """
    return Update.model_validate(
        {
            "update_id": 12,
            "message": {
                "message_id": 51,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "supergroup", "title": "G"},
                "from": actor or member,
                "left_chat_member": member,
            },
        }
    )


async def test_a_departure_flags_the_membership_and_says_goodbye(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy stamped ``is_active=0`` / ``left_at`` on every leave
    (bot.py:44187-44191) and posted a farewell (bot.py:44203-44207); the
    port did neither, so ``left_at`` has been NULL for every departure
    since cutover. The row must survive — ``joined_at`` is what the
    profile card's "in this group since" line reads.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 561, "is_bot": False, "first_name": "Ann", "language_code": "ru"}
    await dispatcher.feed_update(bot, _new_members_update([member], chat_id=-1002))
    joined_at = (await _stored_joins(registry))[0].joined_at

    result = await dispatcher.feed_update(bot, _left_member_update(member, chat_id=-1002))
    assert result is not UNHANDLED

    rows = await _stored_joins(registry)
    assert len(rows) == 1  # flagged, not deleted
    assert rows[0].is_active == 0
    assert rows[0].left_at is not None
    assert rows[0].joined_at == joined_at

    # Exactly two sends: the welcome card, then the farewell. Indexing
    # from the end instead would pass even with the farewell deleted —
    # the welcome names the same person.
    assert len(sent) == 2
    assert sent[1]["chat_id"] == -1002
    assert "Ann" in sent[1]["text"]
    assert sent[1]["text"] != sent[0]["text"]


async def test_a_kick_flags_the_member_not_the_admin_who_kicked(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a kick Telegram sets ``from`` to the admin who did it.

    Keying any mutation off ``from_user`` would mark the *moderator* as
    having left the group — and leave the person who actually left still
    counted as present.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)

    victim = {"id": 562, "is_bot": False, "first_name": "Vic", "language_code": "ru"}
    admin = {"id": 563, "is_bot": False, "first_name": "Mod", "language_code": "ru"}
    await dispatcher.feed_update(bot, _new_members_update([victim, admin], chat_id=-1002))

    await dispatcher.feed_update(bot, _left_member_update(victim, chat_id=-1002, actor=admin))

    rows = {r.user_id: r for r in await _stored_joins(registry)}
    assert rows[562].is_active == 0
    assert rows[563].is_active == 1
    assert rows[563].left_at is None


async def test_a_departure_seen_only_as_a_chat_member_transition_is_recorded(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#605: a chat that hides join/leave notices hides *both* of them.

    The join half already had a ``chat_member`` registration; the leave
    half did not, so in exactly the chats where the service message never
    arrives, ``left_at`` stayed NULL forever, the marriage's
    ``auto_divorce`` never ran and the global rank was never reset. A
    "delete and leave" from the client has the same shape.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 571, "is_bot": False, "first_name": "Ida", "language_code": "ru"}
    await dispatcher.feed_update(bot, _new_members_update([member], chat_id=-1002))

    result = await dispatcher.feed_update(
        bot,
        _chat_member_update(member, chat_id=-1002, old_status="member", new_status="left"),
    )
    assert result is not UNHANDLED

    rows = await _stored_joins(registry)
    assert len(rows) == 1
    assert rows[0].is_active == 0
    assert rows[0].left_at is not None

    # Welcome card, then farewell — the same two sends the announced
    # departure produces, from the transport that used to produce none.
    assert len(sent) == 2
    assert "Ida" in sent[1]["text"]
    assert sent[1]["text"] != sent[0]["text"]


async def test_the_twin_departure_updates_wave_goodbye_exactly_once(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kick fires both transports, in no guaranteed order.

    Whichever lands first claims the leaver; the loser must return
    having done nothing. Without the claim the chat gets two farewells
    for one departure — the mirror of the bug ``_RECENT_ONBOARDS``
    exists to prevent on the join side.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 572, "is_bot": False, "first_name": "Jon", "language_code": "ru"}
    await dispatcher.feed_update(bot, _new_members_update([member], chat_id=-1002))

    await dispatcher.feed_update(
        bot,
        _chat_member_update(member, chat_id=-1002, old_status="member", new_status="kicked"),
    )
    await dispatcher.feed_update(bot, _left_member_update(member, chat_id=-1002))

    assert len(sent) == 2  # welcome + one farewell, not two
    rows = await _stored_joins(registry)
    assert len(rows) == 1
    assert rows[0].is_active == 0


@pytest.mark.parametrize(
    ("old_status", "new_status"),
    [
        ("member", "restricted"),
        ("restricted", "member"),
        ("member", "administrator"),
        ("left", "member"),
        ("kicked", "left"),
    ],
)
async def test_a_transition_that_is_not_an_exit_waves_nobody_off(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    old_status: str,
    new_status: str,
) -> None:
    """The departure test is ``was in, is out`` — not "ended up out".

    ``kicked`` -> ``left`` is the second half of the ban+unban the
    captcha kick performs: one departure delivered as two transitions,
    and treating the tail as its own would wave the same person off
    twice. The mute and the lift are not exits at all, and ``left`` ->
    ``member`` is a join, handled by the other branch.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 573, "is_bot": False, "first_name": "Kim", "language_code": "ru"}
    await dispatcher.feed_update(bot, _new_members_update([member], chat_id=-1002))
    sent.clear()

    await dispatcher.feed_update(
        bot,
        _chat_member_update(member, chat_id=-1002, old_status=old_status, new_status=new_status),
    )

    assert sent == []
    rows = await _stored_joins(registry)
    assert len(rows) == 1
    assert rows[0].is_active == 1
    assert rows[0].left_at is None


async def test_a_departing_bot_is_neither_recorded_nor_waved_off(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy skipped bots outright (bot.py:44183-44184).

    They never had a membership row to flag, and a farewell card for a
    removed bot is noise in a group that just tidied itself up.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    other = {"id": 4343, "is_bot": True, "first_name": "OtherBot", "username": "ob"}
    await dispatcher.feed_update(bot, _left_member_update(other, chat_id=-1002))

    assert await _stored_joins(registry) == []
    assert sent == []


async def test_a_failed_leave_write_still_lets_the_farewell_through(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of the join-side guarantee.

    ``make_wired()`` with no schemas leaves ``user_group_joins`` absent,
    so the write raises exactly as it would on a half-migrated deploy —
    and legacy swallowed this write too (bot.py:44192-44193). The group
    must still see the goodbye.
    """
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 564, "is_bot": False, "first_name": "Dee", "language_code": "ru"}
    result = await dispatcher.feed_update(bot, _left_member_update(member))

    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Dee" in sent[0]["text"]


# ---------------------------------------------------------------------------
# #233: the departure applies the marriage's ``auto_divorce`` mode
# ---------------------------------------------------------------------------


_SPOUSE = 600
_PARTNER = 601


async def _seed_marriage(registry: EngineRegistry, mode: str, *, chat_id: int = -1002) -> None:
    """One active marriage between :data:`_SPOUSE` and :data:`_PARTNER`."""
    async with session_for(registry, DBName.USERS) as session:
        session.add(
            Marriage(
                chat_id=chat_id,
                user1_id=_SPOUSE,
                user2_id=_PARTNER,
                created_at=datetime(2024, 1, 1, 12, 0),
                status="active",
                auto_divorce=mode,
            )
        )


async def _marriage_status(registry: EngineRegistry, *, chat_id: int = -1002) -> str | None:
    async with session_for(registry, DBName.USERS) as session:
        row = (await session.execute(select(Marriage))).scalar_one()
        assert row.chat_id == chat_id
        return row.status


def _spouse_leaves() -> Update:
    return _left_member_update(
        {"id": _SPOUSE, "is_bot": False, "first_name": "Eve", "language_code": "ru"}
    )


async def test_auto_divorce_one_dissolves_the_marriage_when_a_spouse_leaves(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode ``one`` needs no probe at all (bot.py:44223-44224).

    ``_capture_sends`` without ``caller_status`` fails the test on any
    Telegram call other than a send, so this doubles as proof that the
    ``one`` branch never reaches for ``getChatMember``.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_marriage(registry, "one")

    await dispatcher.feed_update(bot, _spouse_leaves())

    assert await _marriage_status(registry) == "divorced"


async def test_auto_divorce_two_waits_while_the_partner_is_still_in_the_chat(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode ``two`` dissolves only once *both* are gone (bot.py:44236)."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch, caller_status="member")
    await _seed_marriage(registry, "two")

    await dispatcher.feed_update(bot, _spouse_leaves())

    assert await _marriage_status(registry) == "active"


async def test_auto_divorce_two_fires_once_the_partner_is_gone_too(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch, caller_status="left")
    await _seed_marriage(registry, "two")

    await dispatcher.feed_update(bot, _spouse_leaves())

    assert await _marriage_status(registry) == "divorced"


async def test_auto_divorce_off_leaves_the_marriage_alone(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default, and the reason the leave path may not divorce blindly."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_marriage(registry, "off")

    await dispatcher.feed_update(bot, _spouse_leaves())

    assert await _marriage_status(registry) == "active"


async def test_a_failed_partner_probe_keeps_the_marriage(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed, exactly like legacy's bare ``except: pass``.

    A marriage dissolved on a network hiccup cannot be undone by
    waiting; one left standing can always be ended by hand.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_marriage(registry, "two")

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("Bad Gateway")

    monkeypatch.setattr(bot, "get_chat_member", _boom)

    await dispatcher.feed_update(bot, _spouse_leaves())

    assert await _marriage_status(registry) == "active"


# ---------------------------------------------------------------------------
# #278-4: leaving the main chat drops the global rank back to 0
# ---------------------------------------------------------------------------


_MAIN_CHAT = -1002
_MAIN_CHAT_CONFIG = BotConfig(BOT_TOKEN=SecretStr("123:abc"), CHAT_ID=_MAIN_CHAT)


async def _seed_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    async with session_for(registry, DBName.USERS) as session:
        session.add(UserRow(user_id=user_id, first_name="Mod", rank=rank))


async def _stored_rank(registry: EngineRegistry, user_id: int) -> int | None:
    async with session_for(registry, DBName.USERS) as session:
        row = await session.get(UserRow, user_id)
        return None if row is None else row.rank


def _spy_on_set_rank(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record every ``RankService.set_rank`` *attempt*, performing none.

    ``set_rank`` refuses a developer demotion on its own, so a test that
    only checked the stored rank could not tell "never asked" from
    "asked and was turned down" — and the difference is a warning line
    on every owner who ever leaves the group.
    """
    calls: list[tuple[int, int]] = []

    async def _record(_self: RankService, user_id: int, rank: int, *, by: int) -> bool:
        calls.append((user_id, rank))
        return True

    monkeypatch.setattr(RankService, "set_rank", _record)
    return calls


def _member_leaves(user_id: int, *, chat_id: int = _MAIN_CHAT) -> Update:
    return _left_member_update(
        {"id": user_id, "is_bot": False, "first_name": "Mod", "language_code": "ru"},
        chat_id=chat_id,
    )


async def test_leaving_the_main_chat_resets_the_rank(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy demoted anyone who walked out of the main chat
    (bot.py:44250-44255); the port dropped the whole block, so a
    moderator could leave and keep every privilege.
    """
    clear_rank_caches()
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], bot_config=_MAIN_CHAT_CONFIG)
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_rank(registry, 610, 3)

    await dispatcher.feed_update(bot, _member_leaves(610))

    assert await _stored_rank(registry, 610) == 0


async def test_leaving_a_side_group_keeps_the_rank(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ranks are global (users_repo.py:199-216), so the gate is what
    stops any group the bot happens to sit in from stripping a
    moderator by kicking them once.
    """
    clear_rank_caches()
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], bot_config=_MAIN_CHAT_CONFIG)
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_rank(registry, 611, 3)

    await dispatcher.feed_update(bot, _member_leaves(611, chat_id=-1003))

    assert await _stored_rank(registry, 611) == 3


async def test_a_developer_leaving_is_never_offered_for_demotion(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy skipped ``DEVELOPER_IDS`` before touching anything
    (bot.py:44253) — and so must this, or the refusal warning fires
    every time the owner leaves.
    """
    clear_rank_caches()
    calls = _spy_on_set_rank(monkeypatch)
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(
            BOT_TOKEN=SecretStr("123:abc"), CHAT_ID=_MAIN_CHAT, DEVELOPER_ID_1=612
        ),
    )
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_rank(registry, 612, 6)

    await dispatcher.feed_update(bot, _member_leaves(612))

    assert calls == []
    assert await _stored_rank(registry, 612) == 6


async def test_an_ordinary_member_leaving_costs_no_write(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy short-circuits on ``get_user_rank(...) > 0``
    (bot.py:44253). Almost every departure is an ordinary member, so
    the read has to be the whole cost of the common case.
    """
    clear_rank_caches()
    calls = _spy_on_set_rank(monkeypatch)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], bot_config=_MAIN_CHAT_CONFIG)
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_rank(registry, 613, 0)

    await dispatcher.feed_update(bot, _member_leaves(613))

    assert calls == []


async def test_without_a_main_chat_no_departure_touches_ranks(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CHAT_ID`` defaults to 0 (``BotConfig.main_chat_id``).

    An unset main chat must not be read as "every chat is the main
    chat" — that would demote on any leave anywhere.
    """
    clear_rank_caches()
    calls = _spy_on_set_rank(monkeypatch)
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)
    await _seed_rank(registry, 614, 3)

    await dispatcher.feed_update(bot, _member_leaves(614))

    assert calls == []
    assert await _stored_rank(registry, 614) == 3


# ---------------------------------------------------------------------------
# #111: the bot's own membership drives ``bot_groups``
# ---------------------------------------------------------------------------


async def _stored_group(registry: EngineRegistry, chat_id: int) -> BotGroup | None:
    async with session_for(registry, DBName.USERS) as session:
        return await session.get(BotGroup, chat_id)


async def test_bot_join_registers_the_group(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing wrote ``bot_groups`` after the cutover, so every group the
    bot joined was invisible to ``/mygroups``, ``/shop`` and the 15% cut.
    The join itself is what has to create the row."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)

    await dispatcher.feed_update(bot, _membership_update(chat_id=-1001, adder_id=7))

    row = await _stored_group(registry, -1001)
    assert row is not None
    assert (row.added_by_user_id, row.chat_title, row.is_active) == (7, "My Group", 1)
    # Joined as a plain member — no admin rights yet.
    assert row.bot_has_admin_rights == 0


async def test_bot_promotion_refreshes_rights_without_re_greeting(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """member → administrator matches neither a join nor a leave filter.

    It is exactly the transition a two-handler design would drop, and it
    is the one that flips ``bot_has_admin_rights``. It must not re-post
    the welcome notice: the bot did not arrive, it got promoted.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    await dispatcher.feed_update(bot, _membership_update(chat_id=-1001, adder_id=7))
    sent.clear()
    await dispatcher.feed_update(
        bot,
        _membership_update(
            chat_id=-1001,
            adder_id=8,
            old=_member("member"),
            new=_member("administrator"),
        ),
    )

    row = await _stored_group(registry, -1001)
    assert row is not None
    assert row.bot_has_admin_rights == 1
    assert sent == []


@pytest.mark.parametrize("status", ["left", "kicked"])
async def test_bot_removal_deactivates_the_group(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """Being removed — walked out or banned — retires the group.

    The row stays: it is what the group cut is attributed against. Only
    its ``is_active`` flag moves, which is what hides it from ``/shop``.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    await dispatcher.feed_update(bot, _membership_update(chat_id=-1001, adder_id=7))
    sent.clear()
    await dispatcher.feed_update(
        bot,
        _membership_update(chat_id=-1001, old=_member("member"), new=_member(status)),
    )

    row = await _stored_group(registry, -1001)
    assert row is not None
    assert row.is_active == 0
    assert row.added_by_user_id == 7
    # A departure is not an occasion to post anything anywhere.
    assert sent == []


async def test_re_added_bot_keeps_the_original_owner(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payout-hijack guard, end to end.

    User 7 registers the group; user 8 kicks the bot and adds it back.
    If the re-add re-attributed the row, user 8 would now collect the
    15% cut of every purchase made for a group they never registered —
    and the owner's ``/transfer_rights`` decisions would be undoable by
    anyone with the right to remove a bot.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)

    await dispatcher.feed_update(bot, _membership_update(chat_id=-1001, adder_id=7))
    await dispatcher.feed_update(
        bot,
        _membership_update(chat_id=-1001, old=_member("member"), new=_member("left")),
    )
    await dispatcher.feed_update(bot, _membership_update(chat_id=-1001, adder_id=8))

    row = await _stored_group(registry, -1001)
    assert row is not None
    assert row.added_by_user_id == 7
    assert row.is_active == 1


async def test_a_muted_bot_is_still_a_member(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``restricted`` says nothing about presence on its own — Telegram
    puts that in ``is_member``. A bot that was merely muted has not left,
    and retiring its group would quietly cost the owner their cut."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)

    await dispatcher.feed_update(bot, _membership_update(chat_id=-1001, adder_id=7))
    await dispatcher.feed_update(
        bot,
        _membership_update(
            chat_id=-1001,
            old=_member("member"),
            new=_member("restricted", is_member=True),
        ),
    )

    row = await _stored_group(registry, -1001)
    assert row is not None
    assert row.is_active == 1


async def test_a_restricted_non_member_bot_is_deactivated(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of ``restricted``: ``is_member=False`` is a removal."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch)

    await dispatcher.feed_update(bot, _membership_update(chat_id=-1001, adder_id=7))
    await dispatcher.feed_update(
        bot,
        _membership_update(
            chat_id=-1001,
            old=_member("member"),
            new=_member("restricted", is_member=False),
        ),
    )

    row = await _stored_group(registry, -1001)
    assert row is not None
    assert row.is_active == 0


async def test_a_transition_about_someone_else_registers_nothing(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``my_chat_member`` is about the bot by definition, but the guard
    is what keeps a future ``chat_member`` registration from filing every
    human who joins as the group's registered owner."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    human = {"id": 4141, "is_bot": False, "first_name": "Someone"}
    await dispatcher.feed_update(
        bot,
        _membership_update(
            chat_id=-1001,
            old=_member("left", user=human),
            new=_member("member", user=human),
        ),
    )

    assert await _stored_group(registry, -1001) is None
    assert sent == []


async def test_a_failed_registration_still_greets_the_group(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``make_wired()`` with no schemas leaves ``bot_groups`` absent, so
    the write raises exactly as it would mid-migration. Onboarding is
    still the point of the update — the greeting must go out anyway."""
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    result = await dispatcher.feed_update(bot, _membership_update(chat_id=-1001))

    assert result is not UNHANDLED
    assert any(m["chat_id"] == -1001 for m in sent)


# ---------------------------------------------------------------------------
# Welcome-config admin commands (#215)
# ---------------------------------------------------------------------------
#
# These four commands were dead on arrival in every group: their router
# wrappers asked aiogram for a parameter named ``data``, aiogram injects
# by name out of the middleware dict, and no middleware has ever put a
# key called ``data`` there — so every single call raised ``TypeError``
# before the handler body ran and the caller saw the generic error card.
# The tests below feed real updates through the real dispatcher, which is
# the only level at which that failure is visible: the inner handlers
# were always fine, and unit-testing them directly is exactly what let
# the bug live.

_ERROR_CARD_MARKER = "Произошла ошибка"

#: Every spelling the four commands answer to — the Russian aliases are
#: as much a part of the contract as the English ones, and they route
#: through the same wrappers, so a regression would take them all.
_WELCOME_COMMANDS: list[str] = [
    "/setwelcome Привет, {user}!",
    "/set_welcome Привет, {user}!",
    "/приветствие_текст Привет, {user}!",
    "/welcome_off",
    "/w_off",
    "/приветствие_выкл",
    "/welcome_on",
    "/w_on",
    "/приветствие_вкл",
    "/welcome_test",
    "/w_test",
    "/приветствие_тест",
]


def _command_update(
    text: str,
    *,
    chat_id: int = -1003,
    user_id: int = 77,
    update_id: int = 12,
) -> Update:
    """A group text message carrying a slash command."""
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 60,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "supergroup", "title": "My Group"},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Admin",
                    "language_code": "ru",
                },
                "text": text,
                "entities": [
                    {
                        "type": "bot_command",
                        "offset": 0,
                        "length": len(text.split(maxsplit=1)[0]),
                    }
                ],
            },
        }
    )


async def _welcome_row(registry: EngineRegistry, chat_id: int) -> WelcomeConfig | None:
    async with session_for(registry, DBName.MODERATION) as session:
        return (
            await session.execute(select(WelcomeConfig).where(WelcomeConfig.group_id == chat_id))
        ).scalar_one_or_none()


@pytest.mark.parametrize("text", _WELCOME_COMMANDS)
async def test_welcome_admin_commands_reach_their_handler(
    text: str,
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#215 regression: every spelling gets a real answer, not the error
    card. Deliberately asserts on the *absence* of the generic error text
    rather than on each command's own copy — the failure this guards
    against is uniform across all twelve and has nothing to do with what
    any individual command means."""
    bot, dispatcher, _registry = await make_wired(schemas=[ModerationBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch, caller_status="administrator")

    result = await dispatcher.feed_update(bot, _command_update(text))

    assert result is not UNHANDLED
    assert sent, f"{text} produced no reply at all"
    assert _ERROR_CARD_MARKER not in sent[-1]["text"], sent[-1]["text"]


async def test_setwelcome_stores_the_template_and_previews_it(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/setwelcome`` persists the raw template and answers with it
    rendered against the caller's own name and the chat title."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch, caller_status="administrator")

    await dispatcher.feed_update(
        bot, _command_update("/setwelcome Привет, {user}, добро пожаловать в {chat}!")
    )

    row = await _welcome_row(registry, -1003)
    assert row is not None
    # Stored verbatim — substitution belongs to render time, not write time.
    assert row.template == "Привет, {user}, добро пожаловать в {chat}!"
    assert "Привет, Admin, добро пожаловать в My Group!" in sent[-1]["text"]


async def test_setwelcome_without_text_answers_with_usage(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``/setwelcome`` is a usage answer, not a stored empty
    template — the row must stay absent."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch, caller_status="administrator")

    await dispatcher.feed_update(bot, _command_update("/setwelcome"))

    assert await _welcome_row(registry, -1003) is None
    assert "/setwelcome" in sent[-1]["text"]


async def test_welcome_off_then_on_toggles_the_stored_row(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/welcome_off`` suppresses the custom template without discarding
    it, and ``/welcome_on`` restores it — the whole point of keeping the
    toggle separate from the template."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase])
    _stub_bot_identity(bot, monkeypatch)
    _capture_sends(bot, monkeypatch, caller_status="administrator")

    await dispatcher.feed_update(bot, _command_update("/setwelcome Привет, {user}!"))

    await dispatcher.feed_update(bot, _command_update("/welcome_off", update_id=13))
    row = await _welcome_row(registry, -1003)
    assert row is not None
    assert row.enabled is False
    assert row.template == "Привет, {user}!"

    await dispatcher.feed_update(bot, _command_update("/welcome_on", update_id=14))
    row = await _welcome_row(registry, -1003)
    assert row is not None
    assert row.enabled is True


async def test_welcome_commands_refuse_a_titular_admin(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#337: an ``administrator`` Telegram granted no moderation right is
    not an admin for ``_require_admin``, so the template stays unwritten."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch, caller_status="administrator", caller_titular=True)

    await dispatcher.feed_update(bot, _command_update("/setwelcome Привет, {user}!"))

    assert await _welcome_row(registry, -1003) is None
    assert sent
    assert _ERROR_CARD_MARKER not in sent[-1]["text"]


async def test_welcome_commands_still_refuse_a_non_admin(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #215 fix changes the signature, not the gate: a plain member
    is still refused and writes nothing."""
    bot, dispatcher, registry = await make_wired(schemas=[ModerationBase])
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch, caller_status="member")

    await dispatcher.feed_update(bot, _command_update("/setwelcome Привет, {user}!"))

    assert await _welcome_row(registry, -1003) is None
    assert sent
    assert _ERROR_CARD_MARKER not in sent[-1]["text"]


async def test_nameless_english_joiner_gets_an_english_stand_in(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1344: the placeholder for a nameless joiner follows the card's
    language.

    ``first_name`` is blank and there is no ``username``, so the handler
    falls through to its stand-in. That stand-in used to be the literal
    ``"друг"``, which the English template then rendered as
    "Hi, друг! Welcome to the chat" — Cyrillic in an English card. The
    sibling case above only pins the no-Cyrillic contract for a joiner
    who HAS a name, so it never touched this branch.
    """
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 701, "is_bot": False, "first_name": "", "language_code": "en"}
    await dispatcher.feed_update(bot, _new_members_update([member]))

    assert len(sent) == 1
    text = sent[0]["text"]
    assert not any("\u0400" <= ch <= "\u04ff" for ch in text), text
    assert t("h_default_name", "en") in text


async def test_nameless_english_leaver_gets_an_english_stand_in(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1344 on the farewell half — ``left_chat`` is a second call site
    with its own copy of the stand-in, so it can regress alone."""
    bot, dispatcher, _registry = await make_wired()
    _stub_bot_identity(bot, monkeypatch)
    sent = _capture_sends(bot, monkeypatch)

    member = {"id": 702, "is_bot": False, "first_name": "", "language_code": "en"}
    await dispatcher.feed_update(bot, _left_member_update(member))

    assert len(sent) == 1
    text = sent[0]["text"]
    assert not any("\u0400" <= ch <= "\u04ff" for ch in text), text
    assert t("h_default_name", "en") in text
