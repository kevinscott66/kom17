"""End-to-end handler tests for the support module.

Covers Stage-14 (``/feedback``, ``/faq``, ``/check``) and
T-022 full ticket lifecycle (``/support`` FSM flow, ``/my_tickets``,
``/admin_tickets``, ``/ticket_reply``, ``/ticket_close``).

The repo is integration-tested separately. Here we verify the
dispatcher contract: routes match, ticket is written, admin
notification fires (or is skipped when ``ADMIN_CHAT_ID=0``), failures
keep the bot up.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25. The ``ADMIN_CHAT_ID=0`` variant passes a tailored
``BotConfig`` to ``make_wired`` instead of the previous indirect
``request.param`` plumbing.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr
from sqlalchemy import select

from telegram_invite_bot.config.settings import BotConfig, HelpConfig
from telegram_invite_bot.core.ranks import rank_name
from telegram_invite_bot.db.models.base import ModerationBase, UsersBase
from telegram_invite_bot.db.models.support import SupportTicket
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.fsm.support import SupportStates
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import FaqContinue
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD
from telegram_invite_bot.services.rank_service import clear_rank_caches
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    assert_only_the_stale_tail_answered,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.fixture(autouse=True)
def _isolate_rank_caches() -> None:
    """The rank system caches overrides and per-user ranks at module
    level for 300s, so a file that never clears them inherits whatever
    the previously-run file left behind — and the three ticket commands
    below are gated on rank, which made their outcome depend on test
    ORDER. Same autouse guard the other rank-touching e2e files carry.
    """
    clear_rank_caches()


async def _set_rank(registry: EngineRegistry, user_id: int, rank: int) -> None:
    async with session_for(registry, DBName.USERS) as session:
        await UsersRepo(session).set_rank(user_id, rank)
    clear_rank_caches()


def _admin_bot_config(admin_chat_id: int = 999_888) -> BotConfig:
    return BotConfig(BOT_TOKEN=SecretStr("123:abc"), ADMIN_CHAT_ID=admin_chat_id)


def _update(
    text: str,
    *,
    chat_type: str = "private",
    message_id: int = 1,
    update_id: int = 1,
    language_code: str | None = None,
    user_id: int = 555,
) -> Update:
    """File-local defaults: user 555 named ``Tester`` with username
    ``tester``. Delegates to the shared builder.

    ``message_id`` and ``update_id`` are exposed so multi-step FSM
    tests can simulate sequential updates without confusing aiogram's
    deduplication logic. ``language_code`` drives ``LanguageMiddleware``
    for tests that assert on the English rendering — and ``user_id``
    goes with it, because that middleware memoises the resolved
    language per user, so an RU-then-EN pair from the SAME id would
    serve the Russian answer twice.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Tester",
        username="tester",
        message_id=message_id,
        update_id=update_id,
        language_code=language_code,
    )


