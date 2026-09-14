"""Game-card TTL sweep (RR-3 #33) — :mod:`handlers.game_cards`.

Legacy shredded every game reply 30s after posting
(``AUTO_DELETE_GAMES``/``GAME_MESSAGES_TTL``, bot.py:2588-2589). The port
restores the capability with the default INVERTED — our cards now carry
the balance, unlocked achievements and the remaining allowance, so the
sweep is something an operator turns on for a spammy group, not
something that happens to everyone.

These tests pin the three gates (flag, chat type, and the strong task
reference that keeps a sleeping sweep from being garbage-collected)
without waiting on a real 30-second TTL.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from telegram_invite_bot.config.settings import GamesConfig
from telegram_invite_bot.handlers import game_cards

if TYPE_CHECKING:
    from collections.abc import Iterator

# Bound BEFORE any monkeypatching: the tests replace ``asyncio.sleep``
# module-wide to skip the TTL, and the replacement (plus the drain
# helpers) must still be able to yield to the loop for real.
_REAL_SLEEP = asyncio.sleep


async def _noop_sleep(_delay: float) -> None:
    """Zero-cost stand-in for ``asyncio.sleep`` inside the sweep."""
    await _REAL_SLEEP(0)


class _FakeCard:
    """Stand-in for the sent Message — records whether it was deleted."""

    def __init__(self, *, fail: bool = False) -> None:
        self.deleted = False
        self._fail = fail

    async def delete(self) -> None:
        if self._fail:
            raise RuntimeError("message to delete not found")
        self.deleted = True


def _settings(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    """Point the module at a throwaway GamesConfig (get_settings is cached)."""
    games = GamesConfig(**kwargs)

    def _fake_get_settings() -> Any:
        return type("S", (), {"games": games})()

    monkeypatch.setattr(game_cards, "get_settings", _fake_get_settings)


@pytest.fixture(autouse=True)
def _drain_pending() -> Iterator[None]:
    """Never leak a scheduled sweep into the next test."""
    yield
    for task in list(game_cards._pending_deletions):
        task.cancel()
    game_cards._pending_deletions.clear()


async def _settle() -> None:
    """Yield to the loop until every scheduled sweep has finished."""
    pending = list(game_cards._pending_deletions)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_sweep_deletes_the_card_in_a_group_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, auto_delete=True, ttl_seconds=5)
    # 5s is the config floor; zero the actual wait so the test is instant.
    monkeypatch.setattr(game_cards.asyncio, "sleep", _noop_sleep)
    card = _FakeCard()

    game_cards.schedule_card_sweep(card, chat_type="supergroup")  # type: ignore[arg-type]
    await _settle()

    assert card.deleted is True


async def test_sweep_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default keeps every card — legacy's ON is deliberately
    not restored (see GamesConfig)."""
    _settings(monkeypatch)
    monkeypatch.setattr(game_cards.asyncio, "sleep", _noop_sleep)
    card = _FakeCard()

    game_cards.schedule_card_sweep(card, chat_type="supergroup")  # type: ignore[arg-type]
    await _settle()

    assert card.deleted is False
    assert not game_cards._pending_deletions  # nothing was even scheduled


async def test_sweep_never_touches_a_private_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DM has no spam problem, and shredding someone's own history is
    surprising — the group gate holds even with the flag on."""
    _settings(monkeypatch, auto_delete=True, ttl_seconds=5)
    monkeypatch.setattr(game_cards.asyncio, "sleep", _noop_sleep)
    card = _FakeCard()

    game_cards.schedule_card_sweep(card, chat_type="private")  # type: ignore[arg-type]
    await _settle()

    assert card.deleted is False
    assert not game_cards._pending_deletions


async def test_a_failed_delete_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No delete rights / already-gone / >48h old are all ordinary. None of
    them may surface as an error on a settled game."""
    _settings(monkeypatch, auto_delete=True, ttl_seconds=5)
    monkeypatch.setattr(game_cards.asyncio, "sleep", _noop_sleep)
    card = _FakeCard(fail=True)

    game_cards.schedule_card_sweep(card, chat_type="supergroup")  # type: ignore[arg-type]
    [task] = list(game_cards._pending_deletions)
    await _settle()

    assert task.exception() is None
    assert card.deleted is False


async def test_unreadable_settings_never_break_the_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_settings`` validates the whole app config. A failure there must
    disable the (cosmetic) sweep, not raise out of a settled game whose
    card is already sent and whose wallet is already debited."""

    def _boom() -> Any:
        raise ValueError("1 validation error for BotConfig")

    monkeypatch.setattr(game_cards, "get_settings", _boom)
    monkeypatch.setattr(game_cards.asyncio, "sleep", _noop_sleep)
    card = _FakeCard()

    game_cards.schedule_card_sweep(card, chat_type="supergroup")  # type: ignore[arg-type]
    await _settle()

    assert card.deleted is False
    assert not game_cards._pending_deletions


async def test_scheduled_task_is_strongly_referenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio only weakly references a running task; without the module
    set a sleeping sweep can be collected mid-flight and never fire."""
    _settings(monkeypatch, auto_delete=True, ttl_seconds=5)
    monkeypatch.setattr(game_cards.asyncio, "sleep", _noop_sleep)
    card = _FakeCard()

    game_cards.schedule_card_sweep(card, chat_type="group")  # type: ignore[arg-type]

    assert len(game_cards._pending_deletions) == 1
    # …and the set drains itself once the task completes.
    await _settle()
    await _REAL_SLEEP(0)  # let the done-callback run
    assert not game_cards._pending_deletions
