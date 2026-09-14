"""End-to-end tests for /marry, /marry_accept, /marry_decline, /divorce, /breakup.

Stage T-019.  Each test feeds an aiogram :class:`Update` through the full
dispatcher (including the ``SessionMiddleware`` that attaches
``bonds_write_repo``) and asserts on the outgoing Telegram wire calls.

Scenarios covered:
* /marry — happy-path proposal (reply + inline keyboard)
* /marry — rejection: self-target, bot target, already married, target married
* /marry — rejection: insufficient relationship level
* /marry in private chat → falls through to legacy (UNHANDLED)
* /marry_accept slash command (latest proposal lookup)
* /marry_accept: wrong user (not_for_you)
* /marry_decline slash command
* Inline callback marry_accept_<id> — happy-path
* Inline callback marry_decline_<id> — happy-path
* /divorce happy-path
* /divorce: not married → single message
* /breakup happy-path
* /breakup: self, no reply, not together
* #1860 — the users.db (and, for /marry_extend, economy.db) write
  transaction ends before the outgoing card, so a bot that cannot
  deliver the confirmation no longer un-marries, un-divorces or refunds
  what it already wrote.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from sqlalchemy import select, update

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.users import Marriage, MarriageProposal, Relationship
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
from tests.e2e.handlers.conftest import assert_chat_scope_refusal, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _group_msg(
    text: str,
    *,
    user_id: int = 10,
    first_name: str = "Alice",
    chat_id: int = -100,
    chat_type: str = "supergroup",
    reply_to_user_id: int | None = None,
    reply_to_first_name: str = "Bob",
    reply_to_is_bot: bool = False,
    update_id: int = 1,
) -> Update:
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        first_name=first_name,
        reply_to_user_id=reply_to_user_id,
        reply_to_first_name=reply_to_first_name,
        reply_to_is_bot=reply_to_is_bot,
        update_id=update_id,
    )


def _cb(
    data: str,
    *,
    user_id: int = 20,
    first_name: str = "Bob",
    chat_id: int = -100,
    message_id: int = 10,
) -> Update:
    """Build a callback_query update with a group chat context.

    ``make_callback_update`` defaults to private chat; we patch the
    ``message.chat`` to be a supergroup so the ``F.chat.type`` filter (if
    any) and ``call.message.chat.id`` return the expected chat_id.
    """
    payload: dict[str, Any] = {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "from": {"id": user_id, "is_bot": False, "first_name": first_name},
            "chat_instance": "ci1",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "supergroup", "title": "G"},
                "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                "text": "proposal",
            },
        },
    }
    return Update.model_validate(payload)


# ---------------------------------------------------------------------------
# /marry
# ---------------------------------------------------------------------------


async def test_marry_no_reply_sends_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(bot, _group_msg("/marry", user_id=10))
    assert len(sent) == 1
    assert "ответь на сообщение" in sent[0]["text"].lower() or "reply" in sent[0]["text"].lower()


async def test_marry_self_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(
        bot,
        _group_msg("/marry", user_id=10, reply_to_user_id=10, reply_to_first_name="Alice"),
    )
    assert any("собой" in m["text"].lower() or "yourself" in m["text"].lower() for m in sent)


async def test_marry_bot_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(
        bot,
        _group_msg(
            "/marry",
            user_id=10,
            reply_to_user_id=999,
            reply_to_first_name="Bot",
            reply_to_is_bot=True,
        ),
    )
    assert any("бот" in m["text"].lower() or "bot" in m["text"].lower() for m in sent)


async def test_marry_already_married(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Marriage(
                chat_id=-100,
                user1_id=10,
                user2_id=30,
                created_at=datetime(2024, 1, 1),
                experience=0,
                status="active",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg("/marry", user_id=10, reply_to_user_id=20, reply_to_first_name="Bob"),
    )
    assert any("брак" in m["text"].lower() or "married" in m["text"].lower() for m in sent)


async def test_marry_target_married(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Marriage(
                chat_id=-100,
                user1_id=20,
                user2_id=30,
                created_at=datetime(2024, 1, 1),
                experience=0,
                status="active",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg("/marry", user_id=10, reply_to_user_id=20, reply_to_first_name="Bob"),
    )
    # Must mention that target is already married
    text = sent[0]["text"].lower()
    assert "пользователь уже" in text or "user is already" in text


async def test_marry_insufficient_rel_level(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No active relationship → insufficient level gate fires."""
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(
        bot,
        _group_msg("/marry", user_id=10, reply_to_user_id=20, reply_to_first_name="Bob"),
    )
    assert len(sent) == 1
    assert "уровень" in sent[0]["text"].lower() or "level" in sent[0]["text"].lower()


