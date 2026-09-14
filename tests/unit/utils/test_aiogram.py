"""Guards on ``utils.aiogram.edit_card`` and on who uses it.

Prod (12.08) logged ``Bad Request: message is not modified`` twice out of
``handlers.rating.handle_rating_nav``: the page number lives in the
callback data, so tapping the page you are already on re-renders
byte-identical text. Raw ``edit_text`` turns that into an exception,
the global error router catches it and replies "⚠️ Произошла ошибка" —
a user-visible failure for a button that had nothing to change.

Prod (16.08–17.08) then logged a second family out of the same handler:
``Bad Request: there is no text in the message to edit``, five times on a
``ratnav`` page tap. That one is not a no-op — the card is a media
message, whose body is a caption that ``editMessageText`` cannot touch.
Swallowing it would leave the button dead; the helper falls back to
``editMessageCaption`` so the tap does what it promised.

Two layers of guard here. The behavioural tests pin what ``edit_card``
swallows, what it retries as a caption and what it must keep loud. The
source-level test pins that the handlers which redraw their own card
actually go through it, because the behavioural tests cannot see a call
site that quietly reverts to ``message.edit_text``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import telegram_invite_bot
from telegram_invite_bot.utils.aiogram import (
    BENIGN_EDIT_REJECTS,
    NO_TEXT_TO_EDIT,
    edit_card,
    reply_or_send,
)
from telegram_invite_bot.utils.render import TELEGRAM_CAPTION_LIMIT


class _Ref:
    """The ``chat.id`` / ``message_id`` pair the caption path logs."""

    id = -100123


class _Card:
    """Minimal ``Message`` stand-in recording the calls we care about.

    Carries both edit methods because the media fallback needs the pair:
    a card that refuses ``edit_text`` is exactly the card that answers
    ``edit_caption``, and a stub with only the first cannot show that.
    """

    chat = _Ref()
    message_id = 42

    def __init__(
        self,
        raises: Exception | None = None,
        *,
        caption_raises: Exception | None = None,
    ) -> None:
        self._raises = raises
        self._caption_raises = caption_raises
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.caption_calls: list[dict[str, Any]] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.calls.append((text, kwargs))
        if self._raises is not None:
            raise self._raises

    async def edit_caption(self, **kwargs: Any) -> None:
        self.caption_calls.append(kwargs)
        if self._caption_raises is not None:
            raise self._caption_raises


class _Chat:
    """``Message`` stand-in for the reply/send pair, per-method failures."""

    def __init__(
        self,
        *,
        reply_raises: Exception | None = None,
        answer_raises: Exception | None = None,
    ) -> None:
        self._reply_raises = reply_raises
        self._answer_raises = answer_raises
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def reply(self, text: str, **kwargs: Any) -> None:
        self.calls.append(("reply", text, kwargs))
        if self._reply_raises is not None:
            raise self._reply_raises

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.calls.append(("answer", text, kwargs))
        if self._answer_raises is not None:
            raise self._answer_raises


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message=message)  # type: ignore[arg-type]


async def test_a_successful_edit_reports_true_and_forwards_kwargs() -> None:
    card = _Card()

    assert await edit_card(card, "text", disable_web_page_preview=True) is True  # type: ignore[arg-type]

    text, kwargs = card.calls[0]
    assert text == "text"
    assert kwargs == {"reply_markup": None, "disable_web_page_preview": True}


@pytest.mark.parametrize("marker", BENIGN_EDIT_REJECTS)
async def test_benign_rejects_are_swallowed(marker: str) -> None:
    # Telegram prefixes the reason, so the helper matches on a substring.
    card = _Card(raises=_bad_request(f"Bad Request: {marker}"))

    assert await edit_card(card, "text") is False  # type: ignore[arg-type]


async def test_a_real_bad_request_still_propagates() -> None:
    # Malformed HTML in a card is our bug and must stay loud — swallowing
    # it would hide a broken template behind a silently unchanged card.
    card = _Card(raises=_bad_request("Bad Request: can't parse entities"))

    with pytest.raises(TelegramBadRequest):
        await edit_card(card, "<b>text")  # type: ignore[arg-type]


async def test_a_media_card_is_redrawn_through_its_caption() -> None:
    # The whole point of the fallback: the user tapped "next page" on a
    # card that happens to be a photo, and the page has to turn. Failing
    # here is what put "⚠️ Произошла ошибка" under five prod taps.
    card = _Card(raises=_bad_request(f"Bad Request: {NO_TEXT_TO_EDIT}"))

    assert await edit_card(card, "page 2", disable_web_page_preview=True) is True  # type: ignore[arg-type]

    assert len(card.caption_calls) == 1
    # ``disable_web_page_preview`` is a sendMessage/editMessageText
    # parameter; forwarding it would raise TypeError inside the rescue.
    assert card.caption_calls[0] == {"caption": "page 2", "reply_markup": None}


async def test_a_body_too_long_for_a_caption_is_not_attempted() -> None:
    # Captions get a quarter of a message. Sending one anyway trades this
    # 400 for "message caption is too long" — same error router, same
    # toast, one more round trip.
    card = _Card(raises=_bad_request(f"Bad Request: {NO_TEXT_TO_EDIT}"))

    assert await edit_card(card, "x" * (TELEGRAM_CAPTION_LIMIT + 1)) is False  # type: ignore[arg-type]

    assert card.caption_calls == []


async def test_the_caption_fallback_swallows_a_benign_refusal() -> None:
    # A media card can be stale for all the ordinary reasons too — the
    # fallback inherits the same three-marker swallow, not a new policy.
    card = _Card(
        raises=_bad_request(f"Bad Request: {NO_TEXT_TO_EDIT}"),
        caption_raises=_bad_request("Bad Request: message can't be edited"),
    )

    assert await edit_card(card, "page 2") is False  # type: ignore[arg-type]

    assert len(card.caption_calls) == 1


async def test_the_caption_fallback_keeps_a_malformed_body_loud() -> None:
    # And it inherits the other half: broken markup is our bug on a photo
    # card exactly as it is on a text one.
    card = _Card(
        raises=_bad_request(f"Bad Request: {NO_TEXT_TO_EDIT}"),
        caption_raises=_bad_request("Bad Request: can't parse entities"),
    )

    with pytest.raises(TelegramBadRequest):
        await edit_card(card, "<b>page 2")  # type: ignore[arg-type]


async def test_reply_or_send_prefers_a_reply() -> None:
    # In a busy group the result has to stay attached to the tap that
    # paid for it, so the reply is not just a stylistic preference.
    chat = _Chat()

    assert await reply_or_send(chat, "result") is True  # type: ignore[arg-type]

    assert [call[0] for call in chat.calls] == ["reply"]


async def test_reply_or_send_falls_back_when_the_card_is_gone() -> None:
    # Telegram rejects the whole call when the reply target is missing —
    # the message the user tapped was deleted between the tap and the
    # result. The coins are already spent; silence is not an option.
    chat = _Chat(reply_raises=_bad_request("Bad Request: message to be replied not found"))

    assert await reply_or_send(chat, "result") is True  # type: ignore[arg-type]

    assert [call[0] for call in chat.calls] == ["reply", "answer"]
    assert chat.calls[1][1] == "result"


async def test_reply_or_send_reports_a_dead_chat() -> None:
    # Kicked from the group, or blocked. Nothing left to try, and the
    # caller logs it — a paid action that reached nobody is worth a line.
    chat = _Chat(
        reply_raises=_bad_request("Bad Request: message to be replied not found"),
        answer_raises=TelegramForbiddenError(method=None, message="bot was kicked"),  # type: ignore[arg-type]
    )

    assert await reply_or_send(chat, "result") is False  # type: ignore[arg-type]

    assert [call[0] for call in chat.calls] == ["reply", "answer"]


async def test_reply_or_send_keeps_a_malformed_body_loud() -> None:
    # Same line as ``edit_card`` draws: broken markup in the body is our
    # bug. Retrying the identical text as a fresh send would only fail
    # again, so the fallback must not paper over it.
    chat = _Chat(reply_raises=_bad_request("Bad Request: can't parse entities"))

    with pytest.raises(TelegramBadRequest):
        await reply_or_send(chat, "<b>result")  # type: ignore[arg-type]

    assert [call[0] for call in chat.calls] == ["reply"]


#: Handlers that redraw an inline card the user can tap twice (or that
#: edit a message minutes after sending it). Each one produced a real or
#: latent "⚠️ Произошла ошибка" before the conversion to ``edit_card``,
#: or swallowed *every* ``TelegramBadRequest`` — which hides a malformed
#: card just as effectively as it hides a no-op edit.
#:
#: ``rps`` and ``ads`` are deliberately absent: their remaining raw
#: ``edit_text`` calls catch the reject and *send a fresh message*
#: instead, which is strictly better than either swallowing or raising.
CARD_REDRAW_MODULES = (
    "checks.py",
    "couple_activities.py",
    "duel.py",
    "games.py",
    "main_menu.py",
    "marriage.py",
    "mygroups.py",
    "profile.py",
    "rating.py",
    "transfer_rights.py",
    "voice_settings.py",
)


@pytest.mark.parametrize("module", CARD_REDRAW_MODULES)
def test_card_redraw_handlers_do_not_call_edit_text_raw(module: str) -> None:
    handlers = Path(telegram_invite_bot.__file__).parent / "handlers"
    source = (handlers / module).read_text(encoding="utf-8")

    offenders = [
        line.strip()
        for line in source.splitlines()
        if ".edit_text(" in line and not line.lstrip().startswith("#")
    ]

    assert not offenders, (
        f"{module} redraws a card with a raw edit_text; use "
        f"utils.aiogram.edit_card so a no-op or expired edit does not "
        f"reach the global error router: {offenders}"
    )
