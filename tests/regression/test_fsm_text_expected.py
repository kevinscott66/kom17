"""A text FSM step must answer non-text input instead of dropping it (#125).

Every text step of every interview carries an ``F.text`` filter beside
its :class:`~aiogram.filters.StateFilter`. That filter is correct — a
sticker is not an amount — but on its own it produces silence: the step
declines the photo, nothing else claims an update from a user who is
mid-flow, and the dispatcher drops it. The bot asked a question and then
ignored the answer, which reads as "it died" and gets the same message
sent again.

:func:`~telegram_invite_bot.handlers.fsm_text.register_text_expected`
is the other half — one extra registration per step, same state,
opposite content type. This file guards both halves:

* **Behaviour** — a photo sent in-state is answered, the state survives
  (the user's real answer is one text message away), text still reaches
  the step, and neither a stateless photo nor a service event triggers
  the reprompt.
* **Coverage** — a static scan asserting that every ``StateFilter`` +
  bare ``F.text`` registration under ``handlers/`` has its states named
  in a ``register_text_expected(...)`` call in the same module. A new
  interview added without the twin fails here rather than shipping the
  silence back.

The scan reads source, not the assembled tree, deliberately: the point
is that the pair is *written down together*, so the next person editing
the step sees the guard three lines below it.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser

from telegram_invite_bot.handlers.fsm_text import register_text_expected
from telegram_invite_bot.i18n import t

pytestmark = pytest.mark.integration

_HANDLERS = Path(__file__).resolve().parents[2] / "src/telegram_invite_bot/handlers"

_CHAT_ID = 4242
_USER_ID = 4242

# Steps that deliberately ship without the twin, each with the reason.
# An entry here is a claim that silence is the *better* behaviour for
# that step — not a to-do — so it is checked from both sides below.
_ALLOWED: dict[str, str] = {
    # Both live in a group, and the common non-answer case is
    # abandonment: the admin taps ➕, gets distracted, and the state sits
    # until the sweeper clears it. A reprompt would answer every meme in
    # the room for as long as that lasts. The prompt is on screen and
    # the panel is one tap away, so silence costs less here than noise.
    "groupadmin.py::GroupStaffStates.awaiting_grant": "group-scoped: reprompts would spam the room",
    "groupadmin.py::GroupWordsStates.awaiting_word": "group-scoped: reprompts would spam the room",
}


# ── behaviour ───────────────────────────────────────────────────────


class _DemoStates(StatesGroup):
    """A stand-in for every real interview: one state, one text step."""

    awaiting_amount = State()


def _message_update(**fields: Any) -> Update:
    """A private message from ``_USER_ID``, carrying whatever ``fields`` say."""
    payload: dict[str, Any] = {
        "message_id": 1,
        "date": 1_700_000_000,
        "chat": {"id": _CHAT_ID, "type": "private"},
        "from": {"id": _USER_ID, "is_bot": False, "first_name": "T"},
    }
    payload.update(fields)
    return Update.model_validate({"update_id": 1, "message": payload})


def _photo_update() -> Update:
    """The shape Telegram sends for a picture with no caption."""
    return _message_update(
        photo=[{"file_id": "p1", "file_unique_id": "p1u", "width": 90, "height": 90}]
    )


def _service_update() -> Update:
    """A join event — a message the user did not author.

    Service messages arrive on the same observer as human ones, so a
    reprompt keyed on "not text" would fire on them too.
    """
    return _message_update(new_chat_members=[{"id": 77, "is_bot": False, "first_name": "N"}])


def _build(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Bot, Dispatcher, MemoryStorage, list[str], list[str]]:
    """A dispatcher holding one text step plus its ``register_text_expected`` twin.

    Bespoke rather than the production tree on purpose: the contract
    under test is the *pairing*, and a local router proves it without
    dragging in schemas, middlewares or a DB. ``dispatcher["lang"]``
    stands in for ``LanguageMiddleware``, which production installs as a
    root outer middleware.
    """
    bot = Bot(token="42:TEST-token")
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher["lang"] = "ru"

    stepped: list[str] = []
    sent: list[str] = []

    async def _step(message: Message) -> None:
        stepped.append(message.text or "")

    router = Router()
    router.message.register(_step, StateFilter(_DemoStates.awaiting_amount), F.text, F.from_user)
    register_text_expected(router, _DemoStates.awaiting_amount)
    dispatcher.include_router(router)

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        assert type(method).__name__ == "SendMessage", type(method).__name__
        sent.append(method.text)
        return Message(
            message_id=2,
            date=datetime(2024, 1, 1),
            chat=Chat(id=method.chat_id, type="private"),
            from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
            text=method.text,
        )

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return bot, dispatcher, storage, stepped, sent


def _key(bot: Bot) -> StorageKey:
    return StorageKey(bot_id=bot.id, chat_id=_CHAT_ID, user_id=_USER_ID)


async def test_a_photo_mid_step_is_answered_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: this update used to match nothing and vanish."""
    bot, dispatcher, storage, stepped, sent = _build(monkeypatch)
    await storage.set_state(_key(bot), _DemoStates.awaiting_amount)

    result = await dispatcher.feed_update(bot, _photo_update())

    assert result is not UNHANDLED
    assert sent == [t("h_fsm_text_expected", "ru")]
    assert stepped == []


