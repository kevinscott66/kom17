"""Integration matrix for #117 — ``/report`` to the group's admins.

The router is wired into a bespoke Dispatcher (NOT through
``build_main_router``) so the matrix stays independent of include
order, and every Telegram call the handler can make is served by one
fake ``make_request``: ``SendMessage``, ``ForwardMessage``, ``GetChat``,
``GetChatMember``, ``GetChatAdministrators``.

What each block pins, and why it is worth a test:

* **Group path** — the only path that works on Bot API 7.0. Reply
  required, self-report refused, admins DM'd with the body *and* a
  copy of the offending message, bots and the reporter excluded from
  the fan-out, and the confirmation counts what actually landed.
* **Delivery robustness** — a failed ``ForwardMessage`` (protected
  content) must NOT reduce the notified count, and a
  ``TelegramRetryAfter`` must be slept through and retried once rather
  than dropping the rest of the batch. Both are regressions of the
  legacy shape, which wrapped body+forward in one ``try`` and treated a
  429 as an ordinary failure.
* **Escaping** — the body is HTML now (legacy sent Markdown). A chat
  title or comment containing ``<`` must arrive escaped, or the whole
  card fails to parse and NO admin is told anything.
* **Cooldown** — stamped only on a dispatch that actually happened, so
  a "no admins" outcome does not lock the reporter out for a minute.
* **DM path** — legacy's transport, kept for the forwards that still
  carry a source chat, and refused (with a hint) for the ones Bot API
  7.0 made unresolvable. The membership check is the security half:
  legacy let anyone holding a forward DM-blast that chat's staff.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import (
    Chat,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    Message,
    Update,
)
from aiogram.types import User as TelegramUser

from telegram_invite_bot.handlers import report as report_module
from telegram_invite_bot.handlers.report import build_router
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

pytestmark = pytest.mark.integration

GROUP_CHAT_ID = -1001234
GROUP_TITLE = "T"
REPORTER_ID = 100
OFFENDER_ID = 200
ADMIN_A = 301
ADMIN_B = 302
ADMIN_BOT = 399
DM_CHAT_ID = REPORTER_ID
SOURCE_CHAT_ID = -1009999


class _FakeUser:
    """Stand-in for the ORM row ``UserService.touch`` returns."""

    def __init__(self, user_id: int, language: str = "ru") -> None:
        self.user_id = user_id
        self.language = language


class _FakeUserService:
    """Minimal ``user_service`` — the handler reads ``.language`` only."""

    def __init__(self, language: str = "ru") -> None:
        self._language = language

    async def touch(self, tg_user: TelegramUser) -> _FakeUser:
        return _FakeUser(tg_user.id, self._language)


def _administrator(user: TelegramUser) -> ChatMemberAdministrator:
    """One ``getChatAdministrators`` row, built without validation.

    ``ChatMemberAdministrator`` carries a dozen required permission
    booleans that grow with every Bot API bump (``can_post_stories`` &
    co. were the latest). The handler reads ``.user.id`` and
    ``.user.is_bot`` and nothing else, so spelling the permission set
    out here would be pure maintenance cost — same reasoning as
    :func:`_chat_info`.
    """
    return ChatMemberAdministrator.model_construct(status=ChatMemberStatus.ADMINISTRATOR, user=user)


def _admins(*ids: int, bots: tuple[int, ...] = ()) -> list[Any]:
    """A ``getChatAdministrators`` reply: first id is the owner."""
    members: list[Any] = []
    for index, user_id in enumerate(ids):
        user = TelegramUser(id=user_id, is_bot=False, first_name=f"A{user_id}")
        if index == 0:
            members.append(ChatMemberOwner(user=user, is_anonymous=False))
        else:
            members.append(_administrator(user))
    for bot_id in bots:
        members.append(_administrator(TelegramUser(id=bot_id, is_bot=True, first_name="helper")))
    return members


def _chat_info(chat_id: int, title: str) -> ChatFullInfo:
    """``getChat`` reply built without validation.

    ``ChatFullInfo`` grows required fields with every Bot API bump
    (``accepted_gift_types`` was the latest); ``model_construct`` keeps
    this fixture from needing an edit each time, and the handler reads
    exactly one attribute off it.
    """
    return ChatFullInfo.model_construct(id=chat_id, type="supergroup", title=title)


class _Telegram:
    """Scripted Telegram side: records calls, replays canned answers."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.forwarded: list[dict[str, Any]] = []
        self.admins: list[Any] = _admins(ADMIN_A, ADMIN_B)
        self.admins_error: Exception | None = None
        #: When set, the FIRST ``getChatAdministrators`` parks on
        #: ``admins_release`` after announcing itself on ``admins_parked``
        #: — the only way to hold one ``/report`` mid-dispatch while
        #: another is fed (#2015).
        self.park_admins = False
        self.admins_parked = asyncio.Event()
        self.admins_release = asyncio.Event()
        self.chat_error: Exception | None = None
        self.chat_title: str = "Источник"
        self.member: Any = ChatMemberMember(
            user=TelegramUser(id=REPORTER_ID, is_bot=False, first_name="R")
        )
        self.member_error: Exception | None = None
        self.forward_error: Exception | None = None
        #: user_id → exceptions to raise on the NEXT sends to them.
        self.send_errors: dict[int, list[Exception]] = {}

    async def __call__(self, _bot: Bot, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendMessage":
            queued = self.send_errors.get(method.chat_id)
            if queued:
                raise queued.pop(0)
            self.sent.append({"chat_id": method.chat_id, "text": method.text})
            return Message.model_construct(
                message_id=900 + len(self.sent),
                date=1,
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if name == "ForwardMessage":
            if self.forward_error is not None:
                raise self.forward_error
            self.forwarded.append(
                {
                    "chat_id": method.chat_id,
                    "from_chat_id": method.from_chat_id,
                    "message_id": method.message_id,
                }
            )
            return Message.model_construct(
                message_id=1,
                date=1,
                chat=Chat(id=method.chat_id, type="private"),
            )
        if name == "GetChat":
            if self.chat_error is not None:
                raise self.chat_error
            return _chat_info(method.chat_id, self.chat_title)
        if name == "GetChatAdministrators":
            if self.admins_error is not None:
                raise self.admins_error
            if self.park_admins:
                self.park_admins = False
                self.admins_parked.set()
                # Bounded so a regression can never hang the suite.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.admins_release.wait(), timeout=30.0)
            return self.admins
        if name == "GetChatMember":
            if self.member_error is not None:
                raise self.member_error
            return self.member
        raise AssertionError(f"unexpected Telegram call: {name}")


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=message)


@pytest.fixture(autouse=True)
def _clean_cooldown() -> Iterator[None]:
    """The cooldown cache is module-level; leaking it couples the cases."""
    report_module._reset_cooldown_for_tests()  # noqa: SLF001
    yield
    report_module._reset_cooldown_for_tests()  # noqa: SLF001


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the fan-out's sleeps instead of serving them in real time."""
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(report_module.asyncio, "sleep", _fake_sleep)
    return slept


@pytest.fixture
async def wired(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Dispatcher, Bot, _Telegram]]:
    bot = Bot(token="42:TEST-token")
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["user_service"] = _FakeUserService()
    dispatcher.include_router(build_router())
    telegram = _Telegram()
    monkeypatch.setattr(bot.session, "make_request", telegram)
    try:
        yield dispatcher, bot, telegram
    finally:
        await bot.session.close()


