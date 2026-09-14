"""Unit tests for the developer /broadcast flow (Cluster A2, L-94).

Covers the legacy contract (``bot.py:25925-26015``) plus the strangler
deltas:

* developer + private gating on entry;
* draft intake: text (3500-char cap, bot.py:25955), photo+caption
  (1024 cap), non-content nudge, newest-draft-wins replacement;
* cancel callback clears state (bot.py:25979-25986);
* confirm: stale-session toast (bot.py:25991-25993), empty-audience
  short-circuit, audience snapshot before ``state.clear()``;
* background send loop: per-user Forbidden swallowed into ``failed``
  (bot.py:26008-26009), progress report every 100 sends, final
  sent/failed report, photo branch uses ``send_photo``;
* an interrupted fan-out (outer crash, or cancellation at shutdown)
  reports the abort rather than "finished" (#888);
* FSM sweeper ``on_expire_broadcast`` DM.
* the router scopes its two callbacks to private chats, and does it
  through ``F.message.chat.type`` — a CallbackQuery has no ``chat``
  field, so reusing the message-side ``_private`` would kill both
  buttons instead of scoping them (#1588).

Telegram plumbing is faked with the same duck-typed stubs as
``test_ads.py`` — the handlers only touch ``.reply`` / ``.answer*`` /
``send_*`` / FSMContext-shaped objects. i18n keys are not yet in the
YAML during unit runs; ``t()`` falls back to the raw key, so assertions
match key names.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Update

from telegram_invite_bot.handlers import broadcast as bc_mod
from telegram_invite_bot.handlers.broadcast import (
    BroadcastStates,
    _preview_slice,
    on_expire_broadcast,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD

# ``Any``-typed aliases: handlers are exercised with duck-typed fakes;
# per-argument ``type: ignore`` at every call site would obscure asserts.
handle_broadcast_command: Any = bc_mod.handle_broadcast_command
handle_broadcast_content: Any = bc_mod.handle_broadcast_content
handle_broadcast_confirm: Any = bc_mod.handle_broadcast_confirm
handle_broadcast_cancel: Any = bc_mod.handle_broadcast_cancel
# The loop itself, for the shutdown path — a cancelling fake bot is not
# a ``Bot`` and driving it through the confirm handler cannot express
# "cancelled mid-run" without cancelling the awaiting test too.
_run_broadcast: Any = bc_mod._run_broadcast
cancel_inflight: Any = bc_mod.cancel_inflight

_DEV_ID = 555


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeState:
    """Minimal FSMContext stand-in: state string + data dict."""

    def __init__(self, state: str | None = None, data: dict[str, Any] | None = None) -> None:
        self._state = state
        self._data = data or {}
        self.cleared = False

    async def get_state(self) -> str | None:
        return self._state

    async def set_state(self, state: Any) -> None:
        self._state = getattr(state, "state", state)

    async def get_data(self) -> dict[str, Any]:
        return dict(self._data)

    async def set_data(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    async def clear(self) -> None:
        self.cleared = True
        self._state = None
        self._data = {}


class FakeMessage:
    def __init__(
        self,
        *,
        chat_type: str = "private",
        text: str = "",
        user_id: int = _DEV_ID,
        photo_file_id: str | None = None,
        caption: str | None = None,
    ) -> None:
        self.chat = SimpleNamespace(type=chat_type, id=user_id)
        self.from_user = SimpleNamespace(id=user_id, first_name="Dev", username="dev", is_bot=False)
        self.text = text
        self.caption = caption
        self.photo = (
            [SimpleNamespace(file_id="small"), SimpleNamespace(file_id=photo_file_id)]
            if photo_file_id
            else None
        )
        self.replies: list[tuple[str, Any]] = []
        self.answers: list[str] = []
        self.photo_answers: list[tuple[str, str | None]] = []

    async def reply(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, kwargs.get("reply_markup")))

    async def answer(self, text: str, **_: Any) -> None:
        self.answers.append(text)

    async def answer_photo(self, file_id: str, caption: str | None = None, **_: Any) -> None:
        self.photo_answers.append((file_id, caption))


class FakeBot:
    """Records sends; raises Forbidden for ids in ``blocked``."""

    def __init__(self, *, blocked: frozenset[int] = frozenset()) -> None:
        self.blocked = blocked
        self.sent: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str, str | None]] = []
        self.edits: list[tuple[int, int, str]] = []

    def _check(self, chat_id: int) -> None:
        if chat_id in self.blocked:
            raise TelegramForbiddenError(
                method="sendMessage",  # type: ignore[arg-type]
                message="bot was blocked by the user",
            )

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self._check(chat_id)
        self.sent.append((chat_id, text))

    async def send_photo(
        self, chat_id: int, file_id: str, caption: str | None = None, **_: Any
    ) -> None:
        self._check(chat_id)
        self.photos.append((chat_id, file_id, caption))

    async def edit_message_text(
        self, text: str, *, chat_id: int, message_id: int, **_: Any
    ) -> None:
        self.edits.append((chat_id, message_id, text))


class FakeCallback:
    def __init__(self, *, user_id: int = _DEV_ID) -> None:
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        # Plain namespace — NOT an aiogram Message, so the handler takes
        # the "inaccessible message" fallback branch (progress via DM).
        self.message = SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=42)
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, **_: Any) -> None:
        self.answers.append(text)


class FakeUsersRepo:
    def __init__(self, ids: list[int]) -> None:
        self._ids = ids
        self.calls = 0

    async def all_user_ids(self) -> list[int]:
        self.calls += 1
        return list(self._ids)


def _settings(*, dev_ids: frozenset[int] = frozenset({_DEV_ID})) -> Any:
    return SimpleNamespace(bot=SimpleNamespace(is_developer=lambda uid: uid in dev_ids))


async def _drain_background() -> None:
    """Await every spawned broadcast task to completion."""
    while bc_mod._BACKGROUND_TASKS:
        await asyncio.gather(*list(bc_mod._BACKGROUND_TASKS))


@pytest.fixture(autouse=True)
def _no_send_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero out the inter-send pause so loop tests run instantly."""
    monkeypatch.setattr(bc_mod, "_SEND_PAUSE_SECONDS", 0)


