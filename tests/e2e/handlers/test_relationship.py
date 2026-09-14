"""End-to-end tests for /relationship + rel_accept_/rel_decline_ (A-02).

The ``/relationship`` propose→accept flow is the *only* way to create a
relationship row, and ``/marry`` requires a level-6 relationship — so
without this flow the whole marriage feature was bricked once the legacy
bridge was removed.

Each test feeds an aiogram :class:`Update` through the full dispatcher
(including the ``SessionMiddleware`` that attaches ``bonds_write_repo``)
and asserts on the outgoing Telegram wire calls.

Scenarios covered:
* /relationship — happy-path proposal (reply + inline keyboard)
* /relationship — rejection: self-target, bot target, already together
* /relationship in private chat → refused with the group-only twin
* /relationship no-reply → empty status (single) and populated status list
* Inline callback rel_accept_<id> — happy-path creates the row
* Inline callback rel_accept_<id> — wrong user (not_for_you)
* Inline callback rel_accept_<id> — already resolved
* Inline callback rel_decline_<id> — happy-path
* #1860 — accept and decline are committed before the card is redrawn,
  so a bot that cannot deliver the card no longer un-does the bond it
  already wrote.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from sqlalchemy import select

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import Relationship, RelationshipProposal
from telegram_invite_bot.db.names import DBName
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
# /relationship — propose
# ---------------------------------------------------------------------------


async def test_relationship_self_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(
        bot,
        _group_msg("/relationship", user_id=10, reply_to_user_id=10, reply_to_first_name="Alice"),
    )
    assert any("собой" in m["text"].lower() or "yourself" in m["text"].lower() for m in sent)


async def test_relationship_bot_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(
        bot,
        _group_msg(
            "/relationship",
            user_id=10,
            reply_to_user_id=999,
            reply_to_first_name="Bot",
            reply_to_is_bot=True,
        ),
    )
    assert any("бот" in m["text"].lower() or "bot" in m["text"].lower() for m in sent)


async def test_relationship_already_together(
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
                experience=0,
                status="active",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dp.feed_update(
        bot,
        _group_msg("/relationship", user_id=10, reply_to_user_id=20),
    )
    assert any(
        "уже в отношениях" in m["text"].lower() or "already" in m["text"].lower() for m in sent
    )


async def test_relationship_happy_path_sends_proposal_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    result = await dp.feed_update(
        bot,
        _group_msg("/relationship", user_id=10, reply_to_user_id=20, reply_to_first_name="Bob"),
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Alice" in sent[0]["text"] or "Bob" in sent[0]["text"]

    # A pending proposal row must have been written.
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        rows = (await session.execute(select(RelationshipProposal))).scalars().all()
    assert len(rows) == 1
    assert rows[0].from_id == 10
    assert rows[0].to_id == 20
    assert rows[0].status == "pending"


async def test_relationship_in_private_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A DM ``/relationship`` is answered, not ignored (#123)."""
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dp.feed_update(
        bot,
        make_message_update("/relationship", chat_type="private", user_id=10),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="group", command="relationship")


# ---------------------------------------------------------------------------
# /relationship — no-reply status view
# ---------------------------------------------------------------------------


async def test_relationship_no_reply_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    await dp.feed_update(bot, _group_msg("/relationship", user_id=10))
    assert len(sent) == 1
    assert "пока нет" in sent[0]["text"].lower() or "no relationships" in sent[0]["text"].lower()


