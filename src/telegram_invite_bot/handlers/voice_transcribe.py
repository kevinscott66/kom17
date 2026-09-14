"""Group voice-message transcription (L-70).

Ports the legacy faster-whisper group-STT pipeline (``bot.py`` around
``send_transcription_to_target``:38789 / ``_format_transcription_as_quote``:38766)
to the OpenAI Whisper API. An ``F.voice`` group-message handler:

1. loads the per-group :class:`VoiceSettings`; if transcription is
   disabled → no-op (the common case — most groups never enable it, so
   the early return keeps the hot path cheap);
2. if the group already burned its per-UTC-day Whisper allowance —
   either the request count (``OPENAI_STT_GROUP_DAILY_LIMIT``) or the
   audio budget (``OPENAI_STT_GROUP_DAILY_SECONDS``) → no-op. This is
   the one OpenAI-billed path the user pays nothing for, so the
   operator's key is the only thing standing between a busy group and
   an open tab. Both ceilings are needed because they bound different
   things: Whisper charges by the second, so counting requests alone
   prices a three-hour voice note the same as a three-second one;
3. skips a single voice longer than ``OPENAI_STT_MAX_VOICE_SECONDS``;
4. if ``only_admins`` and the speaker is not a group admin → no-op;
5. skips oversized voices (> :data:`_MAX_VOICE_BYTES`, mirroring the
   ``/voice`` TTS cap M-P-3);
6. downloads the ogg/opus bytes and calls
   :meth:`WhisperSttService.transcribe`;
7. on degraded (no ``OPENAI_API_KEY``) → silently skip (debug log, NO
   group spam) — same posture as the Phase A payment providers;
8. on success → persists via :class:`VoiceTranscriptionRepo` and routes
   the formatted-quote text to the configured target.

Two things the ceilings used to miss, fixed together because they are
the same hole seen from either end — the ``voice_transcriptions`` row is
what the ceilings count, and it was written neither often enough nor
early enough:

* #260 — a Whisper reply that arrives on an HTTP 200 but carries no
  usable transcript (``empty``: silence or noise; ``bad_response``: a
  body we could not read) is a request OpenAI served and billed by the
  second. No row was written, so neither counter moved and a group could
  spend the owner's key indefinitely on 300-second voices that
  transcribe to nothing. Those two outcomes now write a text-less row.
  ``network``, ``http_*`` and ``no_key`` deliberately do NOT — see
  :data:`_BILLED_ERRORS`.
* #221 — the counters were read, then three network calls happened
  before the row appeared, so every voice in the same tick saw the same
  "before" and the ceilings held only for strictly sequential traffic.
  The decision now runs under a per-chat lock and books a reservation
  that outlives it — see :func:`_admit`.

Parse mode is HTML (bot-wide); the transcript is ``html.escape``-d and
wrapped in a ``<blockquote>`` (the legacy Markdown ``> `` quote ported to
HTML). Language for the i18n wrapper comes from the GROUP setting
(``transcription_language``), not the speaker's Telegram locale — the
card is addressed to the group, so it follows the group's configured
language.
"""

from __future__ import annotations

import asyncio
import html
import io
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.voice_settings_repo import (
    VoiceSettings,
    VoiceSettingsRepo,
)
from telegram_invite_bot.repositories.voice_transcription_repo import (
    VoiceTranscriptionRepo,
)
from telegram_invite_bot.services.whisper_stt_service import WhisperSttService
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.language import lang_from_code
from telegram_invite_bot.utils.plural import plural

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiogram.types import Message, User, Voice

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="handlers.voice_transcribe")

# Visible-char cap for the inline quote (legacy
# ``VOICE_TRANSCRIPTION_VISIBLE_CHARS`` = 600, bot.py:38777). Beyond this
# the quote is truncated with an "… N more chars" note rather than
# flooding the chat with a wall of text.
_VISIBLE_CHARS = 600

# Hard size cap on the voice file we'll download+transcribe. Mirrors the
# ``/voice`` TTS audio cap (M-P-3) and keeps a single oversized voice from
# pulling a 25MB download into memory. Telegram voice notes are tiny in
# practice (ogg/opus), so this only ever rejects pathological forwards.
_MAX_VOICE_BYTES = 25 * 1024 * 1024

