"""Integration tests for the L-02 ``/accept`` + ``/decline`` commands.

The router under test (``handlers/challenge_commands.py``) is wired
into a bespoke Dispatcher here (NOT through ``build_main_router``) so
a failure points at this router and not at its neighbours:

* no pending challenge → localized "nothing to accept/decline";
* /accept flips a pending duel to ``awaiting_rolls`` and a pending
  rps match to ``awaiting_moves`` — the SAME transitions the inline
  buttons drive (the cores are shared);
* /decline clears the FSM for both games;
* both an rps and a duel challenge pending → the MOST RECENT one (by
  ``state_entered_at``) is targeted; /accept then hits the shared
  core's M-G-2 opponent-busy guard (button-identical), while /decline
  clears the fresh one and unblocks accepting the older;
* challenges in OTHER chats / where the caller is the challenger (not
  the opponent) are invisible to the command;
* a private-chat /accept or /decline can't work (the FSM keys are
  group-scoped) and now says so instead of matching nothing (#122);
* the Russian aliases ``/принять`` / ``/отклонить`` route.

Text assertions go through ``t(key, "ru")`` rather than literal strings,
so a copy edit stays a copy edit and only a broken handler→key wiring
fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.fsm.duel import DuelStates
from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.handlers.challenge_commands import (
    _find_pending_challenges,
    build_router,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.scheduler.fsm_sweeper import (
    STATE_ENTERED_AT_FIELD,
    memory_storage_keys,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection
    from pathlib import Path

GROUP_CHAT_ID = -1002
CHALLENGER_ID = 100
OPPONENT_ID = 200
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

RPS_CARD_MSG_ID = 77
DUEL_CARD_MSG_ID = 88


@pytest.fixture
async def wired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[tuple[Bot, Dispatcher, list[dict[str, Any]]]]:
    """Bespoke (Bot, Dispatcher, outbox) trio with ONLY the L-02 router.

    ``lang`` rides in via dispatcher workflow data (prod injects it
    through the main router's LanguageMiddleware, which this isolated
    harness deliberately skips); ``fsm_storage`` is supplied by
    aiogram itself. The outbox records SendMessage and EditMessageText
    so tests can assert which card got edited where.

    The registry is a real one-engine :class:`EngineRegistry` over a
    throwaway ``economy.db``, not the mock it used to be: #1664 hung an
    ``EconomyMiddleware`` on this router so ``/accept`` pays the same
    shared game budget the inline button does, and that middleware
    opens a real session per update. An empty ``game_plays`` table
    means every accept below still passes the caps on its merits.
    """
    bot = Bot(token="42:TEST-token")
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["lang"] = "ru"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    registry = EngineRegistry(
        engines={DBName.ECONOMY: engine},
        sessions={DBName.ECONOMY: async_sessionmaker(engine, expire_on_commit=False)},
    )
    dispatcher.include_router(build_router(registry))

    outbox: list[dict[str, Any]] = []

    def _synth(chat_id: int, text: str, message_id: int = 1) -> Message:
        return Message(
            message_id=message_id,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=TelegramUser(id=42, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendMessage":
            outbox.append({"kind": "send", "chat_id": method.chat_id, "text": method.text})
            return _synth(method.chat_id, method.text, message_id=900 + len(outbox))
        if name == "EditMessageText":
            outbox.append(
                {
                    "kind": "edit",
                    "chat_id": method.chat_id,
                    "message_id": method.message_id,
                    "text": method.text,
                }
            )
            return _synth(method.chat_id, method.text, message_id=method.message_id)
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    try:
        yield bot, dispatcher, outbox
    finally:
        await bot.session.close()
        await registry.dispose()


def _key(
    bot: Bot, *, chat_id: int = GROUP_CHAT_ID, challenger_id: int = CHALLENGER_ID
) -> StorageKey:
    return StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=challenger_id)


async def _seed_rps(
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    chat_id: int = GROUP_CHAT_ID,
    challenger_id: int = CHALLENGER_ID,
    opponent_id: int = OPPONENT_ID,
    entered_at: datetime = _NOW,
) -> None:
    """Seed the exact FSM shape ``handle_cpc`` writes at challenge time."""
    key = _key(bot, chat_id=chat_id, challenger_id=challenger_id)
    await dispatcher.storage.set_state(key, RpsStates.awaiting_acceptance.state)
    await dispatcher.storage.set_data(
        key,
        {
            "opponent_id": opponent_id,
            "bet": 50,
            "challenger_chat_id": chat_id,
            "challenger_lang": "ru",
            STATE_ENTERED_AT_FIELD: entered_at.isoformat(),
            "opponent_accept_message_id": RPS_CARD_MSG_ID,
        },
    )


async def _seed_duel(
    bot: Bot,
    dispatcher: Dispatcher,
    *,
    chat_id: int = GROUP_CHAT_ID,
    challenger_id: int = CHALLENGER_ID,
    opponent_id: int = OPPONENT_ID,
    entered_at: datetime = _NOW,
) -> None:
    """Seed the exact FSM shape ``handle_duel`` writes at challenge time."""
    key = _key(bot, chat_id=chat_id, challenger_id=challenger_id)
    await dispatcher.storage.set_state(key, DuelStates.awaiting_acceptance.state)
    await dispatcher.storage.set_data(
        key,
        {
            "opponent_id": opponent_id,
            "bet": 50,
            "chat_id": chat_id,
            "lang": "ru",
            STATE_ENTERED_AT_FIELD: entered_at.isoformat(),
            "challenge_message_id": DUEL_CARD_MSG_ID,
        },
    )


def _cmd(
    text: str,
    *,
    user_id: int = OPPONENT_ID,
    chat_id: int = GROUP_CHAT_ID,
    chat_type: str = "supergroup",
) -> Any:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        first_name="O",
        language_code="ru",
    )


async def _state_name(bot: Bot, dispatcher: Dispatcher, **kw: int) -> str | None:
    return await dispatcher.storage.get_state(_key(bot, **kw))


async def _state_data(bot: Bot, dispatcher: Dispatcher, **kw: int) -> dict[str, Any]:
    return await dispatcher.storage.get_data(_key(bot, **kw))


# ── Nothing pending ──────────────────────────────────────────────────


async def test_accept_nothing_pending(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    bot, dispatcher, outbox = wired

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert outbox == [
        {
            "kind": "send",
            "chat_id": GROUP_CHAT_ID,
            "text": t("h_challenge_nothing_to_accept", "ru"),
        }
    ]


async def test_decline_nothing_pending(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    bot, dispatcher, outbox = wired

    await dispatcher.feed_update(bot, _cmd("/decline"))

    assert outbox == [
        {
            "kind": "send",
            "chat_id": GROUP_CHAT_ID,
            "text": t("h_challenge_nothing_to_decline", "ru"),
        }
    ]


async def test_pending_in_other_chat_is_invisible(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    """A challenge keyed on ANOTHER group must not be answerable here."""
    bot, dispatcher, outbox = wired
    await _seed_duel(bot, dispatcher, chat_id=-9999)

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert outbox[0]["text"] == t("h_challenge_nothing_to_accept", "ru")
    assert await _state_name(bot, dispatcher, chat_id=-9999) == DuelStates.awaiting_acceptance.state


async def test_challenger_cannot_accept_own_challenge(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    """The scan matches ``opponent_id`` — the CHALLENGER typing /accept
    sees "nothing to accept" (their own outgoing challenge is not an
    incoming one)."""
    bot, dispatcher, outbox = wired
    await _seed_duel(bot, dispatcher)

    await dispatcher.feed_update(bot, _cmd("/accept", user_id=CHALLENGER_ID))

    assert outbox[0]["text"] == t("h_challenge_nothing_to_accept", "ru")
    assert await _state_name(bot, dispatcher) == DuelStates.awaiting_acceptance.state


# ── Duel accept / decline ────────────────────────────────────────────


async def test_accept_pending_duel_flips_to_awaiting_rolls(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    bot, dispatcher, outbox = wired
    await _seed_duel(bot, dispatcher)

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert await _state_name(bot, dispatcher) == DuelStates.awaiting_rolls.state
    data = await _state_data(bot, dispatcher)
    assert data["challenger_roll"] is None
    assert data["opponent_roll"] is None
    # Confirmation reply in the group + the challenge card edited to
    # the roll keyboard (same edit the Accept button performs).
    sends = [m for m in outbox if m["kind"] == "send"]
    edits = [m for m in outbox if m["kind"] == "edit"]
    assert any(m["text"] == t("h_challenge_accept_ok", "ru") for m in sends)
    assert edits == [
        {
            "kind": "edit",
            "chat_id": GROUP_CHAT_ID,
            "message_id": DUEL_CARD_MSG_ID,
            "text": t(
                "h_duel_accepted",
                "ru",
                challenger_id=CHALLENGER_ID,
                opponent_id=OPPONENT_ID,
                bet=50,
            ),
        }
    ]


async def test_decline_pending_duel_clears_fsm(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    bot, dispatcher, outbox = wired
    await _seed_duel(bot, dispatcher)

    await dispatcher.feed_update(bot, _cmd("/decline"))

    assert await _state_name(bot, dispatcher) is None
    sends = [m for m in outbox if m["kind"] == "send"]
    edits = [m for m in outbox if m["kind"] == "edit"]
    assert any(m["text"] == t("h_challenge_decline_ok", "ru") for m in sends)
    assert edits[0]["message_id"] == DUEL_CARD_MSG_ID
    assert edits[0]["chat_id"] == GROUP_CHAT_ID


# ── RPS accept / decline ─────────────────────────────────────────────


async def test_accept_pending_rps_flips_to_awaiting_moves(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    bot, dispatcher, outbox = wired
    await _seed_rps(bot, dispatcher)

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert await _state_name(bot, dispatcher) == RpsStates.awaiting_moves.state
    data = await _state_data(bot, dispatcher)
    assert data["challenger_move"] is None
    assert data["opponent_move"] is None
    # The rps challenge card lives in the acceptor's PM — edited there
    # by the stored ``opponent_accept_message_id`` (the same card the
    # inline Accept button would edit in place).
    edits = [m for m in outbox if m["kind"] == "edit"]
    assert edits == [
        {
            "kind": "edit",
            "chat_id": OPPONENT_ID,
            "message_id": RPS_CARD_MSG_ID,
            "text": t("h_rps_choose_move", "ru"),
        }
    ]
    # Both seats got their move surface, message ids stashed for the
    # sweeper/cancel keyboard-drop.
    assert data["opponent_move_message_id"] == RPS_CARD_MSG_ID
    challenger_sends = [m for m in outbox if m["kind"] == "send" and m["chat_id"] == CHALLENGER_ID]
    assert len(challenger_sends) == 1
    assert challenger_sends[0]["text"] == t("h_rps_choose_move", "ru")
    assert data["challenger_move_message_id"] is not None


async def test_decline_pending_rps_clears_fsm_and_notifies_challenger(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    bot, dispatcher, outbox = wired
    await _seed_rps(bot, dispatcher)

    await dispatcher.feed_update(bot, _cmd("/decline"))

    assert await _state_name(bot, dispatcher) is None
    edits = [m for m in outbox if m["kind"] == "edit"]
    assert edits == [
        {
            "kind": "edit",
            "chat_id": OPPONENT_ID,
            "message_id": RPS_CARD_MSG_ID,
            "text": t("h_rps_declined_opponent", "ru"),
        }
    ]
    challenger_sends = [m for m in outbox if m["kind"] == "send" and m["chat_id"] == CHALLENGER_ID]
    assert challenger_sends == [
        {
            "kind": "send",
            "chat_id": CHALLENGER_ID,
            "text": t("h_rps_declined_challenger", "ru"),
        }
    ]


# ── Most-recent preference ───────────────────────────────────────────


async def test_both_pending_accept_targets_most_recent_and_hits_busy_guard(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    """rps (older) + duel (newer) pending → /accept targets the duel,
    and the SHARED accept core's M-G-2 opponent-busy guard rejects it
    (the OTHER pending challenge already lists the caller as opponent)
    — exactly what tapping the duel card's Accept button would do.
    Both challenges stay pending; /decline-ing one unblocks the other
    (see the follow-up test)."""
    bot, dispatcher, outbox = wired
    rps_challenger = 111
    duel_challenger = 222
    await _seed_rps(
        bot,
        dispatcher,
        challenger_id=rps_challenger,
        entered_at=_NOW - timedelta(seconds=30),
    )
    await _seed_duel(bot, dispatcher, challenger_id=duel_challenger, entered_at=_NOW)

    await dispatcher.feed_update(bot, _cmd("/accept"))

    # Most-recent preference: the DUEL core was dispatched (its busy
    # toast, not the rps one).
    assert outbox == [
        {
            "kind": "send",
            "chat_id": GROUP_CHAT_ID,
            "text": t("h_duel_already_in_game", "ru"),
        }
    ]
    assert (
        await _state_name(bot, dispatcher, challenger_id=duel_challenger)
        == DuelStates.awaiting_acceptance.state
    )
    assert (
        await _state_name(bot, dispatcher, challenger_id=rps_challenger)
        == RpsStates.awaiting_acceptance.state
    )


async def test_both_pending_accept_most_recent_rps_symmetric(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    """Symmetric case — the rps challenge is the fresher one, so the
    RPS core is dispatched and ITS busy toast renders."""
    bot, dispatcher, outbox = wired
    rps_challenger = 111
    duel_challenger = 222
    await _seed_rps(bot, dispatcher, challenger_id=rps_challenger, entered_at=_NOW)
    await _seed_duel(
        bot,
        dispatcher,
        challenger_id=duel_challenger,
        entered_at=_NOW - timedelta(seconds=30),
    )

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert outbox == [
        {
            "kind": "send",
            "chat_id": GROUP_CHAT_ID,
            "text": t("h_rps_already_in_game", "ru"),
        }
    ]
    assert (
        await _state_name(bot, dispatcher, challenger_id=rps_challenger)
        == RpsStates.awaiting_acceptance.state
    )
    assert (
        await _state_name(bot, dispatcher, challenger_id=duel_challenger)
        == DuelStates.awaiting_acceptance.state
    )


async def test_both_pending_decline_clears_most_recent_then_accept_works(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    """/decline answers the MOST RECENT challenge (the duel) — decline
    has no busy guard — and once it is cleared, /accept lands on the
    remaining rps challenge and succeeds."""
    bot, dispatcher, _outbox = wired
    rps_challenger = 111
    duel_challenger = 222
    await _seed_rps(
        bot,
        dispatcher,
        challenger_id=rps_challenger,
        entered_at=_NOW - timedelta(seconds=30),
    )
    await _seed_duel(bot, dispatcher, challenger_id=duel_challenger, entered_at=_NOW)

    await dispatcher.feed_update(bot, _cmd("/decline"))

    assert await _state_name(bot, dispatcher, challenger_id=duel_challenger) is None
    assert (
        await _state_name(bot, dispatcher, challenger_id=rps_challenger)
        == RpsStates.awaiting_acceptance.state
    )

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert (
        await _state_name(bot, dispatcher, challenger_id=rps_challenger)
        == RpsStates.awaiting_moves.state
    )


# ── Routing surface ──────────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["/accept", "/принять", "/decline", "/отклонить"])
async def test_private_chat_is_refused_out_loud(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
    alias: str,
) -> None:
    """The group-only rule is real, but the opponent has no way of
    knowing it unless we say so (#122).

    The new pipeline's FSM keys make a private ``/accept`` unresolvable
    — see the ``build_router`` docstring — so the command genuinely
    cannot work here. What changed is that it now says that instead of
    matching nothing and answering with silence.
    """
    bot, dispatcher, outbox = wired

    result = await dispatcher.feed_update(
        bot, _cmd(alias, chat_id=OPPONENT_ID, chat_type="private")
    )

    assert result is not UNHANDLED
    assert len(outbox) == 1
    assert outbox[0]["chat_id"] == OPPONENT_ID
    assert alias in outbox[0]["text"]


async def test_russian_aliases_route(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _outbox = wired
    await _seed_duel(bot, dispatcher)

    await dispatcher.feed_update(bot, _cmd("/принять"))
    assert await _state_name(bot, dispatcher) == DuelStates.awaiting_rolls.state

    # Re-arm and decline via the Russian alias.
    await dispatcher.storage.set_state(_key(bot), DuelStates.awaiting_acceptance.state)
    await dispatcher.feed_update(bot, _cmd("/отклонить"))
    assert await _state_name(bot, dispatcher) is None


async def test_accept_does_not_match_mid_acceptance_rps_moves_stage(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
) -> None:
    """A match already past acceptance (awaiting_moves) is not
    re-acceptable — same as the buttons, whose accept keyboard is gone
    by then."""
    bot, dispatcher, outbox = wired
    await _seed_rps(bot, dispatcher)
    await dispatcher.storage.set_state(_key(bot), RpsStates.awaiting_moves.state)

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert outbox[0]["text"] == t("h_challenge_nothing_to_accept", "ru")
    assert await _state_name(bot, dispatcher) == RpsStates.awaiting_moves.state


# ── #306: the card fallback has to be able to fail ───────────────────


async def test_accept_rps_survives_an_undeliverable_card(
    wired: tuple[Bot, Dispatcher, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/accept`` typed in a group must still arm the match when the
    acceptor's card can be neither edited nor re-sent.

    ``_edit_rps_card`` falls back to a PM when the edit fails, and that
    fallback caught only ``TelegramForbiddenError`` — "the user blocked
    the bot", which presupposes a PM that once existed. But the command
    path is exactly the one that does not prove a PM exists: ``/accept``
    is typed in the group, so an acceptor who has never written to the
    bot gets ``Bad Request: chat not found`` instead, and it escaped
    through the shared accept core, past the state flip it had already
    performed.

    The match is meant to continue without the card — the acceptor can
    still be reached by the group, and the sweeper owns the deadline.
    """
    bot, dispatcher, outbox = wired
    await _seed_rps(bot, dispatcher)

    async def unreachable_acceptor(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if getattr(method, "chat_id", None) == OPPONENT_ID:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        if name == "SendMessage":
            outbox.append({"kind": "send", "chat_id": method.chat_id, "text": method.text})
            return Message(
                message_id=900 + len(outbox),
                date=datetime(2026, 1, 1, tzinfo=UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=42, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", unreachable_acceptor)

    await dispatcher.feed_update(bot, _cmd("/accept"))

    assert await _state_name(bot, dispatcher) == RpsStates.awaiting_moves.state
    data = await _state_data(bot, dispatcher)
    assert data["opponent_move_message_id"] is None
    assert isinstance(data["challenger_move_message_id"], int)
    # The group got its confirmation and the challenger got a keyboard.
    assert {e["chat_id"] for e in outbox} == {GROUP_CHAT_ID, CHALLENGER_ID}


# ── #1684: the scan itself ─────────────────────────────────────────


_SCAN_BOT_ID = 42


class _BulkStorage(MemoryStorage):
    """A :class:`MemoryStorage` that also offers the #1451 bulk scan.

    Counts every call so a test can pin WHICH reads the scan makes,
    not merely that it answered correctly. ``iter_records`` reaches
    through :class:`MemoryStorage` directly because it stands in for
    one SQL statement and must not inflate the per-key counters it
    exists to remove.
    """

    def __init__(self) -> None:
        super().__init__()
        self.iter_keys_calls = 0
        self.iter_records_calls = 0
        self.get_state_calls = 0
        self.get_data_calls = 0

    async def iter_keys(self) -> list[StorageKey]:
        self.iter_keys_calls += 1
        return memory_storage_keys(self)

    async def iter_records(
        self, states: Collection[str]
    ) -> list[tuple[StorageKey, str, dict[str, Any]]]:
        self.iter_records_calls += 1
        wanted = set(states)
        found: list[tuple[StorageKey, str, dict[str, Any]]] = []
        for key in memory_storage_keys(self):
            state = await MemoryStorage.get_state(self, key)
            if state is not None and state in wanted:
                found.append((key, state, await MemoryStorage.get_data(self, key)))
        return found

    async def get_state(self, key: StorageKey) -> str | None:
        self.get_state_calls += 1
        return await super().get_state(key)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        self.get_data_calls += 1
        return await super().get_data(key)


async def _seed_scan_key(
    storage: MemoryStorage,
    *,
    chat_id: int = GROUP_CHAT_ID,
    challenger_id: int = CHALLENGER_ID,
    bot_id: int = _SCAN_BOT_ID,
    state: str | None,
    opponent_id: int = OPPONENT_ID,
) -> StorageKey:
    key = StorageKey(bot_id=bot_id, chat_id=chat_id, user_id=challenger_id)
    await storage.set_state(key, state)
    await storage.set_data(
        key,
        {"opponent_id": opponent_id, STATE_ENTERED_AT_FIELD: _NOW.isoformat()},
    )
    return key


async def test_pending_scan_uses_the_bulk_read_when_the_backend_offers_one() -> None:
    """#1684: ``/accept`` costs one query, not ``1 + N + M``.

    This is the third scanner built on the same duck-typed walk as
    ``FsmTimeoutSweeper`` (#1451) and ``fsm_busy._scan_seats`` (#1491):
    one ``iter_keys``, a ``get_state`` per key in the WHOLE store, and
    a ``get_data`` per key that survived. Under ``FSM_BACKEND=sqlite``
    every one of those queues behind the single connection
    ``SQLiteStorage`` holds for the life of the process, which is the
    same connection every routed update's own ``get_state`` uses.

    The states it wants are exactly ``_ACCEPTANCE_STATES``, fixed at
    import and never user input, so the backend answers in one
    statement. Correctness is pinned by every other test in this file,
    which all run on a plain :class:`MemoryStorage` and therefore still
    exercise the portable key-by-key path.
    """
    storage = _BulkStorage()
    await _seed_scan_key(storage, state=RpsStates.awaiting_acceptance.state)
    # Noise the SQL filter must drop: a match already past acceptance.
    await _seed_scan_key(
        storage, challenger_id=CHALLENGER_ID + 1, state=RpsStates.awaiting_moves.state
    )

    found = await _find_pending_challenges(
        storage, bot_id=_SCAN_BOT_ID, chat_id=GROUP_CHAT_ID, user_id=OPPONENT_ID
    )

    assert [p.kind for p in found] == ["rps"]
    assert found[0].challenger_id == CHALLENGER_ID
    assert storage.iter_records_calls == 1
    assert storage.iter_keys_calls == 0
    assert storage.get_state_calls == 0
    assert storage.get_data_calls == 0


async def test_bulk_read_still_scopes_the_scan_to_this_bot_and_chat() -> None:
    """The chat/bot filter has to survive the fast path.

    An FSM key is stored as ONE ``|``-joined TEXT column, so the
    ``IN (...)`` statement can narrow by state but cannot narrow by
    ``bot_id`` or ``chat_id``. That half of the filter stays in Python
    on both paths, and a challenge pending in a DIFFERENT group must
    remain invisible to an ``/accept`` typed here — otherwise the
    command would answer a card the caller cannot even see.
    """
    storage = _BulkStorage()
    await _seed_scan_key(
        storage, chat_id=GROUP_CHAT_ID - 1, state=RpsStates.awaiting_acceptance.state
    )
    await _seed_scan_key(
        storage,
        challenger_id=CHALLENGER_ID + 1,
        bot_id=_SCAN_BOT_ID + 1,
        state=DuelStates.awaiting_acceptance.state,
    )

    found = await _find_pending_challenges(
        storage, bot_id=_SCAN_BOT_ID, chat_id=GROUP_CHAT_ID, user_id=OPPONENT_ID
    )

    assert found == []
    assert storage.iter_records_calls == 1
    assert storage.get_state_calls == 0