@pytest.fixture(autouse=True)
def _no_stray_fan_out() -> None:
    """Start every test with an empty task set.

    The set is module-global and, since #1496, load-bearing: a task left
    behind by an earlier test would make the next confirm refuse. Every
    test that spawns drains, so this only guards against a future one
    that forgets.
    """
    bc_mod._BACKGROUND_TASKS.clear()


# ── Entry gating ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_broadcast_rejects_non_developer() -> None:
    msg = FakeMessage(user_id=111)
    state = FakeState()
    await handle_broadcast_command(msg, state, _settings(), "ru")
    assert await state.get_state() is None
    assert "только разработчику" in msg.replies[0][0]


@pytest.mark.asyncio
async def test_broadcast_enters_fsm_with_sweeper_stamp() -> None:
    msg = FakeMessage()
    state = FakeState()
    await handle_broadcast_command(msg, state, _settings(), "en")
    assert await state.get_state() == BroadcastStates.awaiting_content.state
    data = await state.get_data()
    assert STATE_ENTERED_AT_FIELD in data
    assert data["lang"] == "en"
    assert "Mass broadcast" in msg.replies[0][0]


@pytest.mark.asyncio
async def test_broadcast_refuses_to_stomp_other_flow() -> None:
    msg = FakeMessage()
    state = FakeState(state="WithdrawStates:awaiting_confirm")
    await handle_broadcast_command(msg, state, _settings(), "ru")
    assert await state.get_state() == "WithdrawStates:awaiting_confirm"
    assert "/cancel" in msg.replies[0][0]


# ── Draft intake ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_content_text_draft_capped_and_previewed() -> None:
    msg = FakeMessage(text="x" * 5000)
    state = FakeState(state=BroadcastStates.awaiting_content.state)
    await handle_broadcast_content(msg, state, _settings(), "ru")
    data = await state.get_data()
    assert data["bcast_kind"] == "text"
    assert len(data["bcast_text"]) == 3500  # legacy bot.py:25955 cap
    text, markup = msg.replies[0]
    assert "всем" in text
    assert markup is not None  # confirm/cancel keyboard attached


