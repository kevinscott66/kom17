"""Global error handler — fires for any handler-raised exception.

The router catches every exception that escapes a feature router,
bumps the ``HANDLER_ERRORS`` metric, logs with full traceback, and
sends the user a generic non-leaking reply.

These tests verify the contract end-to-end with a *real* dispatcher
and a stub router that raises on purpose. We don't mock the error
router — that's the unit under test.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from telegram_invite_bot.handlers.errors import build_errors_router
from telegram_invite_bot.webhook.metrics import HANDLER_ERRORS


def _update_for(text: str, *, user_id: int = 9001, lang: str = "ru") -> Update:
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 100,
                "date": 1_700_000_000,
                "chat": {"id": user_id, "type": "private"},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Bob",
                    "language_code": lang,
                },
                "text": text,
            },
        }
    )


def _build_dispatcher_with_failing_handler(
    fail_with: type[Exception] = RuntimeError,
) -> Dispatcher:
    """Wire a dispatcher whose only feature router always raises.

    The errors router is included AFTER the failing one — same order
    as production. This pins that aiogram's @router.error() does in
    fact fire for an upstream-raised handler, not just for handlers
    in the same router as the error handler.
    """
    feature = Router(name="feature_that_fails")

    @feature.message()
    async def _boom(_message: Any) -> None:
        raise fail_with("synthetic test failure")

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(feature)
    dispatcher.include_router(build_errors_router())
    return dispatcher


def _synth_message(chat_id: int, text: str) -> Any:
    """Stub Telegram response so aiogram's response parser is happy."""
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    return Message(
        message_id=1,
        date=datetime(2024, 1, 1, tzinfo=UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
        text=text,
    )


def _make_bot(
    capture: list[dict[str, Any]],
    *,
    fail_send: bool = False,
    answers: list[dict[str, Any]] | None = None,
) -> Bot:
    """Build a Bot whose session records outgoing SendMessage calls.

    ``fail_send=True`` makes every outbound call raise — used to test
    the "user blocked the bot" path where ``message.answer()`` itself
    raises and the error handler must swallow that quietly.

    ``answers`` opts the caller into recording ``answerCallbackQuery``
    too. Leaving it ``None`` keeps the strict default — an unexpected
    call type still trips the AssertionError — so the message-update
    tests keep proving the error path never answers a callback that
    does not exist.
    """
    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ASYNC109
    ) -> Any:
        if fail_send:
            from aiogram.exceptions import TelegramForbiddenError

            raise TelegramForbiddenError(method=method, message="bot was blocked")
        name = type(method).__name__
        if name == "SendMessage":
            capture.append({"chat_id": method.chat_id, "text": method.text})
            return _synth_message(method.chat_id, method.text)
        if name == "AnswerCallbackQuery" and answers is not None:
            answers.append({"id": method.callback_query_id, "text": method.text})
            return True
        raise AssertionError(f"unexpected Telegram call: {name}")

    bot.session.make_request = fake_make_request  # type: ignore[method-assign,assignment]
    return bot


def _counter_value(exc_type: str) -> float:
    """Read the current ``HANDLER_ERRORS{exc_type=...}`` value, returning
    0.0 if the label combination has never been incremented (label-set
    is lazy in prometheus_client — accessing ``.labels(...)._value`` would
    create the timeseries as a side effect, polluting other tests).
    """
    for metric in HANDLER_ERRORS.collect():
        for sample in metric.samples:
            if sample.labels.get("exc_type") == exc_type and sample.name.endswith("_total"):
                return float(sample.value)
    return 0.0


@pytest.mark.asyncio
async def test_error_handler_bumps_metric_and_replies_to_user() -> None:
    sent: list[dict[str, Any]] = []
    bot = _make_bot(sent)
    dispatcher = _build_dispatcher_with_failing_handler(RuntimeError)

    before = _counter_value("RuntimeError")
    result = await dispatcher.feed_update(bot, _update_for("anything"))
    after = _counter_value("RuntimeError")

    # Metric ticked exactly once for THIS class — alerting can scope
    # to the failure type without firing on unrelated handlers.
    assert after == before + 1.0

    # User got the polite generic reply, not the raw exception text.
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 9001
    assert "synthetic test failure" not in sent[0]["text"]  # CRITICAL — no leak
    assert "ошибка" in sent[0]["text"].lower()

    # Dispatcher saw the error handler return True → no UNHANDLED.
    assert result is not None


