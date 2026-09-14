"""End-to-end Kom control keyboard + quota rescue — RR-6 #64/#65.

The monolith→split port answered every Kom message with bare text, so
switching persona, leaving the session, clearing the context or taking
the transcript home were all commands you had to already know about; the
daily-limit refusal was a flat dead end. This file pins the restored
surfaces and, more importantly, the properties that make them safe:

* PRIVATE only — a control tap from a group is not routed at all, so the
  keyboard's ``MainMenu`` siblings (private-filtered router) can never
  ship as dead buttons.
* The VIP gate is re-checked at TAP time. A keyboard rendered while the
  user was VIP outlives the subscription; the button must not.
* A tap edits the KEYBOARD, never the text. Legacy replaced the message
  body with a status card (bot.py:38338), destroying the answer the user
  had just asked for.

The module-level stores in :mod:`handlers.ai` are process-global by
design (they mirror the legacy in-memory singletons), so every test here
runs against freshly swapped-in instances.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from pydantic import SecretStr

from telegram_invite_bot.config.settings import AiConfig, AiQuotaSettings
from telegram_invite_bot.core.ai_modes import AiModeStore
from telegram_invite_bot.db.models.ai_quota import AiDailyRequest  # noqa: F401
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import ai as ai_module
from telegram_invite_bot.services import ai_service as ai_service_module
from telegram_invite_bot.services.ai_memory import AiMemoryStore, KomModeStore
from tests.e2e.handlers.conftest import (
    assert_only_the_stale_tail_answered,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

USER = 555

# #419: ``/ai`` now refuses BEFORE the quota gate when no provider key is
# set, so any test that exercises the model path has to model a
# CONFIGURED bot. The value is a dummy and nothing leaves the process:
# every one of these tests patches the ``AiService`` seam. The suite-wide
# default stays key-less (conftest.py:381) because ``/quote`` reaches the
# network directly once a key exists.
_CONFIGURED_AI = AiConfig(DEEPSEEK_API_KEY=SecretStr("test-key"))


@pytest.fixture(autouse=True)
def _fresh_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the process-global Kom stores for empty ones per test.

    Without this, a mode set by one test leaks into the next one's
    check-mark assertions — the exact cross-test coupling that makes
    in-memory singletons hard to trust.
    """
    monkeypatch.setattr(ai_module, "_MODE_STORE", AiModeStore())
    monkeypatch.setattr(ai_module, "_MEMORY_STORE", AiMemoryStore())
    monkeypatch.setattr(ai_module, "_KOM_MODE_STORE", KomModeStore())


def _tap(data: str, *, chat_type: str = "private", chat_id: int | None = None) -> Update:
    return make_callback_update(data, user_id=USER, chat_type=chat_type, chat_id=chat_id)


async def _seed_vip(registry: Any, *, vip_till: float | None) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=USER, balance=0, language="ru", vip_till=vip_till))
        await session.commit()


def _future_ts() -> float:
    # Far enough out that clock skew on a slow CI box can't expire it.
    return 4_102_444_800.0  # 2100-01-01


def _toasts(sink: list[dict[str, Any]]) -> list[str]:
    return [e["text"] for e in sink if e["kind"] == "callback_answer"]