@pytest.mark.asyncio
async def test_content_photo_draft_with_caption_cap() -> None:
    msg = FakeMessage(photo_file_id="big-file-id", caption="c" * 2000)
    state = FakeState(state=BroadcastStates.awaiting_content.state)
    await handle_broadcast_content(msg, state, _settings(), "ru")
    data = await state.get_data()
    assert data["bcast_kind"] == "photo"
    assert data["bcast_file_id"] == "big-file-id"  # largest PhotoSize wins
    assert len(data["bcast_caption"]) == 1024
    # Photo echoed back, then the confirm prompt with keyboard.
    assert msg.photo_answers == [("big-file-id", "c" * 1024)]
    assert msg.replies[0][1] is not None


@pytest.mark.asyncio
async def test_content_text_draft_is_echoed_as_recipients_will_see_it() -> None:
    """The operator must see the rendered body before 5 000 people do.

    The photo branch has always echoed the draft itself
    (``answer_photo``) and then asked for confirmation; the text branch
    only ever showed the HTML-escaped source inside the confirm card. So
    the one thing a broadcast operator needs to check — whether the
    markup renders — was the one thing the preview could not show, and a
    body Telegram cannot parse was found by the fan-out instead: every
    recipient costs two API calls, because ``ParseModeFallbackMiddleware``
    resends each refusal with parse mode off, and logs an ERROR per send.
    """
    msg = FakeMessage(text="<b>Важно</b> и <i>срочно</i>")
    state = FakeState(state=BroadcastStates.awaiting_content.state)

    await handle_broadcast_content(msg, state, _settings(), "ru")

    assert msg.answers == ["<b>Важно</b> и <i>срочно</i>"], (
        "the draft was never echoed, so the operator never saw it rendered"
    )
    # The confirm card still carries the escaped source, so the operator
    # can read the markup they typed as well as the result of it.
    prompt, markup = msg.replies[0]
    assert "&lt;b&gt;Важно&lt;/b&gt;" in prompt
    assert markup is not None


@pytest.mark.asyncio
async def test_content_rejects_non_text_non_photo() -> None:
    msg = FakeMessage(text="")  # e.g. a sticker landed in the state
    state = FakeState(state=BroadcastStates.awaiting_content.state)
    await handle_broadcast_content(msg, state, _settings(), "ru")
    assert "непустой текст или фото" in msg.replies[0][0]
    assert "bcast_kind" not in await state.get_data()


@pytest.mark.asyncio
async def test_content_newest_draft_replaces_old() -> None:
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "photo", "bcast_file_id": "old", "bcast_caption": "old"},
    )
    msg = FakeMessage(text="new body")
    await handle_broadcast_content(msg, state, _settings(), "ru")
    data = await state.get_data()
    assert data["bcast_kind"] == "text"
    assert data["bcast_text"] == "new body"
    assert "bcast_file_id" not in data


@pytest.mark.asyncio
async def test_content_from_non_dev_clears_state_silently() -> None:
    msg = FakeMessage(user_id=999, text="hijack")
    state = FakeState(state=BroadcastStates.awaiting_content.state)
    await handle_broadcast_content(msg, state, _settings(), "ru")
    assert state.cleared
    assert msg.replies == []


# ── Cancel ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_clears_state() -> None:
    cb = FakeCallback()
    state = FakeState(state=BroadcastStates.awaiting_content.state)
    await handle_broadcast_cancel(cb, state, _settings(), "ru")
    assert state.cleared
    assert cb.answers  # callback always answered


@pytest.mark.asyncio
async def test_cancel_ignores_non_developer() -> None:
    cb = FakeCallback(user_id=12)
    state = FakeState(state=BroadcastStates.awaiting_content.state)
    await handle_broadcast_cancel(cb, state, _settings(), "ru")
    assert not state.cleared


