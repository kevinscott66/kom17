"""#1664: ``/duel`` and ``/cpc`` run the shared anti-abuse caps.

``GameLimitsRepo.count_since`` and ``last_play_at`` filter on
``user_id`` alone — there is no ``game ==`` clause — so the 180 s
cooldown and the 8/hour, 25/day windows are one budget shared by every
game. ``/roll``, ``/flip``, ``/roulette``, ``/pvp_coin`` and
``/pvp_dice`` all drew on it; the two PvP challenge commands, which
carry the largest single-match exposure, drew on nothing. A player who
spent the day's 25 slots on ``/roulette`` simply moved to ``/duel`` and
kept going.

What is stamped is the CHALLENGE, not the settled match: both games
settle rounds later, on somebody else's update, so no single handler
can bracket check and record around the play the way ``/roulette``
does. The challenge is the act the caller chose and it is what puts a
live card in front of someone, so it is the thing worth counting.

The day cap is what these tests exercise, deliberately: it is the only
one of the three whose refusal text carries no clock, so the assertion
is a plain string equality rather than a race against ``wait_sec``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.game_limits import GamePlay
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import DuelAccept, RpsAccept
from telegram_invite_bot.services.roulette_service import MAX_PER_DAY, MAX_PER_HOUR
from tests.e2e.handlers.conftest import _try_capture_send, make_callback_update
from tests.e2e.handlers.test_duel import _duel_callback, _duel_message
from tests.e2e.handlers.test_duel import _get_state_name as _duel_state_name
from tests.e2e.handlers.test_duel import _seed_wallet as _seed_duel_wallet
from tests.e2e.handlers.test_rps import _cpc_message
from tests.e2e.handlers.test_rps import _get_state_name as _rps_state_name
from tests.e2e.handlers.test_rps import _seed_wallet as _seed_rps_wallet

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


GROUP_CHAT_ID = -1001
CHALLENGER_ID = 100
OPPONENT_ID = 200


async def _spend_the_day(registry: Any, user_id: int) -> None:
    """Fill the 24 h window without tripping the cooldown or the hour cap.

    Two hours back is outside both the 180 s cooldown and the one-hour
    window, and comfortably inside the 24 h one — so the only cap left
    to refuse the caller is the daily one.
    """
    # Naive local, because that is what ``game_plays.played_at`` stores.
    stale = datetime.now() - timedelta(hours=2)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for _ in range(MAX_PER_DAY):
            session.add(GamePlay(user_id=user_id, game="roulette", played_at=stale))
        await session.commit()


async def _plays(registry: Any, user_id: int) -> list[str]:
    """Every game name stamped for ``user_id``, oldest first."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = await session.execute(
            select(GamePlay.game).where(GamePlay.user_id == user_id).order_by(GamePlay.id)
        )
    return [str(row) for row in rows.scalars().all()]


def _day_cap_refusal() -> str:
    return t("h_roulette_max_per_day", "ru", max_per_day=MAX_PER_DAY)


# ── /duel ────────────────────────────────────────────────────────────


async def test_a_duel_challenge_is_refused_once_the_shared_day_cap_is_spent(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    await _spend_the_day(registry, CHALLENGER_ID)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))

    assert [m["text"] for m in sent] == [_day_cap_refusal()]
    # No challenge was created: no FSM, and nothing new in game_plays.
    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
    assert await _plays(registry, CHALLENGER_ID) == ["roulette"] * MAX_PER_DAY
    assert await _plays(registry, OPPONENT_ID) == []


async def test_a_duel_challenge_stamps_exactly_one_play_for_the_challenger(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))

    assert await _plays(registry, CHALLENGER_ID) == ["duel"]
    # The opponent has not chosen anything yet — the accept path is
    # where their own slot is spent, not here.
    assert await _plays(registry, OPPONENT_ID) == []


async def test_a_rejected_duel_burns_no_slot(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The gate sits after every parse and balance rejection, so a typo
    can never start someone's cooldown."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel не-число"))

    assert await _plays(registry, CHALLENGER_ID) == []


# ── /cpc ─────────────────────────────────────────────────────────────


async def test_a_cpc_challenge_is_refused_once_the_shared_day_cap_is_spent(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_rps_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_rps_wallet(registry, OPPONENT_ID, balance=500)
    await _spend_the_day(registry, CHALLENGER_ID)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))

    assert [m["text"] for m in sent] == [_day_cap_refusal()]
    # The opponent never saw a card: the refusal is the only send.
    assert [m["chat_id"] for m in sent] == [CHALLENGER_ID]
    assert await _rps_state_name(bot, dispatcher, CHALLENGER_ID) is None
    assert await _plays(registry, CHALLENGER_ID) == ["roulette"] * MAX_PER_DAY