def _payloads(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ---------------------------------------------------------------------------
# The reply card carries the controls (private) and doesn't (group)
# ---------------------------------------------------------------------------


async def test_private_answer_carries_the_control_keyboard(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI, schemas=[EconomyBase, UsersBase]
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "ответ"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, make_message_update("/ask привет", user_id=USER))
    markup = sent[-1]["markup"]
    assert markup is not None
    payloads = _payloads(markup)
    assert "aik_in" in payloads
    assert "aik_clr" in payloads
    assert "aik_exp" in payloads


async def test_group_answer_ships_no_keyboard(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-59: no dead buttons.

    The callback router is PRIVATE-filtered, so a control keyboard on a
    group card would be a row of taps that answer nothing.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "ответ"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(
        bot,
        make_message_update("/ask привет", user_id=USER, chat_type="supergroup", chat_id=-100_1),
    )
    assert sent[-1]["markup"] is None


# ---------------------------------------------------------------------------
# Session toggle
# ---------------------------------------------------------------------------


async def test_enter_button_opens_the_session_and_refreshes_only_the_markup(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=None)
    sink = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(bot, _tap("aik_in"))
    assert result is not UNHANDLED
    assert ai_module._KOM_MODE_STORE.is_active(USER, USER) is True
    assert "Ком слушает" in _toasts(sink)[0]

    # The answer the user was reading is untouched: only the keyboard is
    # re-rendered (legacy overwrote the text with a status card).
    kinds = [e["kind"] for e in sink]
    assert "edit_markup" in kinds
    assert "edit" not in kinds
    assert "aik_out" in _payloads(next(e for e in sink if e["kind"] == "edit_markup")["markup"])


async def test_exit_button_closes_the_session_but_keeps_history(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=None)
    ai_module._KOM_MODE_STORE.enter(USER, USER)
    ai_module._MEMORY_STORE.record_exchange(USER, "q", "a", chat_id=USER)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap("aik_out"))
    assert ai_module._KOM_MODE_STORE.is_active(USER, USER) is False
    assert ai_module._MEMORY_STORE.size(USER, USER) == 2
    assert "Вышли из Ком" in _toasts(sink)[0]


# ---------------------------------------------------------------------------
# Clear context
# ---------------------------------------------------------------------------


async def test_clear_button_drops_the_window_and_keeps_the_session(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=None)
    ai_module._KOM_MODE_STORE.enter(USER, USER)
    ai_module._MEMORY_STORE.record_exchange(USER, "q", "a", chat_id=USER)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap("aik_clr"))
    assert ai_module._MEMORY_STORE.size(USER, USER) == 0
    # 🧹 clears the context; ⏸ is the button for leaving. Doing both here
    # would make the one destructive control also the one that logs you
    # out — legacy's ``/reset`` conflated them and nobody asked for that.
    assert ai_module._KOM_MODE_STORE.is_active(USER, USER) is True
    assert "Контекст очищен" in _toasts(sink)[0]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def test_export_with_an_empty_window_answers_instead_of_sending_a_file(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=None)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap("aik_exp"))
    assert not [e for e in sink if e["kind"] == "document"]
    assert "нечего выгружать" in _toasts(sink)[0]


async def test_export_hands_back_the_transcript(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=None)
    ai_module._MEMORY_STORE.record_exchange(
        USER, "какая погода?", "солнечно <b>и</b> тепло", chat_id=USER
    )
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap("aik_exp"))
    doc = next(e for e in sink if e["kind"] == "document")
    assert doc["filename"].startswith(f"kom_chat_{USER}_")
    assert doc["filename"].endswith(".txt")
    assert "Диалог с Комом" in doc["content"]
    assert "Ты: какая погода?" in doc["content"]
    # Plain text, not HTML: the model's own angle brackets survive intact
    # because the ``.txt`` would otherwise show the escape sequences.
    assert "Ком: солнечно <b>и</b> тепло" in doc["content"]
    assert "забирай" in doc["caption"]


# ---------------------------------------------------------------------------
# Persona switch — the VIP gate
# ---------------------------------------------------------------------------


async def test_mode_button_is_re_gated_at_tap_time(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A keyboard rendered while the user was VIP outlives the grant.

    Rendering hides the persona row from a non-VIP, but the *tap* is what
    has to refuse — an old card (or a hand-crafted payload) is otherwise
    a free persona switch.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=None)
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap("aik_mode:expert"))
    assert ai_module._MODE_STORE.get(USER) == "default"
    assert "привилегия VIP" in _toasts(sink)[0]
    assert not [e for e in sink if e["kind"] == "edit_markup"]


async def test_vip_mode_switch_lands_and_check_marks_the_new_persona(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=_future_ts())
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap("aik_mode:expert"))
    assert ai_module._MODE_STORE.get(USER) == "expert"
    assert "Стиль: 📚 Эксперт" in _toasts(sink)[0]

    markup = next(e for e in sink if e["kind"] == "edit_markup")["markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert [label for label in labels if label.startswith("✅ ")] == ["✅ 📚 Эксперт"]


async def test_forged_mode_token_is_refused(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase, UsersBase])
    await _seed_vip(registry, vip_till=_future_ts())
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap("aik_mode:root"))
    assert ai_module._MODE_STORE.get(USER) == "default"
    assert "Такого стиля нет" in _toasts(sink)[0]


