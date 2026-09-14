"""``/voice`` — VIP-only TTS handler (T-023).

Ports the legacy `vip_emoji_voice` feature in the strangler. The
legacy posture was "VIP sends an emoji-only message → bot replies
with a TTS rendering"; the new pipeline exposes it as a deliberate
slash command (``/voice <text>``) so the trigger is explicit and
doesn't compete with the message-pipeline cutover for ordering.
See ADR 0013 for the rationale.

Flow:
    1. Use the effective language injected as ``lang`` (ru/en).
    2. Require non-empty argument; bare ``/voice`` shows usage.
    3. VIP gate via :class:`VipRepo` from the request-scoped
       :class:`EconomyMiddleware`. Non-VIP → ``h_voice_vip_only``.
    4. NOT_CONFIGURED short-circuit if ``OPENAI_API_KEY`` is unset.
    5. Read wallet (auto-create — same posture as ``/send`` /
       ``/ai``).
    6. Call :class:`TtsService.synthesize` with ``skip_billing``
       derived from :attr:`FeatureFlags.vip_unlimited_voice`.
    7. On SUCCESS → send voice note + i18n reply (``h_voice_success``
       or ``h_voice_gifted``). On any failure outcome → i18n
       error copy, no voice sent, wallet untouched.
    8. Settle the service's open hold once the voice note is
       actually out, or release it if the upload failed (#1895).

Why the VIP gate lives in the handler (not a middleware):
``vip_emoji_voice`` is one of very few VIP-only entry points; a
dedicated middleware would be over-engineering. The
:class:`EconomyMiddleware` already exposes ``vip_repo``; the
handler reads it and branches. When a second VIP-only handler
lands, factor the gate into a middleware — until then in-handler
is simpler and equivalent.

Why a slash command rather than the legacy "emoji-only message"
content filter: the auto-trigger would compete with the existing
message handlers (reward computation, moderation, rewards) for
ordering at the dispatcher level and is best added as a follow-
up filter once the full message-pipeline cutover settles. See
ADR 0013 for the deferral.

Bot OpenAI client is constructed per-request inside the handler
(``_build_openai_client`` factory). The factory is split out
so e2e tests can monkey-patch it to return an :class:`AsyncMock`
without importing the SDK at test-collection time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.ai_rate_limit import VoiceRateLimitMiddleware
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.tts_service import TtsOutcome, TtsService
from telegram_invite_bot.services.voice_quota_service import (
    VoiceQuotaConfig,
    VoiceQuotaOutcome,
    VoiceQuotaService,
)
from telegram_invite_bot.utils.aiogram import command_args, require_from_user

log = logger.bind(component="handlers.vip_emoji_voice")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import (
        FeatureFlags,
        OpenAiConfig,
        TtsConfig,
    )
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.repositories.vip_repo import VipRepo
    from telegram_invite_bot.services.economy_service import EconomyService


def _build_openai_client(api_key: str) -> object:
    """Late-import factory for the OpenAI SDK client.

    Late-imported so the SDK stays out of the module-level import chain
    — the test environment can avoid the package entirely. Tests
    monkey-patch this function to return an :class:`AsyncMock`. This is
    the only SDK client in the tree; ``handlers/ai`` reaches DeepSeek
    through ``httpx`` directly.

    #1973: ``max_retries=0``, against the SDK's default of two. The
    default retries on ``httpx.TimeoutException``, and the SDK never
    sends an ``Idempotency-Key`` (``_base_client`` sets the header name
    to ``None`` and nothing overrides it), so a read timeout that fires
    after OpenAI accepted the request re-synthesises the same text and
    bills the account again — up to three times for the one request
    ``TtsService`` charges the user once for. The timeout is also
    per-attempt, so the sixty-second window ``tts_service``'s #1954
    comment reasons about was really up to ~181.5 s against uvicorn's
    twenty-second graceful shutdown. Zero makes both true again.

    Retrying is what we give up. It is the cheaper half: a failed
    synthesis refunds the hold (``TtsService._refund``) and the user
    taps again, whereas a silent double charge is invisible until the
    invoice.
    """
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, max_retries=0)


async def _close_openai_client(client: object) -> None:
    """Hand the SDK client's socket pool back.

    #1534: :func:`_build_openai_client` mints a fresh ``AsyncOpenAI`` —
    and with it a fresh ``httpx.AsyncClient`` and its connection pool —
    on every ``/voice``. Neither class defines ``__del__``, so garbage
    collection does NOT release those sockets; closing is the caller's
    job per the SDK's own contract. The VIP gate and the 5/60s limiter
    bound how fast the pools accumulate, but nothing bounded it in time
    and the process never restarts on its own.

    ``getattr``-guarded and failure-tolerant on purpose: tests swap the
    factory for a plain namespace, and a cleanup step must never turn a
    delivered voice note into an error card.
    """
    closer = getattr(client, "close", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception as exc:  # pragma: no cover - cleanup must not raise
        log.debug("openai client close failed: {e!r}", e=exc)


async def _deliver_audio(message: Message, audio: bytes) -> None:
    """Send ``audio`` as a voice note, falling back to an audio file.

    Filename matters: Telegram infers MIME from the extension if the
    client doesn't pass one through, and OpenAI returns MP3 by default —
    using ``.ogg`` would be a lie. ``send_voice`` transcodes server-side
    anyway, but the truthful extension keeps things sane for any future
    debugging that examines the upload directly.

    VOICE_MESSAGES_FORBIDDEN means the recipient restricted incoming
    voice notes (Telegram Privacy → Voice Messages, a Premium toggle).
    Synthesis already succeeded AND was billed, so losing the audio (or
    surfacing a generic error) would mean the user paid for nothing.
    The same bytes go out as a regular audio file, which that privacy
    setting does not block. A fresh ``InputFile`` is needed — the first
    upload attempt consumed the buffer.

    Every other failure propagates: the caller refunds on it.
    """
    try:
        await message.answer_voice(BufferedInputFile(audio, filename="voice.mp3"))
    except TelegramBadRequest as exc:
        if "VOICE_MESSAGES_FORBIDDEN" not in str(exc):
            raise
        await message.answer_audio(BufferedInputFile(audio, filename="voice.mp3"))


async def handle_voice(
    message: Message,
    command: CommandObject,
    openai_config: OpenAiConfig,
    tts_config: TtsConfig,
    feature_flags: FeatureFlags,
    economy_repo: EconomyRepo,
    economy_service: EconomyService,
    transactions_repo: TransactionsRepo,
    vip_repo: VipRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/voice <text>`` — VIP-only TTS over OpenAI.

    The bare-form ``/voice`` (no args) is routed to a usage hint by
    a separate registration with ``magic=F.args.is_(None)``; this
    handler only fires when there's a non-empty prompt. Defensive
    re-check for empty prompts below keeps the handler robust to a
    future registration change.
    """
    tg_user = require_from_user(message)
    text = command_args(command).strip()

    bound = log.bind(uid=tg_user.id, text_len=len(text))

    if not text:
        # Defensive — the F.args filter on the OpenAI-call registration
        # should keep us out, but the explicit branch keeps the
        # handler robust to future routing changes.
        await message.answer(t("h_voice_usage", lang))
        return

    # VIP gate. Read the global VIP profile (no group scope — TTS is
    # a per-user perk not a per-chat one). A None profile means the
    # user has no active VIP grant.
    now = datetime.now(UTC)
    vip = await vip_repo.get_active_profile(tg_user.id, now=now)
    if vip is None:
        bound.info("/voice refused: non-VIP")
        await message.answer(t("h_voice_vip_only", lang))
        return

    # NOT_CONFIGURED short-circuit BEFORE touching the wallet — matches
    # the T-021 handler's posture so an operator with no key set sees
    # a deterministic UX without a DB read race-conditioning the
    # failure mode.
    if openai_config.api_key is None:
        bound.warning("openai not configured (handler edge)")
        await message.answer(t("h_voice_not_configured", lang))
        return

    # M-P-4 / L-90: the top-tier cohort is the M-P-4 allowlist, but only
    # when the global flag is ON. This same cohort gets BOTH the free-
    # voice gift (skip_billing, below) AND an unbounded daily quota
    # (the UNLIMITED tier in VoiceQuotaService). When the flag is OFF
    # the allowlist is empty for quota purposes too, so ordinary VIPs
    # and would-be top-tier users alike fall under the daily ceiling —
    # fail-closed, matching the flag's own posture.
    top_tier_ids = (
        feature_flags.parsed_unlimited_voice_tier_user_ids()
        if feature_flags.vip_unlimited_voice
        else frozenset[int]()
    )

    # L-90: per-tier daily voice quota. Runs BEFORE the wallet read and
    # the upstream call so an over-quota VIP pays no cost (no session
    # work, no synthesis). The UNLIMITED cohort bypasses; ordinary VIPs
    # are capped at VoiceQuotaConfig.vip_daily_limit/day (reset at UTC
    # midnight). See services/voice_quota_service.py for the derivation
    # (legacy had no TTS quota — this is the documented minimal-honest
    # version). Counter source is the economy.transactions ledger, so
    # no migration: VoiceQuotaService reads TransactionsRepo, no write.
    quota = VoiceQuotaService(
        transactions_repo,
        config=VoiceQuotaConfig(
            vip_daily_limit=tts_config.vip_daily_limit,
            unlimited_user_ids=top_tier_ids,
        ),
    )
    quota_result = await quota.check(tg_user.id, now=now)
    if quota_result.outcome is VoiceQuotaOutcome.EXCEEDED:
        # Reset is the next UTC midnight (the ledger day-bucket rolls
        # over with the calendar day). Render HH:MM UTC of the boundary.
        reset_at = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        bound.bind(used=quota_result.used, limit=quota_result.limit).info(
            "/voice refused: daily quota exceeded"
        )
        await message.answer(
            t(
                "h_voice_quota_exceeded",
                lang,
                used=quota_result.used,
                limit=quota_result.limit,
                reset=reset_at.strftime("%H:%M"),
            )
        )
        return

    # Wallet read — auto-create so a first-time VIP can spend whatever
    # bonus we'd hand them on /start. Same posture as /send and /ai.
    wallet = await economy_repo.get_or_create(tg_user.id)

    client = _build_openai_client(openai_config.api_key.get_secret_value())
    service = TtsService(
        tts_config,
        economy_service,
        client=client,  # type: ignore[arg-type]
    )
    # M-P-4: the free-voice gift only applies to the top-tier
    # allowlist when the global flag is on. Ordinary VIPs continue
    # to pay even with vip_unlimited_voice=True, so the perk is
    # scoped to a specific cohort rather than every VIP grant. The
    # gift cohort and the quota-unlimited cohort are the SAME set
    # (``top_tier_ids``), so a free-voice user is also uncapped.
    skip_billing = tg_user.id in top_tier_ids
    try:
        result = await service.synthesize(
            user_id=tg_user.id,
            text=text,
            balance=wallet.balance,
            skip_billing=skip_billing,
            checkpoint=checkpoint,
        )
    finally:
        # #1534: the client has done its one job by now — everything
        # below is Telegram-side delivery and copy. Closing here rather
        # than around the whole handler keeps the release unconditional
        # (the synthesis raising is exactly when it matters) without
        # putting the refund path inside a second ``try``.
        await _close_openai_client(client)

    outcome = result.outcome
    if outcome is TtsOutcome.SUCCESS:
        try:
            await _deliver_audio(message, result.audio)
        except (asyncio.CancelledError, Exception):
            # The bytes die with the request — the user blocked the bot,
            # or it was removed from the chat, between the synthesis and
            # the upload. There is nothing left to re-deliver, so the
            # charge must not stand.
            #
            # #1954: ``CancelledError`` is named explicitly because it
            # is a ``BaseException`` and a bare ``except Exception``
            # walks straight past it. It reaches here the same way it
            # reaches the service's own upstream call — a shutdown
            # cancel mid-upload — and with the same consequence, since
            # the hold below is already committed. The block ends in a
            # bare ``raise``, so cancellation still propagates intact.
            #
            # This refund used to be implicit: the debit lived in the
            # per-update transaction and re-raising rolled it back. It
            # no longer does — the debit is committed before the
            # upstream call so a minute of synthesis can't hold the
            # writer lock on economy.db (see TtsService.synthesize). So
            # the coins are handed back in-band, and committed on the
            # spot: the raise below would otherwise roll the release
            # back while the charge kept standing.
            #
            # #1895: ``release``, not ``credit``. The service now
            # leaves the hold open precisely so this path can undo it
            # without touching a lifetime counter — ``credit`` would
            # hand the coins back AND book ``total_earned`` on a round
            # that earned nothing, and no primitive can take that back.
            if result.coins_charged:
                refunded = await economy_service.release(
                    tg_user.id,
                    result.coins_charged,
                    type="tts_refund",
                    reason="delivery_failed",
                )
                if checkpoint is not None:
                    await checkpoint()
                if refunded is None:
                    # SEC-1: ``release`` returns None for a rejected
                    # amount or a vanished wallet. Neither can happen
                    # here — the debit went through moments ago — so
                    # this is a "cannot happen" branch worth an alert
                    # rather than a silent swallow: the user is out of
                    # coins with nothing to show for them.
                    bound.bind(coins=result.coins_charged).error(
                        "/voice refund failed: charge stands"
                    )
                else:
                    bound.bind(coins=result.coins_charged).warning(
                        "/voice refunded: audio undeliverable"
                    )
            raise

        # #1895: the audio is out — only now is the charge genuinely
        # earned, so book the open hold as a lifetime spend. It moves
        # no coins and writes no ledger row (the hold already did
        # both); it only lands the amount in ``total_spent`` so
        # /balance does not under-report what the user paid for voice.
        # Committed on the spot for the same reason the refund is: the
        # billing summary below is another network call, and a failure
        # there must not roll the counter back while the charge stands.
        if result.coins_charged:
            settled = await economy_service.settle_hold(tg_user.id, result.coins_charged)
            if checkpoint is not None:
                await checkpoint()
            if settled is None:
                # The wallet row vanished between the hold and here.
                # Near-impossible, and it costs the user nothing — the
                # coins are already gone and stay gone — but /balance
                # will under-report the spend, so say so loudly.
                bound.bind(coins=result.coins_charged).error(
                    "/voice settle failed: lifetime spend under-reported"
                )

        # Caption-style follow-up message — ``send_voice`` doesn't
        # render Telegram-style captions reliably across clients for
        # voice notes (the field exists but mobile clients often hide
        # it), so we send the billing summary as a separate message.
        if skip_billing:
            await message.answer(t("h_voice_gifted", lang, chars=len(text)))
        else:
            await message.answer(
                t(
                    "h_voice_success",
                    lang,
                    chars=len(text),
                    coins=result.coins_charged,
                    balance=result.balance_after,
                )
            )
        bound.bind(
            chars=len(text),
            coins=result.coins_charged,
            gifted=skip_billing,
        ).info("/voice synthesised")
        return

    # Non-success outcomes — exhaustive per TtsOutcome. A future
    # member without a branch lands on the explicit unreachable
    # else, not a silent miss.
    if outcome is TtsOutcome.NOT_CONFIGURED:
        await message.answer(t("h_voice_not_configured", lang))
    elif outcome is TtsOutcome.EMPTY_TEXT:
        await message.answer(t("h_voice_empty", lang))
    elif outcome is TtsOutcome.TOO_LONG:
        await message.answer(
            t(
                "h_voice_too_long",
                lang,
                chars=len(text),
                limit=tts_config.max_chars,
            )
        )
    elif outcome is TtsOutcome.INSUFFICIENT_BALANCE:
        await message.answer(
            t(
                "h_voice_insufficient_balance",
                lang,
                budget=result.estimated_budget,
                balance=wallet.balance,
            )
        )
    elif outcome is TtsOutcome.UPSTREAM_ERROR:
        await message.answer(t("h_voice_error", lang))
    elif outcome is TtsOutcome.AUDIO_TOO_LARGE:
        # M-P-3: upstream audio exceeded the safety cap; the service
        # has already refunded the pre-authorised debit so the user
        # sees a refusal at no cost.
        await message.answer(t("h_voice_audio_too_large", lang))
    else:  # pragma: no cover — exhaustive on TtsOutcome
        bound.bind(outcome=outcome.value).error("/voice: unhandled outcome")
        await message.answer("…")

    bound.bind(outcome=outcome.value).info("/voice non-success")