async def test_a_cpc_challenge_stamps_exactly_one_play_for_the_challenger(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_rps_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_rps_wallet(registry, OPPONENT_ID, balance=500)
    capture_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))

    assert await _plays(registry, CHALLENGER_ID) == ["cpc"]
    assert await _plays(registry, OPPONENT_ID) == []


async def test_an_undeliverable_cpc_challenge_burns_no_slot(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stamp lands only after the opponent's card is delivered.

    ``handle_cpc``'s ``TelegramBadRequest`` / ``TelegramForbiddenError``
    branch clears the FSM and apologises to the challenger (#305) — the
    challenge simply never happened. Charging a daily slot for it would
    mean a caller who typed a valid id that has never opened a PM with
    the bot pays for a card nobody ever saw, and can be walked through
    the whole day's budget one unreachable id at a time.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_rps_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_rps_wallet(registry, OPPONENT_ID, balance=500)

    sent: list[dict[str, Any]] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if getattr(method, "chat_id", None) == OPPONENT_ID:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        response = _try_capture_send(method, sent)
        if response is None:
            raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")
        return response

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))

    assert await _rps_state_name(bot, dispatcher, CHALLENGER_ID) is None
    assert "не писал боту" in sent[0]["text"]
    assert await _plays(registry, CHALLENGER_ID) == []


async def _stamp_now(registry: Any, user_id: int, count: int) -> None:
    """Stamp ``count`` plays at this instant.

    One of them is enough to arm the 180 s cooldown; ``MAX_PER_HOUR``
    of them also fill the hour window. Both are needed to show that the
    challenge commands opt out of exactly one of the three caps.
    """
    now = datetime.now()
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for _ in range(count):
            session.add(GamePlay(user_id=user_id, game="roulette", played_at=now))
        await session.commit()


async def test_a_fresh_cooldown_does_not_block_a_duel_challenge(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1664: the challenge commands pass ``include_cooldown=False``.

    The three minutes exist to space out self-served plays that settle
    the instant they are made. A challenge settles minutes later and
    only if someone else agrees, so charging it a cooldown would lock a
    player out of every game in the ecosystem for having invited a
    friend who then declined — and would make the one-session-per-chat
    contract R-FIX-008 pins unreachable, because the second group's
    ``/cpc`` could never open while the first one's cooldown ran.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    await _stamp_now(registry, CHALLENGER_ID, 1)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))

    assert sent, "the challenge card was not sent"
    # h_roulette_cooldown is the only refusal that opens with an hourglass.
    assert not any(m["text"].startswith("\N{HOURGLASS WITH FLOWING SAND}") for m in sent)
    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )
    # Exempt from the clock, but still counted: the budget is shared.
    assert await _plays(registry, CHALLENGER_ID) == ["roulette", "duel"]


async def test_the_hour_cap_still_blocks_a_duel_challenge(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The exemption is one cap wide, not a bypass of the budget."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    await _stamp_now(registry, CHALLENGER_ID, MAX_PER_HOUR)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))

    assert [m["text"] for m in sent] == [
        t("h_roulette_max_per_hour", "ru", max_per_hour=MAX_PER_HOUR)
    ]
    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
    assert await _plays(registry, CHALLENGER_ID) == ["roulette"] * MAX_PER_HOUR


# ── accepting is playing too ─────────────────────────────────────────
#
# The gate above only asks whether the CALLER may start a match. The
# acceptor is the other half of the same play: they get the same card,
# the same stake and the same rolls, and until #1664 commit B they paid
# nothing for it. A player whose day was spent could not open a duel of
# their own, but a friend typing /duel could still walk them through an
# unlimited number of matches — the hole, entered from the other seat.
#
# All four accept surfaces (the two inline buttons, the legacy rps
# button and /accept) funnel through the two shared cores, so these
# tests cover the button and the command and trust the core for the
# rest.


async def test_a_duel_accept_is_refused_once_the_acceptors_day_cap_is_spent(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    await _spend_the_day(registry, OPPONENT_ID)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    sink.clear()  # drop the challenge card; only the accept is at issue
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )

    assert sink == [{"kind": "callback_answer", "text": _day_cap_refusal(), "show_alert": False}]
    # The card is still live: a refused acceptor must not consume the
    # challenger's match, who paid a slot for it and can still be
    # accepted by nobody else — /decline and the sweeper end it.
    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )
    assert await _plays(registry, OPPONENT_ID) == ["roulette"] * MAX_PER_DAY