def _group_update(text: str, *, reply_to: int | None = OFFENDER_ID, **kwargs: Any) -> Update:
    return make_message_update(
        text,
        chat_id=GROUP_CHAT_ID,
        chat_type="supergroup",
        user_id=REPORTER_ID,
        reply_to_user_id=reply_to,
        message_id=10,
        **kwargs,
    )


def _dm_update(text: str, *, origin: dict[str, Any] | None, forward_message_id: int = 7) -> Update:
    """``/report`` in a DM as a reply to a (possibly forwarded) message."""
    chat = {"id": DM_CHAT_ID, "type": "private"}
    replied: dict[str, Any] = {
        "message_id": forward_message_id,
        "date": 1_700_000_000,
        "chat": chat,
        "from": {"id": REPORTER_ID, "is_bot": False, "first_name": "R"},
        "text": "(forwarded)",
    }
    if origin is not None:
        replied["forward_origin"] = origin
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 20,
                "date": 1_700_000_001,
                "chat": chat,
                "from": {"id": REPORTER_ID, "is_bot": False, "first_name": "R"},
                "text": text,
                "reply_to_message": replied,
            },
        }
    )


_ORIGIN_USER = {
    "type": "user",
    "date": 1_700_000_000,
    "sender_user": {"id": OFFENDER_ID, "is_bot": False, "first_name": "O"},
}
_ORIGIN_HIDDEN = {"type": "hidden_user", "date": 1_700_000_000, "sender_user_name": "Someone"}
_ORIGIN_CHAT = {
    "type": "chat",
    "date": 1_700_000_000,
    "sender_chat": {"id": SOURCE_CHAT_ID, "type": "supergroup", "title": "Src"},
}
_ORIGIN_CHANNEL = {
    "type": "channel",
    "date": 1_700_000_000,
    "chat": {"id": SOURCE_CHAT_ID, "type": "channel", "title": "Src"},
    "message_id": 3,
}
_ORIGIN_PRIVATE = {
    "type": "chat",
    "date": 1_700_000_000,
    # A positive id is a private chat, not a group — legacy's
    # ``report_chat.id >= 0`` guard, kept.
    "sender_chat": {"id": 55, "type": "private"},
}


