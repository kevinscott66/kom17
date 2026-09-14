"""Unit tests for the per-group antiflood middleware (L-56, cluster F4).

Covered:

* ``_SlidingWindows`` counting, window drain, LRU bound, reset.
* ``AntifloodMiddleware``:
  - mutes + notifies once when a non-admin exceeds the burst threshold;
  - never consumes the update (downstream handler always runs);
  - inert when antiflood is disabled / chat is private / sender is a bot
    or an anonymous (sender_chat) actor;
  - admin exemption, including the fail-safe ``None`` verdict;
  - owner exemption (#1863), answered locally without a Telegram probe;
  - one notice per mute (subsequent burst messages are silent);
  - a restrict failure is swallowed and not retried per-message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers.antiflood import (
    AntifloodMiddleware,
    _SlidingWindows,
)
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigView
from telegram_invite_bot.utils.chat_permissions import MUTED_PERMS

_CHAT = -100123
_USER = 777


# ---------------------------------------------------------------------------
# _SlidingWindows
# ---------------------------------------------------------------------------


def test_window_counts_hits_within_window() -> None:
    w = _SlidingWindows(capacity=10)
    key = (_CHAT, _USER)
    assert w.hit(key, now=0.0, window=10.0) == 1
    assert w.hit(key, now=1.0, window=10.0) == 2
    assert w.hit(key, now=2.0, window=10.0) == 3


def test_window_drains_old_hits() -> None:
    w = _SlidingWindows(capacity=10)
    key = (_CHAT, _USER)
    w.hit(key, now=0.0, window=10.0)
    w.hit(key, now=1.0, window=10.0)
    # 12s later both earlier hits are outside the 10s window.
    assert w.hit(key, now=12.0, window=10.0) == 1


def test_window_reset() -> None:
    w = _SlidingWindows(capacity=10)
    key = (_CHAT, _USER)
    w.hit(key, now=0.0, window=10.0)
    w.reset(key)
    assert w.hit(key, now=0.5, window=10.0) == 1


def test_window_lru_bound() -> None:
    w = _SlidingWindows(capacity=2)
    w.hit((1, 1), now=0.0, window=10.0)
    w.hit((2, 2), now=0.0, window=10.0)
    w.hit((3, 3), now=0.0, window=10.0)  # evicts (1, 1)
    # (1, 1) was evicted → its history is gone, count restarts at 1.
    assert w.hit((1, 1), now=0.1, window=10.0) == 1


# ---------------------------------------------------------------------------
# AntifloodMiddleware
# ---------------------------------------------------------------------------


def _cfg(
    *,
    enabled: bool = True,
    max_msgs: int = 3,
    window_sec: int = 10,
    mute_minutes: int = 10,
) -> GroupModConfigView:
    return GroupModConfigView(
        group_id=_CHAT,
        automod_enabled=True,
        profanity_enabled=True,
        max_warns=3,
        mute_minutes=1440,
        autoban_enabled=True,
        antiflood_enabled=enabled,
        flood_max_msgs=max_msgs,
        flood_window_sec=window_sec,
        flood_mute_minutes=mute_minutes,
    )


def _fake_settings(*developers: int) -> Any:
    """Minimal ``Settings`` stand-in: the middleware reads one predicate."""
    return SimpleNamespace(bot=SimpleNamespace(is_developer=lambda user_id: user_id in developers))


@dataclass
class _FakeBot:
    restrict_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_restrict: bool = False

    async def restrict_chat_member(self, **kwargs: Any) -> bool:
        if self.raise_on_restrict:
            raise RuntimeError("no rights")
        self.restrict_calls.append(kwargs)
        return True


class _RecordingMessage(Message):
    """Real aiogram ``Message`` (so the middleware's ``isinstance`` gate
    passes) with a recording ``answer`` override."""

    answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> Any:  # type: ignore[override]
        self.answers.append(text)
        return None


def _make_message(
    *,
    chat_type: str = "supergroup",
    user_id: int = _USER,
    is_bot: bool = False,
    sender_chat: bool = False,
    no_user: bool = False,
) -> _RecordingMessage:
    msg = _RecordingMessage.model_construct(
        message_id=1,
        date=cast("Any", None),
        chat=cast("Any", SimpleNamespace(id=_CHAT, type=chat_type)),
        from_user=cast(
            "Any",
            None if no_user else SimpleNamespace(id=user_id, is_bot=is_bot, full_name="Flooder"),
        ),
        sender_chat=cast("Any", SimpleNamespace(id=_CHAT) if sender_chat else None),
        text="spam",
    )
    object.__setattr__(msg, "answers", [])
    return msg


def _middleware(
    cfg: GroupModConfigView, *, admin_verdict: bool | None = False
) -> AntifloodMiddleware:
    """A middleware with ``_is_exempt_admin`` stubbed out entirely — the
    owner short-circuit inside the real method is covered separately by
    ``test_owner_is_exempt_without_a_telegram_probe``."""
    mw = AntifloodMiddleware(registry=cast("Any", None), settings=cast("Any", _fake_settings()))

    async def _fake_config_for(group_id: int, now: float) -> GroupModConfigView:
        return cfg

    mw._config_for = _fake_config_for  # type: ignore[method-assign]

    async def _fake_is_exempt(bot: Any, chat_id: int, user_id: int) -> bool:
        return admin_verdict is None or admin_verdict

    mw._is_exempt_admin = _fake_is_exempt  # type: ignore[method-assign]
    return mw


async def _run(mw: AntifloodMiddleware, msg: Message, bot: _FakeBot, times: int = 1) -> bool:
    """Feed ``msg`` through the middleware ``times`` times; return whether
    the downstream handler ran every time (it must)."""
    runs = {"n": 0}

    async def _handler(ev: Any, data: dict[str, Any]) -> str:
        runs["n"] += 1
        return "ok"

    for _ in range(times):
        result = await mw(_handler, msg, {"bot": cast("Any", bot), "lang": "en"})
        assert result == "ok"  # never consumes
    return runs["n"] == times


async def test_mutes_and_notifies_on_burst() -> None:
    mw = _middleware(_cfg(max_msgs=3, mute_minutes=15))
    bot = _FakeBot()
    msg = _make_message()
    # 4th message exceeds max_msgs=3 within the window.
    assert await _run(mw, msg, bot, times=4) is True
    assert len(bot.restrict_calls) == 1
    call = bot.restrict_calls[0]
    assert call["chat_id"] == _CHAT
    assert call["user_id"] == _USER
    # #680: the shared constant, not a one-field literal leaning on
    # Telegram treating every omitted flag as False.
    assert call["permissions"] == MUTED_PERMS
    assert len(msg.answers) == 1  # one notice per mute


class _ParkingBot(_FakeBot):
    """A bot whose ``restrict_chat_member`` parks until released.

    The only way to hold a coroutine inside the Telegram round trip the
    real middleware makes, which is the window #2014 is about.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def restrict_chat_member(self, **kwargs: Any) -> bool:
        self.entered.set()
        await self.release.wait()
        return await super().restrict_chat_member(**kwargs)


