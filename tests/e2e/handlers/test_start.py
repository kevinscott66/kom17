"""End-to-end ``/start`` flow: dispatcher → middleware → handler → DB.

Sends a real ``Update`` through a real Dispatcher with a real
SessionMiddleware against a real (tmp) SQLite users database. The only
fake is ``Bot.send_message``, which is monkey-patched to record outgoing
messages without hitting Telegram.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update_for(text: str, *, chat_type: str = "private", user_id: int = 1001) -> Update:
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 100,
                "date": 1_700_000_000,
                "chat": {"id": user_id, "type": chat_type},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Alice",
                    "username": "alice",
                    "language_code": "ru",
                },
                "text": text,
            },
        }
    )


async def test_start_handler_inserts_user_and_replies(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    # EconomyBase: since RR-6 #60 the welcome reads (and seeds) the wallet.
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/start"))
    assert result is not UNHANDLED  # handler matched

    # Reply went out.
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 1001
    assert "Привет" in sent[0]["text"]

    # Row was committed by the SessionMiddleware.
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        repo = UsersRepo(session)
        user = await repo.get(1001)
    assert user is not None
    assert user.username == "alice"
    assert user.language == "ru"


async def test_start_second_call_renders_welcome_back_template(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """First ``/start`` is :attr:`UserEntity.is_new=True` and gets the
    onboarding feature card; the second call from the same user must hit
    the ``else`` arm and render the returning-user dashboard instead
    (RR-6 #60). The branch matters because a refactor that collapsed the
    two would only surface as a "where did my balance line go" complaint
    from users — not from CI.
    """
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update_for("/start"))
    # Cold start: the feature tour, headed by the signup gift.
    first_body = sent[0]["text"]
    assert "Что я умею" in first_body
    assert "Подарок за старт" in first_body

    sent.clear()
    await dispatcher.feed_update(bot, _update_for("/start"))
    second_body = sent[0]["text"]
    # Returning: greeting + balance widget, no feature tour.
    assert "Что я умею" not in second_body
    assert "Баланс" in second_body
    assert "Игр сыграно" in second_body
    # The seeded welcome balance is what the first card promised.
    assert "<b>100</b>" in second_body


async def test_start_returning_user_owning_groups_gets_crown(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-6 #60: the 👑 glyph is the one part of the card that runs a real
    query (``owns_groups`` against ``bot_groups``), so it gets an e2e
    guard — a broken join would silently demote every group owner to 👤.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    # First touch makes the user known; the second renders the dashboard.
    await dispatcher.feed_update(bot, _update_for("/start"))

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(BotGroup(chat_id=-100_1, added_by_user_id=1001))
        await session.commit()

    sent.clear()
    await dispatcher.feed_update(bot, _update_for("/start"))
    assert "👑" in sent[0]["text"]
    assert "👤" not in sent[0]["text"]


async def test_start_crown_is_dropped_for_a_group_the_bot_left(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#111: the crown says "you run a group with this bot in it".

    The row outlives the bot's removal on purpose (it carries the payout
    attribution), so the glyph has to key off ``is_active`` rather than
    off the row's existence — otherwise everyone who ever added the bot
    anywhere keeps the badge forever.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update_for("/start"))

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(BotGroup(chat_id=-100_1, added_by_user_id=1001, is_active=0))
        await session.commit()

    sent.clear()
    await dispatcher.feed_update(bot, _update_for("/start"))
    assert "👑" not in sent[0]["text"]


async def test_start_in_group_chat_replies_with_dm_button(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group ``/start`` must answer (legacy answered ungated) with a
    group-appropriate welcome and a deep-link button into the private
    chat — not fall silently through.

    Regression guard: when the legacy bridge was removed, group ``/start``
    became a silent dead-end. This locks in the restored behaviour.
    """
    from aiogram.methods import SendMessage
    from aiogram.types import Chat, Message

    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)

    async def fake_get_me() -> Any:
        from aiogram.types import User as TgUser

        return TgUser(id=999, is_bot=True, first_name="Bot", username="my_test_bot")

    monkeypatch.setattr(bot, "get_me", fake_get_me)

    sent: list[SendMessage] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        assert isinstance(method, SendMessage)
        sent.append(method)
        return Message(
            message_id=1,
            date=1_700_000_000,
            chat=Chat(id=method.chat_id, type="group"),
            text=method.text,
        )

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    result = await dispatcher.feed_update(
        bot, _update_for("/start", chat_type="group", user_id=2002)
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert sent[0].chat_id == 2002
    # Deep-link button points back into the private chat.
    markup = sent[0].reply_markup
    assert markup is not None
    url = markup.inline_keyboard[0][0].url
    # #1926: back into the private chat, naming the group it came from.
    assert url == "https://t.me/my_test_bot?start=grp_2002"

    # The user row was seeded by the group interaction.
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        user = await UsersRepo(session).get(2002)
    assert user is not None


async def _seed_wallet(registry: Any, user_id: int, *, language: str = "ru") -> None:
    """Bootstrap an economy wallet so the user counts as an existing
    referrer (legacy ``_user_exists_in_economy``)."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        await EconomyRepo(session).get_or_create(user_id, language=language)
        await session.commit()


async def _referred_by(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(EconomyUser, user_id)
        return None if row is None else row.referred_by


async def test_start_ref_attributes_new_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-031: ``/start ref_<id>`` from a NEW user records the inviter in
    ``economy.users.referred_by`` and best-effort DMs the inviter.

    Restores the legacy invite funnel (bot.py:16294) that the strangler
    cutover dropped when the legacy bridge was removed.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    # Referrer 42 must already exist in the economy (have a wallet).
    await _seed_wallet(registry, 42, language="en")
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/start ref_42", user_id=1001))
    assert result is not UNHANDLED

    # Inviter got a notify; new user got the welcome (+ applied note).
    notify = next(m for m in sent if m["chat_id"] == 42)
    assert "new user" in notify["text"].lower()
    welcome = next(m for m in sent if m["chat_id"] == 1001)
    assert "реферальной ссылке" in welcome["text"]

    # Attribution persisted.
    assert await _referred_by(registry, 1001) == 42


async def test_the_inviter_dm_does_not_hold_the_economy_write_lock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1410 — the attribution is committed BEFORE Telegram is called.

    ``get_or_create`` + ``set_referrer`` open ``economy.db`` as ``BEGIN
    IMMEDIATE``, so until that transaction ends this update is the only
    writer the whole bot has. The very next statement used to be a DM to
    the inviter: an inviter who has blocked the bot, or any Telegram
    stall, parked every other user's wallet write behind one stranger's
    ``/start`` until ``busy_timeout`` turned it into ``database is
    locked``.

    Measured from outside rather than by asserting the checkpoint was
    called: a SECOND session on the same engine reads ``referred_by`` at
    the moment ``send_message`` fires. It can only see 42 if the write
    was already committed — which is the property that matters, and the
    one a later refactor could lose without touching the call.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_wallet(registry, 42, language="en")
    capture_outgoing(bot)
    inner = bot.session.make_request
    seen: list[int | None] = []

    async def _watch(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if getattr(method, "chat_id", None) == 42:
            seen.append(await _referred_by(registry, 1001))
        return await inner(_bot, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", _watch)

    result = await dispatcher.feed_update(bot, _update_for("/start ref_42", user_id=1001))
    assert result is not UNHANDLED
    assert seen == [42]


async def test_start_ref_unknown_referrer_ignored(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A ``ref_<id>`` pointing at a stranger with no wallet is ignored —
    the new user is still onboarded, but no attribution is written and
    no phantom DM is sent."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/start ref_999", user_id=1001))
    assert result is not UNHANDLED

    # Only the welcome went out — nothing to chat 999.
    assert all(m["chat_id"] == 1001 for m in sent)
    assert "реферальной ссылке" not in sent[0]["text"]
    assert await _referred_by(registry, 1001) is None


async def test_start_ref_self_referral_ignored(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/start ref_<self>`` must not self-attribute (legacy
    ``referrer_id != user_id`` guard)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_wallet(registry, 1001)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/start ref_1001", user_id=1001))
    assert result is not UNHANDLED
    assert all(m["chat_id"] == 1001 for m in sent)
    assert await _referred_by(registry, 1001) is None


async def test_start_ref_existing_user_not_reattributed(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An already-known user clicking a referral link keeps their
    original (absent) inviter — the wallet check gates attribution."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_wallet(registry, 42, language="ru")
    sent = capture_outgoing(bot)

    # First touch makes the user known (is_new=False thereafter).
    await dispatcher.feed_update(bot, _update_for("/start", user_id=1001))
    sent.clear()

    result = await dispatcher.feed_update(bot, _update_for("/start ref_42", user_id=1001))
    assert result is not UNHANDLED
    # No inviter notify (attribution skipped for existing user).
    assert all(m["chat_id"] == 1001 for m in sent)
    assert await _referred_by(registry, 1001) is None


async def test_start_ref_group_member_with_a_wallet_is_not_attributable(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#240: a wallet — not a profile row — closes attribution.

    The regression this pins is a *table* mix-up, so it is written from
    the state that actually exists in production rather than from the
    handler's own vocabulary. A group member who chats but never opens a
    DM gets an economy wallet from the activity middleware
    (``MessageActivityMiddleware._reward``) and **no** ``users.db users``
    row, because nothing outside private/command handlers calls
    ``UserService.touch``. Their very first DM is therefore ``is_new``,
    and the old gate handed them to whoever's link they clicked —
    forever, since ``set_referrer`` is first-write-wins.

    Legacy blocked this the moment they said one word in the group
    (``_user_exists_in_economy``, bot.py:16258). ``_seed_wallet`` writes
    the economy row alone, which is exactly that shape.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_wallet(registry, 42, language="ru")
    # The victim: wallet from group activity, never seen in a DM.
    await _seed_wallet(registry, 1001, language="ru")
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/start ref_42", user_id=1001))
    assert result is not UNHANDLED

    assert all(m["chat_id"] == 1001 for m in sent)
    assert "реферальной ссылке" not in sent[0]["text"]
    assert await _referred_by(registry, 1001) is None


def _update_with_name(first_name: str, *, user_id: int = 2002) -> Update:
    return Update.model_validate(
        {
            "update_id": 2,
            "message": {
                "message_id": 200,
                "date": 1_700_000_000,
                "chat": {"id": user_id, "type": "private"},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": first_name,
                    "language_code": "ru",
                },
                "text": "/start",
            },
        }
    )


async def test_start_escapes_html_in_display_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """SEC: a first_name carrying HTML markup must be escaped in the
    welcome (parse_mode=HTML) — no live tags / injected anchors."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)
    payload = "<a href='https://evil.example'>free coins</a><b>x</b>"

    await dispatcher.feed_update(bot, _update_with_name(payload))

    text = sent[-1]["text"]
    # The card legitimately carries its own <b> headers (RR-6 #60), so the
    # assertion is about the *payload*: it must arrive inert, escaped.
    assert "<a href=" not in text
    assert "&lt;a href=" in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text


async def test_capitalised_start_in_a_dm_reaches_the_welcome(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#972: ``/Start`` in a DM must onboard, not answer "unknown form".

    Mobile keyboards auto-capitalise the first character of a message, so
    this is a spelling first-ever users routinely send. The private
    registration omitted ``ignore_case=True`` while its group twin carried
    it, so the capitalised word matched nothing here and fell through to
    the tail catch-all — which *recognises* the word (the group twin
    published it) and answered "I didn't understand that form".

    The cost is not cosmetic: this handler is the only path that seeds a
    wallet and applies referral attribution, so the capitalised spelling
    cost a new user their welcome bonus while the very same ``/Start``
    kept working in a group.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/Start"))

    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Что я умею" in sent[0]["text"]


async def test_capitalised_start_deep_link_still_attributes(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#972, deep-link half: ``/Start ref_<id>`` must credit the inviter.

    Telegram itself always sends a tapped deep link lowercase, but a
    hand-typed or forwarded one arrives however the sender spelled it —
    and a payload-bearing ``/Start`` cannot be rescued by the bare
    registration, whose ``magic`` requires ``args is None``. Without
    ``ignore_case`` on ``CommandStart`` the invite funnel silently
    dropped the attribution.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_wallet(registry, 42, language="en")
    capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/Start ref_42", user_id=1001))

    assert result is not UNHANDLED
    assert await _referred_by(registry, 1001) == 42


# --------------------------------------------------------------------------
# #1926 — ``/start grp_<chat_id>``: the group→DM button carries its chat.
# --------------------------------------------------------------------------

_GRP_CHAT_ID = -1_001_234_567_890


async def _seed_group(
    registry: Any,
    chat_id: int,
    *,
    title: str | None,
    added_by: int = 7,
    is_active: int = 1,
) -> None:
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(
            BotGroup(
                chat_id=chat_id,
                added_by_user_id=added_by,
                chat_title=title,
                is_active=is_active,
            )
        )
        await session.commit()


async def _current_group(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        return await UserSettingsRepo(session).get_current_group(user_id)


def _stub_membership(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str | None,
    probes: list[tuple[int, int]],
) -> None:
    """Answer ``GetChatMember`` with ``status`` (``None`` → API error) and
    record every probe; everything else falls through to whatever capture
    is already installed on the session.
    """
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import ChatMemberLeft, ChatMemberMember
    from aiogram.types import User as TgUser

    inner = bot.session.make_request

    async def _route(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ != "GetChatMember":
            return await inner(_bot, method, timeout)
        probes.append((method.chat_id, method.user_id))
        if status is None:
            raise TelegramBadRequest(method=method, message="Bad Request: user not found")
        user = TgUser(id=method.user_id, is_bot=False, first_name="Alice")
        if status == "left":
            return ChatMemberLeft(user=user)
        return ChatMemberMember(user=user)

    monkeypatch.setattr(bot.session, "make_request", _route)


async def test_start_grp_remembers_the_group_for_a_member(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path: a member taps the button in a group the bot is in,
    and the DM that opens starts speaking for that group.

    Two halves matter, and both are asserted: the write (so anything
    crediting a group — the shop's registrar cut, group settings — has
    an id to work with) and the visible line naming the chat, which is
    the only way the person can notice the DM picked up the *wrong*
    group before they spend anything.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry, _GRP_CHAT_ID, title="Гараж")
    sent = capture_outgoing(bot)
    probes: list[tuple[int, int]] = []
    _stub_membership(bot, monkeypatch, status="member", probes=probes)

    result = await dispatcher.feed_update(
        bot, _update_for(f"/start grp_{_GRP_CHAT_ID}", user_id=1001)
    )

    assert result is not UNHANDLED
    assert await _current_group(registry, 1001) == _GRP_CHAT_ID
    # Membership was checked against Telegram, for this chat and caller.
    assert probes == [(_GRP_CHAT_ID, 1001)]
    # Ordinary welcome, plus the confirmation line naming the group.
    body = sent[-1]["text"]
    assert "Что я умею" in body
    assert "Гараж" in body


async def test_start_grp_falls_back_to_the_chat_id_when_the_title_is_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group registered before the bot could read its title still has
    to be nameable — the line exists so the person can tell which chat
    was picked, and "Группа <b></b> запомнена" tells them nothing.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry, _GRP_CHAT_ID, title=None)
    sent = capture_outgoing(bot)
    _stub_membership(bot, monkeypatch, status="member", probes=[])

    await dispatcher.feed_update(bot, _update_for(f"/start grp_{_GRP_CHAT_ID}", user_id=1001))

    assert await _current_group(registry, 1001) == _GRP_CHAT_ID
    assert str(_GRP_CHAT_ID) in sent[-1]["text"]


async def test_start_grp_ignores_a_group_the_bot_has_left(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deactivated row must not be remembered — and must not even be
    probed. ``is_active`` is cleared when the bot is removed, so the row
    survives only as payout attribution; pointing a DM at it would keep
    a dead chat collecting a live user's activity.

    The empty ``probes`` list is the second half of the assertion: the
    cheap DB read gates the network call, so a URL naming any chat id in
    the world can't be used to make the bot enumerate chats.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry, _GRP_CHAT_ID, title="Гараж", is_active=0)
    sent = capture_outgoing(bot)
    probes: list[tuple[int, int]] = []
    _stub_membership(bot, monkeypatch, status="member", probes=probes)

    result = await dispatcher.feed_update(
        bot, _update_for(f"/start grp_{_GRP_CHAT_ID}", user_id=1001)
    )

    assert result is not UNHANDLED
    assert await _current_group(registry, 1001) is None
    assert probes == []
    # Still an ordinary welcome — the payload is silent when it declines.
    assert "Что я умею" in sent[-1]["text"]
    assert "Гараж" not in sent[-1]["text"]


@pytest.mark.parametrize("status", ["left", None], ids=["left-the-chat", "probe-failed"])
async def test_start_grp_ignores_someone_who_is_not_in_the_chat(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
) -> None:
    """The membership probe is the actual authorisation step, and it is
    fail-closed: ``left`` and a Telegram error both mean "don't store".

    The link is a plain URL — anyone can retype it with a stranger's
    group id — so without this check a DM could be pointed at a chat the
    sender has never been in.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_group(registry, _GRP_CHAT_ID, title="Гараж")
    sent = capture_outgoing(bot)
    _stub_membership(bot, monkeypatch, status=status, probes=[])

    result = await dispatcher.feed_update(
        bot, _update_for(f"/start grp_{_GRP_CHAT_ID}", user_id=1001)
    )

    assert result is not UNHANDLED
    assert await _current_group(registry, 1001) is None
    assert "Гараж" not in sent[-1]["text"]


async def test_start_grp_with_a_malformed_payload_still_onboards(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``grp_`` claims the namespace, so a hand-mangled payload lands
    here rather than on the bare handler. It must still seed the user
    and answer with the welcome — the whole point of ``/start``.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)
    probes: list[tuple[int, int]] = []
    _stub_membership(bot, monkeypatch, status="member", probes=probes)

    result = await dispatcher.feed_update(bot, _update_for("/start grp_zzz", user_id=1001))

    assert result is not UNHANDLED
    assert "Что я умею" in sent[-1]["text"]
    assert probes == []
    assert await _current_group(registry, 1001) is None
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        assert await UsersRepo(session).get(1001) is not None