async def test_the_step_survives_the_reprompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """State is deliberately kept: the real answer is one message away.

    Clearing it would turn "you sent a picture" into "your interview is
    gone", which is a worse outcome than the silence being replaced.
    """
    bot, dispatcher, storage, _stepped, _sent = _build(monkeypatch)
    await storage.set_state(_key(bot), _DemoStates.awaiting_amount)

    await dispatcher.feed_update(bot, _photo_update())

    assert await storage.get_state(_key(bot)) == _DemoStates.awaiting_amount.state


async def test_text_still_reaches_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """The twin must not shadow the handler it accompanies."""
    bot, dispatcher, storage, stepped, sent = _build(monkeypatch)
    await storage.set_state(_key(bot), _DemoStates.awaiting_amount)

    result = await dispatcher.feed_update(bot, _message_update(text="100"))

    assert result is not UNHANDLED
    assert stepped == ["100"]
    assert sent == []


async def test_a_photo_outside_the_flow_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No state, no reprompt — otherwise every picture gets a lecture."""
    bot, dispatcher, _storage, _stepped, sent = _build(monkeypatch)

    result = await dispatcher.feed_update(bot, _photo_update())

    assert result is UNHANDLED
    assert sent == []


async def test_service_events_never_trigger_the_reprompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A join is not an answer. ``_HUMAN_CONTENT`` exists for this case."""
    bot, dispatcher, storage, _stepped, sent = _build(monkeypatch)
    await storage.set_state(_key(bot), _DemoStates.awaiting_amount)

    result = await dispatcher.feed_update(bot, _service_update())

    assert result is UNHANDLED
    assert sent == []


# ── coverage scan ───────────────────────────────────────────────────


def _dotted(node: ast.expr) -> str | None:
    """``FooStates.bar`` → ``"FooStates.bar"``; anything else → ``None``.

    ``StateFilter(None)`` and ``StateFilter(*states)`` land in the
    ``None`` branch, which is right: neither names a concrete step.
    """
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _is_bare_f_text(node: ast.expr) -> bool:
    """``F.text`` itself, not ``~F.text.startswith("/")``.

    The distinction matters: ``broadcast.py`` carries only the negated
    command guard, because a media broadcast is legitimate content
    there. Treating that as a text step would demand a twin that must
    not exist.
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "text"
        and isinstance(node.value, ast.Name)
        and node.value.id == "F"
    )


def _text_steps(tree: ast.Module) -> set[str]:
    """States registered as ``<observer>.message.register(…, StateFilter(X), …, F.text, …)``."""
    steps: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "message"
        ):
            continue
        if not any(_is_bare_f_text(arg) for arg in node.args):
            continue
        for arg in node.args:
            if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)):
                continue
            if arg.func.id == "StateFilter":
                steps.update(name for a in arg.args if (name := _dotted(a)))
    return steps


def _covered(tree: ast.Module) -> set[str]:
    """States named in ``register_text_expected(router, X, Y)`` — the router argument dropped."""
    covered: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_text_expected"
        ):
            covered.update(name for a in node.args[1:] if (name := _dotted(a)))
    return covered


def _scan() -> dict[str, tuple[set[str], set[str]]]:
    """``{filename: (text steps, covered states)}`` for every handler module."""
    out: dict[str, tuple[set[str], set[str]]] = {}
    for path in sorted(_HANDLERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        out[path.name] = (_text_steps(tree), _covered(tree))
    return out


def test_every_text_step_answers_non_text_input() -> None:
    """A text step without its twin is a step that goes silent on a photo."""
    offenders = [
        f"{name}::{step}"
        for name, (steps, covered) in _scan().items()
        for step in sorted(steps - covered)
        if f"{name}::{step}" not in _ALLOWED
    ]
    assert not offenders, (
        "these FSM text steps drop non-text input without a word — add "
        "register_text_expected(router, <state>) next to the registration "
        "(handlers/fsm_text.py), or allowlist it with a reason:\n" + "\n".join(offenders)
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlisted step that no longer exists — or that grew the twin
    anyway — is dead weight hiding the next real one."""
    scanned = _scan()
    stale: list[str] = []
    for entry in sorted(_ALLOWED):
        name, _, step = entry.partition("::")
        steps, covered = scanned.get(name, (set(), set()))
        if step not in steps:
            stale.append(f"{entry}: no longer a StateFilter+F.text step")
        elif step in covered:
            stale.append(f"{entry}: now has register_text_expected — drop the entry")
    assert not stale, "\n".join(stale)


def test_the_scan_actually_finds_the_text_steps() -> None:
    """Guard the guard: a scan that matches nothing asserts nothing.

    If a refactor moves registration behind a helper, the coverage test
    above quietly becomes a tautology. Pin a floor so that degrades
    loudly instead.
    """
    total = sum(len(steps) for steps, _ in _scan().values())
    assert total >= 10, total
