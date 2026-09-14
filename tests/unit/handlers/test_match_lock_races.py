"""Concurrency regressions for the /cpc and /duel per-match lock (#126).

Three collisions used to be losable races, and all three are cheap to
reproduce once the storage is made to suspend where the real one does:

* **accept vs decline** — both read ``awaiting_acceptance``, then one
  flips the match into the play stage while the other clears it. The
  loser's card kept working against an FSM the winner had already
  retired.
* **accept vs deadline** — the sweeper decided to expire a match, did
  its Telegram I/O, and cleared state that an accept had meanwhile
  flipped to the play stage. Both seats ended up holding move
  keyboards for a match that no longer existed.
* **the registry slot itself** — popping the per-match
  :class:`asyncio.Lock` while the match is still live lets the next
  click construct a *second* lock beside the one an older waiter is
  holding, which is the same as having no lock at all. Keeping it
  forever instead is the other failure: one dead ``asyncio.Lock`` per
  tap on an ancient card, for the life of the process. The registry is
  a refcounted
  :class:`~telegram_invite_bot.utils.keyed_locks.KeyedLocks`, which
  cannot do either — but "cannot by construction" is a claim about the
  wiring, so the tests below still exercise it through the handlers.

Why :class:`SlowStorage`: aiogram's :class:`MemoryStorage` is a plain
dict behind ``async def``, so it never actually suspends. Both flows
read the state, then await ``get_data`` before mutating anything — in
production that gap is a real await, which is exactly where the second
click slips in. Without a suspension point the unlocked version of
these tests would pass, and they'd be worthless as regression guards.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.fsm.duel import DuelStates
from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.handlers import duel as duel_mod
from telegram_invite_bot.handlers import rps as rps_mod
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.game_limits_repo import GameLimitsRepo
from telegram_invite_bot.scheduler.fsm_sweeper import (
    STATE_ENTERED_AT_FIELD,
    FsmTimeoutSweeper,
    TimeoutRule,
)
from telegram_invite_bot.services.game_limit_service import GameLimitService

BOT_ID = 42
GROUP_CHAT_ID = -1001
CHALLENGER_ID = 100
OPPONENT_ID = 200
BET = 50


# ── Fakes ────────────────────────────────────────────────────────────


class SlowStorage(MemoryStorage):
    """:class:`MemoryStorage` with a guaranteed suspension in ``get_data``.

    One ``sleep(0)`` per read is enough to make the interleave
    deterministic: the first task parks mid-decision, the second runs
    up to its own park point, and from there only the lock can keep
    them from both acting on the same snapshot.
    """

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        await asyncio.sleep(0)
        return await super().get_data(key)


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class FakeBot:
    """Bot stub: enough surface for both accept cores' post-lock I/O."""

    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_kw: Any) -> FakeMessage:
        self.sent.append((chat_id, text))
        return FakeMessage(999)

    async def edit_message_reply_markup(self, **_kw: Any) -> None:
        return None


class Seat:
    """Closure bundle for one clicker, recording what the core did to it."""

    def __init__(self, name: str, events: list[str]) -> None:
        self._name = name
        self._events = events
        self.rejected: str | None = None
        self.acked = False

    async def reject(self, text: str) -> None:
        self.rejected = text
        self._events.append(f"{self._name}:reject")

    async def ack(self) -> None:
        self.acked = True
        self._events.append(f"{self._name}:ack")

    async def edit_card(self, _text: str, _keyboard: Any = None) -> int | None:
        self._events.append(f"{self._name}:card")
        return 777


@pytest.fixture(autouse=True)
def _no_leaked_lock_slots() -> Any:
    """Both registries are module-global AND self-freeing.

    So rather than scrubbing them between tests, assert they came back
    empty: every test in this file then doubles as a leak check on the
    handler path it drives, and a test that somehow strands a slot is
    the one that fails instead of whatever ran next.
    """
    assert len(rps_mod._match_locks) == 0
    assert len(duel_mod._match_locks) == 0
    yield
    assert len(rps_mod._match_locks) == 0, "rps left a lock slot behind"
    assert len(duel_mod._match_locks) == 0, "duel left a lock slot behind"


