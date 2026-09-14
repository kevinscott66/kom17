"""End-to-end L-17 stake flows: ``/roll <bet> <guess>`` + ``/flip <bet> <side>``.

The stake child router mounts only when ``handlers.games.build_router``
receives an ``EngineRegistry``. To keep the stake flows isolated from
everything else ``main_router`` mounts, these tests wire a
STANDALONE dispatcher: ``LanguageMiddleware`` (for the ``lang`` kwarg)
+ ``build_router(registry)``. ``make_wired`` is still used — for the
Bot, the schemas, and the teardown — its main-router dispatcher is just
unused here.

The matrix pins (mirroring ``test_roulette.py``):

* WIN /roll (dice → 4, guess 4): −bet +5.7×bet; games row (game="dice",
  profit=+4.7×bet); game_plays stamped.
* LOSE /roll: −bet; profit −bet.
* WIN/LOSE /flip with a pinned coin side: ×1.9 gross on a win.
* Bounds / insufficient → localised refusal, no debit, no stamp.
* Invalid guess (``/roll 100 9``) / invalid side → hint, no debit.
* Private chat → group-only refusal (stakes are group-only; the vanity
  forms stay private-friendly — pinned here too).
* Cooldown (seeded ``game_plays``) blocks the play before any debit.
* Free forms still route to the vanity handlers on the SAME router —
  no economy rows written.
* #222-B: a second stake fired while the first receipt is still
  being sent is refused — the anti-abuse stamp is committed inside
  the per-user lock, not left pending until the middleware runs.
* RR-3 #34: both stake receipts close with the remaining-play
  allowance, counted ACROSS games (the windows are shared with
  /roulette); refusals carry no footer.

i18n note: assertions match the RENDERED text, not the key, so a copy
change that drops the win/lose verdict or the allowed-range hint fails
here — those three strings are the entire result of a stake game as the
player sees it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Dice, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, GameResult
from telegram_invite_bot.db.models.game_limits import GamePlay
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import games as games_handler
from telegram_invite_bot.middlewares.language import LanguageMiddleware
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

GROUP_CHAT_ID = -1002
USER_ID = 200


async def _wire(
    make_wired: WiredFactory,
) -> tuple[Bot, Dispatcher, Any]:
    """Bot + standalone dispatcher with the registry-mounted games router."""
    bot, _, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.outer_middleware(LanguageMiddleware(registry))
    dispatcher.include_router(games_handler.build_router(registry))
    return bot, dispatcher, registry


async def _seed_wallet(registry: Any, user_id: int, *, balance: int = 1_000) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _seed_play(registry: Any, user_id: int, *, ago_seconds: float) -> None:
    """One completed-play stamp ``ago_seconds`` in the past (cooldown seed)."""
    now = datetime.now()  # noqa: DTZ005 — naive local, matches game_plays
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            GamePlay(
                user_id=user_id,
                game="dice",
                played_at=now - timedelta(seconds=ago_seconds),
            )
        )
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _game_rows(registry: Any, user_id: int) -> list[GameResult]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GameResult).where(GameResult.user_id == user_id)))
            .scalars()
            .all()
        )
    return list(rows)


async def _play_stamps(registry: Any, user_id: int) -> list[GamePlay]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GamePlay).where(GamePlay.user_id == user_id)))
            .scalars()
            .all()
        )
    return list(rows)


def _update(
    text: str,
    *,
    user_id: int = USER_ID,
    chat_type: str = "supergroup",
    chat_id: int = GROUP_CHAT_ID,
) -> Update:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        first_name="S",
        language_code="ru",
    )


def _capture(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    dice_value: int = 4,
) -> None:
    """Record SendDice / SendMessage; pin the Telegram dice to ``dice_value``."""

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendDice":
            sink.append({"kind": "dice", "emoji": method.emoji})
            return Message(
                message_id=2,
                date=datetime(2024, 1, 1),  # noqa: DTZ001 — synthetic fixture
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                dice=Dice(emoji=method.emoji or "🎲", value=dice_value),
            )
        if name == "SendMessage":
            sink.append({"kind": "text", "text": method.text})
            return Message(
                message_id=3,
                date=datetime(2024, 1, 1),  # noqa: DTZ001 — synthetic fixture
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


# ── /roll stake: win / lose economy ──────────────────────────────────


async def test_roll_stake_win_credits_gross_payout(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=4)

    result = await dispatcher.feed_update(bot, _update("/roll 100 4"))

    assert result is not UNHANDLED
    # Animation first (after validation), then the settled receipt.
    assert [s["kind"] for s in sent] == ["dice", "text"]
    body = sent[-1]["text"]
    assert "УГАДАЛ!" in body  # rendered h_roll_bet_win
    assert "💰 Баланс: 1470 🪙" in body  # existing h_roulette_balance key
    # 1000 − 100 + 570 (bet×5.7 gross, DICE_MULTIPLIER) = 1470.
    assert await _balance(registry, USER_ID) == 1_470
    [row] = await _game_rows(registry, USER_ID)
    assert (row.game, row.win, row.bet, row.profit) == ("dice", True, 100, 470)
    [stamp] = await _play_stamps(registry, USER_ID)
    assert stamp.game == "dice"


async def test_roll_stake_lose_burns_stake(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=2)

    await dispatcher.feed_update(bot, _update("/roll 100 4"))

    body = sent[-1]["text"]
    assert "МИМО!" in body  # rendered h_roll_bet_lose
    assert "💰 Баланс: 900 🪙" in body
    assert await _balance(registry, USER_ID) == 900
    [row] = await _game_rows(registry, USER_ID)
    assert (row.game, row.win, row.profit) == ("dice", False, -100)


# ── /roll stake: rejections (no debit, no stamp, no animation) ───────


async def test_roll_stake_invalid_guess_no_side_effects(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/roll 100 9`` — matches the stake filter (two digit tokens) but
    the guess is off-die; localised hint, wallet untouched, NO dice
    animation (legacy rendered its usage text, bot.py:17400).
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update("/roll 100 9"))

    assert result is not UNHANDLED
    assert [s["kind"] for s in sent] == ["text"]
    assert "от 1 до 6" in sent[0]["text"]  # rendered h_roll_bet_invalid_guess
    assert await _balance(registry, USER_ID) == 1_000
    assert await _game_rows(registry, USER_ID) == []


async def test_roll_stake_below_min_bet_refused(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/roll 5 4"))

    assert [s["kind"] for s in sent] == ["text"]
    assert "Минимальная ставка" in sent[0]["text"]  # existing key renders
    assert await _balance(registry, USER_ID) == 1_000
    assert await _play_stamps(registry, USER_ID) == []


async def test_roll_stake_insufficient_refused_with_balance(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=40)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/roll 100 4"))

    assert [s["kind"] for s in sent] == ["text"]
    assert "Недостаточно средств" in sent[0]["text"]
    assert "40" in sent[0]["text"]
    assert await _balance(registry, USER_ID) == 40


async def test_roll_stake_private_chat_refused(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stakes are GROUP-ONLY (legacy require_group=True, bot.py:17338)."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(
        bot, _update("/roll 100 4", chat_type="private", chat_id=USER_ID)
    )

    assert result is not UNHANDLED
    assert "групповых чатах" in sent[0]["text"]  # rendered h_stake_group_only
    assert await _balance(registry, USER_ID) == 1_000


async def test_roll_stake_cooldown_blocks_before_debit(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A play 10s ago (cooldown 180s) blocks the next stake — the caps
    are the same persistent GameLimitService windows /roulette uses,
    shared across games.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    await _seed_play(registry, USER_ID, ago_seconds=10)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/roll 100 4"))

    assert [s["kind"] for s in sent] == ["text"]
    assert "Подожди" in sent[0]["text"]  # existing h_roulette_cooldown key
    assert await _balance(registry, USER_ID) == 1_000
    assert await _game_rows(registry, USER_ID) == []


# ── /flip stake ──────────────────────────────────────────────────────


async def test_flip_stake_win_credits_gross_payout(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=500)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    # Pin the coin to орёл via the shared single-source RNG helper.
    monkeypatch.setattr(games_handler, "_flip_side", lambda: "орёл")

    result = await dispatcher.feed_update(bot, _update("/flip 50 орёл"))

    assert result is not UNHANDLED
    assert [s["kind"] for s in sent] == ["text"]  # single receipt, no dice
    body = sent[0]["text"]
    assert "УГАДАЛ!" in body  # rendered h_flip_bet_win
    assert "💰 Баланс: 545 🪙" in body  # 500 − 50 + 95 (bet×1.9 gross)
    assert await _balance(registry, USER_ID) == 545
    [row] = await _game_rows(registry, USER_ID)
    assert (row.game, row.win, row.bet, row.profit) == ("flip", True, 50, 45)
    [stamp] = await _play_stamps(registry, USER_ID)
    assert stamp.game == "flip"


async def test_flip_stake_lose_burns_stake(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=500)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    monkeypatch.setattr(games_handler, "_flip_side", lambda: "решка")

    await dispatcher.feed_update(bot, _update("/flip 50 орёл"))

    body = sent[0]["text"]
    assert "МИМО!" in body  # rendered h_flip_bet_lose
    assert await _balance(registry, USER_ID) == 450
    [row] = await _game_rows(registry, USER_ID)
    assert (row.game, row.win, row.profit) == ("flip", False, -50)


async def test_flip_stake_invalid_side_no_side_effects(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=500)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update("/flip 50 pizza"))

    assert result is not UNHANDLED
    assert "орёл или решка" in sent[0]["text"]  # rendered h_flip_bet_invalid_side
    assert await _balance(registry, USER_ID) == 500
    assert await _game_rows(registry, USER_ID) == []


async def test_flip_stake_alias_and_multiword_side(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/монетка 50 решка`` — alias + the legacy ``" ".join(args[1:])``
    side parse both survive the port (bot.py:17463-17465).
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=500)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    monkeypatch.setattr(games_handler, "_flip_side", lambda: "решка")

    result = await dispatcher.feed_update(bot, _update("/монетка 50 решка"))

    assert result is not UNHANDLED
    assert "УГАДАЛ!" in sent[0]["text"]  # rendered h_flip_bet_win
    assert await _balance(registry, USER_ID) == 545


# ── free forms stay free on the registry-mounted router ──────────────


async def test_vanity_roll_still_free_with_registry(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/roll 4`` keeps hitting the vanity handler — no debit, no games
    row — proving the stake child's filters don't swallow the free form.
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=4)

    result = await dispatcher.feed_update(bot, _update("/roll 4"))

    assert result is not UNHANDLED
    assert await _balance(registry, USER_ID) == 1_000
    assert await _game_rows(registry, USER_ID) == []


async def test_vanity_flip_still_free_with_registry(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    monkeypatch.setattr(games_handler, "_FLIP_REVEAL_DELAY", 0)

    # The vanity flip edits its throw message; widen the capture inline.
    sent: list[dict[str, Any]] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        sent.append({"kind": name})
        if name in {"SendMessage", "EditMessageText"}:
            # ``.as_(bot)`` mounts the synthetic Message so the handler's
            # follow-up ``thrown.edit_text(...)`` can issue its call.
            return Message(
                message_id=3,
                date=datetime(2024, 1, 1),  # noqa: DTZ001 — synthetic fixture
                chat=Chat(id=GROUP_CHAT_ID, type="supergroup"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            ).as_(bot)
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)

    result = await dispatcher.feed_update(bot, _update("/flip орёл"))

    assert result is not UNHANDLED
    assert await _balance(registry, USER_ID) == 1_000
    assert await _game_rows(registry, USER_ID) == []


# ── Remaining-play allowance footer (RR-3 #34) ───────────────────────


async def test_roll_stake_card_shows_remaining_allowance(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stake receipt closes with the same allowance footer /roulette
    renders — the anti-abuse windows are shared across all three games,
    so the count must be too."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=4)

    await dispatcher.feed_update(bot, _update("/roll 100 4"))

    body = sent[-1]["text"]
    assert "Осталось игр" in body
    assert "<b>7</b>" in body  # MAX_PER_HOUR (8) − 0 seeded − this play
    assert "<b>24</b>" in body  # MAX_PER_DAY (25) − 0 seeded − this play


async def test_flip_stake_card_shows_remaining_allowance(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same footer on the coin receipt, and the count is CROSS-GAME: one
    dice play seeded an hour-window slot, so the flip advertises one
    fewer than a fresh player's."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    await _seed_play(registry, USER_ID, ago_seconds=200.0)  # past the cooldown
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    monkeypatch.setattr(games_handler, "_flip_side", lambda: "орёл")

    await dispatcher.feed_update(bot, _update("/flip 100 орёл"))

    body = sent[-1]["text"]
    assert "Осталось игр" in body
    assert "<b>6</b>" in body  # 8 − 1 seeded (a /roll) − this play
    assert "<b>23</b>" in body  # 25 − 1 seeded − this play


async def test_stake_rejection_card_carries_no_allowance(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal reports no spin, so it must not advertise an allowance."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=10)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/roll 1000 4"))

    assert "Осталось игр" not in (sent[-1]["text"] or "")
    assert await _balance(registry, USER_ID) == 10


# ── /dice stake alias (RR-3 #31) ──────────────────────────────────────


async def test_dice_alias_plays_the_roll_stake(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy's ``/dice <bet> <guess>`` (bot.py:21129) matched nothing in
    the port — ``dice`` was not among ``/roll``'s aliases, so the stake
    child never saw it. It now settles exactly like ``/roll``."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=4)

    result = await dispatcher.feed_update(bot, _update("/dice 100 4"))

    assert result is not UNHANDLED
    assert [s["kind"] for s in sent] == ["dice", "text"]
    assert "УГАДАЛ!" in sent[-1]["text"]
    assert await _balance(registry, USER_ID) == 1_470  # −100 +570, ×5.7 gross
    [row] = await _game_rows(registry, USER_ID)
    assert (row.game, row.win, row.bet) == ("dice", True, 100)


# ── #222-B: the stamp commits before the lock is handed over ─────────


async def _parking_card(
    monkeypatch: pytest.MonkeyPatch,
    entered: asyncio.Event,
    release: asyncio.Event,
    counter: list[int],
) -> None:
    """Replace ``post_game_card`` with one that parks the FIRST caller.

    The card is sent outside the per-user lock, and in production that
    send is an HTTPS round-trip to Telegram. Parking it there is what
    holds open the window #222-B closes — see the two tests below.
    Only the first call parks: if the bug is live the second update
    reaches its own card too, and parking that one as well would
    deadlock the test instead of failing it.
    """

    async def _card(*args: Any, **kwargs: Any) -> None:
        counter.append(1)
        if len(counter) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(games_handler, "post_game_card", _card)


async def test_roll_second_stake_blocked_while_the_first_card_is_sending(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record`` is a bare ``session.add`` and ``SessionMiddleware``
    commits only after the handler returns — which is after the card is
    sent. Without the checkpoint inside the lock the stamp stays
    invisible to every other connection for the whole length of that
    Telegram call, and a second ``/roll`` fired into the window reads
    ``game_plays`` without the row and settles too (#222-B).
    """
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=2)
    entered = asyncio.Event()
    release = asyncio.Event()
    cards: list[int] = []
    await _parking_card(monkeypatch, entered, release, cards)

    first = asyncio.create_task(dispatcher.feed_update(bot, _update("/roll 100 4")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    # The lock is already back — the card is sent outside it — so the
    # whole second update runs against a first one that has committed
    # nothing yet unless the handler committed it itself.
    assert len(games_handler._stake_locks) == 0
    await dispatcher.feed_update(bot, _update("/roll 100 4"))
    release.set()
    await first

    assert len(await _play_stamps(registry, USER_ID)) == 1
    assert len(await _game_rows(registry, USER_ID)) == 1
    assert await _balance(registry, USER_ID) == 900
    assert sum("Подожди" in (s.get("text") or "") for s in sent) == 1, sent
    assert cards == [1]


async def test_flip_second_stake_blocked_while_the_first_card_is_sending(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same window, same close, on ``/flip``'s own checkpoint (#222-B)."""
    bot, dispatcher, registry = await _wire(make_wired)
    await _seed_wallet(registry, USER_ID, balance=500)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    monkeypatch.setattr(games_handler, "_flip_side", lambda: "решка")
    entered = asyncio.Event()
    release = asyncio.Event()
    cards: list[int] = []
    await _parking_card(monkeypatch, entered, release, cards)

    first = asyncio.create_task(dispatcher.feed_update(bot, _update("/flip 50 орёл")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert len(games_handler._stake_locks) == 0
    await dispatcher.feed_update(bot, _update("/flip 50 орёл"))
    release.set()
    await first

    assert len(await _play_stamps(registry, USER_ID)) == 1
    assert len(await _game_rows(registry, USER_ID)) == 1
    assert await _balance(registry, USER_ID) == 450
    assert sum("Подожди" in (s.get("text") or "") for s in sent) == 1, sent
    assert cards == [1]