# --------------------------------------------------------------------
# group path
# --------------------------------------------------------------------


async def test_group_report_without_reply_asks_for_one(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _group_update("/report", reply_to=None))
    assert [row["text"] for row in telegram.sent] == [t("h_report_reply_required", "ru")]


async def test_group_report_on_own_message_is_refused(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _group_update("/report", reply_to=REPORTER_ID))
    assert [row["text"] for row in telegram.sent] == [t("h_report_self", "ru")]


async def test_group_report_notifies_every_admin_with_body_and_copy(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _group_update("/report спам"))

    admin_messages = [row for row in telegram.sent if row["chat_id"] in {ADMIN_A, ADMIN_B}]
    assert [row["chat_id"] for row in admin_messages] == [ADMIN_A, ADMIN_B]
    body = admin_messages[0]["text"]
    assert GROUP_TITLE in body
    assert "спам" in body
    # The offending message itself, forwarded out of the group.
    assert telegram.forwarded == [
        {"chat_id": ADMIN_A, "from_chat_id": GROUP_CHAT_ID, "message_id": 9},
        {"chat_id": ADMIN_B, "from_chat_id": GROUP_CHAT_ID, "message_id": 9},
    ]
    assert telegram.sent[-1] == {
        "chat_id": GROUP_CHAT_ID,
        "text": t("report_sent", "ru", count=2),
    }


async def test_group_report_without_comment_renders_the_dash(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _group_update("/report"))
    body = next(row["text"] for row in telegram.sent if row["chat_id"] == ADMIN_A)
    assert t("report_no_comment", "ru") in body