# ── Confirm + send loop ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirm_stale_session_toast() -> None:
    cb = FakeCallback()
    state = FakeState(state=None)  # already swept / cancelled
    repo = FakeUsersRepo([1, 2])
    await handle_broadcast_confirm(cb, state, FakeBot(), repo, _settings(), "ru")
    assert cb.answers == ["Сессия истекла"]
    assert repo.calls == 0


@pytest.mark.asyncio
async def test_confirm_without_draft_is_expired() -> None:
    cb = FakeCallback()
    state = FakeState(state=BroadcastStates.awaiting_content.state, data={})
    await handle_broadcast_confirm(cb, state, FakeBot(), FakeUsersRepo([1]), _settings(), "ru")
    assert cb.answers == ["Сессия истекла"]
    assert not state.cleared


@pytest.mark.asyncio
async def test_confirm_empty_audience_short_circuits() -> None:
    cb = FakeCallback()
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hi"},
    )
    await handle_broadcast_confirm(cb, state, FakeBot(), FakeUsersRepo([]), _settings(), "ru")
    assert state.cleared
    assert not bc_mod._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_confirm_sends_to_all_and_counts_failures() -> None:
    bot = FakeBot(blocked=frozenset({2, 4}))
    cb = FakeCallback()
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hello"},
    )
    await handle_broadcast_confirm(
        cb, state, bot, FakeUsersRepo([1, 2, 3, 4, 5]), _settings(), "ru"
    )
    await _drain_background()

    delivered = [chat for chat, text in bot.sent if text == "hello"]
    assert delivered == [1, 3, 5]  # blocked users swallowed, loop continued
    # Final report DM'd to the developer (fallback branch: fake message
    # is not an aiogram Message, so progress goes via send_message).
    reports = [text for chat, text in bot.sent if chat == _DEV_ID]
    assert any("Рассылка завершена" in r for r in reports)
    assert state.cleared


@pytest.mark.asyncio
async def test_flood_wait_is_slept_out_and_the_recipient_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TelegramRetryAfter`` is back-pressure, not a per-user failure.

    Swallowing it into the ``failed`` counter keeps the loop sending at
    20 msg/s straight through Telegram's wait window: every send in that
    window is refused too, so the tail of the audience silently goes
    unreached while the penalty grows. The fan-out must sleep the window
    out and retry the recipient it was refused on.
    """
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds: float) -> None:
        waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(bc_mod.asyncio, "sleep", _record_sleep)

    bot = FakeBot()
    flooded = {3}

    async def _send_message(chat_id: int, text: str, **_: Any) -> None:
        if chat_id in flooded:
            flooded.discard(chat_id)
            raise TelegramRetryAfter(
                method="sendMessage",  # type: ignore[arg-type]
                message="Too Many Requests",
                retry_after=7,
            )
        bot.sent.append((chat_id, text))

    monkeypatch.setattr(bot, "send_message", _send_message)

    cb = FakeCallback()
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hello"},
    )
    await handle_broadcast_confirm(
        cb, state, bot, FakeUsersRepo([1, 2, 3, 4, 5]), _settings(), "ru"
    )
    await _drain_background()

    delivered = [chat for chat, text in bot.sent if text == "hello"]
    assert delivered == [1, 2, 3, 4, 5]  # 3 retried in place, not dropped
    assert 7 in waits  # the flood window was honoured
    reports = [text for chat, text in bot.sent if chat == _DEV_ID]
    assert any("Рассылка завершена" in r for r in reports)


@pytest.mark.asyncio
async def test_flood_wait_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absurd ``retry_after`` must not park the fan-out for an hour.

    We honour Telegram's number up to ``_MAX_RETRY_AFTER_SECONDS``; past
    that the recipient is retried once and, if still refused, counted as
    an ordinary failure.
    """
    waits: list[float] = []
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds: float) -> None:
        waits.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(bc_mod.asyncio, "sleep", _record_sleep)

    bot = FakeBot()

    async def _send_message(chat_id: int, text: str, **_: Any) -> None:
        if chat_id == 1:
            raise TelegramRetryAfter(
                method="sendMessage",  # type: ignore[arg-type]
                message="Too Many Requests",
                retry_after=3600,
            )
        bot.sent.append((chat_id, text))

    monkeypatch.setattr(bot, "send_message", _send_message)

    cb = FakeCallback()
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hello"},
    )
    await handle_broadcast_confirm(cb, state, bot, FakeUsersRepo([1, 2]), _settings(), "ru")
    await _drain_background()

    assert max(waits) == bc_mod._MAX_RETRY_AFTER_SECONDS
    assert [chat for chat, text in bot.sent if text == "hello"] == [2]


