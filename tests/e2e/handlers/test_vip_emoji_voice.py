"""End-to-end ``/voice`` routing (T-023).

Mocks at the OpenAI SDK boundary the same way ``test_ai.py`` does for
``/ai`` / ``/gpt`` / ``/chat``: monkey-patch the handler's
``_build_openai_client`` to return a fake client whose
``audio.speech.create`` returns a duck-typed response with a
``.content`` bytes attribute. No real network, no ``openai`` SDK
import required at test time.

Custom outgoing capture: ``capture_outgoing`` from the shared
conftest only swallows ``SendMessage`` / ``SendPhoto``; the handler
also emits ``SendVoice`` for the audio note. We attach our own
``make_request`` patch that captures both surfaces into a uniform
sink, so each test reads a list of dicts with ``kind`` in
``{"voice", "text"}``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Chat, Update
from aiogram.types import Message as AioMessage
from aiogram.types import User as AioUser
from sqlalchemy import select, update

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import vip_emoji_voice as voice_handler_module
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, chat_type: str = "private", user_id: int = 555) -> Update:
    return make_message_update(text, chat_type=chat_type, user_id=user_id)


def _fake_tts_response(audio: bytes) -> object:
    """Duck-typed OpenAI TTS response.

    The SDK returns ``HttpxBinaryResponseContent`` with a ``.content``
    bytes attribute. :class:`SimpleNamespace` is sufficient and
    avoids importing the SDK just to construct a fixture.
    """
    return SimpleNamespace(content=audio)


def _patch_tts_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: object | None = None,
    raises: Exception | None = None,
) -> AsyncMock:
    """Replace the handler-edge ``_build_openai_client`` factory with
    one returning a mock client whose ``audio.speech.create`` returns
    ``response`` (or raises ``raises``)."""
    create_mock = AsyncMock()
    if raises is not None:
        create_mock.side_effect = raises
    else:
        create_mock.return_value = response

    fake_client = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create_mock)))
    monkeypatch.setattr(voice_handler_module, "_build_openai_client", lambda _key: fake_client)
    return create_mock


def _capture_voice_and_text(monkeypatch: pytest.MonkeyPatch, bot: Any) -> list[dict[str, Any]]:
    """Replacement for the shared ``capture_outgoing`` that also
    swallows ``SendVoice``.

    Returns a sink of dicts shaped like
    ``{"kind": "voice", "chat_id": int, "audio": bytes}`` for voice
    notes and ``{"kind": "text", "chat_id": int, "text": str}`` for
    follow-up text replies. Synthesised return shape keeps aiogram's
    response parser happy.
    """
    sink: list[dict[str, Any]] = []

    def _synth_message(chat_id: int, text: str = "ok") -> AioMessage:
        return AioMessage(
            message_id=1,
            date=datetime(2024, 1, 1, tzinfo=UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=AioUser(id=0, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        if name == "SendMessage":
            sink.append({"kind": "text", "chat_id": method.chat_id, "text": method.text})
            return _synth_message(method.chat_id, method.text)
        if name == "SendVoice":
            voice = getattr(method, "voice", None)
            audio = getattr(voice, "data", b"") if voice is not None else b""
            sink.append(
                {
                    "kind": "voice",
                    "chat_id": method.chat_id,
                    "audio": audio,
                }
            )
            return _synth_message(method.chat_id)
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return sink


async def _seed_wallet(
    registry: Any,
    user_id: int,
    *,
    balance: int,
    vip_till: float | None = None,
) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            EconomyUser(
                user_id=user_id,
                balance=balance,
                language="ru",
                vip_till=vip_till,
            )
        )
        await session.commit()


async def _balance(registry: Any, user_id: int) -> int | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    return wallet.balance if wallet is not None else None


async def _totals(registry: Any, user_id: int) -> tuple[int, int]:
    """``(total_spent, total_earned)`` off the wallet row (#1895)."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        wallet = await EconomyRepo(session).get(user_id)
    assert wallet is not None
    return int(wallet.total_spent), int(wallet.total_earned)


async def _ledger_types(registry: Any) -> list[str]:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        rows = (await session.execute(select(Transaction))).scalars().all()
    return sorted(r.type for r in rows)


def _future_ts(days: int = 30) -> float:
    return (datetime.now(UTC) + timedelta(days=days)).timestamp()


# ---------------------------------------------------------------------------
# Routing: bare /voice → usage hint; group /voice still works
# ---------------------------------------------------------------------------


async def test_voice_bare_shows_usage(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = _capture_voice_and_text(monkeypatch, bot)

    result = await dispatcher.feed_update(bot, _update("/voice"))
    assert result is not UNHANDLED
    assert sent[0]["kind"] == "text"
    assert "/voice" in sent[0]["text"]
    assert "VIP" in sent[0]["text"] or "vip" in sent[0]["text"].lower()


# ---------------------------------------------------------------------------
# VIP gate: non-VIP refusal (no upstream, no wallet touch)
# ---------------------------------------------------------------------------


async def test_voice_non_vip_refused(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=None)
    sent = _capture_voice_and_text(monkeypatch, bot)

    def _explode(_key: str) -> object:
        raise AssertionError("_build_openai_client must not be called for non-VIPs")

    monkeypatch.setattr(voice_handler_module, "_build_openai_client", _explode)

    result = await dispatcher.feed_update(bot, _update("/voice привет"))
    assert result is not UNHANDLED
    assert sent[0]["kind"] == "text"
    assert "VIP" in sent[0]["text"]
    # No voice sent; wallet untouched.
    assert all(s["kind"] != "voice" for s in sent)
    assert await _balance(registry, 555) == 500


# ---------------------------------------------------------------------------
# Success: audio sent, coins debited
# ---------------------------------------------------------------------------


async def test_voice_vip_success_charges_and_sends_audio(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10-char input * 0.001 coin/char = 1 coin (rounded up). Balance
    500 → 499 after debit; audio bytes flow through to SendVoice."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"fake-mp3-bytes"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    create_mock.assert_awaited_once()
    # Exactly one voice + one text follow-up.
    voices = [s for s in sent if s["kind"] == "voice"]
    texts = [s for s in sent if s["kind"] == "text"]
    assert len(voices) == 1
    assert voices[0]["audio"] == b"fake-mp3-bytes"
    assert len(texts) == 1
    assert "1" in texts[0]["text"]  # 1 coin
    assert await _balance(registry, 555) == 499


def _capture_with_voice_forbidden(
    monkeypatch: pytest.MonkeyPatch, bot: Any
) -> list[dict[str, Any]]:
    """Like :func:`_capture_voice_and_text`, but ``SendVoice`` raises
    ``VOICE_MESSAGES_FORBIDDEN`` (recipient restricted voice notes) and
    ``SendAudio`` is captured — exercising the handler's audio fallback.
    """
    sink: list[dict[str, Any]] = []

    def _synth(chat_id: int, text: str = "ok") -> AioMessage:
        return AioMessage(
            message_id=1,
            date=datetime(2024, 1, 1, tzinfo=UTC),
            chat=Chat(id=chat_id, type="private"),
            from_user=AioUser(id=0, is_bot=True, first_name="bot"),
            text=text,
        )

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        if name == "SendMessage":
            sink.append({"kind": "text", "chat_id": method.chat_id, "text": method.text})
            return _synth(method.chat_id, method.text)
        if name == "SendVoice":
            raise TelegramBadRequest(
                method=method,
                message="Bad Request: VOICE_MESSAGES_FORBIDDEN",
            )
        if name == "SendAudio":
            audio = getattr(getattr(method, "audio", None), "data", b"")
            sink.append({"kind": "audio", "chat_id": method.chat_id, "audio": audio})
            return _synth(method.chat_id)
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return sink


async def test_voice_falls_back_to_audio_when_voice_forbidden(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recipient restricted voice notes (VOICE_MESSAGES_FORBIDDEN): the
    already-synthesised+billed audio is re-delivered as a regular audio
    file rather than lost behind a generic error."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_with_voice_forbidden(monkeypatch, bot)

    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"fallback-mp3-bytes"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    # No voice note delivered; the same bytes arrived as an audio file.
    assert all(s["kind"] != "voice" for s in sent)
    audio = [s for s in sent if s["kind"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["audio"] == b"fallback-mp3-bytes"
    # Billing stands (the user got their audio), wallet debited 1 coin.
    assert await _balance(registry, 555) == 499


def _capture_with_voice_undeliverable(
    monkeypatch: pytest.MonkeyPatch, bot: Any
) -> list[dict[str, Any]]:
    """``SendVoice`` fails the way a blocked bot fails — no fallback can
    rescue it, unlike ``VOICE_MESSAGES_FORBIDDEN``.
    """
    sink: list[dict[str, Any]] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        name = type(method).__name__
        sink.append({"kind": name})
        raise TelegramForbiddenError(method=method, message="bot was blocked by the user")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return sink


async def test_voice_refunds_when_the_audio_cannot_be_delivered(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthesis succeeded and was billed, then the upload failed — the
    user blocked the bot, or it was removed from the chat, between the
    request and the send. The bytes die with the request; there is
    nothing left to re-deliver, so the charge must not stand.

    The refund is explicit, and has to be: the debit is committed
    through the update's ``Checkpoint`` before the upstream call (so a
    minute of synthesis can't hold the writer lock on economy.db — see
    the lock test below), which means the rollback on the re-raise no
    longer has a debit to undo. The handler credits the coins back and
    checkpoints that credit on the spot, then re-raises.

    Both halves have to stay: swallowing the send failure would leave
    the user paying for audio they never got, and dropping the explicit
    refund would do the same silently — the assertion below is the same
    either way, so only this test says which mechanism is load-bearing.

    #1895: the handler releases rather than credits, which is only
    possible because the service leaves the hold open. The balance
    assertion here cannot tell the two apart — the lifetime-counter
    test below is the one that can.

    The service's own ``_refund`` covers the other half: an upstream
    failure returns normally, so those coins are handed back in-band
    too.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    attempted = _capture_with_voice_undeliverable(monkeypatch, bot)

    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"lost-mp3-bytes"))

    await dispatcher.feed_update(bot, _update("/voice helloworld"))

    # The scenario has to be the real one: synthesis ran (so the wallet
    # was debited) and the upload was actually attempted. Without these
    # the balance assertion below would pass for the wrong reason.
    create_mock.assert_awaited_once()
    assert attempted[0]["kind"] == "SendVoice"
    # Held 1 coin for the synthesis, released it straight back.
    assert await _balance(registry, 555) == 500


def _capture_with_voice_cancelled(
    monkeypatch: pytest.MonkeyPatch, bot: Any
) -> list[dict[str, Any]]:
    """``SendVoice`` is cancelled the way a shutdown cancels it.

    Not a ``TelegramAPIError`` twin: ``CancelledError`` is a
    ``BaseException``, which is exactly why the handler used to miss it.
    """
    sink: list[dict[str, Any]] = []

    async def fake_make_request(
        _bot: Any,
        method: Any,
        timeout: Any = None,  # noqa: ARG001, ASYNC109
    ) -> Any:
        sink.append({"kind": type(method).__name__})
        raise asyncio.CancelledError

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return sink


async def test_voice_refunds_when_the_upload_is_cancelled(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1954: the shutdown twin of the delivery-failure test above.

    Same shape, same consequence, different exception hierarchy: the
    synthesis succeeded and its hold is committed, then the process is
    told to stop before the bytes reach Telegram. ``except Exception``
    does not see a ``CancelledError``, so the block that hands the coins
    back never ran and the user paid for audio that was never sent.

    The re-raise is asserted too — a compensation that swallowed the
    cancel would keep a handler running inside a loop that is being torn
    down, which is worse than the coin it saves.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    attempted = _capture_with_voice_cancelled(monkeypatch, bot)

    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"lost-mp3"))

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.feed_update(bot, _update("/voice helloworld"))

    # The scenario is the real one: synthesis ran, the upload was
    # genuinely attempted, and only then was the task cancelled.
    create_mock.assert_awaited_once()
    assert attempted[0]["kind"] == "SendVoice"
    assert await _balance(registry, 555) == 500
    assert await _totals(registry, 555) == (0, 0)
    assert await _ledger_types(registry) == ["tts", "tts_refund"]


async def test_voice_success_books_the_spend_once_and_earns_nothing(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1895: the delivered round settles the hold exactly once.

    Settlement runs in the handler, AFTER ``SendVoice`` — not inside
    ``TtsService.synthesize``. The user-visible balance is identical
    either way, so the lifetime counters are what pin the ordering:
    one coin of ``total_spent``, nothing earned, and a single ``tts``
    ledger row, because settling moves no coins and writes no row of
    its own — the hold already did both.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"paid-mp3"))

    await dispatcher.feed_update(bot, _update("/voice helloworld"))

    # The audio really went out — otherwise the counters below would
    # be right for the wrong reason.
    assert [s["kind"] for s in sent].count("voice") == 1
    assert await _balance(registry, 555) == 499
    assert await _totals(registry, 555) == (1, 0)
    assert await _ledger_types(registry) == ["tts"]


async def test_voice_undeliverable_leaves_both_lifetime_counters_at_zero(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1895 proper: the refunded round must be a no-op on both counters.

    The bug this pins: the service used to settle the hold before
    returning, so by the time the upload failed the coins were a
    completed spend and the only primitive that could hand them back
    was ``credit`` — which books ``total_earned``. The round then
    ended with the balance restored (so nobody notices) and BOTH
    lifetime counters up by the cost, permanently:
    ``EconomyRepo.bump_totals`` refuses negative arguments, so there
    is no operator fix short of hand-editing the wallet row. Any VIP
    who blocks the bot between the request and the upload could farm
    it at the rate of the voice limiter.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    attempted = _capture_with_voice_undeliverable(monkeypatch, bot)

    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"lost-mp3"))

    await dispatcher.feed_update(bot, _update("/voice helloworld"))

    # Synthesis ran and the upload was genuinely attempted.
    create_mock.assert_awaited_once()
    assert attempted[0]["kind"] == "SendVoice"
    assert await _balance(registry, 555) == 500
    # hold -> release: the balance round-tripped and no lifetime
    # counter moved in either direction.
    assert await _totals(registry, 555) == (0, 0)
    assert await _ledger_types(registry) == ["tts", "tts_refund"]


async def test_voice_does_not_hold_the_write_lock_across_synthesis(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The debit is committed before the upstream call, not after it.

    ``TtsConfig.timeout_seconds`` defaults to 60 — twelve times
    SQLite's ``busy_timeout``. Under ``BEGIN IMMEDIATE`` the debit owns
    ``economy.db`` until the middleware commits, so a single ``/voice``
    would park every wallet, game, transfer and shop write in the bot
    behind a minute of synthesis, and they would all come back
    ``database is locked``. This is the worst instance of that pattern
    in the bot, which is why it is pinned here.

    Two independent signals, because they fail for different reasons:
    the probe *writes* from a separate session (that write blocks and
    then raises if the lock is still held), and it *reads* the debited
    wallet (WAL readers never block, so an uncommitted debit shows as
    the pre-debit 500 rather than raising).
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    other_updates_could_write: list[bool] = []
    balance_visible_during_synthesis: list[int | None] = []

    async def _synthesise(**_kwargs: Any) -> object:
        async with registry.session(DBName.ECONOMY)() as other:
            other.add(EconomyUser(user_id=999, balance=7, language="ru"))
            await other.commit()
        other_updates_could_write.append(True)
        balance_visible_during_synthesis.append(await _balance(registry, 555))
        return _fake_tts_response(b"unblocked-mp3-bytes")

    create_mock = _patch_tts_client(monkeypatch, response=None)
    create_mock.side_effect = _synthesise

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED

    assert other_updates_could_write == [True]
    # 500 here would mean the debit was still sitting in the update's
    # open transaction — the exact state that holds the writer lock.
    assert balance_visible_during_synthesis == [499]
    # The probe's own write survived, and committing early cost nothing:
    # the charge still stands and the audio still went out.
    assert await _balance(registry, 999) == 7
    assert await _balance(registry, 555) == 499
    assert [s["kind"] for s in sent] == ["voice", "text"]


# ---------------------------------------------------------------------------
# Missing-key fallback
# ---------------------------------------------------------------------------


async def test_voice_missing_key_renders_not_configured(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    def _explode(_key: str) -> object:
        raise AssertionError("must not build client when key is missing")

    monkeypatch.setattr(voice_handler_module, "_build_openai_client", _explode)

    result = await dispatcher.feed_update(bot, _update("/voice hi"))
    assert result is not UNHANDLED
    # No voice was sent; wallet untouched.
    assert all(s["kind"] != "voice" for s in sent)
    assert "временно недоступен" in sent[-1]["text"]
    assert await _balance(registry, 555) == 500


# ---------------------------------------------------------------------------
# Insufficient balance refusal (VIP with empty wallet)
# ---------------------------------------------------------------------------


async def test_voice_vip_insufficient_balance_refuses_before_upstream(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=0, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"never-returned"))

    result = await dispatcher.feed_update(bot, _update("/voice hi"))
    assert result is not UNHANDLED
    create_mock.assert_not_awaited()
    assert all(s["kind"] != "voice" for s in sent)
    assert "Недостаточно" in sent[-1]["text"]
    assert await _balance(registry, 555) == 0


async def test_voice_lost_hold_race_releases_the_write_lock(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1969: the OTHER way ``INSUFFICIENT_BALANCE`` is reached.

    The test above is the cheap pre-check — ``balance < cost``, a plain
    read, nothing held. This one is the race the pre-check cannot
    close: the wallet the handler read said the voice was affordable
    and ``hold``'s ``WHERE balance >= amount`` was the guard that said
    no. A guarded UPDATE matching ZERO rows still promotes the
    connection to ``BEGIN IMMEDIATE`` (``db/engines.py``), so the
    refusal below was rendered with ``economy.db`` locked over a write
    that never happened — the same shape as #1967 / #1968.

    Reproduced the way it actually happens: the row on disk is poorer
    than the wallet this command read, by inflating what
    ``get_or_create`` reports and leaving the row alone. The probe is
    the lock itself — a second connection taking ``BEGIN IMMEDIATE``
    mid-reply.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    # Zero on disk, rich to whoever asks: the gap is what turns ``hold``
    # into a zero-row write while the pre-check waves the call through.
    await _seed_wallet(registry, 555, balance=0, vip_till=_future_ts())
    sessionmaker = registry.session(DBName.ECONOMY)

    original_get = EconomyRepo.get_or_create

    async def rich_get_or_create(self: EconomyRepo, user_id: int, **kwargs: Any) -> Any:
        wallet = await original_get(self, user_id, **kwargs)
        return replace(wallet, balance=wallet.balance + 100_000)

    monkeypatch.setattr(EconomyRepo, "get_or_create", rich_get_or_create)

    sent = _capture_voice_and_text(monkeypatch, bot)
    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"never"))

    probe: list[str] = []
    original_request = bot.session.make_request

    async def probing(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ != "SendMessage":
            return await original_request(_bot, method, timeout=timeout)
        try:
            async with sessionmaker() as other:
                await other.execute(
                    update(EconomyUser).where(EconomyUser.user_id == -1).values(balance=0)
                )
                await other.commit()
        except Exception as exc:  # noqa: BLE001 — the verdict IS the exception
            probe.append(repr(exc))
        else:
            probe.append("free")
        return await original_request(_bot, method, timeout=timeout)

    monkeypatch.setattr(bot.session, "make_request", probing)

    result = await dispatcher.feed_update(bot, _update("/voice hi"))

    assert result is not UNHANDLED
    create_mock.assert_not_awaited()  # refused before OpenAI, as the pre-check path is
    assert "Недостаточно" in sent[-1]["text"]
    assert probe == ["free"], f"the refusal was sent with economy.db locked: {probe}"
    # The guard did its job: no coins moved.
    assert await _balance(registry, 555) == 0


# ---------------------------------------------------------------------------
# Upstream error: no charge, no voice, error copy rendered
# ---------------------------------------------------------------------------


async def test_voice_upstream_error_does_not_charge(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    _patch_tts_client(monkeypatch, raises=RuntimeError("openai connection refused"))

    result = await dispatcher.feed_update(bot, _update("/voice hello"))
    assert result is not UNHANDLED
    assert all(s["kind"] != "voice" for s in sent)
    assert "ошибка провайдера" in sent[-1]["text"]
    assert await _balance(registry, 555) == 500


# ---------------------------------------------------------------------------
# VIP-unlimited flag: synthesis runs, debit skipped
# ---------------------------------------------------------------------------


async def test_voice_vip_unlimited_flag_skips_debit_only_for_top_tier(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-P-4: VIP_UNLIMITED_VOICE=true alone is not enough. The user
    must also be in ``VIP_UNLIMITED_VOICE_USER_IDS`` (the top-tier
    allowlist). For an in-list user: audio sent, wallet untouched,
    gifted-copy rendered."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VIP_UNLIMITED_VOICE", "true")
    monkeypatch.setenv("VIP_UNLIMITED_VOICE_USER_IDS", "555")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=10, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)
    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"gift-mp3"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    voices = [s for s in sent if s["kind"] == "voice"]
    texts = [s for s in sent if s["kind"] == "text"]
    assert len(voices) == 1
    assert voices[0]["audio"] == b"gift-mp3"
    assert "Подарок" in texts[-1]["text"]
    # Wallet not touched despite synthesis.
    assert await _balance(registry, 555) == 10


async def test_voice_vip_unlimited_flag_does_not_gift_low_tier_vip(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-P-4: a VIP NOT in the top-tier allowlist still pays for
    /voice even when the global flag is on. Pins the per-tier
    contract: ordinary VIPs pay, only the named cohort gets gifted.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VIP_UNLIMITED_VOICE", "true")
    monkeypatch.setenv("VIP_UNLIMITED_VOICE_USER_IDS", "999")  # not 555
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)
    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"paid"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    voices = [s for s in sent if s["kind"] == "voice"]
    texts = [s for s in sent if s["kind"] == "text"]
    assert len(voices) == 1
    assert "Подарок" not in texts[-1]["text"]
    # Wallet was debited.
    assert (await _balance(registry, 555)) < 500


# ---------------------------------------------------------------------------
# Group-chat success: same path works outside private chats
# ---------------------------------------------------------------------------


async def test_voice_works_in_group(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)
    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"grp"))

    result = await dispatcher.feed_update(bot, _update("/voice привет", chat_type="supergroup"))
    assert result is not UNHANDLED
    voices = [s for s in sent if s["kind"] == "voice"]
    assert len(voices) == 1