# #260: the ``SttResult.error`` values that still cost money. Both reach
# us on an HTTP 200 — OpenAI accepted the audio, transcribed it and
# charged for the seconds; only the payload was unusable ("empty" =
# silence or noise, "bad_response" = a body we could not parse). They
# have to move the daily counters or the ceilings do not bound spend.
#
# The ones deliberately left OUT are as important. "network" never got a
# reply, so we cannot say a request was served; "http_*" was refused by
# OpenAI before the work happened (429 rate-limit included); "no_key"
# never left the process. Billing those would let a single outage close
# a group's transcription for the rest of the UTC day — the counter has
# no way to give the allowance back.
_BILLED_ERRORS = frozenset({"empty", "bad_response"})


@dataclass(slots=True)
class _InFlight:
    """Whisper requests this process committed to but has not yet counted."""

    calls: int = 0
    seconds: int = 0


# #221: the gate is a check-then-act with three network calls inside the
# window — the admin lookup, the download, and Whisper itself. Until the
# row lands, ``count_since``/``seconds_since`` still report the state
# from before, so N voices arriving in one tick all read the same
# "before" and all pass. A probe with seed=2, limit=3 and five parallel
# voices bought five Whisper requests instead of one.
#
# The fix is a reservation, not a longer lock: holding the chat lock
# across a 60-second Whisper timeout would serialise a chatty group
# behind its slowest voice. Instead the decision runs under the lock and
# the winner books its own request against the day's budget before
# releasing it, so the next voice sees the spend that is still in the
# air. The lock is held for two SELECTs and some arithmetic.
#
# In-process only, which is the honest scope: the bot is one systemd
# unit on one event loop in production. A second
# process would need the reservation in the database; the row written on
# every BILLED outcome stays the durable record either way, so the
# fallback is the pre-#221 behaviour rather than an unbounded key.
_gate_locks: KeyedLocks[int] = KeyedLocks()
_in_flight: dict[int, _InFlight] = {}
# #1938: the same reservation, keyed on the speaker instead of the chat,
# because the budget it feeds spans chats. Weaker than its sibling by
# construction: ``_gate_locks`` is per-chat, so two voices from one
# person in two different groups are decided concurrently and can both
# read the same "before". Booking still narrows that to whatever lands
# inside a single tick — a constant overshoot — instead of the
# unbounded, per-group-multiplied one the budget exists to stop. Making
# it exact would mean a second lock scope around a decision that already
# holds one, i.e. lock ordering between two keyed locks, for an overshoot
# the durable rows correct on the next voice.
_user_in_flight: dict[int, int] = {}


def _book(chat_id: int, user_id: int, duration: int) -> None:
    """Reserve one Whisper request of ``duration`` seconds for ``chat_id``.

    Sync on purpose: no ``await`` between the read and the write means
    no other coroutine can observe the slot mid-update, which is the
    same guarantee :class:`KeyedLocks` relies on. Slot discipline is
    borrowed from it too — created by the first booker, dropped by the
    last one out, so an idle group holds no memory.
    """
    entry = _in_flight.get(chat_id)
    if entry is None:
        entry = _InFlight()
        _in_flight[chat_id] = entry
    entry.calls += 1
    entry.seconds += duration
    _user_in_flight[user_id] = _user_in_flight.get(user_id, 0) + duration


def _unbook(chat_id: int, user_id: int, duration: int) -> None:
    """Release what :func:`_book` reserved.

    Normally the outcome is durable either way by the time this runs: a
    ``voice_transcriptions`` row (every billed outcome writes one) or
    nothing at all (we never reached OpenAI).

    There is a third state, and it is not recoverable: :func:`_persist`
    swallows its own DB failure on purpose so a transcript that already
    exists still reaches the chat (:483-486). That leaves a request that
    WAS billed with no row behind it — and since ``daily_limit`` /
    ``daily_seconds`` are computed from those rows, the day's budget
    never sees the call. We log a warning and let the booking go rather
    than hold the slot: keeping it would leak the reservation for the
    life of the process without ever charging the right day (#743).
    """
    pending = _user_in_flight.get(user_id, 0) - duration
    if pending > 0:
        _user_in_flight[user_id] = pending
    else:
        _user_in_flight.pop(user_id, None)

    entry = _in_flight.get(chat_id)
    if entry is None:  # pragma: no cover — _book always precedes _unbook
        return
    entry.calls -= 1
    entry.seconds -= duration
    if entry.calls <= 0:
        del _in_flight[chat_id]