async def handle_voice_usage(message: Message, lang: str) -> None:
    """Bare ``/voice`` (no args) — usage hint, no VIP-gate yet.

    Showing the usage card to non-VIPs is intentional: the goal of
    the bare form is "tell the user what /voice does"; surfacing
    "VIP only" only AFTER they fill in text would be a worse UX
    than letting them see the feature description first. The
    VIP-gate fires on the next attempt with arguments.
    """
    require_from_user(message)
    await message.answer(t("h_voice_usage", lang))


def build_router(
    registry: EngineRegistry,
    openai_config: OpenAiConfig,
    tts_config: TtsConfig,
    feature_flags: FeatureFlags,
) -> Router:
    """Factory — fresh ``Router`` per call (tests re-wire dispatchers).

    Mounts :class:`EconomyMiddleware` so the handler can read
    ``vip_repo`` / ``economy_repo`` / ``economy_service`` off the
    shared session — the wallet read and the debit (when billing is
    on) commit through the same outer transaction the middleware
    owns.

    The bare-form ``/voice`` (no args) is registered FIRST with
    ``magic=F.args.is_(None)`` so it wins against the with-args
    registration that matches the same command name. aiogram walks
    registrations in include order; putting the bare-form branch
    first guarantees the usage hint stays visible for no-argument
    calls without depending on filter-priority subtleties.

    Same chat-type posture as ``/ai`` (T-021) — private + group.
    TTS doesn't depend on chat context, so the alias works wherever
    the user wants it.

    The ``F.args`` filter on the with-args registration is applied
    via ``Command(magic=F.args)`` — NOT as a bare standalone filter.
    The bare-form variant is the bug fixed in commit 846970a
    (T-021): ``F.args`` against a ``Message`` raises because
    ``Message`` has no ``args`` attribute; the magic-filter form
    re-binds the same attribute access against the ``CommandObject``
    where it does exist.
    """

    async def _handle_voice(
        message: Message,
        command: CommandObject,
        economy_repo: EconomyRepo,
        economy_service: EconomyService,
        transactions_repo: TransactionsRepo,
        vip_repo: VipRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_voice(
            message,
            command,
            openai_config,
            tts_config,
            feature_flags,
            economy_repo,
            economy_service,
            transactions_repo,
            vip_repo,
            lang,
            checkpoint,
        )

    router = Router(name="vip_emoji_voice")
    # M-P-1: per-user rate limit attached BEFORE the economy session
    # so a rejected /voice doesn't pay the cost of opening a wallet
    # session. 5-burst, ~5/min sustained — tighter than /ai because
    # TTS calls are heavier (audio upload + per-char upstream cost).
    # The bare form is free of charge — see
    # :meth:`VoiceRateLimitMiddleware._is_free` (#1537).
    router.message.middleware(VoiceRateLimitMiddleware())
    router.message.middleware(EconomyMiddleware(registry))

    # Bare /voice → usage hint. Registered FIRST so it wins against
    # the with-args registration on the same command name.
    router.message.register(
        handle_voice_usage,
        Command("voice", "голос", ignore_case=True, magic=F.args.is_(None)),
        F.from_user,
    )

    # /voice <text> → TTS flow. F.args magic ensures the bare-form
    # registration above wins for no-argument calls.
    router.message.register(
        _handle_voice,
        Command("voice", "голос", ignore_case=True, magic=F.args),
        F.from_user,
    )
    return router