async def test_a_duel_accept_stamps_exactly_one_play_for_the_acceptor(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )

    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_rolls"
    )
    # One each: the match cost both seats a slot, and neither was
    # charged twice by the rolls that follow.
    assert await _plays(registry, CHALLENGER_ID) == ["duel"]
    assert await _plays(registry, OPPONENT_ID) == ["duel"]


async def test_a_duel_accept_from_the_wrong_seat_burns_no_slot(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The stamp lands after the seat guard, not before it.

    A forged or stale button gives a bystander an accept that cannot
    succeed; charging it would let anyone drain a stranger's daily
    budget by tapping a card that was never theirs.
    """
    bystander_id = 300
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    await _seed_duel_wallet(registry, bystander_id, balance=1_000)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=bystander_id
        ),
    )

    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )
    assert await _plays(registry, bystander_id) == []
    assert await _plays(registry, OPPONENT_ID) == []


# ── /accept — the same gate, without the button ──────────────────────


async def test_the_accept_command_is_refused_once_the_acceptors_day_cap_is_spent(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1664 hung ``EconomyMiddleware`` on the challenge-commands router.

    Without it this router had no economy session at all, so ``/accept``
    could not have been given the limiter even in principle — and would
    have stayed a one-word bypass of the gate the button enforces.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    await _spend_the_day(registry, OPPONENT_ID)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    sent.clear()
    await dispatcher.feed_update(
        bot,
        _duel_message("/accept", user_id=OPPONENT_ID, reply_to_user_id=None, message_id=6),
    )

    assert [m["text"] for m in sent] == [_day_cap_refusal()]
    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )
    assert await _plays(registry, OPPONENT_ID) == ["roulette"] * MAX_PER_DAY


async def test_the_accept_command_stamps_exactly_one_play_for_the_acceptor(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_duel_wallet(registry, CHALLENGER_ID, balance=1_000)
    await _seed_duel_wallet(registry, OPPONENT_ID, balance=1_000)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_message("/accept", user_id=OPPONENT_ID, reply_to_user_id=None, message_id=6),
    )

    assert (
        await _duel_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_rolls"
    )
    assert await _plays(registry, OPPONENT_ID) == ["duel"]


# ── /cpc accept ──────────────────────────────────────────────────────


async def test_a_cpc_accept_is_refused_once_the_acceptors_day_cap_is_spent(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_rps_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_rps_wallet(registry, OPPONENT_ID, balance=500)
    await _spend_the_day(registry, OPPONENT_ID)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    sink.clear()
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=CHALLENGER_ID, bet=100, chat_id=CHALLENGER_ID).pack(),
            user_id=OPPONENT_ID,
            language_code="ru",
        ),
    )

    assert sink == [{"kind": "callback_answer", "text": _day_cap_refusal(), "show_alert": False}]
    assert await _rps_state_name(bot, dispatcher, CHALLENGER_ID) == "RpsStates:awaiting_acceptance"
    assert await _plays(registry, OPPONENT_ID) == ["roulette"] * MAX_PER_DAY


async def test_a_cpc_accept_stamps_exactly_one_play_for_the_acceptor(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_rps_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_rps_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _cpc_message("/cpc 200 100"))
    await dispatcher.feed_update(
        bot,
        make_callback_update(
            RpsAccept(challenger_id=CHALLENGER_ID, bet=100, chat_id=CHALLENGER_ID).pack(),
            user_id=OPPONENT_ID,
            language_code="ru",
        ),
    )

    assert await _rps_state_name(bot, dispatcher, CHALLENGER_ID) == "RpsStates:awaiting_moves"
    assert await _plays(registry, CHALLENGER_ID) == ["cpc"]
    assert await _plays(registry, OPPONENT_ID) == ["cpc"]