@pytest.mark.asyncio
async def test_error_handler_picks_english_for_en_user() -> None:
    """``language_code=en-US`` user gets the English reply. The handler
    falls back to a literal string pair instead of touching the i18n
    YAML — keeps the error path dependency-free so a broken i18n module
    can't bring down the safety net.
    """
    sent: list[dict[str, Any]] = []
    bot = _make_bot(sent)
    dispatcher = _build_dispatcher_with_failing_handler(ValueError)

    await dispatcher.feed_update(bot, _update_for("anything", lang="en-US"))
    assert len(sent) == 1
    assert "wrong" in sent[0]["text"].lower()


def _callback_update(*, lang: str = "ru") -> Update:
    """A button press in a private chat — the shape every spinner test
    needs. ``id`` is what ``answerCallbackQuery`` must echo back.
    """
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-1",
                "from": {
                    "id": 7777,
                    "is_bot": False,
                    "first_name": "Alice",
                    "language_code": lang,
                },
                "chat_instance": "ci-1",
                "data": "noop",
                "message": {
                    "message_id": 555,
                    "date": 1_700_000_000,
                    "chat": {"id": 7777, "type": "private"},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "press a button",
                },
            },
        }
    )


def _dispatcher_cb_raising(exc: Exception) -> Dispatcher:
    """Dispatcher whose only callback handler raises ``exc``."""
    feature = Router(name="cb_feature_that_fails")

    @feature.callback_query()
    async def _boom_cb(_cb: Any) -> None:
        raise exc

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(feature)
    dispatcher.include_router(build_errors_router())
    return dispatcher


@pytest.mark.asyncio
async def test_error_handler_replies_under_callback_query() -> None:
    """Failures during a callback_query (button press) — the error
    handler must locate the originating message via
    ``update.callback_query.message`` and reply THERE, so the user sees
    the failure under the action they just took rather than as a
    detached message later.
    """
    sent: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    bot = _make_bot(sent, answers=answers)
    dispatcher = _dispatcher_cb_raising(RuntimeError("callback boom"))

    await dispatcher.feed_update(bot, _callback_update())

    # Reply landed in the same chat as the original button-bearing
    # message — that's the "answer in context of action" contract.
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 7777
    assert "ошибка" in sent[0]["text"].lower()
    assert "callback boom" not in sent[0]["text"]  # CRITICAL — no leak


@pytest.mark.asyncio
async def test_callback_chat_reply_is_english_for_en_user() -> None:
    """#186: the reply under a failed button press follows the *tapping*
    user's locale, not the card's author.

    On the callback branch the only message the handler holds is the
    bot's own card — ``_callback_update`` gives it
    ``from: {"id": 0, "is_bot": true}`` with no ``language_code``,
    exactly as Telegram delivers it. Reading the language off that
    message resolves to ``None`` → ``ru``, so an English user was
    always told «Произошла ошибка» in the chat even though the toast
    on the button itself was already correctly English.

    Both surfaces are asserted here on purpose: they take separate
    code paths (``_try_reply_to_user`` vs ``_stop_spinner``) and the
    bug was that they disagreed.
    """
    sent: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    bot = _make_bot(sent, answers=answers)
    dispatcher = _dispatcher_cb_raising(RuntimeError("callback boom"))

    await dispatcher.feed_update(bot, _callback_update(lang="en-GB"))

    assert len(sent) == 1
    assert "wrong" in sent[0]["text"].lower()
    assert "ошибка" not in sent[0]["text"].lower()
    assert len(answers) == 1
    assert "wrong" in (answers[0]["text"] or "").lower()


