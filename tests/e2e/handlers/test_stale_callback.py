"""A tap on a dead inline button gets answered, not left spinning (#159).

Telegram does not treat "no ``answerCallbackQuery``" as "no answer": it
keeps the button in its loading state for roughly fifteen seconds and
then clears it with nothing to show for it. Before this tail, every
callback query that matched no handler ended that way — a card from an
older deploy, a panel whose FSM state was dropped on restart, a
keyboard the user scrolled back to weeks later.

Driven through the real assembled router tree (``make_wired`` builds
``build_main_router``), because the whole claim is about what happens
*after* every real handler has declined. A stub dispatcher would prove
nothing: the tail is trivially reachable when it is the only thing
there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.i18n import t
from telegram_invite_bot.webhook.metrics import HANDLER_ERRORS, STALE_CALLBACKS
from tests.e2e.handlers.conftest import make_callback_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _stale_count(prefix: str) -> float:
    """Current value of the counter for one label.

    ``prometheus_client`` keeps counters in a process-global registry
    and the suite never resets it, so every assertion here is a diff
    against a snapshot taken before the act — the convention the rest
    of the metric tests use.
    """
    return STALE_CALLBACKS.labels(prefix=prefix)._value.get()  # noqa: SLF001


async def test_unmatched_tap_is_answered_with_a_toast(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The spinner stops, and what stops it is a toast, not an alert.

    ``show_alert`` is asserted explicitly: an alert is a modal the user
    has to dismiss, and "the card you tapped is old" does not earn one.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, make_callback_update("no_such_prefix:1:2", user_id=555)
    )

    assert result is not UNHANDLED
    assert sent == [
        {
            "kind": "callback_answer",
            "text": t("h_stale_card", "ru"),
            "show_alert": False,
        }
    ]


async def test_the_toast_says_nothing_about_the_payload(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The raw ``callback_data`` never reaches the user.

    Payloads carry ids, amounts and order numbers. Echoing one back —
    the obvious way to make the toast "more helpful" — would put them
    on screen, and into whatever screenshot the user sends to support.
    Pinned as a test rather than left to the copy, because the copy is
    the easiest thing in the codebase to edit without thinking.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)
    secret = "wd_ok:70123:99999"

    await dispatcher.feed_update(bot, make_callback_update(secret, user_id=555))

    text = sent[0]["text"]
    assert "70123" not in text
    assert "99999" not in text
    assert secret not in text


async def test_the_toast_fits_telegrams_cap(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``answerCallbackQuery`` truncates past 200 characters (#100).

    Both languages, because the copy is edited per file and the Russian
    line is the longer one.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    capture_callback_outgoing(bot)
    for lang in ("ru", "en"):
        assert len(t("h_stale_card", lang)) <= 200, lang


async def test_the_answer_follows_the_users_language(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The tail is behind the language middleware, not beside it.

    Easy to get wrong: an unfiltered handler included at the end of the
    tree still needs ``lang`` injected, and a handler that fell back to
    a hardcoded string would pass every other test in this file.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_callback_update("no_such_prefix", user_id=556, language_code="en")
    )

    assert sent[0]["text"] == t("h_stale_card", "en")
    assert sent[0]["text"] != t("h_stale_card", "ru")


async def test_an_unknown_prefix_is_counted_as_unknown(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Junk data must not mint a time series of its own."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    capture_callback_outgoing(bot)
    before = _stale_count("unknown")

    await dispatcher.feed_update(bot, make_callback_update("prefix_from_2019:7", user_id=555))

    assert _stale_count("unknown") == before + 1


async def test_a_known_prefix_is_counted_under_its_own_name(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The case the metric exists for: a live family, a dead payload.

    ``shop_buy`` is registered, so its filter is reached — and rejects,
    because the payload does not unpack into the factory's fields. That
    is exactly what a card from before a field was added looks like,
    and the counter has to name the family so the owner can tell which
    keyboard went stale rather than only that some did.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    capture_callback_outgoing(bot)
    before = _stale_count("shop_buy")

    result = await dispatcher.feed_update(
        bot, make_callback_update("shop_buy:not-an-int", user_id=555)
    )

    assert result is not UNHANDLED
    assert _stale_count("shop_buy") == before + 1


def _handler_errors(label: str) -> float:
    """Current ``tib_handler_errors_total`` value for one ``exc_type``."""
    return HANDLER_ERRORS.labels(exc_type=label)._value.get()  # noqa: SLF001


def _answer_raises(monkeypatch: pytest.MonkeyPatch, bot: Bot, exc: Exception) -> None:
    """Make every ``answerCallbackQuery`` fail with ``exc``."""

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        raise exc

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def test_an_aged_out_query_is_swallowed(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most likely failure here must not become an error card.

    Telegram refuses an answer ~15 s after the tap. A card old enough
    to have a dead button is precisely a card someone came back to, so
    this is the ordinary outcome, not the exceptional one — and the
    tail is reached often enough that letting it raise would fill the
    log with the one thing it was built to expect.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    _answer_raises(
        monkeypatch,
        bot,
        TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="cb-1"),
            message="Bad Request: query is too old and response timeout expired",
        ),
    )

    result = await dispatcher.feed_update(bot, make_callback_update("no_such_prefix", user_id=555))

    assert result is not UNHANDLED


async def test_any_other_rejection_stays_loud(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken toast is our bug and must reach the error router.

    The narrow swallow exists because the wide one (#46) hides exactly
    this: an over-long or malformed answer text would be refused with
    the same exception class, and swallowing it would leave every stale
    tap silently unanswered again — the original bug, wearing a fix.

    "Loud" means it escapes the handler and reaches the error router,
    which is what bumps ``HANDLER_ERRORS`` and logs the traceback.
    ``feed_update`` still returns normally — the router is the last
    thing in the tree and it does not re-raise — so the counter, not an
    exception, is what this test can observe.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    _answer_raises(
        monkeypatch,
        bot,
        TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="cb-1"),
            message="Bad Request: message text is too long",
        ),
    )
    before = _handler_errors("TelegramBadRequest")
    benign_before = _handler_errors("BenignReject")

    await dispatcher.feed_update(bot, make_callback_update("no_such_prefix", user_id=555))

    assert _handler_errors("TelegramBadRequest") == before + 1
    assert _handler_errors("BenignReject") == benign_before
