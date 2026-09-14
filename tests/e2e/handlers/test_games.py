"""End-to-end ``/dice`` + Stage 18 ``/roll`` / ``/flip`` vanity flows.

Stage 12 ported ``/dice``. Stage 18 adds the vanity-guess slices of
``/roll`` and ``/flip``. Both ports own a deliberately tight contract;
the rest of each command's legacy surface (bet variants, inline
keyboards, bare-command usage hints) keeps falling through.

The tests in this file are the cleanest place to prove the contract:

* Dispatcher routes the exact vanity forms to the new handler (no
  UNHANDLED → no legacy fallback).
* Telegram's native ``send_dice`` fires for the roll path, then a
  follow-up text line gives the guess/result verdict.
* Coin flip replies in legacy format (heads/tails byte-identical).
* No off-contract input (``/roll abc``, ``/roll 7``, ``/dice 100``, …)
  reaches a game. That half of the contract is unchanged and still
  worth pinning; what these tests used to also assert — that such an
  input reaches *nothing* — was a statement about legacy, which owned
  the bet / usage / inline-keyboard paths and rendered its own hints
  there. Legacy is dead in prod, so those assertions had quietly
  become "the bot ignores the user": the bare forms first (#157), and
  every mistyped argument after them (#158). All of them answer now —
  the game handlers still decline, and the tail router picks up.
* Command aliases match legacy (``/кубик``, ``/монетка``, ``/kom_flip``).

Inline ``_capture`` stays (vs the shared ``capture_outgoing`` in
conftest.py): the dice flow emits ``SendDice`` outbound calls, which
the shared helper doesn't model. The flip-only capture is text-only
but kept inline next to its dice sibling for symmetry.
"""

from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Dice, Message, Update
from aiogram.types import User as TelegramUser

from tests.e2e.handlers.conftest import (
    assert_unknown_form_hint,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, chat_type: str = "supergroup") -> Update:
    """File-local defaults: supergroup ``-100``, user 9 named ``Eve``.
    Delegates to the shared builder.
    """
    return make_message_update(
        text,
        chat_id=-100 if chat_type != "private" else 9,
        chat_type=chat_type,
        user_id=9,
        first_name="Eve",
    )