# ---------------------------------------------------------------------------
# Too-long text refusal (boundary at OPENAI_TTS_MAX_CHARS)
# ---------------------------------------------------------------------------


async def test_voice_too_long_refused_before_upstream(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Tighten the cap so the test prompt is over the limit without
    # building a 4096-char string.
    monkeypatch.setenv("OPENAI_TTS_MAX_CHARS", "10")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"never-returned"))

    result = await dispatcher.feed_update(bot, _update("/voice this prompt is way too long"))
    assert result is not UNHANDLED
    create_mock.assert_not_awaited()
    assert all(s["kind"] != "voice" for s in sent)
    assert "Слишком длинный" in sent[-1]["text"]
    assert await _balance(registry, 555) == 500


# ---------------------------------------------------------------------------
# L-90: per-tier daily voice quota
# ---------------------------------------------------------------------------


async def _seed_voice_uses_today(registry: Any, user_id: int, count: int) -> None:
    """Insert ``count`` ``type='tts'`` debit rows dated now (UTC naive)
    so :meth:`TransactionsRepo.voice_today_count` reads them as today's
    usage — the same ledger the quota gate counts."""
    when = datetime.now(UTC).replace(tzinfo=None)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add_all(
            Transaction(
                from_id=user_id,
                amount=1,
                type="tts",
                reason="vip_emoji_voice",
                date=when,
            )
            for _ in range(count)
        )
        await session.commit()