@pytest.fixture
async def game_limits(tmp_path: Path) -> AsyncIterator[GameLimitService]:
    """A real :class:`GameLimitService` over a throwaway ``economy.db``.

    #1664 made the accept cores charge the acceptor a slot of the
    shared per-user game budget, so they take the service as a required
    argument now. An always-allow stub would have kept this file's
    shape, but it would also have made these — the only tests that run
    the cores concurrently — the one place where the new gate is not
    really wired, and a half-wired gate hides precisely under
    concurrency. The table starts empty, so every accept below still
    passes the caps on its own merits.

    ``record`` is a bare ``session.add`` (#222-B) and the cores are
    called with no ``checkpoint``, so nothing here commits; the file
    stays a storage-level test that happens to own a real session.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield GameLimitService(GameLimitsRepo(session))
    await engine.dispose()


def _key() -> StorageKey:
    return StorageKey(bot_id=BOT_ID, chat_id=GROUP_CHAT_ID, user_id=CHALLENGER_ID)


async def _seed(storage: MemoryStorage, *, state: str, age_seconds: int = 0) -> None:
    """Put one live match into storage, optionally already stale."""
    stamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    await storage.set_state(_key(), state)
    await storage.set_data(
        _key(),
        {
            "opponent_id": OPPONENT_ID,
            "bet": BET,
            "max_wins": 1,
            "lang": "ru",
            "challenger_lang": "ru",
            STATE_ENTERED_AT_FIELD: stamp,
        },
    )


# ── accept vs decline ────────────────────────────────────────────────


async def test_duel_accept_and_decline_cannot_both_win(
    game_limits: GameLimitService,
) -> None:
    """One click resolves the match; the other is told it's gone."""
    storage = SlowStorage()
    bot: Any = FakeBot()
    await _seed(storage, state=DuelStates.awaiting_acceptance.state or "")
    events: list[str] = []
    accepting = Seat("accept", events)
    declining = Seat("decline", events)

    await asyncio.gather(
        duel_mod.accept_duel_challenge(
            bot=bot,
            fsm_storage=storage,
            lang="ru",
            chat_id=GROUP_CHAT_ID,
            challenger_id=CHALLENGER_ID,
            acceptor_id=OPPONENT_ID,
            payload_bet=BET,
            game_limit_service=game_limits,
            reject=accepting.reject,
            ack=accepting.ack,
            edit_card=accepting.edit_card,
        ),
        duel_mod.decline_duel_challenge(
            bot=bot,
            fsm_storage=storage,
            lang="ru",
            chat_id=GROUP_CHAT_ID,
            challenger_id=CHALLENGER_ID,
            decliner_id=OPPONENT_ID,
            reject=declining.reject,
            ack=declining.ack,
            edit_card=declining.edit_card,
        ),
    )

    assert [accepting.acked, declining.acked].count(True) == 1
    loser = declining if accepting.acked else accepting
    assert loser.rejected == t("h_duel_match_not_found", "ru")
    # And the FSM agrees with whoever was acked.
    state = await storage.get_state(_key())
    expected = DuelStates.awaiting_rolls.state if accepting.acked else None
    assert state == expected