def _capture(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    dice_value: int = 4,
) -> None:
    """Intercept outbound Telegram calls.

    ``send_dice`` returns a Message with ``.dice.value`` set by Telegram;
    we pin it to ``dice_value`` so the assertion on the follow-up text
    is deterministic. ``send_message`` records the body for assertion.
    """

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "SendDice":
            sink.append({"kind": "dice", "emoji": method.emoji})
            return Message(
                message_id=2,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                dice=Dice(emoji=method.emoji or "🎲", value=dice_value),
            )
        if name == "SendMessage":
            sink.append({"kind": "text", "text": method.text})
            return Message(
                message_id=3,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def test_dice_rolls_native_animation_and_text_followup(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=6)

    result = await dispatcher.feed_update(bot, _update("/dice"))
    assert result is not UNHANDLED
    # Order matters: animation first, then the parity follow-up text.
    assert [s["kind"] for s in sent] == ["dice", "text"]
    assert sent[0]["emoji"] == "🎲"
    # #1657: this line has no legacy counterpart to be identical to.
    # The cited ``bot/handlers/games.py:35`` names a directory that has
    # never existed, and legacy's ``cmd_dice`` has no bare-``/dice`` roll
    # at all: with fewer than three arguments it prints a rules card
    # instead. The text is ``h_dice_result`` in ``i18n/data/ru.yaml``,
    # this port's own; RR-3 #31 appends the verdict and the "how to play
    # it for real" hints under it.
    body = sent[1]["text"]
    assert body.startswith("🎲 <b>Кубик:</b> выпало <b>6</b> из 6.")
    assert "Максимум!" in body  # crit verdict for a 6
    assert "<code>/dice 4</code>" in body  # guess form
    assert "<code>/dice 100 4</code>" in body  # stake form (group chat)


async def test_dice_verdict_and_hints_scale_with_the_roll(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1 gets the consolation line, and the group-only stake hint is
    withheld in DMs (advertising a command that answers "group only"
    is worse than saying nothing)."""
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=1)

    await dispatcher.feed_update(bot, _update("/dice", chat_type="private"))

    body = sent[1]["text"]
    assert "Единица" in body
    assert "<code>/dice 4</code>" in body
    assert "<code>/dice 100 4</code>" not in body


async def test_dice_alias_кубик(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=3)

    result = await dispatcher.feed_update(bot, _update("/кубик"))
    assert result is not UNHANDLED
    assert any("выпало <b>3</b> из 6." in s.get("text", "") for s in sent)


async def test_dice_with_a_lone_bet_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/dice 100`` is half a stake form — bet without a guess (#158).

    The stake child needs both tokens (``/dice 100 5``, the shape
    ``h_cmd_dice`` advertises), and a lone number is neither that nor
    the vanity guess. It used to fall through on the "no-args → new
    pipeline, with-args → legacy" convention; with legacy gone that
    convention only produced silence for the likeliest typo of the two
    forms, so the hint that names both is exactly what belongs here.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/dice 100"))

    assert result is not UNHANDLED
    assert_unknown_form_hint(sent, command="dice")


# --- Stage 18: /roll vanity guess -------------------------------------------


async def test_roll_vanity_match_renders_win(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/roll 4`` and the dice shows 4 → "угадал" line.

    The dice value is mocked deterministically; the test pins both
    that we issued an animation AND that the follow-up text honours
    the legacy "Ты загадал X. Выпало Y — угадал!" template.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=4)

    result = await dispatcher.feed_update(bot, _update("/roll 4"))
    assert result is not UNHANDLED
    assert [s["kind"] for s in sent] == ["dice", "text"]
    body = sent[1]["text"]
    assert "Ты загадал <b>4</b>" in body
    assert "Выпало <b>4</b>" in body
    assert "угадал! 🎉" in body
    assert "не угадал" not in body


async def test_dice_with_a_guess_routes_to_the_roll_handler(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-3 #31: legacy's ``/dice <guess>`` grammar is back.

    Before this, ``/dice 4`` matched nothing at all — bare ``/dice`` was
    guarded by ``args is None`` and ``/roll``'s aliases did not include
    ``dice``.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=4)

    result = await dispatcher.feed_update(bot, _update("/dice 4"))

    assert result is not UNHANDLED
    body = sent[1]["text"]
    assert "Ты загадал <b>4</b>" in body
    assert "угадал! 🎉" in body


async def test_roll_vanity_miss_renders_lose(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/roll 4`` but dice shows 2 → "не угадал" line, no win emoji."""
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=2)

    await dispatcher.feed_update(bot, _update("/roll 4"))
    body = sent[1]["text"]
    assert "Ты загадал <b>4</b>" in body
    assert "Выпало <b>2</b>" in body
    assert "не угадал" in body
    assert "🎉" not in body


async def test_кубик_alias_with_guess(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/кубик 3`` reaches the same vanity-guess handler.

    This is the wiring-disambiguation test: bare ``/кубик`` belongs to
    handle_dice (Stage 12), one-arg ``/кубик`` belongs to
    handle_roll_guess (Stage 18). If the two registrations ever
    accidentally overlap, one of them stops firing — and this assertion
    catches the half that breaks for our port.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=3)

    result = await dispatcher.feed_update(bot, _update("/кубик 3"))
    assert result is not UNHANDLED
    body = sent[1]["text"]
    assert "Ты загадал <b>3</b>" in body
    assert "угадал! 🎉" in body


async def test_bare_кубик_still_goes_to_dice(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the wiring contract: bare ``/кубик`` MUST
    still trigger the Stage 12 ``handle_dice`` (text follow-up uses
    "выпало <b>N</b> из 6"), NOT the Stage 18 guess handler (which
    would crash on the missing arg).
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=5)

    await dispatcher.feed_update(bot, _update("/кубик"))
    body = sent[1]["text"]
    # Stage 12 wording — proves we hit handle_dice, not handle_roll_guess.
    assert body.startswith("🎲 <b>Кубик:</b> выпало <b>5</b> из 6.")


@pytest.mark.parametrize("chat_type", ["supergroup", "private"])
async def test_bare_roll_rolls_a_die(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    chat_type: str,
) -> None:
    """Bare ``/roll`` must answer — in both chat types.

    This test used to assert the opposite (``result is UNHANDLED``,
    "legacy renders the usage hint"). That was true while the telebot
    monolith still ran beside us; it registers ``/roll`` at
    ``bot.py:17334``. It is dead in prod now, and the alias list here
    was the only one of the three ``/roll`` registrations that omitted
    it — the guess and stake forms both carry it. So the user typed
    ``/roll``, nothing matched, and the bot said nothing at all, while
    ``h_games_menu_card`` advertised "/roll [число] — брось кубик" with
    the number in optional brackets.

    Parametrised over both chat types because the silence was not a
    chat-type gate: it was total. ``private`` also proves the roll does
    not smuggle in the group-only stake hint.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=5)

    result = await dispatcher.feed_update(bot, _update("/roll", chat_type=chat_type))

    assert result is not UNHANDLED
    assert [s["kind"] for s in sent] == ["dice", "text"]
    body = sent[1]["text"]
    # Same wording as bare /dice — /roll is an alias of it, not a
    # second implementation.
    assert body.startswith("🎲 <b>Кубик:</b> выпало <b>5</b> из 6.")
    # The hints replace what legacy's usage text used to spell out.
    assert "<code>/dice 4</code>" in body
    assert ("<code>/dice 100 4</code>" in body) is (chat_type != "private")


@pytest.mark.parametrize("text", ["/roll abc", "/roll 7", "/roll 0", "/roll -1"])
async def test_roll_invalid_arg_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    text: str,
) -> None:
    """Off-contract ``/roll`` arguments answer with a hint (#158).

    The original wording of this test was already the right instinct —
    "we MUST NOT swallow them, otherwise users see no feedback at all"
    — but the mechanism it relied on was legacy rendering its own
    "укажи число от 1 до 6". Falling through stopped reaching anyone
    when the bridge was removed, so the exact outcome the test set out
    to prevent is what it started pinning. The guess handler still
    declines every one of these; the tail router answers instead.

    Parametrised rather than looped so a regression names the form.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(text))

    assert result is not UNHANDLED, f"{text} must be answered"
    assert_unknown_form_hint(sent, command="roll")


async def test_roll_bet_form_falls_through_without_registry(
    make_wired: WiredFactory,
    assert_no_outgoing: Callable[[Bot, str], None],
) -> None:
    """``/roll 100 4`` is the L-17 stake form — owned by the stake child
    router, which is mounted only when ``build_router`` receives an
    ``EngineRegistry``. Without one (the transition wiring) the bet form
    must keep falling through rather than half-handle a money flow with
    no economy session. The handled-with-registry case is pinned in
    ``test_stake_games.py``.

    Wired through a standalone games router (not ``make_wired``'s
    main_router) so this contract stays pinned to the no-registry mode
    regardless of when the prod call site starts passing the registry.
    """
    from aiogram import Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from telegram_invite_bot.handlers.games import build_router

    bot, _, _ = await make_wired()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(build_router())
    assert_no_outgoing(bot, "bet form must fall through without a registry")
    result = await dispatcher.feed_update(bot, _update("/roll 100 4"))
    assert result is UNHANDLED


# --- Stage 18: /flip vanity guess -------------------------------------------


def _capture_flip(bot: Bot, monkeypatch: pytest.MonkeyPatch, sink: list[dict[str, Any]]) -> None:
    """``/flip`` mimics the dice animation without a native coin roll:
    a ``SendMessage`` (the 🪙 throw) followed by an ``EditMessageText``
    (the settled result). We record both so a test can assert on the
    final reveal at ``sink[-1]``. The throw delay is zeroed by the
    caller so the reveal lands synchronously.

    The returned ``Message`` is bound to the bot via ``.as_(bot)`` so
    ``message.edit_text`` in the handler can build its edit call.
    """

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name in ("SendMessage", "EditMessageText"):
            kind = "text" if name == "SendMessage" else "edit"
            sink.append({"kind": kind, "text": method.text})
            return Message(
                message_id=2,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="supergroup"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            ).as_(bot)
        raise AssertionError(f"unexpected Telegram call from /flip: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    # Zero the reveal pause so the edit fires synchronously in tests.
    from telegram_invite_bot.handlers import games as games_module

    monkeypatch.setattr(games_module, "_FLIP_REVEAL_DELAY", 0)


def _pin_coin(monkeypatch: pytest.MonkeyPatch, draw: float) -> None:
    """Force the next coin toss: ``< 0.5`` → орёл, otherwise решка.

    The coin comes from ``utils.rng.money_rng`` (a ``SystemRandom``),
    which by design cannot be seeded or pinned — that unpredictability
    is the whole point of it, since the same ``_flip_side`` settles
    staked flips. So the *name the handler reads* is replaced rather
    than the draw behind it. Patching the ``random`` module instead —
    which these tests used to do — silently stops pinning anything and
    leaves a 50/50 coin flipping the assertions at random.
    """
    from telegram_invite_bot.handlers import games as games_module

    monkeypatch.setattr(games_module, "money_rng", SimpleNamespace(random=lambda: draw))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/flip орёл", "орёл"),
        ("/flip орел", "орёл"),  # canonical form normalises legacy alias
        ("/flip орл", "орёл"),
        ("/flip heads", "орёл"),
        ("/flip eagle", "орёл"),
        ("/flip решка", "решка"),
        ("/flip решку", "решка"),
        ("/flip tails", "решка"),
        ("/монетка ОРЁЛ", "орёл"),  # case-insensitive normalisation
        ("/kom_flip орёл", "орёл"),  # third legacy alias
    ],
)
async def test_flip_vanity_aliases_normalise(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expected: str,
) -> None:
    """All legacy guess-aliases must produce the canonical "Ты загадал
    <b>орёл</b>" / "<b>решка</b>" in the reply. The actual flip result
    is non-deterministic, but the user's guess is echoed verbatim and
    that's the parity check we care about.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture_flip(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update(text))
    assert result is not UNHANDLED, f"{text} should be handled"
    # throw (SendMessage) then reveal (EditMessageText); assert on reveal.
    assert [s["kind"] for s in sent] == ["text", "edit"]
    body = sent[-1]["text"]
    assert f"Ты загадал <b>{expected}</b>" in body
    # Result line always names a side and either won or lost.
    assert "Выпало:" in body
    assert ("угадал! 🎉" in body) ^ ("не угадал." in body)


async def test_flip_result_matches_guess_branch(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the win-vs-lose verdict against a seeded RNG.

    ``draw < 0.5`` → heads (легаси bot.py:17492). Pin the draw at 0.1
    and the side is "орёл", so a guess of "орёл" must win and a guess
    of "решка" must lose. Catches a regression where the threshold
    accidentally flipped to ``>``.
    """
    bot, dispatcher, _ = await make_wired()

    _pin_coin(monkeypatch, 0.1)
    sent: list[dict[str, Any]] = []
    _capture_flip(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/flip орёл"))
    assert "Выпало: <b>орёл</b>" in sent[-1]["text"]
    assert "угадал! 🎉" in sent[-1]["text"]

    sent.clear()
    _pin_coin(monkeypatch, 0.9)
    await dispatcher.feed_update(bot, _update("/flip орёл"))
    assert "Выпало: <b>решка</b>" in sent[-1]["text"]
    assert "не угадал" in sent[-1]["text"]


async def test_flip_bare_tosses_coin(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``/flip`` now runs an animated toss (legacy is dead in prod;
    its inline-keyboard branch at bot.py:17501 had no callback handler
    left to answer it). The throw message is edited into the result.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture_flip(bot, monkeypatch, sent)

    _pin_coin(monkeypatch, 0.1)
    result = await dispatcher.feed_update(bot, _update("/flip"))
    assert result is not UNHANDLED
    assert [s["kind"] for s in sent] == ["text", "edit"]
    # Throw first, then the settled side — no guess line for the bare toss.
    assert sent[0]["text"] == "🪙 Подбрасываю монетку…"
    assert sent[-1]["text"] == "🪙 Монетка: выпало <b>орёл</b>."


async def test_flip_alias_bare_tosses_coin(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/монетка`` and ``/kom_flip`` (bare) reach the same toss handler."""
    bot, dispatcher, _ = await make_wired()
    _pin_coin(monkeypatch, 0.9)
    for alias in ("/монетка", "/kom_flip"):
        sent: list[dict[str, Any]] = []
        _capture_flip(bot, monkeypatch, sent)
        result = await dispatcher.feed_update(bot, _update(alias))
        assert result is not UNHANDLED, alias
        assert sent[-1]["text"] == "🪙 Монетка: выпало <b>решка</b>.", alias


@pytest.mark.parametrize("text", ["/flip pizza", "/flip орёл решка"])
async def test_flip_garbage_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    text: str,
) -> None:
    """Off-contract shapes that reach no toss (#158):

    * ``/flip pizza`` — junk arg (non-digit first token, not a side).
    * ``/flip орёл решка`` — two side tokens, no bet.

    Neither tosses a coin, exactly as before. Both used to reach
    *nothing*, which is what changed: the tail router names the command
    and repeats its ``/help`` line rather than letting the update drop.

    The bet form ``/flip 100 орёл`` is not in this list — L-17 made it
    stake-owned (see ``test_stake_games.py``). Its no-registry variant
    is pinned by ``test_roll_bet_form_falls_through_without_registry``,
    which runs on a standalone games router with no tail behind it.
    """
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(text))

    assert result is not UNHANDLED, f"{text} must be answered"
    assert_unknown_form_hint(sent, command="flip")


# --- I18N-1: English-language coverage --------------------------------------

_CYRILLIC = re.compile("[А-Яа-яЁё]")


def _update_en(text: str) -> Update:
    """Same as :func:`_update` but tags the user as English."""
    return make_message_update(
        text,
        chat_id=-100,
        chat_type="supergroup",
        user_id=9,
        first_name="Eve",
        language_code="en",
    )


async def test_dice_english_no_cyrillic(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``en`` user must get an English /dice follow-up with no Cyrillic."""
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=6)

    result = await dispatcher.feed_update(bot, _update_en("/dice"))
    assert result is not UNHANDLED
    body = sent[1]["text"]
    assert not _CYRILLIC.search(body), body
    assert "6" in body


async def test_roll_guess_english_no_cyrillic(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/roll 4`` for an ``en`` user: English win line, no Cyrillic."""
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, dice_value=4)

    await dispatcher.feed_update(bot, _update_en("/roll 4"))
    body = sent[1]["text"]
    assert not _CYRILLIC.search(body), body
    assert "<b>4</b>" in body


async def test_flip_english_side_localized_no_cyrillic(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare ``/flip`` for an ``en`` user: the coin side displays as
    ``heads``/``tails`` (localized DISPLAY label), never the Russian
    canonical value, and the whole reply is Cyrillic-free.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture_flip(bot, monkeypatch, sent)

    _pin_coin(monkeypatch, 0.1)  # heads
    await dispatcher.feed_update(bot, _update_en("/flip"))
    body = sent[-1]["text"]
    assert not _CYRILLIC.search(body), body
    assert "heads" in body


async def test_flip_guess_english_no_cyrillic(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/flip heads`` for an ``en`` user: localized side + verdict, no
    Cyrillic (the guess echo must also display the localized side).
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture_flip(bot, monkeypatch, sent)

    _pin_coin(monkeypatch, 0.1)  # heads
    await dispatcher.feed_update(bot, _update_en("/flip heads"))
    body = sent[-1]["text"]
    assert not _CYRILLIC.search(body), body
    assert "heads" in body
    assert "you got it! 🎉" in body
