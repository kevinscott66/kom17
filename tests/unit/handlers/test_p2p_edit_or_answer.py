"""#722: a re-tap must not stack a second copy of the same P2P card.

Both P2P modules carry their own ``_edit_or_answer`` — a legacy
try-edit-except-send port (``bot.py:19963-19965``, ``:20140-20142``)
that treated *every* ``TelegramBadRequest`` as "the edit failed, send a
new message". Telegram raises "message is not modified" when the card
already renders what the tap asked for, which is not a failure at all:
re-tapping the currency flag you are already filtered on redrew an
identical card and got a duplicate posted underneath it. That is the
same incident ``handlers/rating.py`` records from prod on 12.08.

These tests pin the split: not-modified is a silent no-op, and every
other reject still earns the fallback message the port was written for.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import EditMessageText
from aiogram.types import Message as MessageType

from telegram_invite_bot.handlers.p2p import _edit_or_answer as _edit_menu
from telegram_invite_bot.handlers.p2p_trade import _edit_or_answer as _edit_trade

_HELPERS = (_edit_menu, _edit_trade)


class _Card:
    """A stand-in for the callback's message.

    ``isinstance(target, MessageType)`` gates both helpers, so the fake
    has to pass that check — hence the explicit ``__class__`` override
    rather than a ``MagicMock``.
    """

    __class__ = MessageType  # type: ignore[assignment]

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.edits = 0
        self.answers = 0

    async def edit_text(self, text: str, **kwargs: Any) -> None:  # noqa: ANN401
        self.edits += 1
        if self.error is not None:
            raise self.error

    async def answer(self, text: str, **kwargs: Any) -> None:  # noqa: ANN401
        self.answers += 1


def _reject(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(
        method=EditMessageText(chat_id=1, message_id=1, text="x"), message=message
    )


@pytest.mark.parametrize("helper", _HELPERS)
async def test_not_modified_is_a_silent_noop(helper: Any) -> None:  # noqa: ANN401
    card = _Card(_reject("Bad Request: message is not modified"))
    await helper(card, "same text")
    assert card.edits == 1
    assert card.answers == 0, "a re-tap must not post a duplicate card"


@pytest.mark.parametrize("helper", _HELPERS)
@pytest.mark.parametrize(
    "message",
    ["Bad Request: message to edit not found", "Bad Request: message can't be edited"],
)
async def test_a_lost_card_still_earns_a_fresh_message(helper: Any, message: str) -> None:  # noqa: ANN401
    card = _Card(_reject(message))
    await helper(card, "next screen")
    assert card.answers == 1


@pytest.mark.parametrize("helper", _HELPERS)
async def test_forbidden_still_earns_a_fresh_message(helper: Any) -> None:  # noqa: ANN401
    card = _Card(
        TelegramForbiddenError(
            method=EditMessageText(chat_id=1, message_id=1, text="x"),
            message="Forbidden: bot was blocked",
        )
    )
    await helper(card, "next screen")
    assert card.answers == 1


@pytest.mark.parametrize("helper", _HELPERS)
async def test_a_successful_edit_sends_nothing(helper: Any) -> None:  # noqa: ANN401
    card = _Card(None)
    await helper(card, "next screen")
    assert (card.edits, card.answers) == (1, 0)