def _utc_midnight() -> datetime:
    """Naive-UTC midnight of the current day.

    UTC here — unlike :class:`AiQuotaRepo`'s ``_today_iso``, which
    follows legacy's *local* calendar day — because the column this
    bounds is itself UTC: ``voice_transcriptions.created_at`` is naive
    UTC from both writers (new pipeline ``db_now``, legacy's
    ``DEFAULT CURRENT_TIMESTAMP`` at bot.py:5914, and SQLite's
    ``CURRENT_TIMESTAMP`` is UTC). Bounding a UTC column with a local
    midnight would shift the STT day by the host's offset.
    """
    return datetime.combine(datetime.now(UTC).date(), time.min)


def _format_quote(raw_text: str, lang: str, max_chars: int = _VISIBLE_CHARS) -> str:
    """HTML ``<blockquote>`` of the transcript, truncated past ``max_chars``.

    Ports legacy ``_format_transcription_as_quote`` (bot.py:38781) from
    Markdown to HTML: the text is ``html.escape``-d (operators/speakers
    produce arbitrary content; a literal ``<`` would break the bot-wide
    HTML parse mode) and wrapped in a single ``<blockquote>``. Past
    ``max_chars`` it is cut and a localized "… N more chars" tail added.
    The tail is not escaped and must not be: it is a catalogue string
    interpolating an int and another catalogue string, and the catalogue
    is validated HTML
    (``tests/regression/test_i18n_html_safety.py``).
    """
    text = (raw_text or "").strip()
    if len(text) <= max_chars:
        body = html.escape(text)
        return f"<blockquote>{body}</blockquote>"
    truncated = text[:max_chars].rstrip()
    rest = len(text) - len(truncated)
    body = html.escape(truncated)
    tail = t("h_vtr_more_chars", lang, count=rest, noun=plural(rest, "h_plural_chars", lang))
    return f"<blockquote>{body}</blockquote>\n\n<i>{tail}</i>"


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """True if ``user_id`` is an admin/creator of ``chat_id``.

    Fail-closed: a Telegram-API error returns ``False`` so an
    ``only_admins`` group never transcribes a non-verified speaker when
    the admin list can't be read.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as exc:  # noqa: BLE001 — fail-closed, log and refuse
        log.bind(chat_id=chat_id, exc=repr(exc)).debug(
            "voice_transcribe: get_chat_administrators failed"
        )
        return False
    return any(member.user.id == user_id for member in admins)


async def _group_admin_ids(bot: Bot, chat_id: int) -> list[int]:
    """Non-bot admin user-ids for ``chat_id`` (empty on API error)."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as exc:  # noqa: BLE001
        log.bind(chat_id=chat_id, exc=repr(exc)).debug("voice_transcribe: admin id fetch failed")
        return []
    return [m.user.id for m in admins if not m.user.is_bot]