async def test_voice_over_daily_quota_refused_before_upstream(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary VIP at the default 20/day ceiling is refused on the
    21st call — no upstream, no debit."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    await _seed_voice_uses_today(registry, 555, 20)
    sent = _capture_voice_and_text(monkeypatch, bot)

    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"never-returned"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    # Quota gate fired before the upstream client was even built/called.
    create_mock.assert_not_awaited()
    assert all(s["kind"] != "voice" for s in sent)
    assert "озвучка кончилась" in sent[-1]["text"]
    # Wallet untouched.
    assert await _balance(registry, 555) == 500


async def test_voice_under_daily_quota_still_synthesises(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """19 prior uses today (default ceiling 20) → the 20th still goes
    through: audio sent, debit lands."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    await _seed_voice_uses_today(registry, 555, 19)
    sent = _capture_voice_and_text(monkeypatch, bot)
    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"ok-mp3"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    assert any(s["kind"] == "voice" for s in sent)
    assert (await _balance(registry, 555)) < 500


async def test_voice_top_tier_cohort_bypasses_quota(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``VIP_UNLIMITED_VOICE`` allowlist cohort is UNLIMITED tier:
    synthesis goes through even far over the ordinary daily ceiling,
    and (gifted) the wallet is untouched."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VIP_UNLIMITED_VOICE", "true")
    monkeypatch.setenv("VIP_UNLIMITED_VOICE_USER_IDS", "555")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=10, vip_till=_future_ts())
    await _seed_voice_uses_today(registry, 555, 50)  # way over 20/day
    sent = _capture_voice_and_text(monkeypatch, bot)
    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"gift-mp3"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    assert any(s["kind"] == "voice" for s in sent)
    # Gifted cohort → wallet untouched.
    assert await _balance(registry, 555) == 10


async def test_voice_daily_quota_ceiling_is_operator_tunable(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1963: the ceiling the module calls "Operator-tunable" must obey
    an operator.

    ``VoiceQuotaConfig.vip_daily_limit`` had no env alias and the sole
    construction site passed only ``unlimited_user_ids``, so 20/day was
    reachable exactly one way: edit the source and redeploy. The
    neighbouring AI quota has been env-driven since it was written
    (``AI_QUOTA_VIP_DAILY_LIMIT``), and TTS is the metered one of the
    two — the module docstring calls an unbounded VIP allowance "an
    operator-cost abuse vector" in as many words. During an upstream
    cost incident the only lever that existed was the allowlist, which
    removes the cap rather than lowering it.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_TTS_VIP_DAILY_LIMIT", "3")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    await _seed_voice_uses_today(registry, 555, 3)
    sent = _capture_voice_and_text(monkeypatch, bot)
    create_mock = _patch_tts_client(monkeypatch, response=_fake_tts_response(b"never-returned"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    create_mock.assert_not_awaited()
    assert all(s["kind"] != "voice" for s in sent)
    assert "озвучка кончилась" in sent[-1]["text"]
    assert "(3/3)" in sent[-1]["text"]
    assert await _balance(registry, 555) == 500


async def test_voice_daily_quota_zero_means_unlimited_from_configuration(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service's ``0 == unlimited`` branch was unreachable too.

    It is the project-wide convention (``AiQuotaConfig`` uses it, and
    ``AI_QUOTA_VIP_DAILY_LIMIT=0`` is documented as how an operator
    restores the old unlimited behaviour), and the voice service
    implements it — but with no alias there was no way to ask for it.
    Distinct from the allowlist bypass, which also stops CHARGING: this
    user still pays.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_TTS_VIP_DAILY_LIMIT", "0")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    await _seed_voice_uses_today(registry, 555, 50)  # far over the default 20
    sent = _capture_voice_and_text(monkeypatch, bot)
    _patch_tts_client(monkeypatch, response=_fake_tts_response(b"uncapped-mp3"))

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    assert any(s["kind"] == "voice" for s in sent)
    # Not the gifted cohort — an uncapped VIP still pays.
    assert (await _balance(registry, 555)) < 500


# ---------------------------------------------------------------------------
# #1534 / #1537: the SDK client is released, and the bare form is free
# ---------------------------------------------------------------------------


async def test_voice_closes_the_openai_client(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1534: a fresh ``AsyncOpenAI`` per ``/voice`` must be closed.

    Neither the SDK client nor its ``httpx.AsyncClient`` defines
    ``__del__``, so an unclosed one keeps its socket pool for the life
    of the process.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    _capture_voice_and_text(monkeypatch, bot)

    close_mock = AsyncMock()
    create_mock = AsyncMock(return_value=_fake_tts_response(b"mp3"))
    fake_client = SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(create=create_mock)),
        close=close_mock,
    )
    monkeypatch.setattr(voice_handler_module, "_build_openai_client", lambda _key: fake_client)

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    create_mock.assert_awaited_once()
    close_mock.assert_awaited_once()


async def test_voice_closes_the_openai_client_when_synthesis_raises(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release is in a ``finally`` — an upstream blow-up still frees it."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    _capture_voice_and_text(monkeypatch, bot)

    close_mock = AsyncMock()
    create_mock = AsyncMock(side_effect=RuntimeError("upstream exploded"))
    fake_client = SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(create=create_mock)),
        close=close_mock,
    )
    monkeypatch.setattr(voice_handler_module, "_build_openai_client", lambda _key: fake_client)

    await dispatcher.feed_update(bot, _update("/voice helloworld"))
    close_mock.assert_awaited_once()


async def test_voice_close_failure_does_not_break_delivery(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup must never turn a delivered voice note into an error card."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_wallet(registry, 555, balance=500, vip_till=_future_ts())
    sent = _capture_voice_and_text(monkeypatch, bot)

    create_mock = AsyncMock(return_value=_fake_tts_response(b"mp3"))
    fake_client = SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(create=create_mock)),
        close=AsyncMock(side_effect=RuntimeError("pool already gone")),
    )
    monkeypatch.setattr(voice_handler_module, "_build_openai_client", lambda _key: fake_client)

    result = await dispatcher.feed_update(bot, _update("/voice helloworld"))
    assert result is not UNHANDLED
    assert [s for s in sent if s["kind"] == "voice"]


async def test_bare_voice_does_not_spend_the_rate_limit_budget(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1537: the usage hint calls nothing, so it must not be limited.

    The 5/60s bucket exists to protect the PAID TTS call. With the hint
    registered under it, five mistyped ``/voice`` locked the real
    command out for the rest of the window.
    """
    bot, dispatcher, _ = await make_wired()
    sent = _capture_voice_and_text(monkeypatch, bot)

    for _ in range(8):
        result = await dispatcher.feed_update(bot, _update("/voice"))
        assert result is not UNHANDLED

    assert len(sent) == 8
    assert all("/voice" in s["text"] for s in sent)


async def test_the_openai_client_does_not_retry_the_billed_call() -> None:
    """#1973: one ``/voice`` must be at most one paid synthesis.

    This is the one place in the tree that constructs an SDK client
    instead of driving ``httpx`` directly, and the SDK's default is
    ``DEFAULT_MAX_RETRIES = 2`` (``openai/_constants.py``) — three
    attempts, retried on ``httpx.TimeoutException`` among others
    (``openai/_base_client.py``, the ``remaining_retries > 0`` branch).
    Two consequences, both real:

    * ``timeout`` is applied PER ATTEMPT, so the 60 s window
      ``tts_service``'s #1954 comment reasons about is really up to
      ~181.5 s (three attempts plus 0.5 s and 1.0 s of backoff), or up
      to 300 s when a 429 carries ``Retry-After``. ``runner/webhook``
      gives a deploy 20 s of graceful shutdown.
    * A read timeout that fires AFTER OpenAI accepted the request
      re-synthesises the same text, and the SDK sends no
      ``Idempotency-Key`` (``_base_client`` sets
      ``_idempotency_header = None`` and never overrides it). The
      account is billed up to three times for the one request the user
      is charged once for.

    Asserting on the constructed client rather than on a call: the
    factory is the seam every other test in this file replaces, so this
    is the only test that sees the real object.

    Async rather than asyncio.run: the SDK client binds itself to
    whatever loop is current when it is built, and a nested
    asyncio.run gave it one loop to be born on and a different one
    to be closed on. The orphaned loop and its two self-pipe sockets
    were then released by the garbage collector, at which point pytest
    reported three unraisable ResourceWarnings — attributed, since
    collection is what decides the timing, to whichever unrelated test
    happened to be running. Under filterwarnings = error that is a
    failure in a file that never touched the SDK, and it appeared only
    on the Python version whose GC timing differed from the local one.
    Building and closing on the one loop the test already runs in ends
    it.
    """
    pytest.importorskip("openai")

    client = voice_handler_module._build_openai_client("sk-not-a-real-key")
    try:
        assert client.max_retries == 0  # type: ignore[attr-defined]
    finally:
        # Constructed, never used: hand the pool back anyway (#1534).
        await voice_handler_module._close_openai_client(client)