async def test_rps_accept_and_decline_cannot_both_win(
    game_limits: GameLimitService,
) -> None:
    storage = SlowStorage()
    bot: Any = FakeBot()
    await _seed(storage, state=RpsStates.awaiting_acceptance.state or "")
    events: list[str] = []
    accepting = Seat("accept", events)
    declining = Seat("decline", events)

    await asyncio.gather(
        rps_mod.accept_rps_challenge(
            bot=bot,
            fsm_storage=storage,
            lang="ru",
            chat_id=GROUP_CHAT_ID,
            challenger_id=CHALLENGER_ID,
            acceptor_id=OPPONENT_ID,
            payload_bet=BET,
            game_limit_service=game_limits,
            reject=accepting.reject,
            ack=accepting.ack,
            edit_opponent_card=accepting.edit_card,
        ),
        rps_mod.decline_rps_challenge(
            bot=bot,
            fsm_storage=storage,
            lang="ru",
            chat_id=GROUP_CHAT_ID,
            challenger_id=CHALLENGER_ID,
            decliner_id=OPPONENT_ID,
            reject=declining.reject,
            ack=declining.ack,
            edit_opponent_card=declining.edit_card,
        ),
    )

    assert [accepting.acked, declining.acked].count(True) == 1
    loser = declining if accepting.acked else accepting
    assert loser.rejected == t("h_rps_match_not_found", "ru")
    state = await storage.get_state(_key())
    expected = RpsStates.awaiting_moves.state if accepting.acked else None
    assert state == expected


async def test_declined_duel_drops_its_registry_slot() -> None:
    """A declined match must not leave its lock behind.

    The drop is nobody's explicit job any more — the last user out of
    the CM frees the slot — so this pins that the decline path really
    does leave the registry through that exit.
    """
    storage = SlowStorage()
    bot: Any = FakeBot()
    await _seed(storage, state=DuelStates.awaiting_acceptance.state or "")
    seat = Seat("decline", [])

    await duel_mod.decline_duel_challenge(
        bot=bot,
        fsm_storage=storage,
        lang="ru",
        chat_id=GROUP_CHAT_ID,
        challenger_id=CHALLENGER_ID,
        decliner_id=OPPONENT_ID,
        reject=seat.reject,
        ack=seat.ack,
        edit_card=seat.edit_card,
    )

    assert seat.acked
    assert len(duel_mod._match_locks) == 0


async def test_a_rejected_click_shares_the_live_matchs_lock() -> None:
    """The flip side, and the one the refcount exists for.

    A decline from the WRONG user returns early with the match still
    alive. If its arrival could be served by a *second* Lock — which is
    what popping a live match's slot causes — it would run beside
    whoever is inside the critical section, and the lock would be
    decoration. Here a holder is parked inside the CM while the click
    lands: it must queue behind, and the registry must never hold two
    slots for one match.
    """
    storage = SlowStorage()
    bot: Any = FakeBot()
    await _seed(storage, state=DuelStates.awaiting_acceptance.state or "")
    seat = Seat("decline", [])
    events: list[str] = []
    holder_may_exit = asyncio.Event()
    widths: list[int] = []

    async def _holder() -> None:
        async with duel_mod._match_lock_cm(
            bot_id=BOT_ID, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
        ):
            events.append("holder:in")
            await holder_may_exit.wait()
            widths.append(len(duel_mod._match_locks))
            events.append("holder:out")

    async def _clicker() -> None:
        await asyncio.sleep(0)  # let the holder get inside first
        holder_may_exit.set()
        await duel_mod.decline_duel_challenge(
            bot=bot,
            fsm_storage=storage,
            lang="ru",
            chat_id=GROUP_CHAT_ID,
            challenger_id=CHALLENGER_ID,
            decliner_id=OPPONENT_ID + 1,
            reject=seat.reject,
            ack=seat.ack,
            edit_card=seat.edit_card,
        )
        events.append("clicker:done")

    await asyncio.gather(_holder(), _clicker())

    assert events == ["holder:in", "holder:out", "clicker:done"]
    assert widths == [1], "a second slot appeared beside the live match"
    assert seat.rejected == t("h_duel_not_your_match", "ru")
    assert await storage.get_state(_key()) == DuelStates.awaiting_acceptance.state


# ── accept vs the deadline ───────────────────────────────────────────


