"""L-40 — married RPS pair earns couple XP (spouse-XP bonus).

When two users who are MARRIED to each other *in that group chat* finish
a /cpc match, the pair's marriage gains :data:`MARRIED_RPS_XP` on top of
the normal coin settlement (legacy ``rock_paper_scissors.py:541`` coin
flow is untouched; the XP machinery mirrors ``bot.py:21645``
``marriage_add_xp``). Pinned here:

* married pair, group chat, win → marriage experience +10, both result
  cards carry the spouse-XP line, coins settle exactly as before;
* married pair, group chat, tie → XP granted too (a completed match is
  a completed match);
* strangers in the same group chat → no XP row touched, no XP line;
* challenger married to a THIRD user → no XP (the bonus is for playing
  *each other*, not for being married in general);
* private chat → no XP even when a marriage row exists for that
  chat_id shape (marriage is per-GROUP-chat; group ids are negative);
* users.db failure during the grant → logged and swallowed, the game's
  coin settlement still lands (the non-atomic compensating posture
  from ``handlers/couple_activities.py``).

The full flow runs through the real dispatcher (same fixtures as
``test_rps.py``). ``/cpc`` needs no per-chat opt-in in groups — see
``tests/integration/handlers/test_rps_group_support.py`` for why the
feature gate that used to guard it was removed.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.users import Marriage
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.rps import MARRIED_RPS_XP
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import RpsAccept, RpsMoveCallback
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot, Dispatcher

    from tests.e2e.handlers.conftest import WiredFactory


GROUP_CHAT = -100500
CHALLENGER = 100
OPPONENT = 200


# ── Seed / read helpers ──────────────────────────────────────────────


async def _seed_wallet(registry: Any, user_id: int, *, balance: int = 500) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _seed_marriage(
    registry: Any, *, chat_id: int, user1: int, user2: int, experience: int = 0
) -> None:
    a, b = sorted((user1, user2))
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add(
            Marriage(
                chat_id=chat_id,
                user1_id=a,
                user2_id=b,
                created_at=datetime(2026, 1, 1, 12, 0, 0),  # noqa: DTZ001 — legacy naive
                experience=experience,
                status="active",
            )
        )
        await session.commit()


async def _marriage_experience(
    registry: Any, *, chat_id: int, user1: int, user2: int
) -> int | None:
    a, b = sorted((user1, user2))
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        result = await session.execute(
            select(Marriage.experience).where(
                Marriage.chat_id == chat_id,
                Marriage.user1_id == a,
                Marriage.user2_id == b,
            )
        )
        experience: int | None = result.scalar_one_or_none()
        return experience


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _play_full_game(
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    chat_id: int,
    chat_type: str,
    challenger_move: str = "rock",
    opponent_move: str = "scissors",
) -> None:
    """Challenge → accept → both moves, through the real dispatcher."""
    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/cpc {OPPONENT} 100",
            user_id=CHALLENGER,
            chat_id=chat_id,
            chat_type=chat_type,
            language_code="ru",
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=CHALLENGER, bet=100, chat_id=chat_id).pack(),
            user_id=OPPONENT,
            language_code="ru",
            update_id=3,
            callback_id="cb-accept",
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=CHALLENGER, move=challenger_move, chat_id=chat_id).pack(),
            user_id=CHALLENGER,
            language_code="ru",
            update_id=4,
            callback_id="cb-move-c",
        ),
    )
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsMoveCallback(challenger_id=CHALLENGER, move=opponent_move, chat_id=chat_id).pack(),
            user_id=OPPONENT,
            language_code="ru",
            update_id=5,
            callback_id="cb-move-o",
        ),
    )


def _spouse_note(lang: str = "ru") -> str:
    return t("h_rps_spouse_xp", lang, xp=MARRIED_RPS_XP)


def _texts_to(sent: list[dict[str, Any]], chat_id: int) -> list[str]:
    return [m["text"] for m in sent if m.get("kind") == "text" and m["chat_id"] == chat_id]


# ── Tests ────────────────────────────────────────────────────────────


async def test_married_pair_group_win_grants_xp_and_announces(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Married A vs B in a group: coins settle as before AND the pair's
    marriage gains MARRIED_RPS_XP; both seats see the XP line."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER)
    await _seed_wallet(registry, OPPONENT)
    await _seed_marriage(
        registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=OPPONENT, experience=40
    )
    sent = capture_callback_outgoing(bot)

    await _play_full_game(bot, dispatcher, chat_id=GROUP_CHAT, chat_type="supergroup")

    # Coin settlement untouched by the XP grant (rock beats scissors).
    assert await _balance(registry, CHALLENGER) == 590
    assert await _balance(registry, OPPONENT) == 400
    # Marriage XP granted exactly once.
    assert (
        await _marriage_experience(registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=OPPONENT)
        == 40 + MARRIED_RPS_XP
    )
    # Both result cards (delivered to each seat's PM) carry the XP line.
    note = _spouse_note()
    assert any(note in text for text in _texts_to(sent, CHALLENGER))
    assert any(note in text for text in _texts_to(sent, OPPONENT))


async def test_married_pair_group_tie_also_grants_xp(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A tie is a completed match — XP is granted, stakes refunded."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER)
    await _seed_wallet(registry, OPPONENT)
    await _seed_marriage(registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=OPPONENT)
    capture_callback_outgoing(bot)

    await _play_full_game(
        bot,
        dispatcher,
        chat_id=GROUP_CHAT,
        chat_type="supergroup",
        challenger_move="rock",
        opponent_move="rock",
    )

    assert await _balance(registry, CHALLENGER) == 500
    assert await _balance(registry, OPPONENT) == 500
    assert (
        await _marriage_experience(registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=OPPONENT)
        == MARRIED_RPS_XP
    )


async def test_strangers_group_game_no_xp_no_line(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Unmarried players: game settles normally, no XP line anywhere."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER)
    await _seed_wallet(registry, OPPONENT)
    sent = capture_callback_outgoing(bot)

    await _play_full_game(bot, dispatcher, chat_id=GROUP_CHAT, chat_type="supergroup")

    assert await _balance(registry, CHALLENGER) == 590
    assert await _balance(registry, OPPONENT) == 400
    note = _spouse_note()
    assert all(note not in m["text"] for m in sent if m.get("kind") == "text")


async def test_married_to_third_party_no_xp(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Challenger is married — but to user 300, not the opponent. The
    bonus is for playing your OWN spouse, so nothing is granted."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER)
    await _seed_wallet(registry, OPPONENT)
    await _seed_marriage(registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=300)
    sent = capture_callback_outgoing(bot)

    await _play_full_game(bot, dispatcher, chat_id=GROUP_CHAT, chat_type="supergroup")

    assert await _balance(registry, CHALLENGER) == 590
    assert (
        await _marriage_experience(registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=300) == 0
    )
    note = _spouse_note()
    assert all(note not in m["text"] for m in sent if m.get("kind") == "text")


async def test_private_chat_game_never_grants_xp(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Marriage is per-GROUP-chat (negative ids). A private-chat match
    must not grant XP even if a marriage row happens to exist with the
    private chat's id shape."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER)
    await _seed_wallet(registry, OPPONENT)
    # Adversarial seed: a (nonsensical) marriage row keyed on the
    # private chat id — the chat_id >= 0 guard must skip it.
    await _seed_marriage(registry, chat_id=CHALLENGER, user1=CHALLENGER, user2=OPPONENT)
    sent = capture_callback_outgoing(bot)

    await _play_full_game(bot, dispatcher, chat_id=CHALLENGER, chat_type="private")

    assert await _balance(registry, CHALLENGER) == 590
    assert await _balance(registry, OPPONENT) == 400
    assert (
        await _marriage_experience(registry, chat_id=CHALLENGER, user1=CHALLENGER, user2=OPPONENT)
        == 0
    )
    note = _spouse_note()
    assert all(note not in m["text"] for m in sent if m.get("kind") == "text")


async def test_xp_write_failure_does_not_break_the_game(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """users.db hiccup during the grant → logged + swallowed; the coin
    settlement and result cards still land (non-atomic posture)."""
    from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo

    async def _boom(self: BondsWriteRepo, chat_id: int, user_id: int, xp: int) -> int | None:
        raise RuntimeError("users.db down")

    monkeypatch.setattr(BondsWriteRepo, "add_marriage_xp", _boom)

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER)
    await _seed_wallet(registry, OPPONENT)
    await _seed_marriage(registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=OPPONENT)
    sent = capture_callback_outgoing(bot)

    await _play_full_game(bot, dispatcher, chat_id=GROUP_CHAT, chat_type="supergroup")

    # Money settled despite the XP failure.
    assert await _balance(registry, CHALLENGER) == 590
    assert await _balance(registry, OPPONENT) == 400
    # No XP landed, no XP line rendered.
    assert (
        await _marriage_experience(registry, chat_id=GROUP_CHAT, user1=CHALLENGER, user2=OPPONENT)
        == 0
    )
    note = _spouse_note()
    assert all(note not in m["text"] for m in sent if m.get("kind") == "text")
    # Result cards still delivered to both seats.
    assert _texts_to(sent, CHALLENGER)
    assert _texts_to(sent, OPPONENT)