async def test_marry_happy_path_sends_proposal_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """With a level-6 relationship the handler must send the proposal card."""
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=60000,  # level 6
                status="active",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dp.feed_update(
        bot,
        _group_msg("/marry", user_id=10, reply_to_user_id=20, reply_to_first_name="Bob"),
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    # Proposal card must mention both users
    assert "Alice" in body or "Bob" in body


async def test_marry_in_private_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A DM ``/marry`` is answered, not ignored (#123)."""
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dp.feed_update(
        bot,
        make_message_update("/marry", chat_type="private", user_id=10),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="group", command="marry")


# ---------------------------------------------------------------------------
# /marry_accept and /marry_decline slash commands
# ---------------------------------------------------------------------------


async def test_marry_accept_no_proposal(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(bot, _group_msg("/marry_accept", user_id=20))
    assert "нет активного" in sent[0]["text"].lower() or "no active" in sent[0]["text"].lower()


async def test_marry_accept_slash_wrong_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A pending proposal addressed to user 20; user 30 tries to accept it."""
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            MarriageProposal(
                chat_id=-100,
                from_id=10,
                to_id=20,
                created_at=datetime.now(),
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    # user 30 tries /marry_accept — should get "not_for_you"
    await dp.feed_update(bot, _group_msg("/marry_accept", user_id=30))
    # user 30 has no proposal addressed to them
    assert "нет активного" in sent[0]["text"].lower() or "no active" in sent[0]["text"].lower()


async def test_marry_decline_slash(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """user 20 declines the proposal from user 10."""
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=60000,
                status="active",
            )
        )
        session.add(
            MarriageProposal(
                chat_id=-100,
                from_id=10,
                to_id=20,
                created_at=datetime.now(),
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dp.feed_update(bot, _group_msg("/marry_decline", user_id=20))
    assert any("отклонен" in m["text"].lower() or "declined" in m["text"].lower() for m in sent)


# ---------------------------------------------------------------------------
# Inline callbacks
# ---------------------------------------------------------------------------


async def test_callback_accept_happy_path(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=60000,
                status="active",
            )
        )
        # Insert the proposal with a known id — use flush to get the PK
        prop = MarriageProposal(
            chat_id=-100,
            from_id=10,
            to_id=20,
            created_at=datetime.now(),
        )
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    sent = capture_callback_outgoing(bot)
    result = await dp.feed_update(
        bot,
        _cb(f"marry_accept_{prop_id}", user_id=20, first_name="Bob", chat_id=-100),
    )
    assert result is not UNHANDLED
    edit_events = [m for m in sent if m.get("kind") == "edit"]
    assert len(edit_events) == 1
    body = edit_events[0]["text"].lower()
    assert "поздравляем" in body or "congratulations" in body


async def test_callback_accept_not_for_you(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """user 30 tries to accept a proposal addressed to user 20."""
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        prop = MarriageProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dp.feed_update(
        bot,
        _cb(f"marry_accept_{prop_id}", user_id=30, chat_id=-100),
    )
    answers = [m for m in sent if m.get("kind") == "callback_answer"]
    assert len(answers) == 1
    answer_text = (answers[0].get("text") or "").lower()
    assert "не для тебя" in answer_text or "not for you" in answer_text


async def test_callback_decline_happy_path(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        prop = MarriageProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dp.feed_update(
        bot,
        _cb(f"marry_decline_{prop_id}", user_id=20, chat_id=-100),
    )
    edit_events = [m for m in sent if m.get("kind") == "edit"]
    assert len(edit_events) == 1
    edit_text = edit_events[0]["text"].lower()
    assert "отклонен" in edit_text or "declined" in edit_text


# ---------------------------------------------------------------------------
# /divorce
# ---------------------------------------------------------------------------


async def test_divorce_happy_path(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Marriage(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=50,
                status="active",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dp.feed_update(bot, _group_msg("/divorce", user_id=10))
    assert result is not UNHANDLED
    assert any("расторгнут" in m["text"].lower() or "dissolved" in m["text"].lower() for m in sent)


async def test_divorce_not_married(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(bot, _group_msg("/divorce", user_id=10))
    assert any(
        "не в браке" in m["text"].lower() or "not married" in m["text"].lower() for m in sent
    )


# ---------------------------------------------------------------------------
# /breakup
# ---------------------------------------------------------------------------


async def test_breakup_happy_path(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=500,
                status="active",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dp.feed_update(
        bot,
        _group_msg(
            "/breakup",
            user_id=10,
            reply_to_user_id=20,
            reply_to_first_name="Bob",
        ),
    )
    assert result is not UNHANDLED
    assert any("прекращены" in m["text"].lower() or "ended" in m["text"].lower() for m in sent)


async def test_breakup_no_reply(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(bot, _group_msg("/breakup", user_id=10))
    assert any("ответь" in m["text"].lower() or "reply" in m["text"].lower() for m in sent)


async def test_breakup_self(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(
        bot,
        _group_msg("/breakup", user_id=10, reply_to_user_id=10, reply_to_first_name="Alice"),
    )
    assert any("собой" in m["text"].lower() or "yourself" in m["text"].lower() for m in sent)


async def test_breakup_not_together(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(
        bot,
        _group_msg("/breakup", user_id=10, reply_to_user_id=20, reply_to_first_name="Bob"),
    )
    assert any(
        "нет отношений" in m["text"].lower() or "not in a relationship" in m["text"].lower()
        for m in sent
    )


async def test_breakup_in_private_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A DM ``/breakup`` is answered, not ignored (#123).

    The module's group gate lives on the router, so a private
    invocation used to match nothing at all. It now reaches the refusal
    twin — and, as before, touches no relationship row.
    """
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dp.feed_update(
        bot,
        make_message_update("/breakup", chat_type="private", user_id=10),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="group", command="breakup")


# ---------------------------------------------------------------------------
# R-FIX-012: /marry_accept@BotName must take the accept branch
# ---------------------------------------------------------------------------


async def test_marry_accept_with_bot_suffix_takes_accept_path(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/marry_accept@SomeBot`` must accept (not decline).

    Previous parser saw the ``@`` suffix on ``message.text`` and fell to
    the decline branch; the new handler reads ``CommandObject.command``
    which aiogram already stripped.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=60000,  # level 6
                status="active",
            )
        )
        session.add(
            MarriageProposal(
                chat_id=-100,
                from_id=10,
                to_id=20,
                created_at=datetime.now(),
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dp.feed_update(bot, _group_msg("/marry_accept@SomeBot", user_id=20))

    # Accept path -> "h_marry_done" ("Поздравляем!" / "Congratulations!")
    assert any(
        "поздравляем" in m["text"].lower() or "congratulations" in m["text"].lower() for m in sent
    )
    # And NOT the decline message
    assert not any("отклонен" in m["text"].lower() or "declined" in m["text"].lower() for m in sent)

    # Marriage row was created
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        from sqlalchemy import select as _sel

        rows = (
            (await session.execute(_sel(Marriage).where(Marriage.chat_id == -100))).scalars().all()
        )
        assert len(rows) == 1
        assert rows[0].user1_id == 10
        assert rows[0].user2_id == 20


# ---------------------------------------------------------------------------
# R-FIX-010: concurrent accept (slash + callback) must produce exactly one bond
# ---------------------------------------------------------------------------


async def test_concurrent_accept_creates_exactly_one_marriage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Two simultaneous /marry_accept calls (slash + button) must:
    * write exactly ONE marriage row
    * surface a friendly "already handled" reply on the loser
    """
    import asyncio

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=60000,
                status="active",
            )
        )
        prop = MarriageProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    msg_sent = capture_outgoing(bot)
    cb_sent = capture_callback_outgoing(bot)

    slash = _group_msg("/marry_accept", user_id=20, update_id=100)
    inline = _cb(f"marry_accept_{prop_id}", user_id=20, chat_id=-100)

    await asyncio.gather(
        dp.feed_update(bot, slash),
        dp.feed_update(bot, inline),
    )

    # Exactly one marriage row exists
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        from sqlalchemy import select as _sel

        rows = (
            (await session.execute(_sel(Marriage).where(Marriage.chat_id == -100))).scalars().all()
        )
        assert len(rows) == 1

    # Exactly one "Поздравляем" success surfaced (either via message reply
    # or callback edit), and at least one "уже обработано" loser reply.
    all_texts = [m["text"].lower() for m in msg_sent if m.get("text")]
    all_texts += [m["text"].lower() for m in cb_sent if m.get("text")]
    successes = sum(1 for t in all_texts if "поздравляем" in t or "congratulations" in t)
    # Loser surfaces EITHER the atomic-claim "already handled" toast
    # (when both transactions raced past get_latest_proposal_for) OR the
    # "no active proposal" reply (when the winner already committed
    # status='accepted' by the time the loser ran the SELECT). Both are
    # correct safe outcomes — what matters is no second marriage row.
    losing_replies = sum(
        1
        for t in all_texts
        if "уже обработано" in t
        or "already been handled" in t
        or "нет активного" in t
        or "no active" in t
    )
    assert successes == 1
    assert losing_replies >= 1


# ---------------------------------------------------------------------------
# M-G-7 — proposal row must be rolled back when the outbound send fails
# ---------------------------------------------------------------------------


async def test_marry_send_failure_rolls_back_proposal_row(
    make_wired: WiredFactory,
) -> None:
    """M-G-7: when Telegram rejects the proposal-card send (e.g. the
    replied-to message was deleted between the /marry parse and the
    reply call → ``TelegramBadRequest``), the proposal row must be
    deleted so the caller can retry instead of being stuck behind a
    pending proposal that nobody can see to /marry_decline.
    """
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage
    from sqlalchemy import select

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=60000,  # level 6 — passes the rel gate
                status="active",
            )
        )
        await session.commit()

    # Make the FIRST SendMessage (the proposal-card reply) explode,
    # then let subsequent sends (the bot's error reply) succeed.
    sends: list[Any] = []
    original = bot.session.make_request

    async def flaky(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            sends.append(method)
            if len(sends) == 1:
                raise TelegramBadRequest(
                    method=method,
                    message="Bad Request: replied message not found",
                )
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = flaky  # type: ignore[method-assign,assignment]

    await dp.feed_update(
        bot,
        _group_msg("/marry", user_id=10, reply_to_user_id=20, reply_to_first_name="Bob"),
    )

    # Both sends were attempted: the failed proposal card + the bot's
    # apology to the caller.
    assert len(sends) >= 2
    # Proposal row was rolled back — table empty.
    async with sm() as session:
        rows = (await session.execute(select(MarriageProposal))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# #303: who the sender is
# ---------------------------------------------------------------------------


def _senderless_msg(text: str, *, update_id: int = 90) -> Update:
    """A group message with no ``from`` at all.

    This is the shape aiogram's model allows — ``Message.from_user`` is
    ``User | None`` — and the one
    :func:`~telegram_invite_bot.utils.aiogram.require_from_user` exists
    to reject. Telegram itself does not deliver it here: channel posts
    arrive as ``channel_post`` updates, and for a message sent on behalf
    of a chat *into a group* the Bot API fills ``from`` with a fake
    sender (see ``handlers/moderation.py:799-802``). The test pins the
    filter, not a live Telegram behaviour.

    ``make_message_update`` always emits a ``from`` key, so this one is
    built by hand.
    """
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": -100, "type": "supergroup", "title": "G"},
                "sender_chat": {"id": -777, "type": "channel", "title": "News"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
            },
        }
    )


def _anon_admin_msg(text: str, *, update_id: int = 91) -> Update:
    """A message from an *anonymous admin* of the group.

    Unlike :func:`_senderless_msg` this one is what Telegram really
    sends: ``from`` is present and carries the ``GroupAnonymousBot``
    service account, while ``sender_chat`` names the group itself.
    """
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": -100, "type": "supergroup", "title": "G"},
                "from": {"id": 1087968824, "is_bot": True, "first_name": "Group"},
                "sender_chat": {"id": -100, "type": "supergroup", "title": "G"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
            },
        }
    )


async def test_senderless_message_is_dropped_by_the_filter(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#303: no ``from`` means no handler, not an ``AssertionError``.

    Every handler in this module opens by demanding a sender, and before
    #303 that demand was an ``assert`` annotated "guaranteed by
    F.chat.type filter + group" — a filter that guaranteed no such
    thing and that no registration carried. The filter now exists, so
    the guarantee is structural. See :func:`_senderless_msg` for why
    this update is a type-level shape rather than one Telegram sends.
    """
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    result = await dp.feed_update(bot, _senderless_msg("/marry"))

    assert result is UNHANDLED
    assert sent == []


async def test_anonymous_admin_still_reaches_the_refusal(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#303: ``F.from_user`` must not swallow the anonymous-admin shape.

    This is the shape that does arrive in practice, and it carries a
    ``from_user`` (``GroupAnonymousBot``), so it still reaches
    ``handle_relationship``, where the ``sender_chat`` check answers it.
    If the filter had also caught this shape the refusal below would
    have become dead code — which is the real risk #303 had to avoid.
    """
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(bot, _anon_admin_msg("/relationship"))

    assert len(sent) == 1
    assert "Анонимные сообщения" in sent[0]["text"]


# ---------------------------------------------------------------------------
# #1546: /marry_extend charges through the escrow pair, not debit/credit
# ---------------------------------------------------------------------------


async def _seed_extendable_marriage(registry: Any, *, balance: int) -> None:
    """An active marriage for users 10/20 in chat -100 plus a funded wallet."""
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Marriage(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=0,
                status="active",
            )
        )
        await session.commit()
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        session.add(EconomyUser(user_id=10, balance=balance, language="ru"))
        await session.commit()


async def _wallet_numbers(registry: Any) -> tuple[int, int, int]:
    """``(balance, total_spent, total_earned)`` for user 10."""
    sm = registry.session(DBName.ECONOMY)
    async with sm() as session:
        wallet = await session.get(EconomyUser, 10)
        assert wallet is not None
        return wallet.balance, wallet.total_spent, wallet.total_earned


async def test_marry_extend_charges_and_books_the_spend(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The happy path settles the escrow, matching legacy ``remove_coins``."""
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_extendable_marriage(registry, balance=500)

    sent = capture_outgoing(bot)
    await dp.feed_update(bot, _group_msg("/marry_extend 3"))

    assert len(sent) == 1
    assert await _wallet_numbers(registry) == (470, 30, 0)  # 3 days × 10


async def test_marry_extend_rollback_leaves_the_lifetime_counters_alone(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1546: divorced between the gate and the write → a free round trip.

    The old ``debit``/``credit`` pair returned the coins but added the
    cost to ``total_spent`` AND ``total_earned`` every time, and the
    user drives both halves of the race (their own command against
    their own ``/divorce``).
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_extendable_marriage(registry, balance=500)

    async def _vanished(*_a: object, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(BondsWriteRepo, "extend_marriage", _vanished)

    sent = capture_outgoing(bot)
    await dp.feed_update(bot, _group_msg("/marry_extend 3"))

    assert len(sent) == 1
    assert await _wallet_numbers(registry) == (500, 0, 0)


# ---------------------------------------------------------------------------
# #1860 — the write lock must not span the outgoing card
# ---------------------------------------------------------------------------


async def _marriage_status(registry: Any) -> str | None:
    """The 10/20 marriage row's status in chat -100, or ``None``."""
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        row = await BondsWriteRepo(session).get_marriage(-100, 10)
        if row is not None:
            return str(row.status)
        result = await session.execute(
            select(Marriage).where(Marriage.chat_id == -100, Marriage.user1_id == 10)
        )
        found = result.scalar_one_or_none()
        return None if found is None else str(found.status)


async def test_callback_accept_survives_a_failed_toast(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1860: ``accept_proposal`` writes the bond, then the handler makes
    two network calls. ``edit_card`` already swallows a stale-card
    error, but ``call.answer`` does not — and ``SessionMiddleware`` rolls
    the whole update back on a raise, so a toast the bot could not
    deliver used to un-marry a couple who were told nothing at all.
    The checkpoint commits the bond first; a lost toast then costs only
    the toast.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import AnswerCallbackQuery

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Relationship(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=60000,
                status="active",
            )
        )
        prop = MarriageProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    # Order matters (see the ``capture_outgoing`` note in test_daily):
    # attach the stub FIRST, then chain onto it, or the fallthrough puts
    # a real request on the wire.
    capture_callback_outgoing(bot)
    original = bot.session.make_request

    async def kicked(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, AnswerCallbackQuery):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = kicked  # type: ignore[method-assign,assignment]

    # The dispatcher's errors router reads a Forbidden as a benign
    # reject and stays silent — which is precisely why the rollback went
    # unnoticed.
    await dp.feed_update(bot, _cb(f"marry_accept_{prop_id}", user_id=20, chat_id=-100))

    assert await _marriage_status(registry) == "active"


async def test_divorce_is_durable_when_the_reply_fails(
    make_wired: WiredFactory,
) -> None:
    """#1860: the divorce is written before the reply, so a bot that
    cannot answer no longer puts the couple back together.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        session.add(
            Marriage(
                chat_id=-100,
                user1_id=10,
                user2_id=20,
                created_at=datetime(2024, 1, 1),
                experience=0,
                status="active",
            )
        )
        await session.commit()

    original = bot.session.make_request

    async def kicked(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = kicked  # type: ignore[method-assign,assignment]
    await dp.feed_update(bot, _group_msg("/divorce"))

    assert await _marriage_status(registry) == "divorced"


async def test_marry_extend_settlement_survives_a_failed_card(
    make_wired: WiredFactory,
) -> None:
    """#1860, the money leg. ``/marry_extend`` is the only site in the
    module holding two write locks at once — the spend in economy.db and
    the new expiry in users.db — and it held both across the
    confirmation card. Worse than the lock: a card that could not be
    delivered rolled the pair back together, so the renewal the user had
    already been charged for simply evaporated (or, symmetrically, was
    granted for free — whichever way the rollback fell).
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_extendable_marriage(registry, balance=500)

    original = bot.session.make_request

    async def kicked(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = kicked  # type: ignore[method-assign,assignment]
    await dp.feed_update(bot, _group_msg("/marry_extend 3"))

    # Charged and settled, exactly as on the happy path.
    assert await _wallet_numbers(registry) == (470, 30, 0)
    # And the thing that was paid for is there: three more days on the bond.
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        row = await BondsWriteRepo(session).get_marriage(-100, 10)
    assert row is not None
    assert row.duration_days == 3


async def test_marry_extend_refund_survives_a_failed_card(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1860 + #1546: the refund arm must be just as durable.

    The checkpoint deliberately sits AFTER ``release``, never between
    the ``hold`` and it — placed earlier, an exception on the way to the
    refund would leave the escrow committed and the coins simply gone.

    The balance alone cannot prove the checkpoint ran: hold and release
    net to zero, so a rollback restores exactly the same numbers. What
    the checkpoint buys here is the LOCK — economy.db is the busiest
    file in the bot and both legs are write-headed, so probe it: a
    second connection must be able to write while the handler is
    mid-reply.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    bot, dp, registry = await make_wired(schemas=[UsersBase, EconomyBase])
    await _seed_extendable_marriage(registry, balance=500)

    async def _vanished(*_a: object, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(BondsWriteRepo, "extend_marriage", _vanished)

    econ = registry.session(DBName.ECONOMY)
    probe: list[str] = []
    original = bot.session.make_request

    async def kicked(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if not isinstance(method, SendMessage):
            return await original(_bot, method, timeout=timeout)
        try:
            async with econ() as other:
                await other.execute(
                    update(EconomyUser).where(EconomyUser.user_id == -1).values(balance=0)
                )
                await other.commit()
        except Exception as exc:  # noqa: BLE001 — the verdict IS the exception
            probe.append(repr(exc))
        else:
            probe.append("free")
        raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked")

    bot.session.make_request = kicked  # type: ignore[method-assign,assignment]
    await dp.feed_update(bot, _group_msg("/marry_extend 3"))

    # The round trip nets to zero and stays that way: balance restored,
    # neither lifetime counter moved.
    assert probe == ["free"]
    assert await _wallet_numbers(registry) == (500, 0, 0)


async def test_marry_top_toggle_refusal_releases_the_write_lock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1860: ``set_marriage_in_top`` refuses with a guarded UPDATE that
    matched zero rows — and a write-headed statement takes
    ``BEGIN IMMEDIATE`` whether or not it changes anything. There is
    nothing to be durable about on this branch, so probe the lock
    itself: a second connection must be able to write users.db while the
    handler is mid-reply.

    (``/divorce``'s own refusal arm is deliberately NOT checkpointed —
    ``soft_divorce`` returns ``False`` off a plain SELECT, so no write
    transaction ever opens there.)
    """
    from aiogram.methods import SendMessage

    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)

    probe: list[str] = []
    sent = capture_outgoing(bot)
    original = bot.session.make_request

    async def probing(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if not isinstance(method, SendMessage):
            return await original(_bot, method, timeout=timeout)
        try:
            async with sm() as other:
                await other.execute(update(Marriage).where(Marriage.chat_id == -1).values(in_top=0))
                await other.commit()
        except Exception as exc:  # noqa: BLE001 — the verdict IS the exception
            probe.append(repr(exc))
        else:
            probe.append("free")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = probing  # type: ignore[method-assign,assignment]
    await dp.feed_update(bot, _group_msg("/marry_top_on"))

    assert probe == ["free"], f"users.db was still locked during the reply: {probe}"
    assert len(sent) == 1
