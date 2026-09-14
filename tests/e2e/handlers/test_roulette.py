"""End-to-end ``/roulette`` flow (A-10) — RUSSIAN ROULETTE.

Single-player 6-chamber spin over ``economy.users.balance``. Group-only
(in-handler gate, mirroring /chatstats + legacy ``require_group=True``).

The matrix pins:

* WIN (rng → shot 4): net +0.89×bet; balance line rendered.
* LOSE (rng → shot 1): net −bet; balance line rendered.
* Insufficient balance → refusal with current balance, no debit.
* Bet below MIN_BET / above MAX_BET → min/max error, no debit.
* Non-int / missing arg → usage/invalid-bet, no debit.
* Private chat → localised group-only refusal (in-handler).
* Cooldown blocks an immediate 2nd play.
* Hourly cap: the 9th play in an hour is blocked.
* Daily cap: the 26th play in a day is blocked.
* RR-3 #34: a SUCCESS card carries the remaining hour/day
  allowance, switches to the hour-done / day-done copy on the last
  play of each window, and blocked/rejected cards carry none.

RNG is injected via ``handlers.roulette._rng``. The anti-abuse caps are
now PERSISTENT (L-25): the handler consults the DI-injected
``GameLimitService`` over ``economy.game_plays`` and uses an internal
``datetime.now()`` clock that is NOT injectable. So instead of advancing
a fake limiter clock, the cap tests SEED ``game_plays`` rows at
controlled past ``played_at`` timestamps to push the user up to (or past)
a cap boundary, then issue a single real ``/roulette`` and assert the
cap fires. Coverage stays equivalent to the old fake-clock matrix.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.game_limits import GamePlay
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import roulette as roulette_handler
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


GROUP_CHAT_ID = -1001
USER_ID = 100


async def _seed_wallet(registry: Any, user_id: int, *, balance: int = 1_000) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _seed_plays(
    registry: Any,
    user_id: int,
    *,
    ago_seconds: list[float],
    game: str = "roulette",
) -> None:
    """Insert ``game_plays`` stamps at ``now - ago_seconds[i]`` each.

    Replaces the old fake-clock limiter seeding: to push a user toward a
    cap we materialise the *completed-play* history the persistent
    ``GameLimitService`` reads. ``played_at`` is naive local time
    (``datetime.now()``), matching the handler + repo convention.
    """
    now = datetime.now()  # noqa: DTZ005 — naive local, matches game_plays
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for secs in ago_seconds:
            session.add(
                GamePlay(
                    user_id=user_id,
                    game=game,
                    played_at=now - timedelta(seconds=secs),
                )
            )
        await session.commit()


async def _play_count(registry: Any, user_id: int) -> int:
    from sqlalchemy import func, select

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(GamePlay).where(GamePlay.user_id == user_id)
            )
        ).scalar()
    return int(total or 0)


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _ledger(registry: Any, user_id: int) -> list[tuple[str, int, int | None, int | None]]:
    """``(type, amount, from_id, to_id)`` for every row touching the user."""
    from sqlalchemy import select

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (await session.execute(select(Transaction))).scalars().all()
    return [(r.type, r.amount, r.from_id, r.to_id) for r in rows if user_id in (r.from_id, r.to_id)]


def _roulette_message(
    text: str,
    *,
    user_id: int = USER_ID,
    chat_type: str = "supergroup",
    chat_id: int = GROUP_CHAT_ID,
) -> Any:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        first_name="R",
        language_code="ru",
    )


class _FakeRng:
    """Module-level RNG stand-in for a deterministic shot."""

    def __init__(self, sequence: list[int]) -> None:
        self._values = list(sequence)
        self._idx = 0

    def randint(self, lo: int, hi: int) -> int:  # noqa: ARG002
        value = self._values[self._idx]
        self._idx += 1
        return value


# ── Win / lose economy ───────────────────────────────────────────────


async def test_roulette_win_credits_payout_and_balance_line(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Shot 4 → WIN. Stake 100 debited, win int(round(100*1.89))=189
    credited. Net +89 → 500→589. Balance line present.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([4]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    assert await _balance(registry, USER_ID) == 589
    text = sent[-1]["text"]
    assert "ПОВЕЗЛО" in text
    assert "189" in text
    assert "Баланс: 589" in text

    # #225: both halves booked. The stake names no counterparty (the
    # house is not a wallet) and the payout names no payer — the shape
    # /duel has always written.
    assert await _ledger(registry, USER_ID) == [
        ("roulette_stake", 100, USER_ID, None),
        ("roulette_win", 189, None, USER_ID),
    ]


async def test_roulette_lose_keeps_only_debit_and_balance_line(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Shot 1 → LOSE. Stake 100 debited, nothing credited. Net −100 →
    500→400. Balance line present.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    assert await _balance(registry, USER_ID) == 400
    text = sent[-1]["text"]
    assert "БАХ" in text
    assert "Баланс: 400" in text
    assert await _ledger(registry, USER_ID) == [("roulette_stake", 100, USER_ID, None)]


# ── Validation rejections (no debit) ─────────────────────────────────


async def test_roulette_insufficient_balance_no_debit(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=50)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([4]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 500"))

    assert await _balance(registry, USER_ID) == 50
    assert "недостаточно" in (sent[-1]["text"] or "").lower()


async def test_roulette_below_min_bet(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _roulette_message("/roulette 5"))

    assert await _balance(registry, USER_ID) == 500
    assert "Минимальная" in (sent[-1]["text"] or "")


async def test_roulette_above_max_bet(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=50_000)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _roulette_message("/roulette 20000"))

    assert await _balance(registry, USER_ID) == 50_000
    assert "Максимальная" in (sent[-1]["text"] or "")


async def test_roulette_non_int_arg_renders_invalid(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _roulette_message("/roulette abc"))

    assert await _balance(registry, USER_ID) == 500
    assert "число" in (sent[-1]["text"] or "").lower()


async def test_roulette_missing_arg_renders_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _roulette_message("/roulette"))

    assert await _balance(registry, USER_ID) == 500
    assert "РУССКАЯ РУЛЕТКА" in (sent[-1]["text"] or "")


async def test_roulette_private_chat_refused_in_handler(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Private invocation gets the localised group-only refusal (the
    handler still runs — the gate is in-handler, NOT a router filter).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, _roulette_message("/roulette 100", chat_type="private", chat_id=USER_ID)
    )

    assert await _balance(registry, USER_ID) == 500
    assert "только в групповых" in (sent[-1]["text"] or "")


async def test_roulette_records_game_and_bumps_counters(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A completed spin writes a GameResult row + bumps games_played/won
    (A-11), committed atomically with the wallet by the middleware."""
    from sqlalchemy import select

    from telegram_invite_bot.db.models.economy import EconomyUser, GameResult

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([4]))  # win

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, USER_ID)
        assert user is not None
        assert user.games_played == 1
        assert user.games_won == 1
        rows = (
            (await session.execute(select(GameResult).where(GameResult.user_id == USER_ID)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].game == "roulette"
    assert rows[0].win is True
    assert rows[0].profit == 89  # int(round(100*1.89)) - 100


async def test_roulette_success_records_game_play_stamp(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """L-25: a SUCCESS spin appends exactly one ``game_plays`` stamp
    (game='roulette'), committed atomically with the wallet by the
    middleware — this is what the persistent anti-abuse caps count."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    from sqlalchemy import select

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(GamePlay).where(GamePlay.user_id == USER_ID)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].game == "roulette"


async def test_roulette_payout_credit_overflow_refunds_the_stake(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """#1560: a win payout that overflows the balance cap makes
    ``credit`` return None, and the service now raises rather than
    announcing a win it could not pay. The caller's transaction rolls
    the stake debit back with it, so the player ends where they began —
    the previous behaviour left them one stake down while the card
    congratulated them on the payout."""
    cap = 10**15
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=cap)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([4]))  # win

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    # The service raised; the global error catcher turned that into a
    # toast, and the session middleware rolled the whole play back on
    # the way out. Nothing about the play survives: not the stake
    # debit, not the ledger row, not the anti-abuse stamp.
    assert await _balance(registry, USER_ID) == cap
    assert await _ledger(registry, USER_ID) == []
    assert await _play_count(registry, USER_ID) == 0
    assert "ПОВЕЗЛО" not in sent[-1]["text"]


# ── Anti-abuse caps (persistent, L-25) ───────────────────────────────


async def test_roulette_cooldown_blocks_immediate_second_play(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A play within COOLDOWN_SEC of the last completed play is blocked →
    no debit. We seed a completed play 10s ago (well under the 180s
    cooldown) and assert the next /roulette is refused."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=1_000)
    await _seed_plays(registry, USER_ID, ago_seconds=[10.0])  # within cooldown
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    # Blocked — no debit; cooldown message shown; no new play stamped.
    assert await _balance(registry, USER_ID) == 1_000
    assert "Подожди" in (sent[-1]["text"] or "")
    assert await _play_count(registry, USER_ID) == 1  # only the seeded stamp


async def test_roulette_no_prior_play_is_allowed(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A fresh user (no game_plays history) passes every cap and plays —
    the spin settles and a stamp is recorded."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    assert await _balance(registry, USER_ID) == 400  # 500 − 100 (loss)
    assert "БАХ" in (sent[-1]["text"] or "")
    assert await _play_count(registry, USER_ID) == 1


async def test_roulette_rejected_bet_does_not_record_a_play(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A rejected attempt (below-min bet) must NOT stamp a play: an
    immediately-following valid play still goes through (it isn't
    cooldown-blocked by a phantom stamp). This pins the check/record
    split — only completed spins count.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    # Rejected (below MIN_BET) — no debit, and must not stamp a play.
    await dispatcher.feed_update(bot, _roulette_message("/roulette 5"))
    assert await _balance(registry, USER_ID) == 500
    assert "Минимальная" in (sent[-1]["text"] or "")
    assert await _play_count(registry, USER_ID) == 0

    # Immediate valid play succeeds (would be cooldown-blocked if the
    # rejected attempt had recorded a stamp).
    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))
    assert await _balance(registry, USER_ID) == 400  # 500 − 100 (loss)
    assert "БАХ" in (sent[-1]["text"] or "")
    assert await _play_count(registry, USER_ID) == 1


async def test_roulette_hourly_cap_blocks_ninth_play(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """MAX_PER_HOUR (8) plays allowed per rolling hour; the 9th is
    blocked. Seed 8 completed plays inside the last hour but all older
    than the 180s cooldown (so cooldown doesn't fire first), then assert
    the next /roulette hits the hourly cap with no debit.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=10_000)
    # 8 stamps spread 200s..3400s ago: all < 3600s (in-hour) and the
    # most recent (200s) is > 180s (past cooldown).
    await _seed_plays(
        registry,
        USER_ID,
        ago_seconds=[200.0, 600.0, 1000.0, 1400.0, 1800.0, 2200.0, 2600.0, 3000.0],
    )
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    # 9th within the hour → blocked, no debit, no new stamp.
    assert await _balance(registry, USER_ID) == 10_000
    assert "в час" in (sent[-1]["text"] or "")
    assert await _play_count(registry, USER_ID) == 8


async def test_roulette_daily_cap_blocks_twenty_sixth_play(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """MAX_PER_DAY (25) plays allowed per rolling day; the 26th is
    blocked. Seed 25 completed plays inside the last day, spaced so that
    no rolling hour holds 8 (sparse) and the most recent is past the
    cooldown — so neither cooldown nor the hourly cap fires first, and
    the 26th trips the daily cap.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=100_000)
    # 25 stamps spaced 3000s apart starting 3000s ago → newest 3000s ago
    # (past cooldown), span 75000s < 86400s (all in-day), and any rolling
    # hour window holds at most 2 (3600/3000) — well under MAX_PER_HOUR.
    await _seed_plays(
        registry,
        USER_ID,
        ago_seconds=[3000.0 * (i + 1) for i in range(25)],
    )
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    # 26th within the day → blocked, no debit, no new stamp.
    assert await _balance(registry, USER_ID) == 100_000
    assert "в сутки" in (sent[-1]["text"] or "")
    assert await _play_count(registry, USER_ID) == 25


async def test_roulette_concurrent_same_user_serialized_by_lock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Two spins fired at the same instant by one player settle as one.

    Nothing is seeded here, and that is the whole point (#273). The
    version this replaces seeded a completed play 10 s ago, which put
    *both* updates inside the 180 s cooldown before either one reached
    the lock: ``GameLimitService.check`` short-circuits on cooldown, so
    the test passed just as happily with no lock at all, and had been
    passing for whatever the lock did or did not do ever since.

    With a clean history the question is the real one — the second
    update reaches ``check`` for real instead of being waved off by a
    seeded cooldown, so the lock has to hold it back while the first is
    still between ``check`` and ``record`` (#222-A). Drop the lock and
    this goes to two spins.

    What it does NOT pin is the commit half (#222-B). Both updates land
    in one ``gather``, which puts the second one's ``SELECT`` and the
    first one's ``COMMIT`` on two different aiosqlite threads a
    microsecond apart; the commit happens to win, so the stamp is
    visible either way and removing the checkpoint leaves this green.
    That half needs the send latency held open on purpose — see
    ``test_second_spin_blocked_while_the_first_card_is_still_sending``.
    """
    import asyncio

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent = capture_outgoing(bot)
    # No stale lock from a prior test's event loop. The registry used to be
    # a plain dict that had to be cleared by hand here; ``KeyedLocks`` frees
    # each slot when its last user leaves, so an empty table is the resting
    # state — assert it instead of resetting it (a reset would have hidden
    # the leak this replaced).
    assert len(roulette_handler._play_locks) == 0
    # Two shots queued, one consumed: the loser is shot 1, and the second
    # update never reaches the spin.
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1, 1]))

    await asyncio.gather(
        dispatcher.feed_update(bot, _roulette_message("/roulette 100")),
        dispatcher.feed_update(bot, _roulette_message("/roulette 100")),
    )

    # Exactly one play: one stamp, one 100 COM debit, one cooldown refusal.
    assert await _play_count(registry, USER_ID) == 1
    assert await _balance(registry, USER_ID) == 900
    texts = [s["text"] or "" for s in sent]
    assert sum("Подожди" in tx for tx in texts) == 1, texts
    # And the slot is handed back once both are done.
    assert len(roulette_handler._play_locks) == 0


async def test_second_spin_blocked_while_the_first_card_is_still_sending(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """#222-B: the stamp is committed before the lock is handed over.

    The gap this pins is the one the sibling test above cannot reach.
    ``record`` is a bare ``session.add``, and the session it lands on is
    committed by ``SessionMiddleware`` *after* the handler returns — but
    the handler does not return straight after the lock. It sends the
    result card first, and in production that is an HTTPS round-trip to
    ``api.telegram.org``. So without the checkpoint the anti-abuse stamp
    stays invisible to every other connection for as long as Telegram
    takes to answer, and a second spin fired into that window reads
    ``game_plays`` without the row and is waved through.

    Held open deterministically here: ``post_game_card`` parks the first
    update on an event, the second update runs start to finish while it
    is parked, and only then is the first released. No sleeps, no
    thread-race — with the checkpoint the second spin is refused, and
    without it both settle.
    """
    import asyncio

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=1_000)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1, 1]))

    entered = asyncio.Event()
    release = asyncio.Event()
    cards = 0

    async def _parked_card(*args: Any, **kwargs: Any) -> None:
        # Only the first card parks. If the bug is live the second update
        # reaches its own card too, and parking that one as well would
        # deadlock the test instead of failing it.
        nonlocal cards
        cards += 1
        if cards == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr(roulette_handler, "post_game_card", _parked_card)

    first = asyncio.create_task(dispatcher.feed_update(bot, _roulette_message("/roulette 100")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    # The lock is already back — the card is sent outside it — so this
    # runs the whole second update against a first one whose session
    # middleware has not committed anything yet.
    assert len(roulette_handler._play_locks) == 0
    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))
    release.set()
    await first

    assert await _play_count(registry, USER_ID) == 1
    assert await _balance(registry, USER_ID) == 900
    texts = [s["text"] or "" for s in sent]
    assert sum("Подожди" in tx for tx in texts) == 1, texts
    assert cards == 1, cards