async def _route_to_target(
    bot: Bot,
    message: Message,
    settings: VoiceSettings,
    body: str,
    lang: str,
) -> None:
    """Deliver the formatted quote to the configured target.

    Ports legacy ``send_transcription_to_target`` (bot.py:38804):

    * ``chat`` → reply in the group;
    * ``private`` → DM the speaker; on failure (DM blocked) fall back to
      a group reply;
    * ``admins`` → DM each (non-bot) group admin (best-effort per admin);
    * ``log_chat`` → send to ``transcription_log_chat_id``; if unset or
      send fails, fall back to a group reply.

    Any unexpected target value falls back to a group reply (legacy's
    ``else`` branch). All copy is HTML.
    """
    target = settings.target or "chat"
    try:
        if target == "private":
            speaker = message.from_user
            if speaker is None:
                # #1538: no sender to DM (channel post / anonymous admin).
                # The visible outcome is the same as a blocked DM, but it is
                # now stated: the old ``assert`` vanished under ``python -O``
                # and the resulting AttributeError landed in the same
                # ``except`` by accident rather than by design.
                await message.reply(t("h_vtr_private_blocked", lang))
                return
            try:
                await bot.send_message(
                    speaker.id,
                    t("h_vtr_private_header", lang) + "\n\n" + body,
                )
            except Exception:  # noqa: BLE001 — DM blocked → group fallback
                await message.reply(t("h_vtr_private_blocked", lang))
            return
        if target == "admins":
            header = t("h_vtr_admins_header", lang)
            for uid in await _group_admin_ids(bot, message.chat.id):
                try:
                    await bot.send_message(uid, header + "\n\n" + body)
                except Exception:  # noqa: BLE001, S112 — best-effort per admin
                    continue
            return
        if target == "log_chat":
            log_chat_id = settings.log_chat_id
            if log_chat_id:
                try:
                    await bot.send_message(int(log_chat_id), body)
                    return
                except Exception:  # noqa: BLE001, S110 — fall through to group reply
                    pass
            await message.reply(body)
            return
        # "chat" and any unknown target → group reply.
        await message.reply(body)
    except Exception as exc:  # noqa: BLE001 — last-ditch plain reply
        log.bind(chat_id=message.chat.id, exc=repr(exc)).debug(
            "voice_transcribe: target routing failed"
        )


@asynccontextmanager
async def _admit(
    registry: EngineRegistry,
    chat_id: int,
    user_id: int,
    duration: int,
    *,
    daily_limit: int,
    daily_seconds: int,
    user_daily_seconds: int,
    max_voice_seconds: int,
) -> AsyncIterator[VoiceSettings | None]:
    """Run the whole spend decision for one voice, then book it (#221).

    Yields the group's settings when the voice is admitted and ``None``
    when it is refused; the caller returns on ``None``. The reservation
    is released when the block exits, whatever the outcome — including
    the paths that give up before OpenAI (download failure, size cap),
    which cost nothing and must give their booking back.

    Everything that reads shared state happens under the per-chat lock:
    the settings read, both day counters, the gates, and the booking.
    Nothing awaits a network call inside it. A disabled group — the
    common case, since most groups never turn transcription on — pays
    one uncontended acquire and a single SELECT, the same round trip it
    paid before.
    """
    admitted: VoiceSettings | None = None
    async with _gate_locks.acquire(chat_id):
        async with session_for(registry, DBName.USERS) as session:
            settings = await VoiceSettingsRepo(session).get(chat_id)
            # Counted on the SAME session as the settings read — one
            # round-trip for the whole gate — and only when the gate can
            # actually fire, so a disabled group stays a single SELECT.
            repo = VoiceTranscriptionRepo(session)
            midnight = _utc_midnight()
            used_today = (
                await repo.count_since(chat_id, midnight)
                if settings.enabled and daily_limit > 0
                else 0
            )
            seconds_today = (
                await repo.seconds_since(chat_id, midnight)
                if settings.enabled and daily_seconds > 0
                else 0
            )
            user_seconds_today = (
                await repo.seconds_since_for_user(user_id, midnight)
                if settings.enabled and user_daily_seconds > 0
                else 0
            )

        if settings.enabled and _passes_gates(
            chat_id,
            user_id,
            duration,
            used_today,
            seconds_today,
            user_seconds_today,
            daily_limit=daily_limit,
            daily_seconds=daily_seconds,
            user_daily_seconds=user_daily_seconds,
            max_voice_seconds=max_voice_seconds,
        ):
            _book(chat_id, user_id, duration)
            admitted = settings

    if admitted is None:
        yield None
        return
    try:
        yield admitted
    finally:
        _unbook(chat_id, user_id, duration)