async def test_relationship_no_reply_lists_partners(
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
                experience=60000,  # level 6
                status="active",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dp.feed_update(bot, _group_msg("/relationship", user_id=10))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    # Partner mention + level name must show
    assert "tg://user?id=20" in body
    assert "Ухаживания" in body or "Courtship" in body


# ---------------------------------------------------------------------------
# Inline callbacks rel_accept_ / rel_decline_
# ---------------------------------------------------------------------------


async def test_callback_rel_accept_happy_path(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        prop = RelationshipProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    sent = capture_callback_outgoing(bot)
    result = await dp.feed_update(
        bot,
        _cb(f"rel_accept_{prop_id}", user_id=20, first_name="Bob", chat_id=-100),
    )
    assert result is not UNHANDLED
    edit_events = [m for m in sent if m.get("kind") == "edit"]
    assert len(edit_events) == 1
    edit_text = edit_events[0]["text"].lower()
    assert "отношениях" in edit_text or "relationship" in edit_text

    # The relationship row must now exist and the proposal must be gone.
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        rels = (await session.execute(select(Relationship))).scalars().all()
        props = (await session.execute(select(RelationshipProposal))).scalars().all()
    assert len(rels) == 1
    assert (rels[0].user1_id, rels[0].user2_id) == (10, 20)
    assert rels[0].status == "active"
    assert len(props) == 0


async def test_callback_rel_accept_not_for_you(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        prop = RelationshipProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dp.feed_update(
        bot,
        _cb(f"rel_accept_{prop_id}", user_id=30, chat_id=-100),
    )
    answers = [m for m in sent if m.get("kind") == "callback_answer"]
    assert len(answers) == 1
    answer_text = (answers[0].get("text") or "").lower()
    assert "не для тебя" in answer_text or "not for you" in answer_text


async def test_callback_rel_accept_already_resolved(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A proposal already flipped to 'accepted' must not create a 2nd row."""
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        prop = RelationshipProposal(
            chat_id=-100,
            from_id=10,
            to_id=20,
            created_at=datetime.now(),
            status="accepted",
        )
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dp.feed_update(
        bot,
        _cb(f"rel_accept_{prop_id}", user_id=20, chat_id=-100),
    )
    answers = [m for m in sent if m.get("kind") == "callback_answer"]
    assert len(answers) == 1
    assert (
        "обработано" in (answers[0].get("text") or "").lower()
        or "handled" in (answers[0].get("text") or "").lower()
    )


async def test_callback_rel_decline_happy_path(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        prop = RelationshipProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = prop.id
        await session.commit()

    sent = capture_callback_outgoing(bot)
    await dp.feed_update(
        bot,
        _cb(f"rel_decline_{prop_id}", user_id=20, chat_id=-100),
    )
    edit_events = [m for m in sent if m.get("kind") == "edit"]
    assert len(edit_events) == 1
    edit_text = edit_events[0]["text"].lower()
    assert "отклонен" in edit_text or "declined" in edit_text

    # No relationship row, proposal gone.
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        rels = (await session.execute(select(Relationship))).scalars().all()
        props = (await session.execute(select(RelationshipProposal))).scalars().all()
    assert len(rels) == 0
    assert len(props) == 0


# ---------------------------------------------------------------------------
# #1860 — the write lock must not span the outgoing card
# ---------------------------------------------------------------------------


async def _rows(registry: Any) -> tuple[int, int]:
    """``(relationships, pending proposals)`` in the whole users.db."""
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        rels = (await session.execute(select(Relationship))).scalars().all()
        props = (await session.execute(select(RelationshipProposal))).scalars().all()
    return len(rels), len(props)


def _forbid_toast(bot: Bot) -> None:
    """Make ``AnswerCallbackQuery`` fail the way a kicked bot does.

    Chains onto whatever transport is installed, so the capture fixture
    must already be attached — calling through to the live session with
    a fake token would put a real request on the wire.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import AnswerCallbackQuery

    original = bot.session.make_request

    async def kicked(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, AnswerCallbackQuery):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = kicked  # type: ignore[method-assign,assignment]


async def _seed_proposal(registry: Any) -> int:
    sm = registry.session(DBName.USERS)
    async with sm() as session:
        prop = RelationshipProposal(chat_id=-100, from_id=10, to_id=20, created_at=datetime.now())
        session.add(prop)
        await session.flush()
        prop_id = int(prop.id)
        await session.commit()
    return prop_id


async def test_rel_accept_survives_a_failed_toast(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1860: ``SessionMiddleware`` rolls the update back on any raise, so
    a toast the bot could not deliver used to take the freshly created
    relationship with it — and the errors router treats a Forbidden as a
    benign reject, so nobody was told. The bond is committed first now.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    prop_id = await _seed_proposal(registry)

    capture_callback_outgoing(bot)
    _forbid_toast(bot)
    await dp.feed_update(bot, _cb(f"rel_accept_{prop_id}", user_id=20, chat_id=-100))

    assert await _rows(registry) == (1, 0)


async def test_rel_decline_survives_a_failed_toast(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1860: same for the decline — a failed card must not resurrect a
    proposal the user already turned down.
    """
    bot, dp, registry = await make_wired(schemas=[UsersBase])
    prop_id = await _seed_proposal(registry)

    capture_callback_outgoing(bot)
    _forbid_toast(bot)
    await dp.feed_update(bot, _cb(f"rel_decline_{prop_id}", user_id=20, chat_id=-100))

    assert await _rows(registry) == (0, 0)