def _duel_acceptance_rule(expired: list[StorageKey]) -> dict[Any, TimeoutRule]:
    """A rule wired to the REAL /duel guard, with a recording callback.

    The notification half of ``on_expire_duel_acceptance`` is covered
    elsewhere; what matters here is that the guard is the handler
    module's own, so the sweeper contends for the very lock the accept
    path takes.
    """

    async def _on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        expired.append(key)

    return {
        DuelStates.awaiting_acceptance: TimeoutRule(
            timeout_seconds=300,
            on_expire=_on_expire,
            guard=duel_mod.expiry_guard,
        )
    }


async def _yield_until_locked(registry: Any) -> None:
    """Hand the loop to the other tasks until ``registry`` holds a slot.

    A slot exists only between the lock CM's entry and its exit, and
    the accept core's first suspension after entering is the ``get_data``
    read — so the moment this returns, the accept is inside the lock and
    has not yet flipped the state.

    ``asyncio.gather`` used to arrange that on its own, because both
    sides did nothing but in-memory storage work. #1664 put three real
    SQLite reads (the shared-budget check) in front of the lock, so a
    bare gather now hands the lock to whichever task finishes its I/O
    first. Which one wins is not what the caller is testing — it is
    testing what the guard does once the accept HAS won — so the
    interleave is pinned here rather than left to the scheduler.
    """
    for _ in range(10_000):
        if registry:
            return
        await asyncio.sleep(0)
    raise AssertionError("the accept never entered the match lock")


async def test_accept_landing_on_the_deadline_beats_the_sweeper(
    game_limits: GameLimitService,
) -> None:
    """The headline #126 bug: the sweeper wiping an accepted match.

    The accept takes the match lock first, so the sweeper's re-read
    under the guard sees ``awaiting_rolls`` and abandons the expiry —
    reported as ``raced``, not ``expired``. Before the guard existed it
    cleared the state regardless and both seats were left clicking a
    match that no longer existed.

    The accept is started alone and run up to the point where it holds
    the lock before the sweeper is let loose, so it is provably the
    holder when the sweeper's guard asks for it. See
    :func:`_yield_until_locked`.
    """
    storage = SlowStorage()
    bot: Any = FakeBot()
    await _seed(storage, state=DuelStates.awaiting_acceptance.state or "", age_seconds=600)
    expired: list[StorageKey] = []
    sweeper = FsmTimeoutSweeper(storage, bot, rules=_duel_acceptance_rule(expired))
    seat = Seat("accept", [])

    accepting = asyncio.create_task(
        duel_mod.accept_duel_challenge(
            bot=bot,
            fsm_storage=storage,
            lang="ru",
            chat_id=GROUP_CHAT_ID,
            challenger_id=CHALLENGER_ID,
            acceptor_id=OPPONENT_ID,
            payload_bet=BET,
            game_limit_service=game_limits,
            reject=seat.reject,
            ack=seat.ack,
            edit_card=seat.edit_card,
        )
    )
    await _yield_until_locked(duel_mod._match_locks)

    report = await sweeper.sweep_once()
    await accepting

    assert seat.acked
    assert expired == []
    assert (report.expired, report.raced) == (0, 1)
    assert await storage.get_state(_key()) == DuelStates.awaiting_rolls.state
    data = await storage.get_data(_key())
    assert data["opponent_id"] == OPPONENT_ID
    assert data["bet"] == BET


async def test_a_click_arriving_after_the_expiry_finds_nothing(
    game_limits: GameLimitService,
) -> None:
    """The other resolution order, forced sequentially.

    Once the sweep has run to completion the state is empty and the
    registry slot is gone; a late accept must be told the truth rather
    than resurrect the match — and must not strand a lock for it.
    """
    storage = SlowStorage()
    bot: Any = FakeBot()
    await _seed(storage, state=DuelStates.awaiting_acceptance.state or "", age_seconds=600)
    expired: list[StorageKey] = []
    sweeper = FsmTimeoutSweeper(storage, bot, rules=_duel_acceptance_rule(expired))

    report = await sweeper.sweep_once()

    assert expired == [_key()]
    assert (report.expired, report.raced) == (1, 0)
    assert len(duel_mod._match_locks) == 0

    seat = Seat("accept", [])
    await duel_mod.accept_duel_challenge(
        bot=bot,
        fsm_storage=storage,
        lang="ru",
        chat_id=GROUP_CHAT_ID,
        challenger_id=CHALLENGER_ID,
        acceptor_id=OPPONENT_ID,
        payload_bet=BET,
        game_limit_service=game_limits,
        reject=seat.reject,
        ack=seat.ack,
        edit_card=seat.edit_card,
    )

    assert not seat.acked
    assert seat.rejected == t("h_duel_match_not_found", "ru")
    assert await storage.get_state(_key()) is None
    assert len(duel_mod._match_locks) == 0