def _passes_gates(
    chat_id: int,
    user_id: int,
    duration: int,
    used_today: int,
    seconds_today: int,
    user_seconds_today: int,
    *,
    daily_limit: int,
    daily_seconds: int,
    user_daily_seconds: int,
    max_voice_seconds: int,
) -> bool:
    """The four ceilings, in cost order. Callers hold the chat lock.

    Each ceiling is ``0`` for unlimited (the project-wide convention,
    cf. ``AiQuotaConfig``). Refusals are silent for the group and loud
    for ops: this module never posts an error for a voice nobody asked
    to transcribe, and past a cap that would mean spamming every voice
    note.
    """
    # Duration first: it is the only gate that can reject a voice on the
    # strength of the update alone, and it is what keeps a single
    # forward from spending an hour of the budget.
    if max_voice_seconds > 0 and duration > max_voice_seconds:
        log.bind(chat_id=chat_id, duration=duration, cap=max_voice_seconds).warning(
            "voice_transcribe: voice longer than the per-voice cap, skipping"
        )
        return False

    # #221: what is already booked but not yet written counts as spent.
    # Without this the two budgets below are read from a snapshot that
    # every concurrent voice in this tick shares.
    entry = _in_flight.get(chat_id)
    pending_calls = entry.calls if entry is not None else 0
    pending_seconds = entry.seconds if entry is not None else 0

    if daily_seconds > 0 and seconds_today + pending_seconds + duration > daily_seconds:
        # ``+ duration``, not ``>=`` on what was already spent: this
        # budget is denominated in the thing being bought.
        log.bind(
            chat_id=chat_id,
            used=seconds_today,
            in_flight=pending_seconds,
            adding=duration,
            budget=daily_seconds,
        ).warning("voice_transcribe: group hit its daily STT audio budget, skipping")
        return False

    # #1938: the same arithmetic one scope out. Checked after the group
    # budget only because a group that is already over spends nothing
    # either way; neither order changes an outcome.
    pending_user_seconds = _user_in_flight.get(user_id, 0)
    if (
        user_daily_seconds > 0
        and user_seconds_today + pending_user_seconds + duration > user_daily_seconds
    ):
        log.bind(
            chat_id=chat_id,
            user_id=user_id,
            used=user_seconds_today,
            in_flight=pending_user_seconds,
            adding=duration,
            budget=user_daily_seconds,
        ).warning("voice_transcribe: speaker hit their daily STT audio budget, skipping")
        return False

    if daily_limit > 0 and used_today + pending_calls >= daily_limit:
        # Checked BEFORE the admin lookup and the download: both cost
        # something (a Telegram call, then bytes plus a billed Whisper
        # request), and the point of the ceiling is to stop spending.
        log.bind(
            chat_id=chat_id,
            used=used_today,
            in_flight=pending_calls,
            limit=daily_limit,
        ).warning("voice_transcribe: group hit its daily STT ceiling, skipping")
        return False

    return True


async def _persist(
    registry: EngineRegistry,
    message: Message,
    voice: Voice,
    speaker: User,
    settings: VoiceSettings,
    *,
    text: str | None,
    processing_ms: int | None,
) -> None:
    """Record one BILLED Whisper request.

    Runs on its own USERS session so a DB hiccup never blocks delivery
    of a transcript that already exists. ``text`` is ``None`` for the
    #260 billed-but-unusable outcomes; that row exists purely so the
    day's counters reflect the invoice, and :meth:`stats` skips it so
    the operator-facing card keeps meaning "transcriptions".
    """
    try:
        async with session_for(registry, DBName.USERS) as session:
            await VoiceTranscriptionRepo(session).add(
                group_id=message.chat.id,
                user_id=speaker.id,
                message_id=message.message_id,
                file_id=voice.file_id,
                file_unique_id=voice.file_unique_id,
                duration=voice.duration,
                transcribed_text=text,
                language=settings.language,
                model_used=WhisperSttService.model_used(),
                processing_time=processing_ms,
            )
    except Exception as exc:  # noqa: BLE001 — persist failure shouldn't drop the reply
        log.bind(chat_id=message.chat.id, exc=repr(exc)).warning(
            "voice_transcribe: persist failed (delivering anyway)"
        )