@pytest.mark.asyncio
async def test_confirm_photo_broadcast_uses_send_photo() -> None:
    bot = FakeBot()
    cb = FakeCallback()
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "photo", "bcast_file_id": "fid", "bcast_caption": "cap"},
    )
    await handle_broadcast_confirm(cb, state, bot, FakeUsersRepo([7, 8]), _settings(), "ru")
    await _drain_background()
    assert bot.photos == [(7, "fid", "cap"), (8, "fid", "cap")]


@pytest.mark.asyncio
async def test_progress_report_every_100_sends() -> None:
    bot = FakeBot()
    cb = FakeCallback()
    audience = list(range(1000, 1250))  # 250 users → progress at 100, 200
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hi"},
    )
    await handle_broadcast_confirm(cb, state, bot, FakeUsersRepo(audience), _settings(), "ru")
    await _drain_background()
    progress = [text for chat, text in bot.sent if "Рассылка:" in text]
    assert len(progress) == 2


class _CrashingBot(FakeBot):
    """Fails every audience send with a NON-Telegram error.

    The reporting path still works, because reports go to ``_DEV_ID`` and
    that id is never in the audience. Without that split the test could
    not tell "reported the wrong thing" from "reported nothing".
    """

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id != _DEV_ID:
            raise RuntimeError("fan-out exploded")
        await super().send_message(chat_id, text, **kwargs)


class _CancellingBot(FakeBot):
    """Raises ``CancelledError`` on the second recipient — process shutdown."""

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id == 2:
            raise asyncio.CancelledError
        await super().send_message(chat_id, text, **kwargs)


@pytest.mark.asyncio
async def test_crashed_fanout_reports_aborted_not_finished() -> None:
    """#888: the outer ``except`` has no ``return``, so control reaches the
    final report either way. Before the fix that report was the success
    key, and a run that died on recipient 1 of 3 told the developer it had
    finished — the one message that guarantees nobody re-runs it.
    """
    bot = _CrashingBot()
    cb = FakeCallback()
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hi"},
    )
    await handle_broadcast_confirm(cb, state, bot, FakeUsersRepo([1, 2, 3]), _settings(), "ru")
    await _drain_background()

    reports = [text for chat, text in bot.sent if chat == _DEV_ID]
    assert reports, "the developer must still be told something"
    assert not any("Рассылка завершена" in r for r in reports)
    aborted = [r for r in reports if "прервана" in r]
    assert aborted, reports
    # Nothing was sent and nothing was counted as failed, so all three
    # recipients are un-attempted — the number ``h_broadcast_done`` cannot
    # express and the only one that tells the operator what is still owed.
    assert "3 из 3" in aborted[-1]


@pytest.mark.asyncio
async def test_cancelled_fanout_reports_aborted_not_finished() -> None:
    """Same misreport on the shutdown path, which had its own copy of the
    success key. Cancellation is still re-raised.
    """
    bot = _CancellingBot()
    with pytest.raises(asyncio.CancelledError):
        await _run_broadcast(
            bot,
            user_ids=[1, 2, 3],
            kind="text",
            text="hi",
            file_id="",
            caption="",
            progress_chat_id=_DEV_ID,
            progress_message_id=None,
            lang="ru",
        )

    reports = [text for chat, text in bot.sent if chat == _DEV_ID]
    assert not any("Рассылка завершена" in r for r in reports)
    aborted = [r for r in reports if "прервана" in r]
    assert aborted, reports
    # One delivered before the cancellation, two never attempted.
    assert "2 из 3" in aborted[-1]