async def test_group_report_skips_bots_and_the_reporter(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    telegram.admins = _admins(REPORTER_ID, ADMIN_A, bots=(ADMIN_BOT,))
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert {row["chat_id"] for row in telegram.sent} == {ADMIN_A, GROUP_CHAT_ID}
    assert telegram.sent[-1]["text"] == t("report_sent", "ru", count=1)


async def test_group_report_caps_the_fan_out(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    telegram.admins = _admins(*range(1000, 1000 + report_module._MAX_ADMINS + 5))  # noqa: SLF001
    await dispatcher.feed_update(bot, _group_update("/report"))
    notified = [row for row in telegram.sent if row["chat_id"] != GROUP_CHAT_ID]
    assert len(notified) == report_module._MAX_ADMINS  # noqa: SLF001


async def test_group_report_with_no_admins_says_so_and_keeps_the_cooldown_clear(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    """A report nobody received must not cost the reporter their minute."""
    dispatcher, bot, telegram = wired
    telegram.admins = []
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert [row["text"] for row in telegram.sent] == [t("h_report_no_admins", "ru")]

    telegram.admins = _admins(ADMIN_A)
    telegram.sent.clear()
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert telegram.sent[-1]["text"] == t("report_sent", "ru", count=1)


async def test_group_report_admin_lookup_failure_is_not_an_exception(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    telegram.admins_error = _bad_request("chat not found")
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert [row["text"] for row in telegram.sent] == [t("h_report_no_admins", "ru")]


async def test_second_report_within_the_cooldown_is_refused(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _group_update("/report"))
    telegram.sent.clear()
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert len(telegram.sent) == 1
    assert telegram.sent[0]["chat_id"] == GROUP_CHAT_ID
    assert telegram.sent[0]["text"].startswith(t("h_report_cooldown", "ru", seconds=0)[:12])


async def test_reports_arriving_during_a_dispatch_share_one_cooldown(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    """#2015: the cooldown must be claimed with the decision to spend it.

    The refusal read ``_COOLDOWN`` at the top of the handler but the
    stamp only landed after the fan-out — and between them sit
    ``getChatAdministrators`` plus a message and a forward per admin.
    Every ``/report`` sent inside that window found a clear cooldown, so
    a reporter tapping the command three times bought three fan-outs:
    the DM burst the minute was there to bound, times three, to the same
    admins.

    Driven rather than raced: the first report is frozen inside its
    admin lookup, two more are fed and allowed to run as far as they
    can, and only then is the first released.
    """
    dispatcher, bot, telegram = wired
    telegram.park_admins = True

    winner = asyncio.create_task(dispatcher.feed_update(bot, _group_update("/report")))
    await telegram.admins_parked.wait()
    losers = [
        asyncio.create_task(dispatcher.feed_update(bot, _group_update("/report"))) for _ in range(2)
    ]
    await asyncio.wait(set(losers), timeout=1.0)
    telegram.admins_release.set()
    await asyncio.gather(winner, *losers)

    dms = [row for row in telegram.sent if row["chat_id"] != GROUP_CHAT_ID]
    assert len(dms) == 2, f"one report, one fan-out — the admins got {len(dms)} DMs: {dms}"
    replies = [row["text"] for row in telegram.sent if row["chat_id"] == GROUP_CHAT_ID]
    assert replies.count(t("report_sent", "ru", count=2)) == 1
    assert len(replies) == 3, f"the two extra taps were not refused: {replies}"


async def test_a_blocked_admin_does_not_stop_the_rest(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    telegram.send_errors[ADMIN_A] = [_bad_request("bot was blocked by the user")]
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert {row["chat_id"] for row in telegram.sent} == {ADMIN_B, GROUP_CHAT_ID}
    assert telegram.sent[-1]["text"] == t("report_sent", "ru", count=1)


async def test_flood_wait_is_slept_through_and_retried_once(
    wired: tuple[Dispatcher, Bot, _Telegram],
    _no_pauses: list[float],
) -> None:
    """A 429 must not drop the remaining admins the way legacy did."""
    dispatcher, bot, telegram = wired
    telegram.send_errors[ADMIN_A] = [
        TelegramRetryAfter(
            method=SendMessage(chat_id=ADMIN_A, text="x"), message="flood", retry_after=3
        )
    ]
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert {row["chat_id"] for row in telegram.sent} == {ADMIN_A, ADMIN_B, GROUP_CHAT_ID}
    assert telegram.sent[-1]["text"] == t("report_sent", "ru", count=2)
    assert 3 in _no_pauses


async def test_flood_wait_longer_than_the_cap_is_clamped(
    wired: tuple[Dispatcher, Bot, _Telegram],
    _no_pauses: list[float],
) -> None:
    dispatcher, bot, telegram = wired
    telegram.send_errors[ADMIN_A] = [
        TelegramRetryAfter(
            method=SendMessage(chat_id=ADMIN_A, text="x"), message="flood", retry_after=9999
        )
    ]
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert report_module._MAX_RETRY_AFTER_SECONDS in _no_pauses  # noqa: SLF001
    assert 9999 not in _no_pauses


async def test_protected_content_forward_failure_still_counts_the_admin(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    """Legacy wrapped body+forward in one ``try`` and reported zero."""
    dispatcher, bot, telegram = wired
    telegram.forward_error = _bad_request("message can't be forwarded")
    await dispatcher.feed_update(bot, _group_update("/report"))
    assert telegram.forwarded == []
    assert telegram.sent[-1]["text"] == t("report_sent", "ru", count=2)


async def test_body_escapes_html_in_title_comment_and_reporter(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    """One unescaped ``<`` and Telegram rejects the card for every admin."""
    dispatcher, bot, telegram = wired
    # Hand-built rather than ``make_message_update``: that helper hard-codes
    # the group title to "T", and the title is one of the three
    # interpolations under test.
    chat = {"id": GROUP_CHAT_ID, "type": "supergroup", "title": "<i>Клуб</i>"}
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1_700_000_001,
                "chat": chat,
                "from": {"id": REPORTER_ID, "is_bot": False, "first_name": "<script>"},
                "text": "/report <b>look</b>",
                "reply_to_message": {
                    "message_id": 9,
                    "date": 1_700_000_000,
                    "chat": chat,
                    "from": {"id": OFFENDER_ID, "is_bot": False, "first_name": "O"},
                    "text": "(replied)",
                },
            },
        }
    )
    await dispatcher.feed_update(bot, update)

    body = next(row["text"] for row in telegram.sent if row["chat_id"] == ADMIN_A)
    assert "&lt;b&gt;look&lt;/b&gt;" in body
    assert "&lt;i&gt;Клуб&lt;/i&gt;" in body
    assert "&lt;script&gt;" in body
    # The frame's own markup survives — only the interpolations escape.
    assert "<b>" in body


async def test_long_comment_is_capped(wired: tuple[Dispatcher, Bot, _Telegram]) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _group_update("/report " + "я" * 5000))
    body = next(row["text"] for row in telegram.sent if row["chat_id"] == ADMIN_A)
    assert len(body) < 1000


async def test_russian_and_kom_spellings_route(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    for spelling in ("/репорт", "/kom_report"):
        report_module._reset_cooldown_for_tests()  # noqa: SLF001
        telegram.sent.clear()
        await dispatcher.feed_update(bot, _group_update(spelling))
        assert telegram.sent[-1]["text"] == t("report_sent", "ru", count=2)


async def test_reporter_name_carries_the_username(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _group_update("/report", username="nick"))
    body = next(row["text"] for row in telegram.sent if row["chat_id"] == ADMIN_A)
    assert "(@nick)" in body


# --------------------------------------------------------------------
# DM path
# --------------------------------------------------------------------


async def test_dm_report_without_a_reply_explains_the_command(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(
        bot, make_message_update("/report", chat_id=DM_CHAT_ID, user_id=REPORTER_ID)
    )
    assert [row["text"] for row in telegram.sent] == [t("h_report_dm_usage", "ru")]


async def test_dm_report_on_a_non_forward_explains_the_command(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _dm_update("/report", origin=None))
    assert [row["text"] for row in telegram.sent] == [t("h_report_dm_usage", "ru")]


@pytest.mark.parametrize("origin", [_ORIGIN_USER, _ORIGIN_HIDDEN, _ORIGIN_PRIVATE])
async def test_dm_report_on_an_unresolvable_forward_points_at_the_group_form(
    wired: tuple[Dispatcher, Bot, _Telegram],
    origin: dict[str, Any],
) -> None:
    """Bot API 7.0 carries no chat for a user forward — say so, don't guess."""
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _dm_update("/report", origin=origin))
    assert [row["text"] for row in telegram.sent] == [t("h_report_forward_hint", "ru")]


@pytest.mark.parametrize("origin", [_ORIGIN_CHAT, _ORIGIN_CHANNEL])
async def test_dm_report_on_a_resolvable_forward_notifies_the_source_admins(
    wired: tuple[Dispatcher, Bot, _Telegram],
    origin: dict[str, Any],
) -> None:
    dispatcher, bot, telegram = wired
    await dispatcher.feed_update(bot, _dm_update("/report мат", origin=origin))

    body = next(row["text"] for row in telegram.sent if row["chat_id"] == ADMIN_A)
    assert telegram.chat_title in body
    assert "мат" in body
    # The copy the admins get is the forward sitting in the reporter's DM.
    assert telegram.forwarded[0] == {
        "chat_id": ADMIN_A,
        "from_chat_id": DM_CHAT_ID,
        "message_id": 7,
    }
    assert telegram.sent[-1] == {"chat_id": DM_CHAT_ID, "text": t("report_sent", "ru", count=2)}


async def test_dm_report_when_the_bot_cannot_see_the_chat(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    telegram.chat_error = _bad_request("chat not found")
    await dispatcher.feed_update(bot, _dm_update("/report", origin=_ORIGIN_CHAT))
    assert [row["text"] for row in telegram.sent] == [t("report_bot_not_in_group", "ru")]


async def test_dm_report_from_a_non_member_is_refused(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    """Legacy let anyone holding a forward DM-blast that chat's staff."""
    dispatcher, bot, telegram = wired
    telegram.member = ChatMemberLeft(
        user=TelegramUser(id=REPORTER_ID, is_bot=False, first_name="R")
    )
    await dispatcher.feed_update(bot, _dm_update("/report", origin=_ORIGIN_CHAT))
    assert [row["text"] for row in telegram.sent] == [t("h_report_not_member", "ru")]


async def test_dm_report_fails_closed_when_membership_cannot_be_established(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    dispatcher, bot, telegram = wired
    telegram.member_error = _bad_request("user not found")
    await dispatcher.feed_update(bot, _dm_update("/report", origin=_ORIGIN_CHAT))
    assert [row["text"] for row in telegram.sent] == [t("h_report_not_member", "ru")]


async def test_a_bare_forward_is_not_claimed(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    """Only an explicit ``/report`` claims a DM.

    The router must leave an ordinary forward alone: a user in a «Войти
    в Ком» AI session forwards things to the bot to ask about them, and
    legacy's auto-claim would swallow those.
    """
    dispatcher, bot, telegram = wired
    update = Update.model_validate(
        {
            "update_id": 2,
            "message": {
                "message_id": 30,
                "date": 1_700_000_000,
                "chat": {"id": DM_CHAT_ID, "type": "private"},
                "from": {"id": REPORTER_ID, "is_bot": False, "first_name": "R"},
                "text": "look at this",
                "forward_origin": _ORIGIN_CHAT,
            },
        }
    )
    assert await dispatcher.feed_update(bot, update) is UNHANDLED
    assert telegram.sent == []


async def test_the_group_half_never_sees_a_private_report(
    wired: tuple[Dispatcher, Bot, _Telegram],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two registrations are chat-type disjoint by construction."""
    dispatcher, bot, telegram = wired
    called = MagicMock()
    monkeypatch.setattr(report_module, "handle_group_report", called)
    await dispatcher.feed_update(
        bot, make_message_update("/report", chat_id=DM_CHAT_ID, user_id=REPORTER_ID)
    )
    assert called.call_count == 0
    assert [row["text"] for row in telegram.sent] == [t("h_report_dm_usage", "ru")]


# --------------------------------------------------------------------
# #220 — the write transaction must end before the fan-out
# --------------------------------------------------------------------


async def test_report_ends_the_write_transaction_before_talking_to_telegram(
    wired: tuple[Dispatcher, Bot, _Telegram],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``touch`` opens ``users.db``; the fan-out must not hold it open.

    In prod the update's ``users.db`` transaction is opened by
    ``UserService.touch`` and, because the engine uses ``BEGIN
    IMMEDIATE``, that DB has exactly one possible writer until the
    session middleware commits. ``/report`` is the worst offender in the
    tree: ``getChatAdministrators``, then a forward *and* a message per
    admin, all of it after the touch. Every other update wanting
    ``users.db`` — ``/start`` included — would wait out ``busy_timeout``
    (5 s) and fail with ``database is locked``, and since the webhook
    still answers 200, Telegram never redelivers it.

    Ordering, not merely presence: a checkpoint awaited *after* the
    fan-out would satisfy an existence check and fix nothing.
    """
    dispatcher, bot, telegram = wired
    order: list[str] = []

    async def _checkpoint() -> None:
        order.append("checkpoint")

    async def _recording(bot_: Bot, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        order.append(type(method).__name__)
        return await telegram(bot_, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", _recording)
    dispatcher["checkpoint"] = _checkpoint

    result = await dispatcher.feed_update(bot, _group_update("/report"))

    assert result is not UNHANDLED
    assert order[0] == "checkpoint", order
    # And the fan-out really did happen after it — otherwise the
    # assertion above would hold for a handler that answered nothing.
    assert "GetChatAdministrators" in order


async def test_report_refusals_also_release_the_lock_first(
    wired: tuple[Dispatcher, Bot, _Telegram],
) -> None:
    """Even the one-line refusals reply over the network (#220).

    The checkpoint sits above every early return for that reason: a
    reporter who forgot to reply still costs one ``sendMessage``, and
    there is no reason for ``users.db`` to be locked for the duration.
    """
    dispatcher, bot, telegram = wired
    order: list[str] = []

    async def _checkpoint() -> None:
        order.append("checkpoint")

    dispatcher["checkpoint"] = _checkpoint

    await dispatcher.feed_update(bot, _group_update("/report", reply_to=None))

    assert order == ["checkpoint"]
    assert telegram.sent[0]["text"] == t("h_report_reply_required", "ru")
