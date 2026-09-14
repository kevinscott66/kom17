"""End-to-end ``/duel`` flow (T-018).

Group-chat dice PvP — twin of /cpc but with reply-to opponent
targeting and a single inline "🎲 Roll" button per match.

The matrix pins:

* Parse-time rejections (no reply, no args, malformed, same player,
  bot target) without any FSM mutation.
* Group-only routing: a /duel in a private chat does NOT trigger the
  handler, but does get a spoken refusal naming the alias typed (#122).
* Challenge → Accept → both roll → resolved result, with wallet
  conservation and FSM cleared on every terminal outcome.
* Decline path: FSM cleared, no escrow.
* Single-side roll keeps FSM ``awaiting_rolls``; balances untouched
  (ADR 0009 no-escrow — this is the load-bearing pin).
* Self-challenge / bot-target / insufficient-balance: typed
  rejection with FSM untouched.
* Best-of-N (RR-2 #22): the ``max_wins`` argument is range-checked,
  an undecided round leaves BOTH wallets alone, a decided match pays
  exactly one stake no matter how many rounds were played, a tied
  round is replayed instead of ending the match, and a best-of-1
  still renders (and refunds) exactly as it always did.

FSM is keyed on the group ``chat_id`` + challenger ``user_id`` —
different from /cpc which keys on the private chat. The helper
``_get_state_name`` synthesises that key.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import duel as duel_handler
from telegram_invite_bot.keyboards.builders import (
    DuelAccept,
    DuelDecline,
    DuelRoll,
)
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot, Dispatcher

    from tests.e2e.handlers.conftest import WiredFactory


# ── Seed helpers ─────────────────────────────────────────────────────


GROUP_CHAT_ID = -1001
CHALLENGER_ID = 100
OPPONENT_ID = 200


async def _seed_wallet(registry: Any, user_id: int, *, balance: int = 1_000) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _get_state_name(
    bot: Bot, dispatcher: Dispatcher, *, chat_id: int, challenger_id: int
) -> str | None:
    """Read the FSM state out-of-band — mirrors ``_fsm_context_for`` in
    the handler. NOTE: /duel keys on the GROUP chat, not the private
    chat — that's why this differs from the /cpc test helper.
    """
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=challenger_id),
    )
    return await state.get_state()


async def _get_state_data(
    bot: Bot, dispatcher: Dispatcher, *, chat_id: int, challenger_id: int
) -> dict[str, Any]:
    state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=challenger_id),
    )
    return await state.get_data()


def _duel_message(
    text: str,
    *,
    user_id: int = CHALLENGER_ID,
    reply_to_user_id: int | None = OPPONENT_ID,
    reply_to_is_bot: bool = False,
    chat_type: str = "supergroup",
    chat_id: int = GROUP_CHAT_ID,
    message_id: int = 5,
) -> Any:
    return make_message_update(
        text,
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
        first_name="C",
        language_code="ru",
        reply_to_user_id=reply_to_user_id,
        reply_to_is_bot=reply_to_is_bot,
        message_id=message_id,
    )


def _duel_callback(payload: str, *, user_id: int) -> Any:
    """Callback update whose synthetic message lives in the GROUP chat."""
    base = make_callback_update(payload, user_id=user_id, language_code="ru")
    # Override the inline-message's chat to the group — the handler
    # reads ``callback.message.chat.id`` to derive the FSM key.
    cb = base.callback_query
    assert cb is not None
    msg = cb.message
    assert msg is not None
    object.__setattr__(msg.chat, "id", GROUP_CHAT_ID)
    object.__setattr__(msg.chat, "type", "supergroup")
    return base


class _FakeRng:
    """Module-level RNG stand-in for deterministic rolls."""

    def __init__(self, sequence: list[int]) -> None:
        self._values = list(sequence)
        self._idx = 0

    def randint(self, lo: int, hi: int) -> int:  # noqa: ARG002
        value = self._values[self._idx]
        self._idx += 1
        return value


# ── Tests: parse-time rejections ─────────────────────────────────────


async def test_duel_private_chat_refuses_without_touching_fsm(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The duel handler never runs outside a group, but the user hears
    about it (#122).

    Before the private twin existed this asserted ``sent == []``: the
    chat-type filter meant a DM ``/duel`` matched no handler at all and
    the user got pure silence. The escrow-free FSM invariant is the same
    either way — the refusal is a plain ``answer``, so nothing is staged.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        _duel_message("/duel 100", chat_type="private", chat_id=CHALLENGER_ID),
    )

    assert len(sent) == 1
    assert "только в группе" in sent[0]["text"]
    assert "/duel" in sent[0]["text"]
    assert (
        await _get_state_name(bot, dispatcher, chat_id=CHALLENGER_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_private_refusal_echoes_the_alias_typed(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Someone who typed ``/дуэль`` should not be told about ``/duel``.

    The shared handler reads the alias off ``CommandObject.command``,
    which aiogram fills from the message itself — so the refusal names
    the word the user actually used.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        _duel_message("/дуэль 100", chat_type="private", chat_id=CHALLENGER_ID),
    )

    assert "/дуэль" in sent[0]["text"]
    assert "/duel" not in sent[0]["text"]


async def test_duel_no_reply_renders_usage(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100", reply_to_user_id=None))

    assert any("Использование" in (m.get("text") or "") for m in sent)
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_bot_target_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100", reply_to_is_bot=True))

    assert any("ботом" in (m.get("text") or "").lower() for m in sent)
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_self_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100", reply_to_user_id=CHALLENGER_ID))

    assert any("собой" in (m.get("text") or "").lower() for m in sent)
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_bad_args(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel abc"))

    # Copy names "аргументы", not "ставку": since RR-2 #22 the command
    # takes two of them and either can be the unparseable one.
    assert any("аргументы" in (m.get("text") or "").lower() for m in sent)
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_insufficient_balance_rejected_no_fsm(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=50)
    await _seed_wallet(registry, OPPONENT_ID, balance=1_000)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 500"))

    assert any("недостаточно" in (m.get("text") or "").lower() for m in sent)
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
    # No escrow.
    assert await _balance(registry, CHALLENGER_ID) == 50
    assert await _balance(registry, OPPONENT_ID) == 1_000


# ── Tests: challenge / accept / decline ──────────────────────────────


async def test_duel_challenge_sets_fsm_no_escrow(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/duel`` posts the challenge card and sets FSM
    ``awaiting_acceptance``. No wallet writes at this stage — ADR 0009.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))

    # Card posted into the group chat.
    assert any(m["chat_id"] == GROUP_CHAT_ID for m in sent)
    state = await _get_state_name(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert state == "DuelStates:awaiting_acceptance"
    data = await _get_state_data(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert data["opponent_id"] == OPPONENT_ID
    assert data["bet"] == 100
    assert data["chat_id"] == GROUP_CHAT_ID
    # L-16: the effective language is stamped so the sweeper's expiry
    # edit can localise without a LanguageMiddleware injection.
    assert data["lang"] == "ru"
    assert isinstance(data["state_entered_at"], str)
    # No wallet movement — load-bearing.
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500


async def test_duel_decline_clears_fsm_no_escrow(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelDecline(challenger_id=CHALLENGER_ID).pack(), user_id=OPPONENT_ID),
    )

    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500


async def test_duel_accept_flips_to_awaiting_rolls(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )

    state = await _get_state_name(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert state == "DuelStates:awaiting_rolls"
    # Still no escrow after Accept.
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500


# ── Tests: happy path / single-roll / no-escrow pin ──────────────────


async def test_duel_happy_path_challenger_wins(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Full flow: /duel → accept → challenger rolls 6 → opponent rolls 1
    → challenger wins. Wallets: 500→600 / 500→400. FSM cleared.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)
    monkeypatch.setattr(duel_handler, "_rng", _FakeRng([6, 1]))

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )
    # Challenger rolls (6).
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=CHALLENGER_ID),
    )
    # Opponent rolls (1) → resolve.
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=OPPONENT_ID),
    )

    # 500 - 100 stake + 190 payout (T-020/R8 keeps 10 of the 200 pot).
    assert await _balance(registry, CHALLENGER_ID) == 590
    assert await _balance(registry, OPPONENT_ID) == 400
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_concurrent_rolls_resolve_not_hang(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """TOCTOU guard: both seats click 🎲 Roll near-simultaneously.

    Without the per-match lock both updates read the same roll-less FSM
    snapshot, each stamp only their own key, and BOTH fall into the
    "waiting on opponent" branch — the round never resolves and hangs
    until the sweeper times it out. The lock + post-stamp re-read makes
    the second-arriving click observe the first's roll and resolve. We
    fire both updates concurrently via ``asyncio.gather`` and assert the
    match settled (wallets moved, FSM cleared).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)
    monkeypatch.setattr(duel_handler, "_rng", _FakeRng([6, 1]))

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )

    # Both seats roll concurrently.
    await asyncio.gather(
        dispatcher.feed_update(
            bot,
            _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=CHALLENGER_ID),
        ),
        dispatcher.feed_update(
            bot,
            _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=OPPONENT_ID),
        ),
    )

    # Resolved exactly once: one winner +90, one loser -100, FSM gone.
    balances = {
        await _balance(registry, CHALLENGER_ID),
        await _balance(registry, OPPONENT_ID),
    }
    assert balances == {400, 590}
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_tie_refunds_both(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)
    monkeypatch.setattr(duel_handler, "_rng", _FakeRng([4, 4]))

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=CHALLENGER_ID),
    )
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=OPPONENT_ID),
    )

    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_single_roll_stays_awaiting_no_escrow(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """ADR 0009 pin: only one seat rolled → FSM stays in awaiting_rolls,
    balances untouched. The second click is what resolves and escrows.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)
    monkeypatch.setattr(duel_handler, "_rng", _FakeRng([3]))

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=CHALLENGER_ID),
    )

    state = await _get_state_name(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert state == "DuelStates:awaiting_rolls"
    data = await _get_state_data(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert data["challenger_roll"] == 3
    assert data.get("opponent_roll") is None
    # Load-bearing: no escrow.
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500


async def test_duel_cross_user_roll_rejected_silently(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A third user clicking 🎲 Roll on someone else's duel is rejected
    silently — FSM untouched.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    await _seed_wallet(registry, 999, balance=500)
    capture_callback_outgoing(bot)
    monkeypatch.setattr(duel_handler, "_rng", _FakeRng([6, 1]))

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )
    pre_data = await _get_state_data(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=999),
    )

    state = await _get_state_name(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert state == "DuelStates:awaiting_rolls"
    post_data = await _get_state_data(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert post_data.get("challenger_roll") == pre_data.get("challenger_roll")
    assert post_data.get("opponent_roll") == pre_data.get("opponent_roll")


async def test_duel_accept_payload_bet_mismatch_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """M-G-1: forged DuelAccept payload carrying a wrong ``bet`` field
    must be silently rejected — no state flip from awaiting_acceptance
    to awaiting_rolls, no money movement. Pins the tamper guard."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    pre_state = await _get_state_name(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert pre_state == "DuelStates:awaiting_acceptance"

    # Forged accept: bet=1 instead of FSM-stored 100.
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=1).pack(),
            user_id=OPPONENT_ID,
        ),
    )

    post_state = await _get_state_name(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert post_state == "DuelStates:awaiting_acceptance"
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500


async def test_duel_timeout_edits_challenge_card_to_expiry_notice(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """L-16: when /duel times out in awaiting_acceptance, the sweeper
    callback must EDIT the original challenge card in place to the
    localized expiry notice (keyboard dropped in the same call) — not
    just post a fresh timeout message under the stale card."""
    from datetime import UTC, datetime, timedelta

    from telegram_invite_bot.app import _duel_timeout_rules
    from telegram_invite_bot.scheduler import FsmTimeoutSweeper

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    pre_state = await _get_state_name(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert pre_state == "DuelStates:awaiting_acceptance"
    challenge_data = await _get_state_data(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    card_id = challenge_data["challenge_message_id"]
    sends_before_sweep = len(sent)

    # Widen the make_request stub so the sweeper's edit_message_text
    # call records correctly instead of raising in the narrow fixture.
    original_make_request = bot.session.make_request
    edits: list[dict[str, Any]] = []

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "EditMessageText":
            edits.append(
                {
                    "chat_id": method.chat_id,
                    "message_id": method.message_id,
                    "text": method.text,
                    "reply_markup": method.reply_markup,
                }
            )
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    future = datetime.now(UTC) + timedelta(seconds=600)
    sweeper = FsmTimeoutSweeper(
        storage=dispatcher.storage,
        bot=bot,
        rules=_duel_timeout_rules(),
        clock=lambda: future,
    )
    report = await sweeper.sweep_once()

    assert report.expired == 1
    assert report.errors == 0
    # The original challenge card was edited in place to the expiry
    # notice with the keyboard dropped.
    assert len(edits) == 1
    assert edits[0]["chat_id"] == GROUP_CHAT_ID
    assert edits[0]["message_id"] == card_id
    assert "не успел принять" in edits[0]["text"]
    assert edits[0]["reply_markup"] is None
    # No duplicate fresh timeout message — the edit IS the notice.
    assert len(sent) == sends_before_sweep


async def test_duel_timeout_falls_back_to_notice_when_edit_fails(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """L-16 fallback ladder: if the expiry edit fails (card deleted /
    edit window closed), the sweeper drops the dead keyboard and posts
    the timeout notice as a fresh message — the M-G-3 posture."""
    from datetime import UTC, datetime, timedelta

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from telegram_invite_bot.app import _duel_timeout_rules
    from telegram_invite_bot.scheduler import FsmTimeoutSweeper

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))

    original_make_request = bot.session.make_request
    edit_markup_calls: list[int] = []

    async def widened(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "EditMessageText":
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=0, message_id=0, text="x"),
                message="message to edit not found",
            )
        if name == "EditMessageReplyMarkup":
            edit_markup_calls.append(method.chat_id)
            return True
        return await original_make_request(_bot, method, timeout=timeout)

    bot.session.make_request = widened  # type: ignore[method-assign,assignment]

    future = datetime.now(UTC) + timedelta(seconds=600)
    sweeper = FsmTimeoutSweeper(
        storage=dispatcher.storage,
        bot=bot,
        rules=_duel_timeout_rules(),
        clock=lambda: future,
    )
    report = await sweeper.sweep_once()

    assert report.expired == 1
    assert report.errors == 0
    # Fallback path: keyboard-drop attempted, fresh notice posted.
    assert GROUP_CHAT_ID in edit_markup_calls
    assert any(
        "не успел принять" in (m.get("text") or "") and m["chat_id"] == GROUP_CHAT_ID for m in sent
    )


async def test_duel_accept_edits_original_challenge_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """L-16: Accept must EDIT the original challenge card (to the
    "duel started" body with the 🎲 Roll keyboard), not post a fresh
    message — the card in chat history is the single live surface."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    sends_before = sum(1 for m in sink if m["kind"] == "text")
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )

    edits = [m for m in sink if m["kind"] == "edit"]
    assert len(edits) == 1
    assert "Дуэль началась" in edits[0]["text"]
    # No fresh SendMessage on accept — the edit carries the transition.
    assert sum(1 for m in sink if m["kind"] == "text") == sends_before


async def test_duel_decline_edits_original_challenge_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """L-16 twin: Decline edits the card to the declined notice."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    sends_before = sum(1 for m in sink if m["kind"] == "text")
    await dispatcher.feed_update(
        bot,
        _duel_callback(DuelDecline(challenger_id=CHALLENGER_ID).pack(), user_id=OPPONENT_ID),
    )

    edits = [m for m in sink if m["kind"] == "edit"]
    assert len(edits) == 1
    assert sum(1 for m in sink if m["kind"] == "text") == sends_before


async def test_duel_accept_opponent_already_in_other_match_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """M-G-2: a player who is already the recorded opponent of an
    existing /cpc match cannot accept a /duel from a third party.
    The duel accept must refuse to flip the state to awaiting_rolls.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)

    # Plant an existing /cpc-style match in storage where OPPONENT_ID
    # is the recorded opponent of some other challenger (777). The
    # opponent-busy scan should refuse the new duel accept.
    other_state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=777, user_id=777),
    )
    await other_state.set_state("RpsStates:awaiting_acceptance")
    await other_state.update_data(opponent_id=OPPONENT_ID, bet=50)

    # Challenger issues /duel.
    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )

    # Opponent tries to accept. The opponent-busy guard refuses.
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(),
            user_id=OPPONENT_ID,
        ),
    )

    # No flip; both balances untouched.
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500


async def test_duel_from_the_opponent_seat_of_another_match_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1517, creation path: twin of the /cpc guard.

    A match lives on the challenger's key alone, so a player who was
    INVITED into one holds an empty key of their own and used to walk
    straight through the handler's ``prior is not None`` read into a
    second match.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_outgoing(bot)

    # CHALLENGER_ID is the recorded opponent of somebody else's match.
    other_state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=777, user_id=777),
    )
    await other_state.set_state("RpsStates:awaiting_acceptance")
    await other_state.update_data(opponent_id=CHALLENGER_ID, bet=50)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))

    assert sent
    assert "активная дуэль" in sent[0]["text"]
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
    assert await _balance(registry, CHALLENGER_ID) == 500


async def test_duel_accept_by_the_challenger_of_another_match_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1517, accept path: the clicker's OWN match counts.

    The M-G-2 scan matched ``opponent_id`` only, so a clicker who is
    the CHALLENGER of another live match read as free — this handler
    opens the key of the match being accepted, never the clicker's.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    capture_callback_outgoing(bot)

    # OPPONENT_ID is the CHALLENGER of a duel running in another group.
    other_state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=-1002, user_id=OPPONENT_ID),
    )
    await other_state.set_state("DuelStates:awaiting_rolls")
    await other_state.update_data(opponent_id=999, bet=50)

    await dispatcher.feed_update(bot, _duel_message("/duel 100"))
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )

    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(),
            user_id=OPPONENT_ID,
        ),
    )

    # No flip to awaiting_rolls, no escrow.
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_acceptance"
    )
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500


# ── Tests: best-of-N rounds (RR-2 #22) ───────────────────────────────


async def _play_bestof(
    bot: Any,
    dispatcher: Any,
    *,
    rolls: list[int],
    monkeypatch: Any,
    command: str,
) -> None:
    """/duel → accept → alternate roll clicks until ``rolls`` is spent.

    Each round consumes two values (challenger first, then opponent),
    matching the click order the tests below drive.
    """
    monkeypatch.setattr(duel_handler, "_rng", _FakeRng(rolls))
    await dispatcher.feed_update(bot, _duel_message(command))
    await dispatcher.feed_update(
        bot,
        _duel_callback(
            DuelAccept(challenger_id=CHALLENGER_ID, bet=100).pack(), user_id=OPPONENT_ID
        ),
    )
    for index in range(len(rolls)):
        seat = CHALLENGER_ID if index % 2 == 0 else OPPONENT_ID
        await dispatcher.feed_update(
            bot,
            _duel_callback(DuelRoll(challenger_id=CHALLENGER_ID).pack(), user_id=seat),
        )


async def test_duel_rounds_arg_out_of_range_rejected_no_fsm(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``max_wins`` is capped (legacy ``DUEL_MAX_ROUNDS``); a request
    past the cap is refused before any FSM is created."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _duel_message("/duel 100 99"))

    assert any("количество побед" in (m.get("text") or "").lower() for m in sent)
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )


async def test_duel_bestof_intermediate_round_does_not_move_coins(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """The load-bearing pin for RR-2 #22: in a best-of-3 the stake moves
    ONCE, at the end — not once per round.

    Round 1 goes to the challenger (6 vs 1). The match is still open, so
    both wallets must be exactly as seeded and the FSM must be back in
    ``awaiting_rolls`` with a 1:0 score and the rolls cleared for the
    next round.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_callback_outgoing(bot)

    await _play_bestof(
        bot, dispatcher, rolls=[6, 1], monkeypatch=monkeypatch, command="/duel 100 3"
    )

    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        == "DuelStates:awaiting_rolls"
    )
    data = await _get_state_data(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert data["challenger_wins"] == 1
    assert data["opponent_wins"] == 0
    assert data["rounds"] == [[6, 1]]
    # Both rolls cleared so the next round can be played on the same card.
    assert data["challenger_roll"] is None
    assert data["opponent_roll"] is None
    # The card announces the round and the live score.
    edits = [m["text"] for m in sent if m["kind"] == "edit"]
    assert any("Раунд 1 сыгран" in text for text in edits)
    assert any("Счёт: <b>1</b> : <b>0</b>" in text for text in edits)


async def test_duel_bestof_settles_once_when_a_seat_reaches_max_wins(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Best-of-2: challenger takes rounds 1 and 2 → match over.

    Exactly one stake changes hands (500→600 / 500→400) even though two
    rounds were played, and the final card carries the score plus the
    per-round recap legacy printed (bot.py:21454).
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_callback_outgoing(bot)

    await _play_bestof(
        bot,
        dispatcher,
        rolls=[6, 1, 5, 2],
        monkeypatch=monkeypatch,
        command="/duel 100 2",
    )

    # 500 - 100 stake + 190 payout (T-020/R8 keeps 10 of the 200 pot).
    assert await _balance(registry, CHALLENGER_ID) == 590
    assert await _balance(registry, OPPONENT_ID) == 400
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
    final = [m["text"] for m in sent if m["kind"] == "edit"][-1]
    assert "Дуэль завершена" in final
    assert "Счёт: <b>2</b> : <b>0</b>" in final
    assert "Раунд 1: <b>6</b> : <b>1</b>" in final
    assert "Раунд 2: <b>5</b> : <b>2</b>" in final
    assert "Выигрыш: <b>190</b>" in final
    assert "Комиссия банка: 10 🪙" in final


async def test_duel_bestof_tied_round_is_replayed(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A tie advances nobody, so a best-of-N replays the round rather
    than ending in a draw — legacy's ``is_finished`` only ever looks at
    the win counters (bot.py:15209).

    Round 1 ties 4:4 and leaves the score at 0:0; round 2 goes to the
    opponent, who still needs a second win. Both rounds are recorded.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_callback_outgoing(bot)

    await _play_bestof(
        bot,
        dispatcher,
        rolls=[4, 4, 2, 5],
        monkeypatch=monkeypatch,
        command="/duel 100 2",
    )

    # Opponent needs 2 wins and has 1 — the match is still open and no
    # coins have moved despite two completed rounds.
    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500
    data = await _get_state_data(
        bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID
    )
    assert data["challenger_wins"] == 0
    assert data["opponent_wins"] == 1
    assert data["rounds"] == [[4, 4], [2, 5]]
    edits = [m["text"] for m in sent if m["kind"] == "edit"]
    assert any("Счёт: <b>0</b> : <b>0</b>" in text for text in edits)


async def test_duel_best_of_one_keeps_the_single_round_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """Regression guard for the default shape: with no rounds argument
    nothing about the match changes — no scoreboard, no round recap,
    and a tie is still a terminal refund rather than a reroll."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_wallet(registry, CHALLENGER_ID, balance=500)
    await _seed_wallet(registry, OPPONENT_ID, balance=500)
    sent = capture_callback_outgoing(bot)

    await _play_bestof(bot, dispatcher, rolls=[4, 4], monkeypatch=monkeypatch, command="/duel 100")

    assert await _balance(registry, CHALLENGER_ID) == 500
    assert await _balance(registry, OPPONENT_ID) == 500
    assert (
        await _get_state_name(bot, dispatcher, chat_id=GROUP_CHAT_ID, challenger_id=CHALLENGER_ID)
        is None
    )
    texts = [m.get("text") or "" for m in sent]
    assert any("Ничья" in text for text in texts)
    assert not any("Счёт:" in text for text in texts)
    assert not any("Играем до" in text for text in texts)
