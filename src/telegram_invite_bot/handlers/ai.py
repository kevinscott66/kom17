"""``/ai``, ``/ask``, ``/gpt``, ``/chat``, ``/kom_ai`` handlers.

One provider (DeepSeek), one billing posture (free, daily-quota
gated), four user-facing aliases that map onto the same path —
matching the legacy monolith, where ``['ai', 'ask', 'chat',
'kom_ai']`` all dispatched into a single DeepSeek assistant
(``ai_assistant``). The strangler iteration briefly diverged by
routing ``/ai`` / ``/gpt`` / ``/chat`` through OpenAI with coin
billing; this module re-aligns with legacy so the four aliases
share one provider, one quota, no coin charge.

* ``/ask <prompt>`` — DeepSeek, any chat (legacy registered it with
  no chat gate). Group context IS injected (chat title + the
  replied-to message, see :func:`_group_context`); what stays unported
  is legacy's feed of recent chat messages, because the new stack has
  no message-history store.
* ``/ai <prompt>`` / ``/ии`` / ``/gpt <prompt>`` / ``/chat <prompt>``
  / ``/kom_ai <prompt>`` — DeepSeek, private + group.
* Bare ``/ai`` / ``/ии`` / ``/gpt`` / ``/chat`` / ``/kom_ai`` (no
  args, any chat) → static help card.

The :class:`AiService` is constructed per-request inside the
handler with a fresh ``httpx.AsyncClient`` so the API key is read
from the typed config at handle-time (no module-level client).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.core.ai_modes import (
    AiModeStore,
    mode_hint,
    resolve_mode_token,
)
from telegram_invite_bot.core.chat_types import GROUP_TYPE_NAMES
from telegram_invite_bot.db.models.ai_quota import AiDailyRequest
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.ai_controls import (
    MODE_ORDER,
    AiContextClear,
    AiExport,
    AiKomEnter,
    AiKomExit,
    AiModePick,
    build_controls_markup,
    build_quota_limit_markup,
    is_known_mode,
    mode_title,
)
from telegram_invite_bot.middlewares.ai_rate_limit import AiRateLimitMiddleware
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware

# Single-source the "today" day boundary with the quota write side
# (local-midnight reset, matching legacy bot.py:38478) — re-deriving
# the date format here would let the /ai_limits card disagree with the
# counter on the date rollover.
from telegram_invite_bot.repositories.ai_quota_repo import _today_iso
from telegram_invite_bot.services.ai_context import (
    AiContextBuilder,
    GroupContext,
    build_reply_target,
    markdown_to_html,
)
from telegram_invite_bot.services.ai_memory import (
    AiMemoryStore,
    KomModeStore,
    recent_for_prompt,
    render_history_text,
)
from telegram_invite_bot.services.ai_quota_service import (
    AiQuotaConfig,
    AiQuotaService,
    QuotaOutcome,
)
from telegram_invite_bot.services.ai_service import (
    AiNotConfiguredError,
    AiRequestError,
    AiResponseCache,
    AiService,
)
from telegram_invite_bot.services.weather_service import WeatherService
from telegram_invite_bot.utils.aiogram import BENIGN_EDIT_REJECTS, command_args
from telegram_invite_bot.utils.render import clamp_utf16, utf16_length

log = logger.bind(component="handlers.ai")

# In-process state shared across every request in this process (matches
# the legacy in-memory ``DeepSeekAI`` singletons). All are bounded and
# reset on restart — a deliberate, documented tradeoff (see each store's
# module docstring): no migration, mode/history are cheap to re-establish.
_MODE_STORE = AiModeStore()
_MEMORY_STORE = AiMemoryStore()
_KOM_MODE_STORE = KomModeStore()
_RESPONSE_CACHE = AiResponseCache()

# Expert mode gets a higher token ceiling (legacy ``bot.py:37216``).
_EXPERT_MAX_TOKENS = 2000

# #1597: ``AiRequestError.reason`` -> the key the reader sees. The
# service used to answer a failed call with a hardcoded Russian
# sentence, which an English reader then got verbatim; the wording
# belongs here, where ``lang`` is in scope. Reasons absent from the
# table (``bad_response``, ``empty_content``) fall through to
# ``h_ai_error_unknown``: upstream answered, but with nothing that
# could be turned into a reply, which is a different sentence from
# "upstream did not answer".
_AI_ERROR_KEYS: dict[str, str] = {
    "timeout": "h_ai_error_timeout",
    "network": "h_ai_error_network",
    "http": "h_ai_error_http",
    "empty_choices": "h_ai_error_empty",
}

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import InlineKeyboardMarkup

    from telegram_invite_bot.config.settings import (
        AiConfig,
        AiQuotaSettings,
    )
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.ai_quota_repo import AiQuotaRepo
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.vip_repo import VipRepo


# The four static AI cards live in ``i18n/data/*.yaml`` as ``h_ai_*``.
#
# #1657: the parity claim that used to stand here is withdrawn, not
# re-anchored. It named ``bot/handlers/ai.py:23-34`` — a directory that
# has never existed in this repository, legacy being one file at the
# root — alongside ``bot.py:39634``, which is ``_cleanup_voice_locks``,
# a voice-lock sweeper thread with nothing to do with AI copy. Searching
# legacy for this copy finds no byte-identical source at all: the
# nearest legacy text is the one-line ``/ai`` command description
# (``bot.py:42582``) and a single bullet in the help card
# (``bot.py:43002``). So these four cards are this port's own writing,
# and the HTML they are written in is the new bot's default parse_mode
# rather than a re-rendering of anything.


# Telegram caps a single message at 4096 chars. We leave headroom for
# the framing (newline at the start of long replies, etc.) and append
# an ellipsis when we truncate.
#
# #1657: "exactly like legacy" was false twice over and is dropped. The
# cited ``bot/handlers/ai.py:60-61`` names a directory that does not
# exist, and the number does not match either: 3997 appears nowhere in
# legacy, whose only ellipsis truncations are two 3900s on paths that
# have nothing to do with ``/ask`` — the owner error alert
# (``bot.py:813``) and the log handler's ``emit`` (``bot.py:937``).
# Legacy's AI reply is not length-capped here at all. This ceiling is
# therefore ours, and the headroom is the reason for it.
_TG_MAX = 3997


def _truncate(text: str) -> str:
    """Telegram-safe truncation with ellipsis. Used by both providers.

    #298: this cuts RAW model text, never converted HTML. Both call
    sites below run it before :func:`markdown_to_html`, and the order
    matters — see the comment at the ``/ask`` call site.

    #1972: measured in UTF-16 units, not code points. Telegram counts
    units, an astral character costs two of them and ``len`` one, and
    the headroom above is 98 — so 99 emoji in the kept prefix spend it
    and the send comes back 400 "message is too long". Nothing
    downstream rescues that: the length guard logs and deliberately
    does not truncate, the parse-mode fallback only retries parse
    errors, and the marker is not benign in ``handlers/errors``, so the
    answer is replaced by the generic error card with the tokens
    already spent. ``clamp_utf16`` is the same pair of corrections
    ``/profile`` and ``/top`` already use; the ellipsis is BMP and
    costs one unit, so the 3998-unit result is unchanged for text that
    is entirely BMP.
    """
    if utf16_length(text) > _TG_MAX:
        return clamp_utf16(text, _TG_MAX) + "…"
    return text


def _controls_for(
    message: Message, uid: int | None, lang: str, *, is_vip: bool
) -> InlineKeyboardMarkup | None:
    """Reply-card controls, or ``None`` where they don't belong (RR-6 #64).

    PRIVATE only, matching legacy (``bot.py:38584`` attached the keyboard
    ``if is_private else None``). Two reasons it isn't merely parity: a
    persona switch is a *personal* setting, so in a group the buttons
    would read as chat-wide controls while quietly editing whoever tapped
    them; and the quota keyboard's siblings route through ``MainMenu``,
    whose router is PRIVATE-filtered — a group card would ship dead
    buttons, which RR-59 established we don't do.
    """
    if uid is None or message.chat.type != "private":
        return None
    return build_controls_markup(
        lang=lang,
        session_active=_KOM_MODE_STORE.is_active(uid, uid),
        can_change_mode=is_vip,
        current_mode=_MODE_STORE.get(uid),
    )


#: Trigger words that address the assistant with no question attached.
#: Kept as one constant because the two checks below (raw, then emoji-
#: stripped) must never drift apart.
_BARE_TRIGGERS = frozenset({"ком", "kom", "ии", "ai"})


def extract_ai_direct_question(text: str | None, *, is_group: bool = False) -> str | None:
    """Port of legacy ``extract_ai_direct_question`` (``bot.py:37809``).

    Pulls the question out of a plain-text message addressed to the Kom
    assistant:

    * ``"ии ..."`` / ``"ии, ..."`` / ``"ии: ..."`` → the trailing text.
    * ``"ком ..."`` / ``"ком, ..."`` / ``"ком: ..."`` → the trailing text.
    * ``"kom ..."`` / ``"kom, ..."`` / ``"kom: ..."`` → the trailing text.
    * ``"ai, ..."`` / ``"ai: ..."`` → the trailing text, in any chat; the
      space form ``"ai ..."`` only in a private chat (see below).
    * Bare ``"ком"`` / ``"kom"`` / ``"ии"`` / ``"ai"`` (optionally wrapped
      in emoji or zero-width chars) → ``""`` so the handler shows the
      usage nudge instead of calling the model with an empty prompt.
    * Anything else → ``None`` (not addressed to the assistant).

    Latin ``"kom"`` is a deliberate addition over legacy
    (``bot.py:37823`` knows only «ии»/«ком»). The English FAQ has always
    told its readers to type ``"kom question"``, which matched nothing
    at all — the assistant is «ком17», and an English-keyboard reader
    has no way to type the Cyrillic spelling (#206). It gets «ком»'s
    rule, not "ai"'s, because it is not an English word: no sentence
    opens with it by accident, so the space form is safe in a group.

    ``is_group`` exists for one word. «ии»/«ком»/"kom" open a sentence
    roughly never, so legacy accepted their space form anywhere — but
    "AI" opens English sentences constantly ("AI is going to change
    everything"), and claiming those would have the bot answering a
    conversation it was not part of. So in a group the two-letter latin
    trigger needs the same thing every other plain-text trigger needs
    there: a marker that the message is addressed to the bot — here the
    comma or colon. In a DM the whole message is addressed to the bot by
    definition, so the bare space form resolves, and «ии что такое X» /
    "ai what is X" finally behave the same (I18N-3).

    Slash commands never match: ``"/ai foo"`` does not start with
    ``"ии "`` / ``"ком "`` and isn't a bare token, so it returns ``None``.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    low = stripped.lower()

    for prefix in ("ии", "ком", "kom"):
        if low.startswith(prefix + " "):
            return stripped[len(prefix) :].strip()
        if low.startswith((prefix + ",", prefix + ":")):
            return stripped[len(prefix) + 1 :].strip()

    if low.startswith(("ai,", "ai:")):
        return stripped[3:].strip()
    if not is_group and low.startswith("ai "):
        return stripped[2:].strip()

    normalized = low.replace("\u200b", "").replace("\u200c", "").strip()
    if normalized in _BARE_TRIGGERS:
        return ""
    no_emoji = "".join(c for c in normalized if c.isalnum() or c in " ,").strip()
    if no_emoji in _BARE_TRIGGERS:
        return ""
    return None


async def handle_ai_help(message: Message, lang: str) -> None:
    """Bare AI alias (no args, any chat) — static help card."""
    await message.answer(t("h_ai_help", lang))


def _reply_target_extra(message: Message, lang: str) -> str | None:
    """L-67: build the reply-target instruction when the call is a reply.

    Returns ``None`` when the message isn't a reply, or replies to a bot
    (no point addressing the bot). Uses the replied-to author's display
    name (first name + username) — the new stack has no group display-name
    store, so we use what Telegram gives us on the message object.
    """
    rm = message.reply_to_message
    if rm is None or rm.from_user is None or rm.from_user.is_bot:
        return None
    target = rm.from_user
    display = target.full_name or (target.username or str(target.id))
    return build_reply_target(display_name=display, username=target.username, lang=lang)


def _group_context(message: Message) -> GroupContext | None:
    """L-64: assemble the group context block from the message.

    The new stack has no message-history store, so we inject the chat
    title plus (when the call is a reply) the replied-to text — exactly
    the degraded path the task specifies. ``recent`` stays ``None`` until
    a history store exists.

    ``text or caption``: a photo, video or document carries its words in
    ``caption`` and leaves ``text`` empty. Reading only ``text`` dropped
    the whole context of the most natural way to ask — reply to the
    picture someone posted and address the bot — and the model then
    answered about nothing, confidently. There is no vision here, so the
    caption is all the context that exists for such a message.
    """
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        return None
    reply_text = None
    rm = message.reply_to_message
    if rm is not None:
        replied_body = rm.text or rm.caption
        if replied_body:
            reply_text = replied_body[:400]
    return GroupContext(title=chat.title, recent=None, reply_text=reply_text)


async def _answer_with_ai(
    message: Message,
    prompt: str,
    lang: str,
    ai_config: AiConfig,
    ai_quota_repo: AiQuotaRepo,
    vip_repo: VipRepo,
    quota_settings: AiQuotaSettings,
    weather_service: WeatherService,
    user_settings_repo: UserSettingsRepo | None = None,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Shared DeepSeek path with injected context (Cluster J).

    Pipeline: quota gate → response-cache probe (history-less) → build
    persona preamble (modes) + dynamic context (time/weather/dice/group)
    + reply-target extra → single completion with rolling history →
    record exchange → cache (history-less) → safe reply.

    Still ONE upstream completion call — this is prompt construction and
    in-process state, not a tool-use loop. Assumes ``prompt`` is non-empty
    (callers render the empty hint themselves).
    """
    uid = message.from_user.id if message.from_user else None
    bound = log.bind(uid=uid, prompt_len=len(prompt))

    is_group = message.chat.type in ("group", "supergroup")
    # History key: group → chat id, private → user id (legacy parity).
    chat_key = message.chat.id if is_group else uid

    # #419: bail out BEFORE the quota gate when no provider key is set.
    # ``AiService.ask_with_context`` raises ``AiNotConfiguredError``
    # before any network call: it delegates to ``_complete``, whose
    # first statement is that check (ai_service.py:256-257) — but by
    # then the slot below is not only consumed, it is
    # committed by the checkpoint on purpose, so the middleware rollback
    # cannot hand it back. The user would then spend a whole day's quota
    # discovering that the feature is switched off, and stay locked out
    # until the host's local midnight on the day the owner finally sets
    # the key (#1952: this counter's day is local, not UTC).
    # ``handlers.quotes`` already guards exactly this way (quotes.py:206).
    if ai_config.api_key is None:
        bound.warning("ai not configured")
        await message.answer(t("h_ai_not_configured", lang))
        return

    # M-P-2: per-user daily quota gate. Consume BEFORE upstream so a
    # retry storm doesn't bypass the abuse limit. ``is_vip`` also gates
    # the persona modes (non-VIP pinned to default).
    is_vip = False
    # #1965: kept in the enclosing scope so the cancellation path around
    # the upstream call can hand the slot back. ``None`` covers the
    # anonymous-actor branch, where no slot was ever taken.
    quota_service: AiQuotaService | None = None
    consumed_slot = False
    if uid is not None:
        is_dev = uid in quota_settings.parsed_dev_user_ids()
        is_vip = (
            is_dev or (await vip_repo.get_active_profile(uid, now=datetime.now(UTC))) is not None
        )
        quota_service = AiQuotaService(
            ai_quota_repo,
            config=AiQuotaConfig(
                free_daily_limit=quota_settings.free_daily_limit,
                vip_daily_limit=quota_settings.vip_daily_limit,
                dev_user_ids=quota_settings.parsed_dev_user_ids(),
            ),
        )
        decision = await quota_service.check_and_consume(uid, is_vip=is_vip)
        # DEV and the unlimited tier write nothing and report ``count``
        # 0; only a real increment is releasable.
        consumed_slot = decision.count > 0
        if decision.outcome is QuotaOutcome.EXCEEDED:
            bound.info("ai quota exceeded")
            # RR-6 #65: a refusal with no way forward is a dead end. In a
            # private chat the card carries the VIP upsell and a route
            # back to the menu; in a group it stays plain text, because
            # both buttons are ``MainMenu`` taps and that router is
            # PRIVATE-filtered.
            in_private = message.chat.type == "private"
            body = t(
                "h_ai_daily_quota_exceeded",
                lang,
                count=decision.count,
                limit=decision.limit,
            )
            if in_private:
                # Branch on ``is_vip`` for the same reason
                # ``build_quota_limit_markup`` does: the default next
                # step is "grab VIP", and telling that to a VIP who just
                # hit the VIP ceiling reads as a bug. The VIP tier has a
                # finite default ceiling now (settings: AiQuotaSettings),
                # so this branch is reachable in stock config, not just
                # for an operator who tuned the limit by hand.
                key = "h_ai_quota_next_step_vip" if is_vip else "h_ai_quota_next_step"
                body = f"{body}\n\n{t(key, lang)}"
            # #1873: ``check_and_consume`` increments BEFORE it compares
            # (ai_quota_service.py:163), so the rejected call spent a
            # slot too — deliberately, because that is what stops a
            # retry storm from walking past the ceiling. This early
            # return sits above the handler's own checkpoint, so without
            # this one a refusal the user managed not to receive would
            # be rolled back by the session middleware together with the
            # increment, handing the slot straight back and making the
            # limit unenforceable for anyone who blocks the bot or hits
            # a FloodWait. Commit the slot, then speak.
            if checkpoint is not None:
                await checkpoint()
            await message.answer(
                body,
                reply_markup=(
                    build_quota_limit_markup(lang, is_vip=is_vip) if in_private else None
                ),
            )
            return

    # Everything written up to here — the caller's ``last_seen`` touch and
    # the quota slot just spent — is final by design, and what follows is
    # a wait on DeepSeek that runs into tens of seconds. Holding the
    # ``users.db`` write lock across it would stall every other update on
    # that DB until ``busy_timeout`` gave up ("database is locked"), so
    # the transaction ends here. It also makes the quota behave the way
    # ``AiQuotaService`` documents: a slot spent on a call that then blew
    # up stays spent, instead of being handed back by the middleware's
    # rollback and turning a failing upstream into an unlimited retry.
    if checkpoint is not None:
        await checkpoint()

    history = _MEMORY_STORE.history(uid, chat_key) if uid is not None else []
    had_history = bool(history)

    # L-65: response cache for short, history-less prompts only.
    if uid is not None and not had_history and len(prompt.strip()) < 200:
        cached = _RESPONSE_CACHE.get(uid, prompt)
        if cached is not None:
            bound.info("ai cache hit")
            # #298: truncate before converting, same as the live path.
            await message.answer(
                markdown_to_html(_truncate(cached)),
                reply_markup=_controls_for(message, uid, lang, is_vip=is_vip),
            )
            return

    # Persona preamble (modes, VIP-gated) + dynamic injected context.
    effective_mode = _MODE_STORE.resolve(uid, is_vip=is_vip) if uid is not None else "default"
    system_prompt = (
        _MODE_STORE.system_prompt(uid, is_vip=is_vip, lang=lang)
        if uid is not None
        else _MODE_STORE.system_prompt(0, is_vip=False, lang=lang)
    )
    # Language + anti-hallucination guard (legacy ``get_system_prompt``).
    #
    # #1345: legacy wrote this guard, the persona and the whole injected
    # context in Russian and relied on ``lang_instruction`` alone to bend
    # the answer into English. It mostly worked and failed in the two ways
    # a mixed-language prompt always fails — drift back to the briefing
    # language on a long answer, and wrong-language echoes of anything the
    # briefing tells the model to reuse. Every piece is now written in the
    # reader's language; ``lang_instruction`` stays as the explicit
    # statement of the contract rather than as the only thing carrying it.
    guard = (
        " ВАЖНО: не выдумывай факты, числа, погоду, новости и статусы сервисов. "
        "Если точных данных нет — прямо скажи, что не знаешь/не можешь проверить сейчас."
        if lang == "ru"
        else " IMPORTANT: do not invent facts, numbers, weather, news or service "
        "statuses. If you do not have exact data, say plainly that you do not "
        "know or cannot check it right now."
    )
    lang_instruction = (
        "\n\nЯзык ответа: русский. Отвечай только на русском."
        if lang == "ru"
        else "\n\nResponse language: English. Reply in English only."
    )
    system_prompt = system_prompt + guard + lang_instruction

    user_tz: str | None = None
    if user_settings_repo is not None and uid is not None:
        try:
            user_tz = await user_settings_repo.get_timezone(uid)
        except Exception as exc:  # noqa: BLE001 — context is best-effort
            bound.info("timezone lookup failed: {e!r}", e=exc)

    reply_extra = _reply_target_extra(message, lang)
    reply_focus = reply_extra is not None

    context_builder = AiContextBuilder(weather_service)
    dynamic_context = await context_builder.build(
        question=prompt,
        mode_hint=mode_hint(effective_mode, lang),
        is_group=is_group,
        group=_group_context(message),
        user_timezone=user_tz,
        user_city=None,
        reply_focus=reply_focus,
        lang=lang,
    )
    system_prompt += dynamic_context

    max_tokens = (
        min(_EXPERT_MAX_TOKENS, ai_config.max_tokens + 500) if effective_mode == "expert" else None
    )

    async with httpx.AsyncClient() as client:
        service = AiService(ai_config, client)
        try:
            answer = await service.ask_with_context(
                prompt,
                system_prompt=system_prompt,
                history=recent_for_prompt(history),
                extra_system=reply_extra,
                max_tokens=max_tokens,
            )
        except AiNotConfiguredError:
            # Reachable only if the key was cleared between the guard at
            # the top and here (#419); kept so that race still answers
            # rather than raising.
            bound.warning("ai not configured")
            await message.answer(t("h_ai_not_configured", lang))
            return
        except AiRequestError as exc:
            # #1597. 401 and 402 need the OWNER, not the reader: a
            # wrong key and an exhausted provider balance both survive
            # any number of retries. They are logged at ERROR and the
            # reader is pointed at the administrator WITHOUT being told
            # which of the two it was — naming the credential fault
            # handed the provider's account state to anyone who can
            # type /ask, which is what this half of the ticket was
            # about.
            #
            # The quota slot stays spent. It was committed before the
            # call on purpose (see the checkpoint note above): handing
            # it back on a failed upstream reopens exactly the hole
            # that commit closes — a broken provider would make the
            # daily limit unlimited, and every retry costs the owner
            # money. The user loses one request to an outage; the
            # owner would otherwise lose the ceiling entirely.
            if exc.reason == "http" and exc.status in (401, 402):
                bound.error("ai key or balance rejected: {s}", s=exc.status)
                key = "h_ai_error_unavailable"
            else:
                bound.warning("ai request failed: {r} {s}", r=exc.reason, s=exc.status)
                key = _AI_ERROR_KEYS.get(exc.reason, "h_ai_error_unknown")
            await message.answer(t(key, lang))
            return
        except asyncio.CancelledError:
            # #1965, and the same shape as ``services/tts_service.py``
            # (#1954): ``CancelledError`` is a ``BaseException``, so no
            # ``except Exception`` on the way out sees it — not the one
            # in this handler, not the session middleware's rollback,
            # not ``handlers/errors.py``, not the webhook route's. The
            # slot was committed by the checkpoint above on purpose, so
            # nothing unwinds it either.
            #
            # What closes the window is ordinary: a deploy restart
            # during the call. ``DEEPSEEK_TIMEOUT_SECONDS`` is 60 by
            # default and uvicorn's ``timeout_graceful_shutdown`` is 20
            # (``runner/webhook.py``), so an in-flight ``/ask`` is
            # cancelled rather than finished. No response is written,
            # Telegram redelivers per its at-least-once contract, and
            # the new process starts with an empty ``seen_updates``
            # (built inside ``create_app``) — so the retry is not a
            # duplicate and spends a SECOND slot. The user pays twice
            # for zero answers.
            #
            # This is NOT the failed-upstream case above, and must not
            # become it: there the slot staying spent is the whole
            # point.
            #
            # Best-effort, then re-raise. A task gets one
            # ``CancelledError`` from ``cancel()``, so these awaits do
            # run — but if the loop is already tearing down they may
            # not, and a failure here must not replace the cancellation
            # the caller is waiting on.
            if quota_service is not None and consumed_slot and uid is not None:
                bound.warning("ai cancelled mid-upstream — releasing the quota slot")
                with contextlib.suppress(Exception):
                    await quota_service.release(uid)
                    # The release lives in the same per-update session
                    # the middleware will never commit on this path.
                    if checkpoint is not None:
                        await checkpoint()
            raise

    # Record the exchange in the rolling window; cache history-less shorts.
    #
    # #1597: the condition used to carry ``and not
    # answer.startswith("❌")``. A failed call came back as an ordinary
    # string, and that marker was the only thing keeping error text out
    # of the conversation memory and the response cache. A failure now
    # raises above, so everything reaching this line is a real answer
    # and there is nothing left to sniff for.
    if uid is not None:
        _MEMORY_STORE.record_exchange(uid, prompt, answer, chat_key)
        if not had_history and len(prompt.strip()) < 200:
            _RESPONSE_CACHE.put(uid, prompt, answer)

    # The DeepSeek answer is arbitrary model text that frequently uses
    # Markdown (``**bold**``, `` `code` ``). Convert it to Telegram-safe
    # HTML: raw ``< > &`` are escaped FIRST (so model angle-brackets can't
    # break parse_mode=HTML), THEN ``**``/`` ` `` markers become
    # ``<b>``/``<code>`` — otherwise the markers leak literally ("выпало
    # **4**!"). ``markdown_to_html`` subsumes the previous bare escape.
    #
    # #298: the cut happens BEFORE the conversion, not after. Slicing
    # converted markup at a fixed offset lands inside a ``<b>`` tag or
    # inside an ``&amp;`` about as readily as it lands between them, and
    # Telegram answers a half-written tag with "can't parse entities".
    # ``middlewares/api_parse_mode_fallback`` then re-sends with parse
    # mode off, so a long answer arrives with its tags showing — the one
    # visible symptom of a bug that only fires on long replies. Cutting
    # the raw Markdown instead can at worst orphan a ``**`` or a
    # backtick, and ``markdown_to_html`` already leaves an unbalanced
    # marker as literal text by design.
    #
    # The 4096 ceiling still holds: Telegram measures it against the
    # text AFTER entity parsing, which is the pre-conversion length
    # again — so escaping making the wire string longer costs nothing.
    safe = markdown_to_html(_truncate(answer))
    await message.answer(safe, reply_markup=_controls_for(message, uid, lang, is_vip=is_vip))
    bound.info("ask answered: chars={c}", c=len(safe))


async def handle_ask(
    message: Message,
    command: CommandObject,
    lang: str,
    ai_config: AiConfig,
    ai_quota_repo: AiQuotaRepo,
    vip_repo: VipRepo,
    quota_settings: AiQuotaSettings,
    weather_service: WeatherService,
    user_settings_repo: UserSettingsRepo | None = None,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/ask <prompt>`` — DeepSeek (Stage 13), any chat.

    M-P-2: enforces the per-user daily quota (``ai_daily_requests``)
    BEFORE the upstream DeepSeek call. Quota is consumed even when
    DeepSeek later fails — the gate exists to protect operator cost
    from abuse, not to penalise upstream flakiness.

    ``lang`` is the effective bot language injected by the root
    ``LanguageMiddleware`` (the consumption API) and threaded into the
    shared completion path.
    """
    prompt = command_args(command)
    if not prompt:
        await message.answer(t("h_ask_usage", lang))
        return
    await _answer_with_ai(
        message,
        prompt,
        lang,
        ai_config,
        ai_quota_repo,
        vip_repo,
        quota_settings,
        weather_service,
        user_settings_repo,
        checkpoint,
    )


async def handle_ai_direct(
    message: Message,
    ai_question: str,
    lang: str,
    ai_config: AiConfig,
    ai_quota_repo: AiQuotaRepo,
    vip_repo: VipRepo,
    quota_settings: AiQuotaSettings,
    weather_service: WeatherService,
    user_settings_repo: UserSettingsRepo | None = None,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Plain-text ``ком ...`` / ``ии ...`` trigger (legacy
    ``ai_direct_handler``, ``bot.py:39639``).

    Bare ``ком`` / ``ии`` (``ai_question == ""``) shows the usage
    nudge; otherwise the question runs through the same quota-gated
    DeepSeek path as ``/ask``. The legacy private-chat membership gate
    (``ensure_kom_access_or_reply``) is intentionally NOT ported — it
    issued a Bot-API ``getChatMember`` call per message, and the new
    ``/ai`` / ``/ask`` commands already dropped that gate in favour of
    the per-user daily quota for cost control.
    """
    if not ai_question:
        await message.answer(t("h_ai_direct_usage", lang))
        return
    await _answer_with_ai(
        message,
        ai_question,
        lang,
        ai_config,
        ai_quota_repo,
        vip_repo,
        quota_settings,
        weather_service,
        user_settings_repo,
        checkpoint,
    )


async def handle_reset(message: Message, lang: str) -> None:
    """``/reset`` — clear the per-(user, chat) conversation window (L-63).

    Legacy ``cmd_ai_reset`` (``bot.py:38364``) cleared the in-memory
    history for the user in the current chat and also exited the
    «Войти в Ком» session. We mirror both: forget the rolling window and
    drop the Kom-mode flag so plain private messages stop routing to the
    model. ``lang`` comes from the root ``LanguageMiddleware``.
    """
    if message.from_user is None:
        return
    uid = message.from_user.id
    is_group = message.chat.type in ("group", "supergroup")
    chat_key = message.chat.id if is_group else uid
    _MEMORY_STORE.clear(uid, chat_key)
    _KOM_MODE_STORE.exit(uid, chat_key)
    await message.answer(t("h_ai_reset_done", lang))


async def handle_ai_mode(
    message: Message, command: CommandObject, lang: str, vip_repo: VipRepo
) -> None:
    """``/mode <name>`` — switch Kom persona (L-62), VIP-gated.

    No argument → list the available modes and the current one. A valid
    name from a VIP/dev user switches; a non-VIP user is told the feature
    is VIP-only (legacy pinned non-VIP to default and gated the switch on
    ``can_change_ai_role``). ``lang`` comes from the root
    ``LanguageMiddleware``.

    The argument is resolved through :func:`resolve_mode_token`, which
    accepts BOTH the canonical English keys (``expert``) and the localised
    display names shown in the ``/mode`` list (``эксперт``, ``креативный``),
    so "/mode эксперт" no longer answers "Unknown style".
    """
    if message.from_user is None:
        return
    uid = message.from_user.id
    arg = command_args(command).strip()

    current = _MODE_STORE.get(uid)
    if not arg:
        # Labels come from yaml, not from ``MODE_TITLES``: that map is a
        # byte-identical legacy port and therefore Russian-only, so an
        # English user's ``/mode`` list used to answer in Cyrillic.
        modes_list = ", ".join(mode_title(mode, lang) for mode in MODE_ORDER)
        await message.answer(
            t(
                "h_ai_mode_list",
                lang,
                current=mode_title(current, lang),
                modes=modes_list,
            )
        )
        return

    is_dev = False  # dev-ids fold into VIP at the quota layer; mode-gate
    # treats VIP profile as the switch right (legacy parity).
    is_vip = is_dev or (await vip_repo.get_active_profile(uid, now=datetime.now(UTC))) is not None
    if not is_vip:
        await message.answer(t("h_ai_mode_vip_only", lang))
        return

    canonical = resolve_mode_token(arg)
    if canonical is None:
        await message.answer(t("h_ai_mode_unknown", lang))
        return
    _MODE_STORE.set(uid, canonical)
    await message.answer(t("h_ai_mode_set", lang, mode=mode_title(canonical, lang)))


async def _quota_used_today(registry: EngineRegistry, user_id: int) -> int:
    """Read-only "used today" count for ``/ai_limits`` (L-10).

    :class:`AiQuotaRepo` deliberately exposes only the consuming
    ``get_and_increment`` primitive — reading the quota must NOT
    burn a slot, so the status card does its own SELECT against the
    same ``ai_daily_requests`` row (same direct-engine-read posture
    as ``handlers/commission.py:_fetch_earnings``). No row yet today
    means zero used.
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(AiDailyRequest.count).where(
                AiDailyRequest.user_id == user_id,
                AiDailyRequest.date_iso == _today_iso(),
            )
        )
        row = result.scalar_one_or_none()
    return int(row) if row is not None else 0


async def handle_ai_limits(
    message: Message,
    lang: str,
    vip_repo: VipRepo,
    registry: EngineRegistry,
    quota_settings: AiQuotaSettings,
) -> None:
    """``/ai_limits`` (L-10) — today's AI usage vs the caller's tier limit.

    Port of legacy ``cmd_ai_limits`` (``bot.py:38376-38396``,
    aliases ``command_aliases.py:647-649`` — EN token only, legacy
    registered no RU alias): VIP/dev renders the unlimited card
    (legacy short-circuited on ``_is_vip_for_ai`` / ``DEVELOPER_IDS``);
    everyone else sees used / remaining / limit for today. Tier
    resolution mirrors ``_answer_with_ai`` exactly (dev ids fold into
    VIP, a tier ceiling of 0 means unlimited) so the card never
    contradicts the gate that enforces it. Read-only — checking your
    quota must not consume it.
    """
    if message.from_user is None:
        return
    uid = message.from_user.id
    is_dev = uid in quota_settings.parsed_dev_user_ids()
    is_vip = is_dev or (await vip_repo.get_active_profile(uid, now=datetime.now(UTC))) is not None
    limit = quota_settings.vip_daily_limit if is_vip else quota_settings.free_daily_limit
    if is_dev or limit <= 0:
        # Unlimited tier (VIP default / dev bypass) — same "0 means
        # no ceiling" contract as AiQuotaService (backlog L-69).
        await message.reply(t("h_ai_limits_unlimited", lang))
        return
    used = await _quota_used_today(registry, uid)
    left = max(0, limit - used)
    tier = t("h_ai_limits_tier_vip" if is_vip else "h_ai_limits_tier_free", lang)
    await message.reply(t("h_ai_limits_card", lang, tier=tier, used=used, left=left, limit=limit))
    log.bind(uid=uid, used=used, limit=limit).info("/ai_limits rendered")


def build_router(
    registry: EngineRegistry,
    ai_config: AiConfig,
    quota_settings: AiQuotaSettings,
    weather_service: WeatherService | None = None,
    ai_rate_limit: AiRateLimitMiddleware | None = None,
) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    One provider (DeepSeek) wired across all four aliases — matches
    the legacy monolith, where ``['ai', 'ask', 'chat', 'kom_ai']``
    all dispatched into a single ``ai_assistant`` handler. The
    OpenAI-billed ``/ai`` / ``/gpt`` / ``/chat`` registration that
    lived here during the strangler iteration has been removed.

    * ``/ask <prompt>`` — DeepSeek, any chat. Group context is
      injected (title + replied-to text); only legacy's recent-message
      feed stays unported — see :func:`_group_context`.
    * ``/ai <prompt>`` / ``/ии`` / ``/gpt <prompt>`` /
      ``/chat <prompt>`` / ``/kom_ai <prompt>`` — DeepSeek, private +
      group. Same daily quota, same free-of-coin posture.
    * Bare ``/ai`` / ``/ии`` / ``/gpt`` / ``/chat`` / ``/kom_ai`` (any
      chat) → static help card.

    The bare-form help registration uses ``magic=F.args.is_(None)``
    across every alias and is included FIRST so the help card wins
    over the DeepSeek with-args registration on the same command
    names. aiogram walks registrations in include order. Legacy
    answered every bare alias in any chat; with the strangler bridge
    gone, the bare forms would otherwise silently no-op.

    EconomyMiddleware is still attached even though the DeepSeek
    path doesn't charge coins — it provides the per-update session
    other ai-adjacent flows expect, and the cost is one no-op
    session open per update, negligible against the upstream
    DeepSeek call.
    """

    # M-P-9 pattern: one shared ``WeatherService`` so its TTL cache warms
    # across AI requests (a fresh per-call instance would defeat the
    # cache). Construct a default here when the caller doesn't inject one.
    weather = weather_service if weather_service is not None else WeatherService()

    async def _handle_ask(
        message: Message,
        command: CommandObject,
        lang: str,
        ai_quota_repo: AiQuotaRepo,
        vip_repo: VipRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_ask(
            message,
            command,
            lang,
            ai_config,
            ai_quota_repo,
            vip_repo,
            quota_settings,
            weather,
            user_settings_repo,
            checkpoint,
        )

    # #1103: the bucket table is per-instance, so this router and the
    # /quote router each building their own meant one user got the full
    # allowance twice over against a single paid DeepSeek key. Same
    # ownership model as ``weather_rate_limit`` (#421): built once in
    # :mod:`routers.main_router`, injected into everything that can
    # reach the upstream. The ``None`` default keeps standalone tests
    # self-contained.
    limiter = ai_rate_limit if ai_rate_limit is not None else AiRateLimitMiddleware()

    router = Router(name="ai")
    # M-P-1: per-user rate limit attached BEFORE the economy session
    # so a rejected /ai or /ask doesn't pay the cost of opening a
    # wallet session. 10-burst, ~10/min sustained — matches the
    # audit's recommendation for /ai.
    router.message.middleware(limiter)
    router.message.middleware(EconomyMiddleware(registry))
    # M-P-2: SessionMiddleware exposes ``ai_quota_repo`` and ``vip_repo``
    # to /ask so the daily-quota gate can read users.db. Same per-update
    # transaction posture as everywhere else.
    router.message.middleware(SessionMiddleware(registry))

    # Bare ``/ai`` / ``/ии`` / ``/gpt`` / ``/chat`` / ``/kom_ai`` (no
    # args, ANY chat) → static help card. The legacy monolith answered
    # every bare AI alias in any chat (``cmd_ai`` had no chat gate); with
    # the strangler bridge gone a bare alias would otherwise silently
    # no-op. Registered FIRST so the help card wins over the with-args
    # registration on the same command names. aiogram walks registrations
    # in include order.
    router.message.register(
        handle_ai_help,
        Command("ai", "ии", "gpt", "chat", "kom_ai", ignore_case=True, magic=F.args.is_(None)),
        F.from_user,
    )

    # ``/ai_limits`` (L-10) — read-only quota status card. Legacy
    # registered the EN token only (``command_aliases.py:647-649``)
    # and answered in any chat. ``vip_repo`` comes from
    # EconomyMiddleware (same injection ``_answer_with_ai`` relies on).
    #
    # ``ии_лимиты`` follows ``/ии`` above (#163): the AI commands are
    # the ones a Russian-speaking user reaches for by name, and this
    # was the last one in the family with no Russian spelling.
    async def _handle_ai_limits(message: Message, lang: str, vip_repo: VipRepo) -> None:
        await handle_ai_limits(message, lang, vip_repo, registry, quota_settings)

    router.message.register(
        _handle_ai_limits,
        Command("ai_limits", "limits", "ии_лимиты", ignore_case=True),
        F.from_user,
    )

    # ``/reset`` (L-63) — clear the rolling conversation window and exit
    # the «Войти в Ком» session. Any chat, since history is per-(user, chat).
    # ``сброс`` is the token ``/city`` and ``/time`` already accept for
    # the same "forget what you stored" idea (#163).
    router.message.register(
        handle_reset,
        Command("reset", "сброс", ignore_case=True),
        F.from_user,
    )

    # ``/mode`` (L-62) — switch Kom persona, VIP-gated. Bare ``/mode``
    # lists the modes; ``/mode expert`` switches.
    async def _handle_mode(
        message: Message, command: CommandObject, lang: str, vip_repo: VipRepo
    ) -> None:
        await handle_ai_mode(message, command, lang, vip_repo)

    router.message.register(
        _handle_mode,
        Command("mode", "режим", "kom_mode", ignore_case=True),
        F.from_user,
    )

    # ``/kom`` / ``/enter_kom`` (L-76) — enter «Войти в Ком» in a PRIVATE
    # chat: every subsequent plain message routes to the model until
    # ``/exit_kom`` or ``/reset``. Private-only because routing every group
    # message to the model would be hostile in a shared chat.
    async def _handle_enter_kom(message: Message, lang: str) -> None:
        if message.from_user is None:
            return
        if message.chat.type != "private":
            await message.answer(t("h_ai_kom_private_only", lang))
            return
        _KOM_MODE_STORE.enter(message.from_user.id, message.from_user.id)
        await message.answer(t("h_ai_kom_entered", lang))

    async def _handle_exit_kom(message: Message, lang: str) -> None:
        if message.from_user is None:
            return
        _KOM_MODE_STORE.exit(message.from_user.id, message.from_user.id)
        await message.answer(t("h_ai_kom_exited", lang))

    router.message.register(
        _handle_enter_kom,
        Command("kom", "enter_kom", "войти_в_ком", ignore_case=True),
        F.from_user,
    )
    router.message.register(
        _handle_exit_kom,
        Command("exit_kom", "выйти_из_ком", ignore_case=True),
        F.from_user,
    )

    # ``/ask`` — DeepSeek, ANY chat. Legacy registered ``ask`` with no
    # chat gate, so group ``/ask <prompt>`` replied too. ``handle_ask``
    # itself renders the empty-prompt hint, so bare ``/ask`` is a reply,
    # not a dead-end. (Group context is injected here as everywhere
    # else; only legacy's recent-message feed is unported — there is no
    # history store to feed it. See ``_group_context``.)
    router.message.register(
        _handle_ask,
        Command("ask", ignore_case=True),
        F.from_user,
    )

    # ``/ai <prompt>`` / ``/ии <prompt>`` / ``/gpt <prompt>`` /
    # ``/chat <prompt>`` / ``/kom_ai <prompt>`` — DeepSeek, private +
    # group. ``magic=F.args`` ensures bare-form calls hit the help card
    # above instead of this upstream-call branch. Legacy mapped all
    # aliases onto a single DeepSeek assistant; we mirror that by reusing
    # ``_handle_ask``.
    router.message.register(
        _handle_ask,
        Command("ai", "ии", "gpt", "chat", "kom_ai", ignore_case=True, magic=F.args),
        F.from_user,
    )

    # Plain-text ``ком ...`` / ``ии ...`` trigger (legacy
    # ``ai_direct_handler``, ``bot.py:39639``). With the strangler bridge
    # gone, a user typing ``ии привет`` or ``ком, что нового?`` (no slash)
    # got no answer at all. The filter extracts the question and injects
    # it as ``ai_question``; ``StateFilter(None)`` keeps the trigger from
    # stealing plain-text FSM steps (support message, withdraw amount),
    # which only run while a state is set. Registered LAST so the explicit
    # command handlers above always win.
    async def _direct_filter(message: Message) -> dict[str, str] | bool:
        # ``text or caption``: sending a picture and addressing the bot in
        # its caption — «ком, что тут не так?» — is the same trigger the
        # user typed, just attached to a photo. Reading only ``text`` left
        # that message unanswered with no hint why, which reads as the bot
        # ignoring you. ``command_body`` is deliberately not used here: it
        # is the narrowing for ``Command`` argument parsing, and this is a
        # plain-text trigger, not a command.
        question = extract_ai_direct_question(
            message.text or message.caption,
            is_group=message.chat.type in GROUP_TYPE_NAMES,
        )
        if question is None:
            return False
        return {"ai_question": question}

    async def _handle_direct(
        message: Message,
        ai_question: str,
        lang: str,
        ai_quota_repo: AiQuotaRepo,
        vip_repo: VipRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_ai_direct(
            message,
            ai_question,
            lang,
            ai_config,
            ai_quota_repo,
            vip_repo,
            quota_settings,
            weather,
            user_settings_repo,
            checkpoint,
        )

    router.message.register(
        _handle_direct,
        StateFilter(None),
        F.from_user,
        _direct_filter,
    )

    # L-76: «Войти в Ком» catch-all. When the user is in an active Kom
    # session (private chat), every plain text message that ISN'T a command
    # and ISN'T already handled above routes to the model. ``StateFilter(None)``
    # keeps it from stealing FSM steps; ``~F.text.startswith("/")`` excludes
    # commands; the session-flag filter keeps it inert for everyone else.
    # Registered LAST so explicit commands and the ии/ком trigger always win.
    async def _kom_session_filter(message: Message) -> bool:
        if message.from_user is None or message.chat.type != "private":
            return False
        if not message.text or message.text.startswith("/"):
            return False
        # Don't double-fire on ии/ком-prefixed text (the direct handler
        # above already claimed it).
        if extract_ai_direct_question(message.text) is not None:
            return False
        return _KOM_MODE_STORE.is_active(message.from_user.id, message.from_user.id)

    async def _handle_kom_session(
        message: Message,
        lang: str,
        ai_quota_repo: AiQuotaRepo,
        vip_repo: VipRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await _answer_with_ai(
            message,
            (message.text or "").strip(),
            lang,
            ai_config,
            ai_quota_repo,
            vip_repo,
            quota_settings,
            weather,
            user_settings_repo,
            checkpoint,
        )

    router.message.register(
        _handle_kom_session,
        StateFilter(None),
        F.from_user,
        _kom_session_filter,
    )

    # --- RR-6 #64: reply-card control keyboard -------------------------
    #
    # Every control is keyed off ``callback.from_user`` — the persona, the
    # session flag and the transcript all belong to the tapping user, so
    # the payload carries no owner id to forge. The PRIVATE filter mirrors
    # where the keyboard is attached (see ``_controls_for``).
    #
    # Divergence from legacy, deliberate: legacy replaced the message text
    # with a status card on every tap (bot.py:38338, 39205), destroying
    # the answer the user had just asked for. Here a tap answers with a
    # toast and refreshes only the keyboard, so the conversation survives
    # its own controls.
    #
    # No :class:`AiRateLimitMiddleware` here, and that is deliberate:
    # not one of the five callbacks below reaches DeepSeek. They set
    # local state (``_MODE_STORE``), clear a context row or re-render a
    # keyboard, so the scarce resource the AI bucket protects — the
    # owner's paid upstream — is not on this path. What a held-down
    # button does spend is Telegram edits, and those are already
    # covered: :class:`ThrottlingMiddleware` is an outer middleware on
    # ``dispatcher.callback_query`` sharing one bucket per user with
    # ``dispatcher.message`` — one ``ThrottlingMiddleware`` instance
    # mounted on both observers in ``AppProvider.dispatcher``
    # (``di/providers.py``). Mount the AI
    # gate here the moment any callback grows an upstream call.
    router.callback_query.middleware(EconomyMiddleware(registry))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def _refresh_controls(
        callback: CallbackQuery, lang: str, uid: int, *, is_vip: bool
    ) -> None:
        """Re-render the keyboard in place; swallow the benign rejects.

        Three of Telegram's ``BadRequest`` answers are just facts about an
        old card, not failures worth an error log: an identical markup is
        ``message is not modified`` (re-tapping the active persona is the
        obvious way to hit it), a card past the 48h window is ``message
        can't be edited``, and a card the user deleted is ``message to
        edit not found``. In all three the state change already landed and
        the toast already told the user — only an unexpected reject should
        surface.
        """
        message = callback.message
        if not isinstance(message, Message):
            return
        markup = build_controls_markup(
            lang=lang,
            session_active=_KOM_MODE_STORE.is_active(uid, uid),
            can_change_mode=is_vip,
            current_mode=_MODE_STORE.get(uid),
        )
        try:
            await message.edit_reply_markup(reply_markup=markup)
        except TelegramBadRequest as exc:
            if not any(marker in str(exc) for marker in BENIGN_EDIT_REJECTS):
                raise

    async def _is_vip_now(uid: int, vip_repo: VipRepo) -> bool:
        """VIP status at TAP time — a lapsed subscription must not switch.

        Mirrors ``_answer_with_ai``: developer ids fold into VIP, so the
        gate the button honours is the gate the answer path enforces.
        """
        if uid in quota_settings.parsed_dev_user_ids():
            return True
        return (await vip_repo.get_active_profile(uid, now=datetime.now(UTC))) is not None

    async def _handle_kom_enter(callback: CallbackQuery, lang: str, vip_repo: VipRepo) -> None:
        uid = callback.from_user.id
        _KOM_MODE_STORE.enter(uid, uid)
        await callback.answer(t("h_ai_toast_entered", lang))
        await _refresh_controls(callback, lang, uid, is_vip=await _is_vip_now(uid, vip_repo))

    async def _handle_kom_exit(callback: CallbackQuery, lang: str, vip_repo: VipRepo) -> None:
        uid = callback.from_user.id
        _KOM_MODE_STORE.exit(uid, uid)
        await callback.answer(t("h_ai_toast_exited", lang))
        await _refresh_controls(callback, lang, uid, is_vip=await _is_vip_now(uid, vip_repo))

    async def _handle_context_clear(callback: CallbackQuery, lang: str, vip_repo: VipRepo) -> None:
        uid = callback.from_user.id
        # Private chat → the history key folds chat onto user, the same
        # key ``/reset`` and the answer path use. The button clears the
        # window but keeps the session: legacy's ``/reset`` did both, and
        # a user who wanted out would have pressed ⏸ instead.
        _MEMORY_STORE.clear(uid, uid)
        await callback.answer(t("h_ai_toast_cleared", lang))
        await _refresh_controls(callback, lang, uid, is_vip=await _is_vip_now(uid, vip_repo))

    async def _handle_export(callback: CallbackQuery, lang: str) -> None:
        uid = callback.from_user.id
        history = _MEMORY_STORE.history(uid, uid)
        if not history:
            await callback.answer(t("h_ai_toast_export_empty", lang), show_alert=False)
            return
        transcript = render_history_text(
            history,
            header=t("h_ai_export_header", lang),
            you_label=t("h_ai_export_you", lang),
            bot_label=t("h_ai_export_bot", lang),
        )
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        message = callback.message
        if isinstance(message, Message):
            await message.answer_document(
                BufferedInputFile(
                    transcript.encode("utf-8"), filename=f"kom_chat_{uid}_{stamp}.txt"
                ),
                caption=t("h_ai_export_caption", lang),
            )
        await callback.answer(t("h_ai_toast_exported", lang))
        log.bind(uid=uid, turns=len(history)).info("kom transcript exported")

    async def _handle_mode_pick(
        callback: CallbackQuery,
        callback_data: AiModePick,
        lang: str,
        vip_repo: VipRepo,
    ) -> None:
        uid = callback.from_user.id
        is_vip = await _is_vip_now(uid, vip_repo)
        if not is_vip:
            # Re-gated at tap time, not just at render time: a keyboard
            # attached while the user was VIP outlives the subscription.
            await callback.answer(t("h_ai_toast_mode_vip_only", lang), show_alert=True)
            return
        mode = callback_data.mode
        if not is_known_mode(mode):
            await callback.answer(t("h_ai_toast_mode_unknown", lang), show_alert=True)
            return
        _MODE_STORE.set(uid, mode)
        await callback.answer(t("h_ai_toast_mode_set", lang, mode=mode_title(mode, lang)))
        await _refresh_controls(callback, lang, uid, is_vip=is_vip)

    router.callback_query.register(_handle_kom_enter, AiKomEnter.filter())
    router.callback_query.register(_handle_kom_exit, AiKomExit.filter())
    router.callback_query.register(_handle_context_clear, AiContextClear.filter())
    router.callback_query.register(_handle_export, AiExport.filter())
    router.callback_query.register(_handle_mode_pick, AiModePick.filter())
    return router