@pytest.mark.asyncio
async def test_callback_chat_reply_stays_russian_for_ru_user() -> None:
    """The other half of #186: the fix must not flip every callback
    reply to English. A ``ru`` tapping user keeps the Russian sentence
    even though the card's author (the bot) carries no locale at all.
    """
    sent: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    bot = _make_bot(sent, answers=answers)
    dispatcher = _dispatcher_cb_raising(RuntimeError("callback boom"))

    await dispatcher.feed_update(bot, _callback_update(lang="ru"))

    assert len(sent) == 1
    assert "ошибка" in sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_error_handler_swallows_unexpected_reply_failure() -> None:
    """Last-resort guard: if ``message.answer()`` raises something OTHER
    than ``TelegramAPIError`` (e.g. a network blow-up inside the SDK
    that surfaces as ``OSError``), the error handler must still NOT
    re-raise. The whole reason this branch exists is so the safety net
    can't itself become a source of crashes.
    """
    sent: list[dict[str, Any]] = []
    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def explode(
        _bot: Any,
        _method: Any,
        timeout: Any = None,  # noqa: ASYNC109
    ) -> Any:
        raise OSError("network gone")

    bot.session.make_request = explode  # type: ignore[method-assign,assignment]
    dispatcher = _build_dispatcher_with_failing_handler(KeyError)

    before = _counter_value("KeyError")
    # Must NOT raise — that's the whole contract for this branch.
    await dispatcher.feed_update(bot, _update_for("anything"))
    after = _counter_value("KeyError")
    assert after == before + 1.0
    assert sent == []


def _dispatcher_raising(exc: Exception) -> Dispatcher:
    """Same wiring as :func:`_build_dispatcher_with_failing_handler`, but
    raising a pre-built instance — ``TelegramBadRequest`` needs a
    ``method``/``message`` pair the class-only helper can't supply.
    """
    feature = Router(name="feature_raising_instance")

    @feature.message()
    async def _boom(_message: Any) -> None:
        raise exc

    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(feature)
    dispatcher.include_router(build_errors_router())
    return dispatcher


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        # Prod, 12.08: re-tapping the rating page you are already on.
        "Bad Request: message is not modified: specified new message content"
        " and reply markup are exactly the same",
        # A card left open past the 48h edit window, or swept away.
        "Bad Request: message to edit not found",
        # A handler that outran Telegram's ~15 s answer window.
        "Bad Request: query is too old and response timeout expired",
    ],
)
async def test_benign_telegram_rejects_never_reach_the_user(message: str) -> None:
    """The bug this whole batch exists for: a stale or unchanged card is
    an ordinary fact about the chat, and reporting it as "⚠️ Произошла
    ошибка" tells the user their tap broke something when it did not.

    Still counted — under its own label so alerts can scope to real
    failures — but no reply and no traceback.
    """
    from aiogram.exceptions import TelegramBadRequest

    sent: list[dict[str, Any]] = []
    bot = _make_bot(sent)
    dispatcher = _dispatcher_raising(TelegramBadRequest(method=None, message=message))  # type: ignore[arg-type]

    before = _counter_value("BenignReject")
    before_real = _counter_value("TelegramBadRequest")
    await dispatcher.feed_update(bot, _update_for("anything"))

    assert sent == []
    assert _counter_value("BenignReject") == before + 1.0
    # And it does NOT pollute the label operators alert on.
    assert _counter_value("TelegramBadRequest") == before_real


@pytest.mark.asyncio
async def test_a_real_bad_request_is_still_reported() -> None:
    """The carve-out is by message, not by class — a malformed card is
    our bug and must keep both the traceback and the user-facing card.
    """
    from aiogram.exceptions import TelegramBadRequest

    sent: list[dict[str, Any]] = []
    bot = _make_bot(sent)
    dispatcher = _dispatcher_raising(
        TelegramBadRequest(method=None, message="Bad Request: can't parse entities")  # type: ignore[arg-type]
    )

    before = _counter_value("TelegramBadRequest")
    await dispatcher.feed_update(bot, _update_for("anything"))

    assert _counter_value("TelegramBadRequest") == before + 1.0
    assert len(sent) == 1
    assert "ошибка" in sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_error_handler_survives_telegram_reply_failure() -> None:
    """If the user-side reply itself fails (user blocked the bot, etc.)
    the error handler must NOT propagate — the metric tick + log line
    are the value we extract from this path. Re-raising would mean an
    aiogram error-handler exception swallows the original cause.
    """
    sent: list[dict[str, Any]] = []
    bot = _make_bot(sent)

    # Rebuild bot with fail_send=True so any outbound call raises a
    # TelegramAPIError — the path the error handler must swallow.
    sent = []
    bot = _make_bot(sent, fail_send=True)
    dispatcher = _build_dispatcher_with_failing_handler(TypeError)

    before = _counter_value("TypeError")
    # Must NOT raise — that's the whole contract.
    await dispatcher.feed_update(bot, _update_for("anything"))
    after = _counter_value("TypeError")
    assert after == before + 1.0
    # Nothing got delivered — the recorder list is empty.
    assert sent == []