async def handle_voice(
    message: Message,
    bot: Bot,
    registry: EngineRegistry,
    api_key: str | None,
    daily_limit: int = 0,
    daily_seconds: int = 0,
    max_voice_seconds: int = 0,
    *,
    user_daily_seconds: int = 0,
) -> None:
    """Transcribe a group voice message per the group's settings.

    Four independent ceilings, each ``0`` for unlimited and each
    defaulting to unlimited so existing callers and tests that only care
    about transcription behaviour don't have to thread them:

    ``daily_limit``
        Billed Whisper calls per group per UTC day
        (``OPENAI_STT_GROUP_DAILY_LIMIT``).
    ``daily_seconds``
        Seconds of audio per group per UTC day
        (``OPENAI_STT_GROUP_DAILY_SECONDS``). This is the one that
        tracks the invoice — see below.
    ``max_voice_seconds``
        Longest single voice we will send at all
        (``OPENAI_STT_MAX_VOICE_SECONDS``).
    ``user_daily_seconds``
        Seconds of audio per SPEAKER per UTC day, across every group
        (``OPENAI_STT_USER_DAILY_SECONDS``). Keyword-only: it arrived
        after the other three (#1938) and the positional callers that
        predate it must keep meaning what they meant. The three above
        are all per-group, and a group is free to create — see the
        settings comment for the shape that walks around them.

    The seconds gate is checked *including the voice about to be sent*,
    unlike the call gate which only asks whether the allowance is
    already gone. The difference is the whole point: a request costs the
    same whatever it carries, so counting it after the fact loses
    nothing, whereas a single voice can carry hours of billable audio
    and "you were under the limit when you started" would wave exactly
    that one through.

    All three live in :func:`_passes_gates`, run under the chat lock by
    :func:`_admit`, which also books the request so a concurrent voice
    cannot re-spend the same allowance (#221).
    """
    voice = message.voice
    speaker = message.from_user
    if voice is None or speaker is None:
        return

    # No key, no transcription — bail before anything is spent (#742).
    # ``WhisperSttService`` already refuses keyless (returning
    # ``error="no_key"`` with no network call), but only after
    # ``_admit`` has taken the chat lock, read the group settings, run
    # both counter queries and BOOKED the request against the daily
    # allowance, and after the ogg bytes have been downloaded. On a
    # deployment without ``OPENAI_API_KEY`` that is a lock, four
    # queries and a full media download per group voice message,
    # buying an allowance nothing can ever spend.
    if api_key is None:
        return

    chat_id = message.chat.id
    duration = voice.duration or 0

    async with _admit(
        registry,
        chat_id,
        speaker.id,
        duration,
        daily_limit=daily_limit,
        daily_seconds=daily_seconds,
        user_daily_seconds=user_daily_seconds,
        max_voice_seconds=max_voice_seconds,
    ) as settings:
        if settings is None:
            return

        # Group-language drives the i18n wrapper (the card addresses the
        # group, not the individual speaker). ``transcription_language``
        # is a Whisper hint (e.g. "ru"); coarsen it to ru/en for copy.
        lang = lang_from_code(settings.language)

        if settings.only_admins and not await _is_group_admin(bot, chat_id, speaker.id):
            return

        # Size sanity — skip pathological voices (M-P-3 parity). Cheap:
        # no bytes moved yet.
        if voice.file_size is not None and voice.file_size > _MAX_VOICE_BYTES:
            log.bind(chat_id=chat_id, size=voice.file_size).debug(
                "voice_transcribe: voice exceeds size cap, skipped"
            )
            return

        # Download the ogg/opus bytes.
        try:
            buffer = io.BytesIO()
            await bot.download(voice.file_id, destination=buffer)
            audio = buffer.getvalue()
        except Exception as exc:  # noqa: BLE001 — transient download failure
            log.bind(chat_id=chat_id, exc=repr(exc)).debug(
                "voice_transcribe: voice download failed"
            )
            return

        # #119: the pre-check above is an optimisation, not the guard —
        # ``file_size`` is Optional in the Bot API, so a message that
        # simply omits it walked straight past it and we uploaded
        # whatever arrived to Whisper (billed by the second, on the
        # owner's key). Measure what we actually hold and fail closed on
        # the real number.
        if len(audio) > _MAX_VOICE_BYTES:
            log.bind(chat_id=chat_id, size=len(audio), declared=voice.file_size).debug(
                "voice_transcribe: downloaded voice exceeds size cap, skipped"
            )
            return

        stt = WhisperSttService(api_key, timeout=60.0)
        # #1966: the audio is on the wire for up to sixty seconds while
        # uvicorn's graceful shutdown budget is twenty
        # (``runner/webhook.py``), so a redeploy cancels this await with
        # the request already accepted and billing by the second.
        # ``CancelledError`` is a ``BaseException``: it walks past every
        # ``except Exception`` on the way out, ``webhook/server.py``
        # writes no response, and Telegram redelivers to a process whose
        # ``seen_updates`` cache starts empty — so the same audio is sent
        # to OpenAI a second time. Both invoices were invisible to the
        # ceilings, which are computed from these rows.
        #
        # This belongs with ``empty``/``bad_response`` rather than with
        # ``network`` (see :data:`_BILLED_ERRORS`): ``network`` means
        # OpenAI never served the request, whereas here WE walked away
        # from one it had accepted. The clause wraps this await and
        # nothing else on purpose — a cancel during the download costs
        # nothing, and charging for it would close the group's day for
        # spend that never happened.
        #
        # ``suppress`` for the same reason as #1954: a write failure must
        # never replace the cancellation the caller is waiting on.
        # ``_persist`` swallows its own errors today, but that is its
        # choice to revisit, not a guarantee this path may lean on.
        try:
            result = await stt.transcribe(audio, language=settings.language)
        except asyncio.CancelledError:
            log.bind(chat_id=chat_id, duration=voice.duration).warning(
                "voice_transcribe: cancelled on the wire — recording the spend"
            )
            with suppress(Exception):
                await _persist(
                    registry,
                    message,
                    voice,
                    speaker,
                    settings,
                    text=None,
                    processing_ms=None,
                )
            raise

        if not result.ok:
            # Degraded (no_key) or soft failure (empty/network/http) —
            # skip silently. NEVER post an error into the group for a
            # voice the user didn't explicitly ask to transcribe.
            #
            # #260: silence is not the same as free. An "empty" or
            # "bad_response" reply came back on an HTTP 200, so the
            # seconds were billed; record the spend before skipping, or
            # the ceilings never see it. See :data:`_BILLED_ERRORS` for
            # why the other error values are NOT recorded.
            if result.error in _BILLED_ERRORS:
                await _persist(
                    registry,
                    message,
                    voice,
                    speaker,
                    settings,
                    text=None,
                    processing_ms=result.processing_ms,
                )
            log.bind(chat_id=chat_id, error=result.error).debug(
                "voice_transcribe: transcription skipped"
            )
            return

        assert result.text is not None

        # Persist, then route.
        await _persist(
            registry,
            message,
            voice,
            speaker,
            settings,
            text=result.text,
            processing_ms=result.processing_ms,
        )

        body = _format_quote(result.text, lang)
        await _route_to_target(bot, message, settings, body, lang)

        # auto_delete_voice: remove the original voice note after
        # transcribing (legacy auto_delete_voice flag). Best-effort —
        # needs delete rights.
        if settings.auto_delete:
            try:
                await bot.delete_message(chat_id, message.message_id)
            except Exception as exc:  # noqa: BLE001 — no rights / already gone
                log.bind(chat_id=chat_id, exc=repr(exc)).debug(
                    "voice_transcribe: auto-delete failed"
                )

        log.bind(chat_id=chat_id, uid=speaker.id, chars=len(result.text)).info("voice transcribed")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Group-only ``F.voice`` router for L-70 transcription.

    The OpenAI key is resolved ONCE at build time from
    ``settings.openai.api_key`` (a ``SecretStr | None``) to its plaintext
    and closed over — the degraded-skip path then needs no settings
    access per message. A private-chat voice never reaches here (router
    filter), so the handler only ever sees group voices.
    """
    api_key = (
        settings.openai.api_key.get_secret_value() if settings.openai.api_key is not None else None
    )
    daily_limit = settings.openai.stt_group_daily_limit
    daily_seconds = settings.openai.stt_group_daily_seconds
    max_voice_seconds = settings.openai.stt_max_voice_seconds
    user_daily_seconds = settings.openai.stt_user_daily_seconds

    router = Router(name="voice_transcribe")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    async def _entry(message: Message, bot: Bot) -> None:
        await handle_voice(
            message,
            bot,
            registry,
            api_key,
            daily_limit,
            daily_seconds,
            max_voice_seconds,
            user_daily_seconds=user_daily_seconds,
        )

    router.message.register(_entry, F.voice, F.from_user)
    return router