async def test_feedback_saves_row_and_notifies_admin(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/feedback Идея: добавить /coffee"))
    assert result is not UNHANDLED

    # User got the saved reply + admin got their notification.
    chat_ids = [m["chat_id"] for m in sent]
    assert 555 in chat_ids
    assert 999_888 in chat_ids
    user_reply = next(m for m in sent if m["chat_id"] == 555)
    admin_dm = next(m for m in sent if m["chat_id"] == 999_888)
    assert "Отзыв сохранён" in user_reply["text"]
    assert "Новый отзыв" in admin_dm["text"]
    assert "Идея: добавить /coffee" in admin_dm["text"]

    # Row is on disk.
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == 555
    assert rows[0].status == "open"
    assert rows[0].text == "Идея: добавить /coffee"


async def test_feedback_empty_body_warns_and_writes_nothing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/feedback"))
    assert result is not UNHANDLED
    assert "Напиши текст отзыва" in sent[0]["text"]

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert rows == []


async def test_feedback_too_long_warns(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    # 2001 chars — exactly one over the legacy cap.
    payload = "x" * 2001
    result = await dispatcher.feed_update(bot, _update(f"/feedback {payload}"))
    assert result is not UNHANDLED
    assert "Напиши текст отзыва" in sent[0]["text"]


async def test_feedback_no_admin_chat_skips_notify(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``ADMIN_CHAT_ID=0`` ⇒ ticket saved, admin DM silently skipped."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/feedback hi"))
    chat_ids = [m["chat_id"] for m in sent]
    assert chat_ids == [555]  # only the user reply, no admin DM
    assert "Отзыв сохранён" in sent[0]["text"]


async def test_feedback_admin_dm_failure_does_not_break_user_flow(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the admin DM raises, the user still sees "saved" and the row persists."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent: list[dict[str, Any]] = []

    from aiogram.exceptions import TelegramAPIError

    async def fake(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "SendMessage":
            if method.chat_id == 999_888:
                raise TelegramAPIError(method=method, message="user blocked bot")
            sent.append({"chat_id": method.chat_id, "text": method.text})
            return Message(
                message_id=1,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError("unexpected")

    monkeypatch.setattr(bot.session, "make_request", fake)

    result = await dispatcher.feed_update(bot, _update("/feedback please help"))
    assert result is not UNHANDLED
    assert "Отзыв сохранён" in sent[0]["text"]

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert len(rows) == 1


async def test_feedback_repo_failure_replies_with_save_failed(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """If the repo INSERT raises, the user sees the failure copy and no row
    is written.

    The handler wraps ``create_open_ticket`` in a broad ``except`` and
    relies on :class:`SessionMiddleware` rolling the transaction back —
    a contract we had no test for. Without coverage, a future refactor
    that removes the try/except (or swaps the order of the reply +
    rollback) could silently tell the user "saved" while losing the
    ticket. The admin-DM failure variant already lives just above; this
    one closes the matching gap on the *primary* write path.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    from telegram_invite_bot.repositories.support_tickets_repo import (
        SupportTicketsRepo,
    )

    async def boom(self: SupportTicketsRepo, **_: Any) -> int:
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(SupportTicketsRepo, "create_open_ticket", boom)

    result = await dispatcher.feed_update(bot, _update("/feedback hi there"))
    assert result is not UNHANDLED

    # User sees the failure message and ONLY the failure message — no
    # admin DM is attempted because the handler returns early.
    assert [m["chat_id"] for m in sent] == [555]
    assert "Не удалось сохранить" in sent[0]["text"]

    # SessionMiddleware rollback must have prevented any persisted row.
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert rows == []


async def test_feedback_without_from_user_is_silent(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Channel posts (no ``from_user``) must NOT crash or write a ticket.

    Legacy crashes on ``.from_user.id`` access in this path. The new
    router gates on ``F.from_user`` so channel posts never reach the
    handler — they fall through unhandled, no DB write, no reply.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    # ``message.from`` omitted → ``message.from_user is None`` in aiogram.
    channel_post = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": 555, "type": "private"},
                "sender_chat": {"id": -100123, "type": "channel", "title": "C"},
                "text": "/feedback automated report",
            },
        }
    )
    result = await dispatcher.feed_update(bot, channel_post)
    # ``F.from_user`` filter rejects the update at routing time: no
    # handler runs, no DB write, no reply. The update falls through
    # to the strangler bridge unhandled.
    assert result is UNHANDLED
    assert sent == []

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert rows == []


async def test_faq_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/faq"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    # The real part-1 card — title, table of contents and the first four
    # section headers. The old six-bullet stub had none of them, so
    # these assertions are the regression pin.
    assert "ЧАСТО ЗАДАВАЕМЫЕ ВОПРОСЫ" in body
    assert "Оглавление:" in body
    for header in ("НАЧАЛО", "МОНЕТЫ", "ПОПОЛНЕНИЕ", "ВЫВОД"):
        assert header in body
    # Part 2's sections must NOT be here — that's the whole point of
    # paging, and a single message can't hold both halves anyway.
    assert "ИГРЫ" not in body


async def test_faq_part1_answers_what_a_command_list_cannot(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-041 REGRESSION PIN.

    Part 1 used to be a command catalogue in prose ("/balance —
    баланс, /daily — бонус…"). That answer now lives on the generated
    site behind the button, and the card spends its 4096 characters on
    the questions a list structurally cannot answer: why the coins
    stopped (the daily cap), what a coin is worth, and why a withdrawal
    is refused.

    The numbers are asserted, not just the topics — they mirror
    ``settings.py`` defaults, and a card quoting a stale limit to a
    paying user is worse than no card. If a default moves, this test
    fails and points at the copy that has to move with it.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/faq"))
    body = sent[0]["text"]
    assert "150 DLAB" in body  # message_reward_daily_cap
    assert "900 DLAB = 1 USDT" in body  # rates.COINS_PER_USD
    assert "4 500" in body and "90 000" in body  # withdraw min / max
    assert "10 000 DLAB в сутки" in body  # daily_limit_coins
    assert "100 000 DLAB в месяц" in body  # monthly_limit_coins
    # The deposit gate + lifetime-payout cap, in words a user can act
    # on. This is the single most common support ticket the FAQ exists
    # to prevent, so it is pinned by meaning rather than by phrasing.
    assert "не больше, чем суммарно внесено" in body


async def test_faq_part1_fits_in_one_telegram_message(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Telegram rejects a message body over 4096 characters.

    Part 1 is the longest static card in the bot and grows every time
    someone documents a new command, so the ceiling is pinned here
    rather than discovered in production as a silent send failure.
    Entity markup is stripped before parsing, so the visible text is
    what counts — measured that way for both languages.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/faq"))
    # Distinct sender for the EN pass: LanguageMiddleware memoises the
    # resolved language per user id, so reusing 555 here would measure
    # the Russian card twice and leave the English one unpinned.
    await dispatcher.feed_update(
        bot,
        _update("/faq", user_id=4243, language_code="en", message_id=2, update_id=2),
    )
    assert len(sent) == 2
    for message in sent:
        visible = re.sub(r"<[^>]+>", "", message["text"])
        assert len(visible) <= 4096, f"FAQ part 1 is {len(visible)} chars"


async def test_faq_renders_english_without_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The stub this replaced was RU-only, so an English user asking
    /faq got a wall of Russian. Assert the whole card — body *and*
    button label — is Cyrillic-free.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/faq", language_code="en"))

    body = sent[0]["text"]
    assert "FREQUENTLY ASKED QUESTIONS" in body
    assert "GETTING STARTED" in body
    label = sent[0]["markup"].inline_keyboard[0][0].text
    assert not re.search(r"[А-Яа-яЁё]", body + label)


async def test_faq_renders_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/faq worked in groups in legacy (bot.py:35415 — "Один ответ в
    группе"). Unlike the rest of the support router (private-only), it
    must still respond in a group now that the legacy bridge is gone —
    otherwise it would silently no-op.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/faq", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert "ЧАСТО ЗАДАВАЕМЫЕ ВОПРОСЫ" in sent[0]["text"]


async def test_feedback_in_group_stays_private_only(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Counterpart to /faq: the ticket flow stays out of groups (#123).

    ``/faq`` is deliberately answerable anywhere; ``/feedback`` — which
    opens a support ticket carrying whatever the user typed — is not,
    and the private child router still keeps it out. Since #123 the
    group side gets the refusal twin (with the DM deep link) rather
    than silence, so this asserts the refusal *instead of* the ticket
    prompt: anything else here would mean the flow leaked into a group.

    This used to be two tests feeding the identical update — one
    asserting ``UNHANDLED``, one asserting an empty sink. They are one
    scenario and are now one test.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/feedback hi", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="feedback")


async def test_feedback_replies_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """All three ``/feedback`` replies — the usage warning, the ack and
    (below) the save-failed line — were Russian literals. The usage and
    ack branches are separate returns, so both are exercised here.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/feedback", language_code="en", user_id=557))
    usage = sent[0]["text"]
    assert "Type the feedback text after the command" in usage
    # The cap is interpolated, not baked into the copy.
    assert "2000" in usage
    assert not any("Ѐ" <= ch <= "ӿ" for ch in usage), usage

    sent.clear()
    await dispatcher.feed_update(
        bot,
        _update(
            "/feedback the bot ate my coins",
            language_code="en",
            user_id=557,
            message_id=2,
            update_id=2,
        ),
    )
    ack = sent[0]["text"]
    assert "Feedback saved" in ack
    assert not any("Ѐ" <= ch <= "ӿ" for ch in ack), ack


async def test_feedback_save_failure_is_reported_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The save-failed line is the one a user sees when everything else
    has already gone wrong — an unreadable apology is the worst place to
    leak the wrong locale."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    from telegram_invite_bot.repositories.support_tickets_repo import (
        SupportTicketsRepo,
    )

    async def boom(self: SupportTicketsRepo, **_: Any) -> int:
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(SupportTicketsRepo, "create_open_ticket", boom)

    await dispatcher.feed_update(
        bot, _update("/feedback anything", language_code="en", user_id=558)
    )
    body = sent[0]["text"]
    assert "Could not save the feedback" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


# NOTE (#26): ``/check`` is now owned by handlers/checks.py (real
# claim/create flow over CheckService), not the static support stub.
# The former ``test_check_renders`` lived here; it was removed with the
# stub. Coverage moved to the checks repo/service/handler tests.


# ── Stage 23: /faq inline-button + FaqContinue callback ─────────────────


async def test_faq_renders_with_continue_button(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The /faq reply now carries a one-button inline keyboard whose
    callback_data packs into the ``FaqContinue`` prefix. Pinning the
    presence of the markup (not just the body) is the contract the
    Stage 23 callback router stands on — if a future edit drops the
    keyboard, the part-2 callback becomes unreachable.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/faq"))
    assert result is not UNHANDLED

    assert sent[0]["kind"] == "text"
    assert "ЧАСТО ЗАДАВАЕМЫЕ ВОПРОСЫ" in sent[0]["text"]
    markup = sent[0]["markup"]
    assert markup is not None
    rows = markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1
    btn = rows[0][0]
    # Wire format owned by FaqContinue (prefix ``faq_cont``).
    assert btn.callback_data == FaqContinue().pack()
    assert btn.callback_data.startswith("faq_cont")


async def test_faq_part1_carries_the_guide_button_when_configured(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-041: the command-list link belongs on page ONE.

    Part 1 no longer enumerates commands — it points at the generated
    site instead. Shipping that link only under part 2 would mean a
    reader looking for "which command does X" has to page through an
    answer they didn't ask for to find the door.

    Both languages are exercised with deliberately different URLs, so a
    copy-paste bug that always reads the RU field fails here instead of
    passing by coincidence.
    """
    help_config = HelpConfig(
        TELEGRAPH_COMMANDS_URL="https://example.test/ru/commands",
        TELEGRAPH_COMMANDS_URL_EN="https://example.test/en/commands",
    )
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
        help_config=help_config,
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/faq"))
    # Distinct sender: LanguageMiddleware memoises the resolved language
    # per user id, so reusing the default user would serve the RU card
    # to the EN request and the per-language assertion would pass for
    # the wrong reason.
    await dispatcher.feed_update(
        bot,
        _update("/faq", user_id=4242, language_code="en", message_id=2, update_id=2),
    )

    ru_rows = sent[0]["markup"].inline_keyboard
    assert len(ru_rows) == 2, "continue button first, guide link under it"
    assert ru_rows[0][0].callback_data == FaqContinue().pack()
    assert ru_rows[1][0].url == "https://example.test/ru/commands"
    assert ru_rows[1][0].callback_data is None, "guide button must be a URL button"

    en_button = sent[1]["markup"].inline_keyboard[1][0]
    assert en_button.url == "https://example.test/en/commands"
    assert not re.search(r"[А-Яа-яЁё]", en_button.text)


async def test_faq_part1_keeps_continue_button_without_a_guide_url(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Unconfigured deployment → continue button only, never a dead link.

    Telegram rejects a URL button with an empty URL, so "no URL
    configured" has to mean "no second row" — and crucially it must NOT
    take the continue button down with it, or part 2 becomes
    unreachable on every deployment that hasn't set the guide URL.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/faq"))

    rows = sent[0]["markup"].inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].callback_data == FaqContinue().pack()


async def test_faq_continue_callback_edits_to_part2(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Click → AnswerCallbackQuery + EditMessageText with the part-2
    body. Verifies the FaqContinue.filter() registration actually
    matches and the handler reaches its edit branch.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        make_callback_update(FaqContinue().pack(), user_id=555),
    )
    assert result is not UNHANDLED

    kinds = [m["kind"] for m in sent]
    assert "callback_answer" in kinds
    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits, "expected an EditMessageText for the part-2 body"
    body = edits[0]["text"]
    # Stage 22's h_faq_part2 RU body — language defaults to RU when
    # the callback's from_user has no language_code header.
    assert "ИГРЫ" in body
    assert "ПОДДЕРЖКА" in body


async def test_faq_continue_callback_honours_language_code(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``language_code=en`` on the clicker → English part-2 body.
    Pinned because the callback's user can legitimately differ from
    the asker (legacy posture); the wire carries no language and the
    render must come from ``callback.from_user.language_code``.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            FaqContinue().pack(),
            user_id=555,
            language_code="en",
        ),
    )
    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    body = edits[0]["text"]
    assert "GAMES" in body
    # And NOT the Russian header — would mean lang fell back wrongly.
    assert "ИГРЫ" not in body


async def test_faq_continue_callback_carries_guide_button(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-6 #70: the edited part-2 card must carry the same "all
    commands" URL button that ``/faq2`` renders.

    The two entry points reach part 2 by different code paths (edit
    vs send), and the port originally shipped the link on neither —
    so a user who paged through /faq never learned the full command
    guide exists. Pinned on the edit path specifically because it is
    the one most users take.
    """
    help_config = HelpConfig(
        TELEGRAPH_COMMANDS_URL="https://example.test/ru/commands",
        TELEGRAPH_COMMANDS_URL_EN="https://example.test/en/commands",
    )
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
        help_config=help_config,
    )
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(FaqContinue().pack(), user_id=555),
    )

    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    button = edits[0]["markup"].inline_keyboard[0][0]
    assert button.url == "https://example.test/ru/commands"


async def test_faq_continue_callback_has_no_guide_button_without_url(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No guide URL configured → the part-2 edit drops the keyboard
    entirely rather than leaving the stale "continue" button behind.

    Leaving it would be worse than no button at all: a second tap
    would re-render part 2 over itself and Telegram answers a
    no-op edit with "message is not modified".
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(FaqContinue().pack(), user_id=555),
    )

    edits = [m for m in sent if m["kind"] == "edit"]
    assert edits
    assert edits[0]["markup"] is None


async def test_no_support_router_claims_a_prefix_it_does_not_own(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """REGRESSION PIN: filters here must stay as narrow as they read.

    A router that widens its filter — ``F.data`` with no prefix, a
    ``startswith`` rewrite — starts claiming payloads belonging to
    other features, and the tap is served by the wrong card rather
    than the right one. Feeding a payload no router in this file owns
    is the cheapest way to notice.

    Written against the strangler bridge, whose callback half needed
    ``UNHANDLED`` so it could forward the tap to legacy. Legacy is
    gone; #159 ends the tree with a tail that acknowledges anything
    left over, so an unclaimed payload is no longer silent. What it
    must still be is unclaimed — exactly one stale-card toast and no
    card of any kind.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update("shop_buy_42", user_id=555),
    )
    assert_only_the_stale_tail_answered(sent, "an unowned prefix must not be claimed")


# ── T-022: /support FSM flow ─────────────────────────────────────────────────


def _dev_bot_config(developer_id: int = 555, admin_chat_id: int = 999_888) -> BotConfig:
    """BotConfig where ``developer_id`` is a recognised developer.

    Uses the ``DEVELOPER_ID_1`` slot so ``is_developer(developer_id)``
    returns True. The admin_chat_id mirrors ``_admin_bot_config``.
    """
    return BotConfig(
        BOT_TOKEN=SecretStr("123:abc"),
        ADMIN_CHAT_ID=admin_chat_id,
        DEVELOPER_ID_1=developer_id,
    )


async def test_support_no_args_starts_fsm(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/support with no args enters FSM state and prompts the user."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/support"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Опиши проблему" in sent[0]["text"]
    assert sent[0]["chat_id"] == 555


async def test_support_fsm_text_saves_ticket(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/support (no args) then a free-text message saves the ticket.

    Two dispatcher feed calls simulate two sequential updates from the
    same user. After the second call a row must be in the DB and the
    user sees the "saved" reply.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(admin_chat_id=0),  # skip admin DM for simplicity
    )
    sent = capture_outgoing(bot)

    # First update: enter FSM
    await dispatcher.feed_update(bot, _update("/support"))
    assert len(sent) == 1  # prompt

    # Second update: free-text while in awaiting_text
    await dispatcher.feed_update(bot, _update("Мой вопрос о боте", message_id=2, update_id=2))
    assert len(sent) == 2
    assert "Обращение #" in sent[1]["text"]
    assert "сохранено" in sent[1]["text"]

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id == 555
    assert rows[0].text == "Мой вопрос о боте"


async def test_support_fsm_notifies_admin(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """After FSM text is received the admin should get a notification DM."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(admin_chat_id=999_888),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/support"))
    await dispatcher.feed_update(bot, _update("Вопрос от пользователя", message_id=2, update_id=2))

    chat_ids = [m["chat_id"] for m in sent]
    assert 999_888 in chat_ids
    admin_dm = next(m for m in sent if m["chat_id"] == 999_888)
    assert "Новый отзыв" in admin_dm["text"]


async def test_support_inline_arg_saves_immediately(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/support <text> (with inline arg) saves immediately without FSM."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/support Inline text here"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Обращение #" in sent[0]["text"]

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert len(rows) == 1
    assert rows[0].text == "Inline text here"


class _OtherFlowStates(StatesGroup):
    """Stand-in for whatever interview the user is already inside.

    Deliberately synthetic: the contract is "any state that is not
    ``SupportStates.awaiting_text``", not "the withdraw flow in
    particular". A real flow's states would tie this test to that
    flow's registration order.
    """

    waiting_for_amount = State()


def _fsm_key(*, chat_id: int, user_id: int, bot_id: int) -> StorageKey:
    """The key shape aiogram's storage middleware builds for a private
    message — tests must match it or they seed a state nobody reads."""
    return StorageKey(bot_id=bot_id, chat_id=chat_id, user_id=user_id)


async def test_support_runs_from_inside_another_flow(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/support`` mid-interview explains the way out (#165).

    The escape hatch used to carry a router-level ``StateFilter(None)``,
    so the one moment a user needs it most — stuck inside some other
    interview — was the one moment it did not run: the update fell
    through to ``unknown_form``, which told them they had used the wrong
    *argument form* of a command they had typed perfectly.

    The reply must NOT be the ticket prompt: the user's next message
    still belongs to the other flow, so promising "describe your
    problem" would send their text somewhere else entirely. And the
    other flow's state has to survive untouched — an escape hatch that
    silently drops the interview is its own bug.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(admin_chat_id=0),
    )
    sent = capture_outgoing(bot)
    ctx = FSMContext(
        storage=dispatcher.fsm.storage,
        key=_fsm_key(chat_id=555, user_id=555, bot_id=bot.id if bot.id else 0),
    )
    await ctx.set_state(_OtherFlowStates.waiting_for_amount)
    await ctx.update_data(pending_amount=1234)

    result = await dispatcher.feed_update(bot, _update("/support"))

    assert result is not UNHANDLED
    assert sent[0]["text"] == t("h_support_busy", "ru")
    assert await ctx.get_state() == _OtherFlowStates.waiting_for_amount.state
    assert await ctx.get_data() == {"pending_amount": 1234}


async def test_support_with_text_files_from_inside_another_flow(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/support <текст>`` files the ticket from anywhere (#165).

    The inline-arg form never touches the FSM, so there is nothing to
    conflict with — and it is the form the busy reply above points at,
    which makes this the promise that reply has to keep.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(admin_chat_id=0),
    )
    sent = capture_outgoing(bot)
    ctx = FSMContext(
        storage=dispatcher.fsm.storage,
        key=_fsm_key(chat_id=555, user_id=555, bot_id=bot.id if bot.id else 0),
    )
    await ctx.set_state(_OtherFlowStates.waiting_for_amount)

    result = await dispatcher.feed_update(bot, _update("/support Застрял в выводе"))

    assert result is not UNHANDLED
    assert "Обращение #" in sent[0]["text"]
    assert await ctx.get_state() == _OtherFlowStates.waiting_for_amount.state

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert [r.text for r in rows] == ["Застрял в выводе"]


async def test_support_twice_re_sends_the_prompt(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Second ``/support`` while already waiting repeats the prompt.

    Dropping the router gate in #165 put this branch back in reach, so
    it is now live code rather than the unreachable leftover it had
    been — the user who forgot what they were asked gets asked again,
    not told they are "in another dialog" about the dialog they are in.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    ctx = FSMContext(
        storage=dispatcher.fsm.storage,
        key=_fsm_key(chat_id=555, user_id=555, bot_id=bot.id if bot.id else 0),
    )

    await dispatcher.feed_update(bot, _update("/support"))
    # Backdate the sweeper stamp to a moment the sweeper would reap, then
    # re-state intent: the repeat must move the clock, or the user gets
    # expired on the *first* /support's timer while visibly still here.
    await ctx.update_data({STATE_ENTERED_AT_FIELD: "2000-01-01T00:00:00+00:00"})
    await dispatcher.feed_update(bot, _update("/support", message_id=2, update_id=2))

    assert [m["text"] for m in sent] == [t("h_support_prompt", "ru")] * 2
    assert await ctx.get_state() == SupportStates.awaiting_text.state
    assert (await ctx.get_data())[STATE_ENTERED_AT_FIELD] > "2001"


async def test_support_fsm_cancel_clears_state(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/cancel while in awaiting_text should clear FSM state.

    The global /cancel handler (handlers/cancel.py) calls state.clear()
    unconditionally. After /cancel a subsequent /support must re-enter
    the FSM (prompt shown) not see "already in state" or crash.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    # Enter FSM
    await dispatcher.feed_update(bot, _update("/support"))
    assert len(sent) == 1

    # Cancel
    await dispatcher.feed_update(bot, _update("/cancel", message_id=2, update_id=2))
    # After cancel we should get a reply from the cancel handler.

    # A subsequent /support should show the prompt again (FSM was cleared)
    await dispatcher.feed_update(bot, _update("/support", message_id=3, update_id=3))
    prompt_msgs = [m for m in sent if "Опиши проблему" in m.get("text", "")]
    assert len(prompt_msgs) == 2  # first /support + second /support after cancel

    # No ticket should have been created (user never sent the text).
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert rows == []


# ── T-022: /my_tickets ───────────────────────────────────────────────────────


async def test_my_tickets_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/my_tickets with no existing tickets returns the empty message."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/my_tickets"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "нет обращений" in sent[0]["text"].lower()


async def test_my_tickets_with_rows(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/my_tickets lists the user's tickets with id, status, preview."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_admin_bot_config(admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    # Create a ticket for user 555 via /feedback so we don't need FSM.
    await dispatcher.feed_update(bot, _update("/feedback Первый вопрос"))
    assert len(sent) == 1  # saved reply

    sent.clear()
    result = await dispatcher.feed_update(bot, _update("/my_tickets", message_id=2, update_id=2))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "#" in body  # ticket id
    assert "open" in body
    assert "Первый вопрос" in body


# ── T-022: /admin_tickets ────────────────────────────────────────────────────


async def test_admin_tickets_non_admin_denied(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A non-developer is refused — and refused by the RANK GATE, before
    the handler runs.

    The three ticket commands carry ``default_rank=5`` in the catalog
    (#114), so ``CommandAccessMiddleware`` answers first and the
    handler's own ``is_developer`` check never sees the update. Both
    layers are wanted: the gate is configurable per deployment through
    ``/cmdcfg``, the handler check is not — see
    ``test_admin_tickets_denies_a_rank5_non_developer`` for the case
    where the gate passes and the handler still refuses.

    ``ModerationBase`` is materialised on purpose: the middleware fails
    OPEN when the overrides table is missing, so without the schema this
    test would assert whichever layer happened to answer.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, ModerationBase],
        session_middleware=True,
        # user 555 is NOT a developer here — admin_chat_id=999_888 would
        # map 999_888 as developer, but user 555 won't match.
        bot_config=_admin_bot_config(admin_chat_id=999_888),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/admin_tickets"))
    assert result is not UNHANDLED
    assert sent[0]["text"] == t(
        "h_cmdaccess_denied", "ru", rank_name=rank_name(5, "ru", in_group=True)
    )


async def test_admin_tickets_admin_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/admin_tickets for a developer shows open ticket listing."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        # user 555 is developer (DEVELOPER_ID_1=555).
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    # Create a ticket via /feedback so there is something to show.
    await dispatcher.feed_update(bot, _update("/feedback Тестовый тикет"))
    assert len(sent) == 1
    sent.clear()

    result = await dispatcher.feed_update(bot, _update("/admin_tickets", message_id=2, update_id=2))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Открытые обращения" in body
    assert "#" in body


async def test_admin_tickets_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/admin_tickets with no open tickets returns the empty copy."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/admin_tickets"))
    assert result is not UNHANDLED
    assert "нет" in sent[0]["text"].lower()


# ── T-022: /ticket_reply ─────────────────────────────────────────────────────


async def test_ticket_reply_sends_dm_to_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/ticket_reply <id> <text> admin → saves answer + DMs the user.

    The admin (user 555) creates a ticket first (via /feedback as
    another user is not easy here, so we create it via /feedback as
    user 555 and admin also replies as 555 — the important thing is the
    dispatcher wiring, not the semantic correctness of self-reply).
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    # Create ticket as user 555
    await dispatcher.feed_update(bot, _update("/feedback Хочу помощи"))
    assert len(sent) == 1
    sent.clear()

    # Find ticket id
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert len(rows) == 1
    tid = rows[0].id

    # Admin (also user 555 in this test) replies
    result = await dispatcher.feed_update(
        bot,
        _update(f"/ticket_reply {tid} Вот ваш ответ", message_id=2, update_id=2),
    )
    assert result is not UNHANDLED
    # Two sends: user DM + admin confirmation
    assert len(sent) == 2
    user_dm = next(m for m in sent if "Ответ на твоё обращение" in m.get("text", ""))
    assert "Вот ваш ответ" in user_dm["text"]
    admin_ack = next(m for m in sent if "Ответ на #" in m.get("text", ""))
    assert str(tid) in admin_ack["text"]

    # Verify DB row updated
    async with sessionmaker() as session:
        row = await session.get(SupportTicket, tid)
    assert row is not None
    assert row.status == "answered"
    assert row.answer == "Вот ваш ответ"


async def test_ticket_reply_non_admin_denied(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, ModerationBase],
        session_middleware=True,
        bot_config=_admin_bot_config(admin_chat_id=999_888),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/ticket_reply 1 hello"))
    assert result is not UNHANDLED
    assert sent[0]["text"] == t(
        "h_cmdaccess_denied", "ru", rank_name=rank_name(5, "ru", in_group=True)
    )


async def test_ticket_reply_not_found(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/ticket_reply 99999 text"))
    assert result is not UNHANDLED
    assert "не найдено" in sent[0]["text"].lower()


async def test_ticket_reply_refuses_a_second_answer(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#565: the second ``/ticket_reply`` on the same id must be refused.

    The row holds a single ``answer`` column and nothing archives the
    previous one, so overwriting loses the first answer, its author and
    its timestamp irrecoverably. Legacy guarded the write
    (bot.py:35600-35604) but discarded the ``False`` and reported success
    anyway (bot.py:35844); here the admin is told, and no DM goes out.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/feedback Хочу помощи"))
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        tid = (await session.execute(select(SupportTicket))).scalars().one().id

    await dispatcher.feed_update(
        bot, _update(f"/ticket_reply {tid} первый", message_id=2, update_id=2)
    )
    sent.clear()

    result = await dispatcher.feed_update(
        bot, _update(f"/ticket_reply {tid} второй", message_id=3, update_id=3)
    )

    assert result is not UNHANDLED
    # One send only: the refusal to the admin. No DM to the user.
    assert len(sent) == 1
    assert sent[0]["text"] == t("h_ticket_reply_not_open", "ru", id=tid, status="answered")
    async with sessionmaker() as session:
        row = await session.get(SupportTicket, tid)
    assert row is not None
    assert row.answer == "первый"


async def test_ticket_reply_dms_user_in_their_own_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#564: the DM is addressed to the ticket author, not to the admin.

    ``lang`` in the handler is the developer's language — every string
    there except the DM is written to them. Legacy hard-coded Russian
    with no ``t()`` at all (bot.py:35852-35854), so an English-speaking
    user got a Russian answer header.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    # Another user opens the ticket...
    await dispatcher.feed_update(bot, _update("/feedback help me", user_id=777))
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        tid = (await session.execute(select(SupportTicket))).scalars().one().id
        # ...having picked English explicitly via /lang. The override is
        # seeded on top of a Russian client locale on purpose: reading
        # ``users.language_code`` alone would answer "ru" here, which is
        # exactly the shortcut this helper must not take
        # (middlewares/language.py:192-218 resolves it the same way).
        await UsersRepo(session).upsert_from_telegram(
            user_id=777,
            username=None,
            first_name="Recipient",
            last_name=None,
            language_code="ru",
            is_premium=False,
        )
        await UserSettingsRepo(session).set_language(777, "en")
        await session.commit()
    sent.clear()

    # ...and the Russian-speaking developer answers it.
    result = await dispatcher.feed_update(
        bot, _update(f"/ticket_reply {tid} Done", message_id=2, update_id=2)
    )

    assert result is not UNHANDLED
    user_dm = next(m for m in sent if m.get("chat_id") == 777)
    assert user_dm["text"] == t("h_ticket_user_reply_received", "en", id=tid, answer="Done")
    # The admin acknowledgement stays in the admin's own language.
    admin_ack = next(m for m in sent if m.get("chat_id") == 555)
    assert admin_ack["text"] == t("h_ticket_reply_ok", "ru", id=tid)


async def test_ticket_reply_survives_a_failed_admin_acknowledgement(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1872: the stored answer outlives the ack that never arrived.

    ``h_ticket_reply_ok`` is the one call in this handler that sits
    outside both wrappers, and it runs AFTER the user's DM has already
    been delivered. ``BaseSessionMiddleware`` rolls the session back on
    any raise (``middlewares/base.py:126-127``), so before the
    checkpoint a rejected ack un-stored an answer the ticket author had
    already read: the ticket went back to ``open``, ``/my_tickets``
    showed nothing, and because
    :meth:`SupportTicketsRepo.add_admin_reply` accepts only ``open``
    tickets a second ``/ticket_reply`` was free to overwrite it.
    """
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/feedback help me", user_id=777))
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        tid = (await session.execute(select(SupportTicket))).scalars().one().id
    sent.clear()

    # Armed only now: the ``/feedback`` above sends too, and arming
    # earlier would kill the setup instead of the ack under test.
    original = bot.session.make_request

    async def refuse(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, SendMessage) and method.chat_id == 555:
            raise TelegramForbiddenError(method=method, message="Forbidden: blocked")
        return await original(_bot, method, timeout=timeout)

    bot.session.make_request = refuse  # type: ignore[method-assign,assignment]
    with contextlib.suppress(TelegramForbiddenError):
        await dispatcher.feed_update(
            bot, _update(f"/ticket_reply {tid} Done", message_id=2, update_id=2)
        )

    # The DM went out first — that is what makes the rollback lossy.
    assert [m["chat_id"] for m in sent] == [777]
    async with sessionmaker() as session:
        row = await session.get(SupportTicket, tid)
    assert row is not None
    assert row.answer == "Done"
    assert row.status == "answered"


# ── T-022: /ticket_close ─────────────────────────────────────────────────────


async def test_ticket_close_flips_status(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/ticket_close <id> marks the ticket closed in the DB."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=_dev_bot_config(developer_id=555, admin_chat_id=0),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/feedback Закрой меня"))
    sent.clear()

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        rows = (await session.execute(select(SupportTicket))).scalars().all()
    tid = rows[0].id

    result = await dispatcher.feed_update(
        bot, _update(f"/ticket_close {tid}", message_id=2, update_id=2)
    )
    assert result is not UNHANDLED
    assert "закрыто" in sent[0]["text"].lower()

    async with sessionmaker() as session:
        row = await session.get(SupportTicket, tid)
    assert row is not None
    assert row.status == "closed"


async def test_ticket_close_non_admin_denied(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, ModerationBase],
        session_middleware=True,
        bot_config=_admin_bot_config(admin_chat_id=999_888),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/ticket_close 1"))
    assert result is not UNHANDLED
    assert sent[0]["text"] == t(
        "h_cmdaccess_denied", "ru", rank_name=rank_name(5, "ru", in_group=True)
    )


async def test_admin_tickets_denies_a_rank5_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Rank 5 is "owner / group admin" — an ordinary group admin can
    reach it, and the ticket queue holds other users' private support
    messages. The rank gate lets them through; the handler must not.

    This is the half of the pair that the rank gate cannot cover: the
    gate's threshold is operator-configurable via ``/cmdcfg set``, so
    the developer-only check has to live in the handler as well.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, ModerationBase],
        session_middleware=True,
        bot_config=_admin_bot_config(admin_chat_id=999_888),
    )
    await _set_rank(registry, 555, 5)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/admin_tickets"))
    assert result is not UNHANDLED
    assert sent[0]["text"] == t("h_admin_tickets_only", "ru")