# ---------------------------------------------------------------------------
# The PRIVATE gate on the callback router
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data", ["aik_in", "aik_out", "aik_clr", "aik_exp", "aik_mode:expert"])
async def test_group_taps_are_not_routed(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    data: str,
) -> None:
    """A control tap from a group must not reach the Kom controls.

    Since #159 the end of the tree acknowledges whatever no handler
    claimed, so "not routed" is no longer visible as an empty sink: it
    is visible as the stale-card toast and nothing else. Any edit, any
    keyboard refresh, any other copy would mean the private-filtered
    router served a group tap after all — and the mode store below
    stays untouched either way.
    """
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase, UsersBase])
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _tap(data, chat_type="supergroup", chat_id=-100_1))
    assert_only_the_stale_tail_answered(sent, "a group tap must not reach the Kom controls")
    assert ai_module._MODE_STORE.get(USER) == "default"


# ---------------------------------------------------------------------------
# RR-6 #65: the daily-limit refusal offers a way forward
# ---------------------------------------------------------------------------


async def test_quota_refusal_offers_vip_and_the_menu(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI,
        schemas=[EconomyBase, UsersBase],
        ai_quota=AiQuotaSettings(free_daily_limit=1),
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, make_message_update("/ask q", user_id=USER))
    sent.clear()
    await dispatcher.feed_update(bot, make_message_update("/ask q", user_id=USER))

    body = sent[0]["text"]
    assert "исчерпан" in body
    # The refusal now names both exits instead of stopping at "no".
    # (The base copy already mentions the reset hour, so the next-step
    # line is pinned by its own opening words.)
    assert "Дальше —" in body
    assert _payloads(sent[0]["markup"]) == ["menu:shop", "menu:home"]


async def test_quota_refusal_in_a_group_stays_plain(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MainMenu`` is private-filtered — a group rescue keyboard would be
    two dead buttons under a refusal, which is worse than no buttons."""
    bot, dispatcher, _ = await make_wired(
        ai_config=_CONFIGURED_AI,
        schemas=[EconomyBase, UsersBase],
        ai_quota=AiQuotaSettings(free_daily_limit=1),
    )
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    group = {"chat_type": "supergroup", "chat_id": -100_1}
    await dispatcher.feed_update(bot, make_message_update("/ask q", user_id=USER, **group))
    sent.clear()
    await dispatcher.feed_update(bot, make_message_update("/ask q", user_id=USER, **group))

    assert "исчерпан" in sent[0]["text"]
    assert "Дальше —" not in sent[0]["text"]
    assert sent[0]["markup"] is None


async def test_quota_refusal_does_not_sell_vip_to_a_vip(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The VIP tier has a finite ceiling, so a VIP CAN hit this refusal.

    The keyboard already dropped the upsell button for a VIP; the body
    text used to keep offering "grab VIP and stop counting" regardless,
    which reads as a bug to the one person who already paid. Both
    surfaces must agree.
    """
    bot, dispatcher, registry = await make_wired(
        ai_config=_CONFIGURED_AI,
        schemas=[EconomyBase, UsersBase],
        ai_quota=AiQuotaSettings(free_daily_limit=99, vip_daily_limit=1),
    )
    await _seed_vip(registry, vip_till=_future_ts())
    sent = capture_outgoing(bot)

    async def fake_ask(self: Any, prompt: str, **_kwargs: Any) -> str:
        return "ok"

    monkeypatch.setattr(ai_service_module.AiService, "ask_with_context", fake_ask)

    await dispatcher.feed_update(bot, make_message_update("/ask q", user_id=USER))
    sent.clear()
    await dispatcher.feed_update(bot, make_message_update("/ask q", user_id=USER))

    body = sent[0]["text"]
    assert "исчерпан" in body
    # The VIP-specific next step, not the upsell one.
    assert "Дальше —" not in body
    assert "VIP-лимит на сегодня" in body
    # And no shop button either — the two surfaces stay in lockstep.
    assert _payloads(sent[0]["markup"]) == ["menu:home"]