# ── the registry must not grow on stale-card taps ────────────────────


@pytest.mark.parametrize("module_name", ["rps", "duel"])
async def test_a_tap_on_a_dead_card_leaves_no_orphan_lock(
    module_name: str, game_limits: GameLimitService
) -> None:
    """Every entry into the lock CM materialises a registry slot, and a
    tap on an ancient card reaches no terminal branch that could have
    been given the job of dropping it. Under a hand-managed registry
    that is one dict entry per stale tap, never freed (#73's growth
    class); under the refcount the exit frees it.
    """
    storage = SlowStorage()
    bot: Any = FakeBot()
    seat = Seat("accept", [])
    kwargs: dict[str, Any] = {
        "bot": bot,
        "fsm_storage": storage,
        "lang": "ru",
        "chat_id": GROUP_CHAT_ID,
        "challenger_id": CHALLENGER_ID,
        "acceptor_id": OPPONENT_ID,
        "payload_bet": BET,
        "game_limit_service": game_limits,
        "reject": seat.reject,
        "ack": seat.ack,
    }
    if module_name == "duel":
        await duel_mod.accept_duel_challenge(edit_card=seat.edit_card, **kwargs)
        registry: Any = duel_mod._match_locks
        expected_toast = t("h_duel_match_not_found", "ru")
    else:
        await rps_mod.accept_rps_challenge(edit_opponent_card=seat.edit_card, **kwargs)
        registry = rps_mod._match_locks
        expected_toast = t("h_rps_match_not_found", "ru")

    assert seat.rejected == expected_toast
    assert len(registry) == 0


@pytest.mark.parametrize("module_name", ["rps", "duel"])
async def test_a_rejected_click_on_a_live_match_frees_its_slot_too(
    module_name: str, game_limits: GameLimitService
) -> None:
    """The boundary the hand-managed version could not cross.

    A key sitting in some OTHER state is a live match, so the old
    reclaim helper had to refuse to drop the slot here — and therefore
    left one behind for every stale tap arriving mid-match. Refcounting
    has no such dilemma: this caller is the last one out, so the slot
    goes, and a genuinely concurrent click would have kept it (see
    :func:`test_a_rejected_click_shares_the_live_matchs_lock`).
    """
    storage = SlowStorage()
    bot: Any = FakeBot()
    seat = Seat("accept", [])
    kwargs: dict[str, Any] = {
        "bot": bot,
        "fsm_storage": storage,
        "lang": "ru",
        "chat_id": GROUP_CHAT_ID,
        "challenger_id": CHALLENGER_ID,
        "acceptor_id": OPPONENT_ID,
        "payload_bet": BET,
        "game_limit_service": game_limits,
        "reject": seat.reject,
        "ack": seat.ack,
    }
    if module_name == "duel":
        # Already past acceptance — a second ✅ Accept tap on the old card.
        await _seed(storage, state=DuelStates.awaiting_rolls.state or "")
        await duel_mod.accept_duel_challenge(edit_card=seat.edit_card, **kwargs)
        registry: Any = duel_mod._match_locks
    else:
        await _seed(storage, state=RpsStates.awaiting_moves.state or "")
        await rps_mod.accept_rps_challenge(edit_opponent_card=seat.edit_card, **kwargs)
        registry = rps_mod._match_locks

    assert not seat.acked
    assert len(registry) == 0
    assert await storage.get_state(_key()) is not None
