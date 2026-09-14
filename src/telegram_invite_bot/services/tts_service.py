"""OpenAI text-to-speech service for ``vip_emoji_voice`` (T-023).

The only OpenAI-billed surface left in the bot: VIP voice synthesis.
The strangler iteration's OpenAI chat-completions sibling
(``openai_chat_service``) was removed when ``/ai``/``/gpt``/``/chat``
realigned to legacy's DeepSeek-only posture, so this module now
stands alone — it still imports :class:`OpenAiConfig.api_key` (the
shared SDK key) but no longer shares plumbing with a sibling text
service.

Posture, kept for orientation:
- Calls ``client.audio.speech.create`` (audio bytes, not text).
- Billing knob: ``coin_per_char`` on :class:`TtsConfig` — TTS is
  charged by output character, not token.
- The ``TtsConfig.max_chars`` ceiling applies BEFORE the cost
  pre-check: a 5000-char prompt is refused as too long, not just as
  expensive.

Charge order, read off :meth:`TtsService.synthesize` rather than
remembered (#1629): NOT_CONFIGURED → EMPTY_TEXT → the too-long
check, which measures ``len(cleaned)``, the stripped text, and not
``len(text)`` → pre-check (refuse if unaffordable) → debit
(ceil(len(text) * coin_per_char), rounded UP, minimum 1) → upstream
synthesis call → refund if that call fails → return audio bytes +
coins charged, the hold still OPEN. #1895: the last leg — settle
or release — belongs to the caller, which is the only party that
knows whether the audio ever reached anyone. The debit moved
ahead of the upstream in M-P-3 (see
:meth:`TtsService.synthesize` for why); the user-visible contract
is unchanged — a failed call costs nothing. If
``vip_unlimited_voice`` is on (handler-side flag), the debit step
is skipped — synthesis still runs, but the wallet is untouched. The
skip happens at the HANDLER layer, not here: the service stays
single-responsibility (synthesise + charge) and the policy decision
(charge or gift) lives where the feature flag is read.

Retry safety: the service calls the upstream and the debit at most
once per :meth:`synthesize` invocation. A handler-level retry that
re-invokes the service would run two upstream calls + two debits,
but no double-charge is possible from a single invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

log = logger.bind(component="services.tts")

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import TtsConfig
    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.economy_service import EconomyService


class TtsOutcome(StrEnum):
    """Mutually-exclusive results :meth:`TtsService.synthesize` reports.

    StrEnum so log lines render the name directly. The handler maps
    each value to an i18n key (``h_voice_*``); a future addition
    without a corresponding handler branch surfaces as the explicit
    "unreachable" else rather than a silent miss.
    """

    SUCCESS = "success"
    """Audio synthesised and (unless flag-gifted) the coins held.

    The hold is still OPEN (#1895): the caller settles it once the
    audio has actually been delivered, or releases it if the
    delivery fails."""

    NOT_CONFIGURED = "not_configured"
    """``OPENAI_API_KEY`` unset — handler renders the unavailable
    copy without touching the wallet or the upstream."""

    EMPTY_TEXT = "empty_text"
    """Caller passed empty / whitespace-only text. Defensive: the
    handler's emoji-only-message filter shouldn't dispatch here on
    empty input, but a future change in trigger semantics shouldn't
    silently round-trip OpenAI with an empty payload."""

    TOO_LONG = "too_long"
    """``len(text)`` exceeds :attr:`TtsConfig.max_chars`. Surfaced as
    a refusal before the cost pre-check so the user sees "your
    message is too long" rather than a coin-budget error for a
    request the API would have refused anyway."""

    INSUFFICIENT_BALANCE = "insufficient_balance"
    """Wallet balance < the per-call coin cost. NOT reached when
    ``vip_unlimited_voice`` skips billing (handler-side decision).
    No upstream call made; no wallet mutation."""

    UPSTREAM_ERROR = "upstream_error"
    """OpenAI raised or returned a non-audio payload. Generic
    "временная ошибка" for the user, full traceback in the log.
    No debit (M-P-3: any pre-authorized debit is refunded before
    surfacing this outcome)."""

    AUDIO_TOO_LARGE = "audio_too_large"
    """M-P-3: upstream returned audio bytes exceeding the configured
    safety cap. The default is 25MB — half of Telegram's documented
    50MB upload ceiling, chosen for multipart-form headroom and to
    keep webhook timeouts bounded (#1627: the ceiling itself is 50MB
    and ``TtsConfig.max_audio_bytes`` accepts it, so this number is a
    deliberate margin and not an external limit).
    The pre-authorized debit is refunded; the user sees a refusal
    instead of the bot trying to upload a payload Telegram would
    reject."""


@dataclass(frozen=True, slots=True)
class TtsResult:
    """What :meth:`TtsService.synthesize` produced.

    Mirrors :class:`OpenAiResult` (T-021) so the handler renders a
    uniform reply template with per-outcome branching, not per-field
    guards. On failure paths the byte / numeric fields are
    zero-valued.
    """

    outcome: TtsOutcome
    audio: bytes = b""
    coins_charged: int = 0
    balance_after: int = 0
    estimated_budget: int = 0
    """Coin cost the call WOULD have charged. Surfaced on
    INSUFFICIENT_BALANCE so the handler can render the budget vs.
    balance gap. Also non-zero on TOO_LONG (informational) so an
    operator can see what the prospective debit would have been."""


class SpeechCreateProtocol(Protocol):
    """Structural type for ``client.audio.speech.create``.

    Tests pass an :class:`unittest.mock.AsyncMock`; production passes
    the bound method off :class:`openai.AsyncOpenAI`. The single
    surface we touch is ``create(model=..., voice=..., input=...,
    timeout=...)`` returning an object with a ``.content`` bytes
    attribute (mirrors the SDK's ``HttpxBinaryResponseContent`` shape).
    """

    async def create(self, **kwargs: Any) -> Any: ...


class _AudioNamespace(Protocol):
    speech: SpeechCreateProtocol


class AsyncOpenAITtsLike(Protocol):
    """Structural type for the subset of :class:`openai.AsyncOpenAI`
    this service depends on. Test doubles shaped as
    ``Mock(audio=Mock(speech=AsyncMock()))`` satisfy the protocol
    without dragging the SDK into the test environment.
    """

    audio: _AudioNamespace


class TtsService:
    """Single-shot OpenAI TTS caller with optional wallet billing."""

    def __init__(
        self,
        config: TtsConfig,
        economy_service: EconomyService,
        *,
        client: AsyncOpenAITtsLike | None = None,
    ) -> None:
        """Construct.

        ``client`` is optional so the constructor can be called when
        ``OPENAI_API_KEY`` is missing — :meth:`synthesize` short-
        circuits on the NOT_CONFIGURED path before touching the
        client. Tests pass a mock; production passes a real
        :class:`openai.AsyncOpenAI`.
        """
        self._config = config
        self._economy = economy_service
        self._client = client

    def coin_cost_for(self, text: str) -> int:
        """Coin charge for synthesising ``text`` at the configured rate.

        Rounded UP to the next integer so a fractional charge never
        slips through as zero — a 50-char request at 0.001 coin/char
        is one coin, not zero. The ``max(1, …)`` floor enforces a
        minimum charge even for very short inputs (a single emoji is
        ~1-2 chars but still consumes upstream cost).
        """
        return max(1, math.ceil(len(text) * self._config.coin_per_char))

    async def synthesize(
        self,
        *,
        user_id: int,
        text: str,
        balance: int,
        skip_billing: bool = False,
        checkpoint: Checkpoint | None = None,
    ) -> TtsResult:
        """Execute the full TTS flow.

        ``balance`` is passed in (not re-read from the repo) so the
        caller can use a freshly-fetched value from the outer
        middleware's session. ``skip_billing=True`` makes the call
        free for the user — the handler sets this when
        :attr:`FeatureFlags.vip_unlimited_voice` is on, so the VIP
        gift policy lives at the handler layer (where the flag is
        read) while this service stays focused on synthesis +
        optional charge.

        Billing posture (M-P-3 — changed from T-023 original):

        The debit happens BEFORE the upstream synthesis call (pre-
        authorisation). If the upstream errors, the bytes-size check
        rejects an oversize payload, or the SDK returns a non-bytes
        response, the pre-authorised debit is REFUNDED via a
        symmetric credit before the failure outcome is returned. The
        net effect from the user's wallet perspective: a successful
        call is billed once; a failed call costs nothing.

        Why pre-debit (not post-debit): the original posture (debit
        AFTER success) allowed two concurrent /voice calls to both
        pass the pre-check at balance=5/cost=5, both synthesise,
        but only one debit lands — the second one's upstream cost
        is absorbed by ops. Pre-debiting routes the race through the
        SQL writer lock at the wallet row, so the second call's
        debit fails BEFORE the upstream is touched. The refund-on-
        failure step keeps the user-side invariant intact.

        Escrow primitives (#1567): the pre-authorisation is a ``hold``
        and the refund a ``release``, both of which move ``balance``
        alone. ``debit``/``credit`` would bump ``total_spent`` on the
        way out and ``total_earned`` on the way back, so every failed
        synthesis handed the caller a free pair of lifetime-counter
        increments — no counterparty, no cost, repeatable at the rate
        of the AI rate limiter.

        Settlement is the CALLER's (#1895). SUCCESS returns with the
        hold still open: the coins have left the wallet, but nothing
        has reached ``total_spent`` yet. The caller owns the last leg
        — ``settle_hold`` once the audio is delivered, ``release``
        if it could not be — because only the caller knows whether
        the round produced anything. Settling here instead made an
        undeliverable upload refund a *completed* spend, and the only
        primitive that can put coins back on top of one is
        ``credit``, which bumps ``total_earned``: the round ended
        with the balance whole but BOTH lifetime counters inflated by
        the cost, irreversibly (``EconomyRepo.bump_totals`` refuses
        negative arguments).

        Refund failure (vanishingly rare — would require the wallet
        row to disappear between debit and credit) is logged at
        ERROR; the outcome still surfaces the upstream failure so
        the user sees a coherent refusal. Recovery is operator-side
        via ledger inspection.

        ``checkpoint`` commits each money movement the moment it is
        final, instead of leaving both to the per-update transaction.
        It exists because the upstream call is allowed
        :attr:`TtsConfig.timeout_seconds` — sixty seconds by default,
        twelve times SQLite's ``busy_timeout`` — and the debit is a
        write: without it, one ``/voice`` holds the single writer slot
        on ``economy.db`` for a minute and every wallet, game, transfer
        and shop purchase in the bot fails with ``database is locked``.
        See :class:`db.session.Checkpoint`.

        Committing the debit early also *strengthens* the M-P-3 race
        argument above: a second simultaneous ``/voice`` now reads the
        reduced balance immediately, rather than blocking on the writer
        lock for the length of the first call's synthesis.

        What it costs: the debit no longer disappears if the update is
        rolled back later, so every path that ends without audio has to
        hand the coins back in-band. That is what ``_refund`` does here
        — and it checkpoints too, so the refund cannot itself be rolled
        back by a later failure — and what the handler does when the
        upload fails. Passing ``checkpoint=None`` restores the old
        all-or-nothing behaviour and is what the unit tests use.
        """
        bound = log.bind(uid=user_id, text_len=len(text))

        # NOT_CONFIGURED gate — no SDK touch, no work done.
        if self._client is None:
            bound.warning("tts not configured")
            return TtsResult(outcome=TtsOutcome.NOT_CONFIGURED)

        cleaned = text.strip()
        if not cleaned:
            bound.warning("tts empty text")
            return TtsResult(outcome=TtsOutcome.EMPTY_TEXT)

        if len(cleaned) > self._config.max_chars:
            bound.bind(limit=self._config.max_chars).info("tts text too long")
            return TtsResult(
                outcome=TtsOutcome.TOO_LONG,
                estimated_budget=self.coin_cost_for(cleaned),
            )

        cost = self.coin_cost_for(cleaned)
        # Pre-check (only when billing). The skip-billing path bypasses
        # this entirely — a VIP-gift user with a zero balance should
        # still receive audio.
        if not skip_billing and balance < cost:
            bound.bind(balance=balance, cost=cost).info("tts pre-check insufficient")
            return TtsResult(
                outcome=TtsOutcome.INSUFFICIENT_BALANCE,
                estimated_budget=cost,
            )

        # M-P-3: pre-authorise the charge BEFORE the upstream call.
        # Routes the concurrent-spend race through the SQL writer
        # lock at the wallet row, so a second simultaneous /voice
        # cannot pass the pre-check and then synthesise on a
        # balance the first call is about to consume.
        #
        # #1567: this leg is a ``hold``, not a ``debit``. Every
        # failure path below hands the coins straight back, and the
        # debit/credit pair would move a lifetime counter in each
        # direction on a round trip that moved no money.
        balance_after_hold = balance
        if not skip_billing:
            hold_wallet = await self._economy.hold(
                user_id,
                cost,
                type="tts",
                reason="vip_emoji_voice",
            )
            if hold_wallet is None:
                bound.bind(cost=cost).info(
                    "tts pre-authorisation failed (race) — refusing without upstream"
                )
                # #1969: this is the LOST RACE, not the pre-check above
                # — the caller's balance said yes and ``hold``'s
                # ``WHERE balance >= amount`` said no. A guarded UPDATE
                # matching zero rows still promoted the connection to
                # ``BEGIN IMMEDIATE`` (``db/engines.py``), so without
                # this the handler renders its refusal with economy.db
                # locked over a write that never happened, against a 5s
                # ``busy_timeout``. Nothing is being made durable here;
                # the lock is simply handed back. The pre-check return
                # a few lines up never wrote and does not need it.
                if checkpoint is not None:
                    await checkpoint()
                return TtsResult(
                    outcome=TtsOutcome.INSUFFICIENT_BALANCE,
                    estimated_budget=cost,
                )
            balance_after_hold = hold_wallet.balance
            # The hold is final — the coins are out of the wallet for
            # the length of the attempt and released below if it
            # fails. Commit it before the minute-long upstream call so
            # economy.db stays writable.
            if checkpoint is not None:
                await checkpoint()

        async def _refund(reason: str) -> None:
            """Symmetric release to undo the pre-authorisation."""
            if skip_billing:
                return
            refund_wallet = await self._economy.release(
                user_id, cost, type="tts_refund", reason=reason
            )
            # The refund is as final as the hold was, and the hold is
            # already committed: leaving this one to the middleware
            # would mean a later failure (the handler's own error reply
            # failing to send, say) rolls back the release while the
            # charge stands.
            if checkpoint is not None:
                await checkpoint()
            if refund_wallet is None:
                # Wallet row vanished between hold and release —
                # operationally near-impossible. Loud log only;
                # ledger has the debit row for manual reconciliation.
                bound.bind(cost=cost, reason=reason).error(
                    "tts refund failed — manual reconciliation needed"
                )

        # Upstream call. Broad-except matches T-021's posture: the
        # openai SDK raises a tree of typed exceptions and we want
        # them all collapsed to one user-facing outcome with the
        # repr captured for ops.
        try:
            response = await self._client.audio.speech.create(
                model=self._config.model,
                voice=self._config.voice,
                input=cleaned,
                timeout=self._config.timeout_seconds,
            )
        except asyncio.CancelledError:
            # #1954: cancellation is a ``BaseException``, so the broad
            # ``except`` below never saw it — and by this point the hold
            # is COMMITTED (see ``checkpoint`` in the docstring), so
            # unwinding takes the user's coins with it and leaves
            # nothing to unwind them from. The window is the whole
            # upstream call, up to :attr:`TtsConfig.timeout_seconds`
            # (sixty by default), and what closes it is ordinary: a
            # deploy restart during synthesis, where uvicorn's bounded
            # graceful shutdown cancels whatever is still in flight.
            #
            # "The whole upstream call" holds only because the client is
            # built with ``max_retries=0`` (#1973): the SDK applies
            # ``timeout`` per ATTEMPT, so its default of two retries
            # would make this window three times the number above.
            #
            # Best-effort, then re-raise: a task gets one
            # ``CancelledError`` from ``cancel()``, so the awaits in
            # ``_refund`` do run — but if the loop is already tearing
            # down they may not, and a failure there must not replace
            # the cancellation the caller is waiting on.
            bound.bind(cost=cost).warning("tts cancelled mid-upstream — releasing the hold")
            with contextlib.suppress(Exception):
                await _refund("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — see comment above
            bound.warning("tts upstream error: {e!r}", e=exc)
            await _refund("upstream_error")
            return TtsResult(outcome=TtsOutcome.UPSTREAM_ERROR)

        # Extract bytes. The SDK returns an ``HttpxBinaryResponseContent``
        # with ``.content`` (bytes) and ``.read()`` (async bytes). Tests
        # pass a simple object exposing ``.content``. ``getattr``
        # cascade defends against an unexpected shape — better an
        # UPSTREAM_ERROR than an AttributeError that 500s the webhook.
        audio = getattr(response, "content", None)
        if not isinstance(audio, (bytes, bytearray)) or not audio:
            bound.warning("tts non-bytes response: {t!r}", t=type(response).__name__)
            await _refund("upstream_non_bytes")
            return TtsResult(outcome=TtsOutcome.UPSTREAM_ERROR)
        audio_bytes = bytes(audio)

        # M-P-3: bound the upstream audio payload. A pathological
        # response could otherwise saturate the webhook worker and
        # the Telegram upload step. Refund before refusing.
        if len(audio_bytes) > self._config.max_audio_bytes:
            bound.bind(
                bytes_len=len(audio_bytes),
                cap=self._config.max_audio_bytes,
            ).warning("tts audio over size cap — refunding")
            await _refund("audio_too_large")
            return TtsResult(
                outcome=TtsOutcome.AUDIO_TOO_LARGE,
                estimated_budget=cost,
            )

        if skip_billing:
            bound.bind(cost=cost).info("tts gifted (vip unlimited voice)")
            return TtsResult(
                outcome=TtsOutcome.SUCCESS,
                audio=audio_bytes,
                coins_charged=0,
                balance_after=balance,
                estimated_budget=cost,
            )

        # #1895: the hold stays OPEN across the return. Booking it as
        # a lifetime spend here — before a single byte has reached
        # Telegram — meant an undeliverable upload had to be refunded
        # with ``credit``, the only primitive that can put coins back
        # on top of a settled spend, and ``credit`` bumps
        # ``total_earned``. The round then ended with the balance
        # whole but both lifetime counters inflated by the cost, and
        # ``EconomyRepo.bump_totals`` refuses the negative arguments
        # that would undo it. The caller settles after delivery.
        bound.bind(coins=cost, bytes_len=len(audio_bytes)).info("tts synthesised")
        return TtsResult(
            outcome=TtsOutcome.SUCCESS,
            audio=audio_bytes,
            coins_charged=cost,
            balance_after=balance_after_hold,
            estimated_budget=cost,
        )