async def test_a_burst_arriving_during_the_restrict_call_mutes_once() -> None:
    """#2014: the mute marker must be claimed with the threshold decision.

    aiogram dispatches every update in its own task, and a flood is by
    definition several messages in flight at once. The already-muted
    guard reads ``_muted_until`` at the top of the middleware, but the
    marker was written only after ``restrict_chat_member`` returned —
    so every burst message that arrived while that call was in the air
    re-crossed the threshold, restricted again, and posted its own
    ``h_af_muted_notice`` into the group.

    The group sees a wall of "muted" notices for one mute, and each
    repeat also writes a ``moderation_log`` row that
    ``/groupadmin → Статистика`` counts — the admin-visible mute number
    is inflated by however fast the flooder types.
    """
    mw = _middleware(_cfg(max_msgs=2, mute_minutes=10))
    bot = _ParkingBot()
    msg = _make_message()

    async def _handler(ev: Any, data: dict[str, Any]) -> str:
        return "ok"

    data: dict[str, Any] = {"bot": cast("Any", bot), "lang": "en"}
    for _ in range(2):
        await mw(_handler, msg, data)

    # Three more land together; the first to cross the threshold parks
    # inside the restrict call and the other two run while it is there.
    tasks = [asyncio.create_task(mw(_handler, msg, data)) for _ in range(3)]
    await bot.entered.wait()
    await asyncio.sleep(0)
    bot.release.set()
    await asyncio.gather(*tasks)

    assert len(bot.restrict_calls) == 1, (
        f"one burst, one mute — restrict was called {len(bot.restrict_calls)} times"
    )
    assert len(msg.answers) == 1, (
        f"the group got {len(msg.answers)} mute notices for one mute: {msg.answers}"
    )


async def test_under_threshold_no_action() -> None:
    mw = _middleware(_cfg(max_msgs=5))
    bot = _FakeBot()
    msg = _make_message()
    assert await _run(mw, msg, bot, times=5) is True
    assert bot.restrict_calls == []
    assert msg.answers == []


async def test_notice_only_once_per_mute() -> None:
    mw = _middleware(_cfg(max_msgs=2, mute_minutes=10))
    bot = _FakeBot()
    msg = _make_message()
    # Keep flooding well past the threshold: one restrict, one notice.
    assert await _run(mw, msg, bot, times=10) is True
    assert len(bot.restrict_calls) == 1
    assert len(msg.answers) == 1


async def test_disabled_config_is_inert() -> None:
    mw = _middleware(_cfg(enabled=False, max_msgs=1))
    bot = _FakeBot()
    msg = _make_message()
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []


async def test_private_chat_skipped() -> None:
    mw = _middleware(_cfg(max_msgs=1))
    bot = _FakeBot()
    msg = _make_message(chat_type="private")
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []


async def test_bot_sender_skipped() -> None:
    mw = _middleware(_cfg(max_msgs=1))
    bot = _FakeBot()
    msg = _make_message(is_bot=True)
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []


async def test_anonymous_sender_chat_skipped() -> None:
    mw = _middleware(_cfg(max_msgs=1))
    bot = _FakeBot()
    msg = _make_message(sender_chat=True)
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []


async def test_missing_from_user_skipped() -> None:
    mw = _middleware(_cfg(max_msgs=1))
    bot = _FakeBot()
    msg = _make_message(no_user=True)
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []


async def test_admin_exempt() -> None:
    mw = _middleware(_cfg(max_msgs=2), admin_verdict=True)
    bot = _FakeBot()
    msg = _make_message()
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []
    assert msg.answers == []


async def test_api_error_verdict_treated_as_admin() -> None:
    # ``None`` verdict (get_chat_member failed) must fail safe: no mute.
    mw = _middleware(_cfg(max_msgs=2), admin_verdict=None)
    bot = _FakeBot()
    msg = _make_message()
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []


async def test_restrict_failure_swallowed_and_not_retried() -> None:
    mw = _middleware(_cfg(max_msgs=2))
    bot = _FakeBot(raise_on_restrict=True)
    msg = _make_message()
    # Must not raise; downstream handler always runs; no notice is posted
    # for a failed restrict and the failure is not retried per-message.
    assert await _run(mw, msg, bot, times=10) is True
    assert msg.answers == []


async def test_config_error_swallowed() -> None:
    mw = AntifloodMiddleware(registry=cast("Any", None), settings=cast("Any", _fake_settings()))

    async def _boom(group_id: int, now: float) -> GroupModConfigView:
        raise RuntimeError("db down")

    mw._config_for = _boom  # type: ignore[method-assign]
    bot = _FakeBot()
    msg = _make_message()
    assert await _run(mw, msg, bot, times=3) is True
    assert bot.restrict_calls == []


async def test_admin_verdict_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ``_is_exempt_admin`` caches the underlying verdict."""
    mw = AntifloodMiddleware(registry=cast("Any", None), settings=cast("Any", _fake_settings()))
    calls = {"n": 0}

    async def _fake_is_chat_admin_any(bot: Any, chat_id: int, user_id: int) -> bool:
        calls["n"] += 1
        return False

    monkeypatch.setattr(
        "telegram_invite_bot.handlers.antiflood.is_chat_admin_any",
        _fake_is_chat_admin_any,
    )
    bot = cast("Any", _FakeBot())
    assert await mw._is_exempt_admin(bot, _CHAT, _USER) is False
    assert await mw._is_exempt_admin(bot, _CHAT, _USER) is False
    assert calls["n"] == 1  # second call served from cache


async def test_owner_is_exempt_without_a_telegram_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1863: the bot owner is never auto-muted, and finding that out
    costs no ``get_chat_member``.

    The owner passes every other moderation gate and need not be an
    administrator of a group their own bot serves, so before this the
    bot would mute its owner for a burst of their own messages —
    ``wordfilter._is_exempt_from_sanction`` already honoured the owner
    and this twin did not (#1848). The probe counter is the point of
    the test: the check must be answered locally, ahead of the network.
    """
    mw = AntifloodMiddleware(
        registry=cast("Any", None), settings=cast("Any", _fake_settings(_USER))
    )
    cfg = _cfg(max_msgs=3)

    async def _fake_config_for(group_id: int, now: float) -> GroupModConfigView:
        return cfg

    mw._config_for = _fake_config_for  # type: ignore[method-assign]

    probes = {"n": 0}

    async def _fake_is_chat_admin_any(bot: Any, chat_id: int, user_id: int) -> bool:
        probes["n"] += 1
        return False

    monkeypatch.setattr(
        "telegram_invite_bot.handlers.antiflood.is_chat_admin_any",
        _fake_is_chat_admin_any,
    )

    bot = _FakeBot()
    msg = _make_message()
    assert await _run(mw, msg, bot, times=10) is True
    assert bot.restrict_calls == []
    assert msg.answers == []
    assert probes["n"] == 0


async def test_non_owner_still_probes_and_is_muted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1863 must exempt the owner and nobody else: a stranger in the
    same chat still takes the Telegram probe and still gets muted."""
    mw = AntifloodMiddleware(
        registry=cast("Any", None), settings=cast("Any", _fake_settings(_USER + 1))
    )
    cfg = _cfg(max_msgs=3)

    async def _fake_config_for(group_id: int, now: float) -> GroupModConfigView:
        return cfg

    mw._config_for = _fake_config_for  # type: ignore[method-assign]

    probes = {"n": 0}

    async def _fake_is_chat_admin_any(bot: Any, chat_id: int, user_id: int) -> bool:
        probes["n"] += 1
        return False

    monkeypatch.setattr(
        "telegram_invite_bot.handlers.antiflood.is_chat_admin_any",
        _fake_is_chat_admin_any,
    )

    bot = _FakeBot()
    msg = _make_message()
    assert await _run(mw, msg, bot, times=4) is True
    assert probes["n"] == 1
    assert len(bot.restrict_calls) == 1