@pytest.mark.asyncio
async def test_clean_run_still_reports_finished() -> None:
    """The guard against over-correcting: a run that completes must keep
    the success key, or every broadcast would read as a failure.
    """
    bot = FakeBot(blocked=frozenset({2}))
    cb = FakeCallback()
    state = FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hi"},
    )
    await handle_broadcast_confirm(cb, state, bot, FakeUsersRepo([1, 2, 3]), _settings(), "ru")
    await _drain_background()

    reports = [text for chat, text in bot.sent if chat == _DEV_ID]
    assert any("Рассылка завершена" in r for r in reports)
    assert not any("прервана" in r for r in reports)


def _draft_state() -> FakeState:
    """A session parked on a ready-to-send text draft."""
    return FakeState(
        state=BroadcastStates.awaiting_content.state,
        data={"bcast_kind": "text", "bcast_text": "hello"},
    )


@pytest.mark.asyncio
async def test_confirm_refuses_while_a_fan_out_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1496 — one fan-out at a time.

    Two confirms used to spawn two loops over the same audience: every
    user got the message twice, and the two loops shared one outbound
    rate budget. The refusal must land before the audience snapshot,
    which is why ``repo.calls`` is asserted as well as the toast.
    """
    gate = asyncio.Event()

    async def blocked_loop(*_args: Any, **_kwargs: Any) -> None:
        await gate.wait()

    monkeypatch.setattr(bc_mod, "_run_broadcast", blocked_loop)

    first = FakeCallback()
    await handle_broadcast_confirm(
        first, _draft_state(), FakeBot(), FakeUsersRepo([1, 2]), _settings(), "ru"
    )
    assert len(bc_mod._BACKGROUND_TASKS) == 1

    second = FakeCallback()
    second_state = _draft_state()
    repo = FakeUsersRepo([1, 2])
    await handle_broadcast_confirm(second, second_state, FakeBot(), repo, _settings(), "ru")

    assert second.answers == [t("h_broadcast_already_running", "ru")]
    assert repo.calls == 0, "the refusal must precede the audience snapshot"
    assert not second_state.cleared, "the draft survives, so the dev can resend later"
    assert len(bc_mod._BACKGROUND_TASKS) == 1

    gate.set()
    await _drain_background()


@pytest.mark.asyncio
async def test_confirm_allowed_again_once_the_fan_out_finishes() -> None:
    """#1496 refuses while running — and only while running."""
    bot = FakeBot()
    await handle_broadcast_confirm(
        FakeCallback(), _draft_state(), bot, FakeUsersRepo([1]), _settings(), "ru"
    )
    await _drain_background()
    assert not bc_mod._BACKGROUND_TASKS  # done-callback discarded it

    second = FakeCallback()
    second_state = _draft_state()
    await handle_broadcast_confirm(second, second_state, bot, FakeUsersRepo([1]), _settings(), "ru")
    await _drain_background()

    assert second_state.cleared
    assert t("h_broadcast_already_running", "ru") not in second.answers
    assert [chat for chat, text in bot.sent if text == "hello"] == [1, 1]


class SlowUsersRepo(FakeUsersRepo):
    """``all_user_ids`` that suspends, the way a real query does."""

    async def all_user_ids(self) -> list[int]:
        await asyncio.sleep(0)
        return await super().all_user_ids()