async def test_roulette_first_play_shows_new_achievement(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A-12: the very first /roulette unlocks ``first_game`` and the
    result message carries the "🏆 Новые достижения!" block + the name."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))  # loss; still first game

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    text = sent[-1]["text"]
    assert "Новые достижения" in text
    assert "Новичок" in text  # first_game RU name


async def test_achievements_card_populates_after_a_game(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """End-to-end A-12: play /roulette, then /achievements lists the
    freshly-earned achievement (write side actually reaches the card)."""
    # /achievements needs ``user_service`` from the dispatcher-level
    # SessionMiddleware (for the caller's language) — enable it here.
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase, UsersBase], session_middleware=True
    )
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))
    await dispatcher.feed_update(bot, _roulette_message("/achievements"))

    card = sent[-1]["text"]
    assert "Новичок" in card  # earned first_game now rendered on the card


# ── Remaining-play allowance footer (RR-3 #34) ───────────────────────


async def test_roulette_success_card_shows_remaining_allowance(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A fresh player's card ends with the allowance footer, counting the
    spin that just happened: 8−0−1 = 7 left this hour, 25−0−1 = 24 today.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    text = sent[-1]["text"]
    assert "Осталось игр" in text
    assert "<b>7</b>" in text  # MAX_PER_HOUR (8) − 0 seeded − this play
    assert "<b>24</b>" in text  # MAX_PER_DAY (25) − 0 seeded − this play


async def test_roulette_last_play_of_the_hour_says_so(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Seven plays already in the window → this spin is the 8th and last
    of the hour, so the footer switches to the hour-exhausted line rather
    than promising another spin in three minutes."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    # Inside the hour, all past the 180s cooldown so the play is allowed.
    await _seed_plays(registry, USER_ID, ago_seconds=[200.0 + 60 * i for i in range(7)])
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    text = sent[-1]["text"]
    assert "БАХ" in text  # the spin really happened
    assert "Игры на этот час закончились" in text
    assert "Осталось игр" not in text


async def test_roulette_last_play_of_the_day_says_so(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """24 plays earlier today (all OUTSIDE the hour window, so the hourly
    cap stays clear) → this spin is the 25th and last of the day. The
    day line wins over the hour one."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    await _seed_plays(registry, USER_ID, ago_seconds=[3_700.0 + 60 * i for i in range(24)])
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))

    text = sent[-1]["text"]
    assert "БАХ" in text
    assert "Дневной лимит игр выбран" in text
    assert "Осталось игр" not in text


async def test_roulette_blocked_and_rejected_cards_carry_no_allowance(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """The footer belongs to a card that reports a real spin. A cooldown
    block and a below-min rejection must both stay free of it."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, USER_ID, balance=500)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(roulette_handler, "_rng", _FakeRng([1]))

    # Rejected bet (no spin, no cooldown started).
    await dispatcher.feed_update(bot, _roulette_message("/roulette 5"))
    assert "Осталось игр" not in (sent[-1]["text"] or "")

    # Real spin, then an immediate second one → cooldown-blocked.
    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))
    await dispatcher.feed_update(bot, _roulette_message("/roulette 100"))
    blocked = sent[-1]["text"] or ""
    assert "Подожди" in blocked
    assert "Осталось игр" not in blocked