@pytest.mark.asyncio
async def test_callback_error_stops_the_spinner_with_a_toast() -> None:
    """#216 — a raised callback handler used to leave the tapped button
    loading until Telegram timed the query out (~15 s), because the only
    thing the error path did was ``message.answer()`` — a *new message*,
    which does nothing for the spinner. Legacy ``bot.py:1305`` answered
    the query first and then sent the reply; parity means both.
    """
    sent: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    bot = _make_bot(sent, answers=answers)
    dispatcher = _dispatcher_cb_raising(RuntimeError("callback boom"))

    await dispatcher.feed_update(bot, _callback_update())

    # The spinner stopped, on THIS query, with a visible explanation.
    assert len(answers) == 1
    assert answers[0]["id"] == "cb-1"
    assert "ошибка" in answers[0]["text"].lower()
    # Telegram hard-caps the popup; the regression guard measures the
    # i18n toasts, this literal one needs its own check.
    assert len(answers[0]["text"]) <= 200
    assert "callback boom" not in answers[0]["text"]  # CRITICAL — no leak
    # …and the chat reply is still delivered, not replaced by the toast.
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_callback_toast_is_english_for_en_user() -> None:
    """Same literal RU/EN pairing as the chat reply — the toast is keyed
    off ``callback_query.from_user`` because the message the button
    hangs on may be the inaccessible variant.
    """
    sent: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    bot = _make_bot(sent, answers=answers)
    dispatcher = _dispatcher_cb_raising(ValueError("boom"))

    await dispatcher.feed_update(bot, _callback_update(lang="en-GB"))

    assert len(answers) == 1
    assert "wrong" in answers[0]["text"].lower()


@pytest.mark.asyncio
async def test_benign_reject_stops_the_spinner_without_a_popup() -> None:
    """The benign branch must stay silent — telling the user "произошла
    ошибка" on a stale card is the bug #45/#159 removed. But silent
    means *no text*, not *no answer*: skipping ``answerCallbackQuery``
    entirely leaves the button spinning on an outcome we deliberately
    classify as fine. ``text=None`` stops the spinner and shows nothing.
    """
    from aiogram.exceptions import TelegramBadRequest

    sent: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    bot = _make_bot(sent, answers=answers)
    dispatcher = _dispatcher_cb_raising(
        TelegramBadRequest(
            method=None,  # type: ignore[arg-type]
            message=(
                "Bad Request: message is not modified: specified new message"
                " content and reply markup are exactly the same"
            ),
        )
    )

    await dispatcher.feed_update(bot, _callback_update())

    assert sent == []  # still no chat message
    assert len(answers) == 1
    assert answers[0]["id"] == "cb-1"
    assert answers[0]["text"] is None  # spinner stopped, popup suppressed


@pytest.mark.asyncio
async def test_spinner_stop_failure_never_blocks_the_chat_reply() -> None:
    """An aged-out query cannot be answered at all — ``query is too old``
    is itself one of the benign markers. That failure must be swallowed
    *and* must not stop the chat reply, which is the only channel left
    for telling the user anything.
    """
    from aiogram.exceptions import TelegramBadRequest

    sent: list[dict[str, Any]] = []
    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ASYNC109
    ) -> Any:
        name = type(method).__name__
        if name == "AnswerCallbackQuery":
            raise TelegramBadRequest(
                method=method,
                message="Bad Request: query is too old and response timeout expired",
            )
        if name == "SendMessage":
            sent.append({"chat_id": method.chat_id, "text": method.text})
            return _synth_message(method.chat_id, method.text)
        raise AssertionError(f"unexpected Telegram call: {name}")

    bot.session.make_request = fake_make_request  # type: ignore[method-assign,assignment]
    dispatcher = _dispatcher_cb_raising(RuntimeError("callback boom"))

    # Must NOT raise — a failure in the spinner-stop may never displace
    # the original exception nor abort the rest of the error path.
    await dispatcher.feed_update(bot, _callback_update())

    assert len(sent) == 1
    assert "ошибка" in sent[0]["text"].lower()