@pytest.mark.asyncio
async def test_two_simultaneous_confirms_spawn_one_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1496 — why the emptiness check needs ``_confirm_lock`` around it.

    The check is a read and the spawn that makes it true is several
    awaits later, the audience snapshot among them. Two taps arriving
    close enough together both find the set empty, and the second one's
    snapshot is exactly the suspension the first needs to slip past it.
    Serialising the whole body is what turns "usually one loop" into
    "one loop".
    """
    gate = asyncio.Event()

    async def blocked_loop(*_args: Any, **_kwargs: Any) -> None:
        await gate.wait()

    monkeypatch.setattr(bc_mod, "_run_broadcast", blocked_loop)

    repo = SlowUsersRepo([1, 2])
    first = FakeCallback()
    second = FakeCallback()
    await asyncio.gather(
        handle_broadcast_confirm(first, _draft_state(), FakeBot(), repo, _settings(), "ru"),
        handle_broadcast_confirm(second, _draft_state(), FakeBot(), repo, _settings(), "ru"),
    )

    assert len(bc_mod._BACKGROUND_TASKS) == 1, "the loser must not spawn a second loop"
    assert repo.calls == 1, "and must not even reach the audience snapshot"
    assert t("h_broadcast_already_running", "ru") in first.answers + second.answers

    gate.set()
    await _drain_background()


@pytest.mark.asyncio
async def test_confirm_ignores_non_developer() -> None:
    cb = FakeCallback(user_id=13)
    state = FakeState(state=BroadcastStates.awaiting_content.state)
    repo = FakeUsersRepo([1])
    await handle_broadcast_confirm(cb, state, FakeBot(), repo, _settings(), "ru")
    assert repo.calls == 0
    assert not state.cleared


# ── Helpers + sweeper ────────────────────────────────────────────────────────


def test_preview_slice_ellipsis() -> None:
    assert _preview_slice("a" * 500) == "a" * 500
    assert _preview_slice("a" * 501) == "a" * 500 + "…"


@pytest.mark.asyncio
async def test_on_expire_broadcast_dms_developer() -> None:
    bot = FakeBot()
    key = SimpleNamespace(user_id=_DEV_ID, chat_id=_DEV_ID, bot_id=1)
    await on_expire_broadcast(bot, key, {"lang": "en"})  # type: ignore[arg-type]
    assert len(bot.sent) == 1 and bot.sent[0][0] == _DEV_ID and "cancelled" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_on_expire_broadcast_swallows_blocked() -> None:
    bot = FakeBot(blocked=frozenset({_DEV_ID}))
    key = SimpleNamespace(user_id=_DEV_ID, chat_id=_DEV_ID, bot_id=1)
    await on_expire_broadcast(bot, key, {})  # type: ignore[arg-type]
    assert bot.sent == []


# ── Callback scope (#1588) ───────────────────────────────────────────────────


def _callback(chat_type: str) -> Any:
    """A minimal CallbackQuery carrying an inline keyboard in ``chat_type``."""
    return Update.model_validate(
        {
            "update_id": 1,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": _DEV_ID, "is_bot": False, "first_name": "T"},
                "chat_instance": "ci-1",
                "data": "bc:confirm",
                "message": {
                    "message_id": 1,
                    "date": 1_700_000_000,
                    "chat": {"id": _DEV_ID, "type": chat_type},
                    "from": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "text": "prompt",
                },
            },
        }
    ).callback_query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "passes"),
    [("private", True), ("group", False), ("supergroup", False), ("channel", False)],
    ids=["private", "group", "supergroup", "channel"],
)
async def test_callbacks_are_scoped_to_private_chats(chat_type: str, passes: bool) -> None:
    """The confirm/cancel buttons must not be reachable from a group.

    Both bodies gate on ``is_developer`` anyway, so this is defence in
    depth — but nothing pinned the scope, and the two registrations
    carried no filter at all before #1588.
    """
    router = bc_mod.build_router(_settings())
    accepted, _ = await router.callback_query.check_root_filters(_callback(chat_type))
    assert bool(accepted) is passes


def test_the_message_scope_filter_does_not_work_on_callbacks() -> None:
    """Why the fix could not simply reuse ``_private`` (#1588).

    ``_private`` is ``F.chat.type == ChatType.PRIVATE`` and a
    CallbackQuery has no ``chat`` field at all, so it resolves to
    nothing for every callback — attaching it would have made both
    buttons permanently dead rather than private-scoped. The chat is
    one level down, on the message the button hangs off.
    """
    private = _callback("private")
    assert not (F.chat.type == ChatType.PRIVATE).resolve(private)
    assert (F.message.chat.type == ChatType.PRIVATE).resolve(private)


# ── Shutdown drain (#1815) ───────────────────────────────────────────────────


class _HangingBot(FakeBot):
    """Blocks forever on audience sends; the report to ``_DEV_ID`` lands.

    A fan-out that has already returned cannot be cancelled, so the test
    needs a loop that is genuinely parked mid-send when shutdown arrives
    — which is also the only state in which #1815 costs anything.
    """

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id != _DEV_ID:
            await asyncio.sleep(3600)
        await super().send_message(chat_id, text, **kwargs)


class _WedgedBot(FakeBot):
    """Blocks forever on EVERY send, the abort report included."""

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        await asyncio.sleep(3600)


async def _spawn_fanout(bot: Any, *, audience: list[int]) -> asyncio.Task[None]:
    """Put a parked fan-out into ``_BACKGROUND_TASKS``, the way confirm does."""
    task: asyncio.Task[None] = asyncio.create_task(
        _run_broadcast(
            bot,
            user_ids=audience,
            kind="text",
            text="hi",
            file_id="",
            caption="",
            progress_chat_id=_DEV_ID,
            progress_message_id=None,
            lang="ru",
        )
    )
    bc_mod._BACKGROUND_TASKS.add(task)
    task.add_done_callback(bc_mod._BACKGROUND_TASKS.discard)
    # Let the loop reach its first (blocking) send before we cancel it.
    for _ in range(5):
        await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_cancel_inflight_drains_the_set_and_reports_the_abort() -> None:
    """#1815 — nothing was cancelling this task at shutdown.

    ``Application.close`` drains its OWN list of sweepers; the fan-out
    lives here instead, because the set doubles as the single-flight
    marker (#1496). So the already-tested ``CancelledError`` branch never
    fired: the loop kept going until the bot session closed under it, and
    a ``RuntimeError`` from a closed aiohttp session is not a
    ``TelegramAPIError`` — it escaped the report's own suppression too.

    The report matters more than the cancellation. The set is process
    state, so the restart that follows forgets a broadcast ever ran; a
    re-run then sends twice to everyone the first pass reached. The
    operator can only weigh that if they are told where it stopped.
    """
    bot = _HangingBot()
    task = await _spawn_fanout(bot, audience=[1, 2, 3])
    assert list(bc_mod._BACKGROUND_TASKS) == [task]

    await cancel_inflight()

    assert task.done()
    assert not bc_mod._BACKGROUND_TASKS
    reports = [text for chat, text in bot.sent if chat == _DEV_ID]
    assert reports, "the developer must be told the run was cut short"
    assert not any("Рассылка завершена" in r for r in reports)
    # Parked on recipient 1, so all three are still owed.
    assert "прервана" in reports[-1]
    assert "3 из 3" in reports[-1]


@pytest.mark.asyncio
async def test_cancel_inflight_is_bounded_when_the_report_send_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain sits between the sweepers and ``bot.session.close``.

    A report send that never answers must not hold the process open:
    an unbounded await here turns a tidy shutdown into a ``SIGKILL`` 90
    seconds later (``TimeoutStopSec``), which loses the very teardown
    steps the drain was added to run before. Bounded, not shielded —
    on timeout we WANT the send interrupted.
    """
    monkeypatch.setattr(bc_mod, "_SHUTDOWN_DRAIN_SECONDS", 0.05)
    bot = _WedgedBot()
    task = await _spawn_fanout(bot, audience=[1, 2, 3])

    started = time.monotonic()
    await cancel_inflight()
    elapsed = time.monotonic() - started

    assert elapsed < 5, elapsed
    assert task.done()
    assert not bc_mod._BACKGROUND_TASKS
    assert bot.sent == []


@pytest.mark.asyncio
async def test_cancel_inflight_is_a_no_op_with_nothing_running() -> None:
    """The common case: shutdown with no broadcast in flight."""
    assert not bc_mod._BACKGROUND_TASKS
    await cancel_inflight()
    assert not bc_mod._BACKGROUND_TASKS
