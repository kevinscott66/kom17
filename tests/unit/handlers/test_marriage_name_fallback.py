"""A nameless user must not drag a Russian noun into an English card (#128).

``first_name`` is optional on Telegram's side and empty on ours for any
``users`` row that predates the column, so every marriage/relationship
card has to render *some* stand-in label. This module used to hardcode
the Russian ``Пользователь`` in fourteen places: an English-speaking
group saw "Пользователь and Bob are now married".

Four surfaces are covered because the module renders names two different
ways and both were wrong: the proposal cards interpolate an escaped name
into prose, while the status cards wrap it in a ``tg://user`` mention.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.db.models.users import Marriage
from telegram_invite_bot.handlers import marriage as m
from telegram_invite_bot.i18n import t

EN_FALLBACK = t("h_relations_default_name", "en")
RU_FALLBACK = t("h_relations_default_name", "ru")


class _FakeMessage:
    """Only what the four handlers under test read."""

    def __init__(self, *, reply_to: SimpleNamespace | None = None) -> None:
        self.chat = SimpleNamespace(id=-100, type="supergroup")
        # The caller is nameless too — both halves of a proposal card
        # come from ``from_user``/``reply_to_message.from_user``.
        self.from_user = SimpleNamespace(id=7, is_bot=False, first_name=None)
        self.sender_chat = None
        self.entities: list[Any] = []
        self.reply_to_message = reply_to
        self.sent: list[str] = []

    async def reply(self, text: str, **_kw: Any) -> None:
        self.sent.append(text)


def _replied_to(user_id: int) -> SimpleNamespace:
    target = SimpleNamespace(id=user_id, is_bot=False, first_name=None)
    replied = SimpleNamespace(from_user=target)
    # ``handle_marry`` answers the message it was replying to, not the
    # command — capture that text too.
    replied.sent = []  # type: ignore[attr-defined]

    async def _reply(text: str, **_kw: Any) -> None:
        replied.sent.append(text)  # type: ignore[attr-defined]

    replied.reply = _reply  # type: ignore[attr-defined]
    return replied


def _marriage_row() -> Marriage:
    return Marriage(
        id=1,
        chat_id=-100,
        user1_id=7,
        user2_id=9,
        created_at=datetime(2026, 1, 1),
        experience=100,
        duration_days=0,
    )


class _FakeRepo:
    """A DB where nobody has a first_name on record."""

    RELATIONSHIP_LEVEL_XP = m._RELATIONSHIP_LEVEL_XP

    def __init__(self, *, marriage: Marriage | None = None, rel: Any | None = None) -> None:
        self._marriage = marriage
        self._rel = rel
        self._rels = [
            SimpleNamespace(
                user1_id=7,
                user2_id=9,
                experience=1_000_000,
                created_at=datetime(2026, 1, 1),
            )
        ]

    async def get_first_name(self, user_id: int) -> str | None:
        return None

    async def get_marriage(self, chat_id: int, user_id: int) -> Marriage | None:
        return self._marriage

    async def get_relationship(self, chat_id: int, a: int, b: int) -> Any:
        return self._rel

    def _rel_xp_to_level(self, exp: int) -> int:
        return sum(1 for threshold in self.RELATIONSHIP_LEVEL_XP if exp >= threshold) - 1

    async def propose_marriage(self, chat_id: int, a: int, b: int) -> Any:
        return SimpleNamespace(id=1, from_id=a, to_id=b)

    async def propose_relationship(self, chat_id: int, a: int, b: int) -> Any:
        return SimpleNamespace(id=1, from_id=a, to_id=b)

    async def list_relationships_for(self, chat_id: int, user_id: int) -> list[Any]:
        return self._rels


@pytest.mark.asyncio
async def test_the_marriage_proposal_card_speaks_the_users_language() -> None:
    """Both names are interpolated as prose here, not as mentions."""
    replied = _replied_to(9)
    message = _FakeMessage(reply_to=replied)

    # Well past the level-6 gate, so ``/marry`` reaches the card.
    repo = _FakeRepo(rel=SimpleNamespace(experience=1_000_000))

    await m.handle_marry(cast("Message", message), cast("Any", repo), "en")

    text = replied.sent[0]
    assert RU_FALLBACK not in text
    assert text.count(EN_FALLBACK) == 2  # proposer and target


@pytest.mark.asyncio
async def test_the_relationship_proposal_card_speaks_the_users_language() -> None:
    replied = _replied_to(9)
    message = _FakeMessage(reply_to=replied)

    await m.handle_relationship(cast("Message", message), cast("Any", _FakeRepo()), "en")

    text = replied.sent[0]
    assert RU_FALLBACK not in text
    assert text.count(EN_FALLBACK) == 2


@pytest.mark.asyncio
async def test_the_status_card_mentions_a_nameless_partner_in_english() -> None:
    """``/marriage`` renders the partner as a tg:// mention — the label
    goes inside the link, so it must still be localized."""
    message = _FakeMessage()

    await m.handle_marriage(
        cast("Message", message),
        cast("Any", _FakeRepo(marriage=_marriage_row())),
        "en",
    )

    text = message.sent[0]
    assert RU_FALLBACK not in text
    assert f'<a href="tg://user?id=9">{EN_FALLBACK}</a>' in text


@pytest.mark.asyncio
async def test_another_users_card_names_both_spouses_in_english() -> None:
    replied = _replied_to(9)
    message = _FakeMessage(reply_to=replied)

    await m.handle_marry_other(
        cast("Message", message),
        cast("Any", _FakeRepo(marriage=_marriage_row())),
        "en",
    )

    text = message.sent[0]
    assert RU_FALLBACK not in text
    assert f'<a href="tg://user?id=7">{EN_FALLBACK}</a>' in text
    assert f'<a href="tg://user?id=9">{EN_FALLBACK}</a>' in text


@pytest.mark.asyncio
async def test_the_bond_list_localizes_both_the_mention_and_the_button() -> None:
    """The list resolves the name once and spends it twice — inside an
    HTML mention and inside a plain inline-button caption."""
    message = _FakeMessage()

    await m.handle_relationship(cast("Message", message), cast("Any", _FakeRepo()), "en")

    text = message.sent[0]
    assert RU_FALLBACK not in text
    assert f'<a href="tg://user?id=9">{EN_FALLBACK}</a>' in text


@pytest.mark.asyncio
async def test_the_russian_card_still_reads_russian() -> None:
    """The fix is localization, not translation to English everywhere."""
    message = _FakeMessage()

    await m.handle_marriage(
        cast("Message", message),
        cast("Any", _FakeRepo(marriage=_marriage_row())),
        "ru",
    )

    assert RU_FALLBACK in message.sent[0]
