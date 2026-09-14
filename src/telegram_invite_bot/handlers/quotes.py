"""``/quote`` — AI-generated wise quote with a local pool fallback (RR-6 #71).

Legacy ``cmd_quote`` (bot.py:17319) is **AI-generated**: it hands
``_run_ai_request`` a one-line prompt and ships whatever the model
returns. The port replaced that with a seven-entry static list, which is
the regression this module closes — a "wisdom" command that repeats
itself every few calls is a worse product than no command at all.

What is restored, and what is deliberately different:

* **AI first, pool second.** The model is asked for one short quote; any
  failure at all (not configured, timeout, HTTP error, empty answer,
  implausible answer) degrades to the static pool. ``/quote`` therefore
  *always* answers — the legacy behaviour of replying "❌ Ошибка ИИ" to a
  content command is not worth reproducing.
* **The daily AI quota still applies**, exactly as legacy's
  ``_run_ai_request`` consumed it (``ai_request_tracker.check_and_add`` +
  the non-VIP daily counter). Quota exhaustion is silent here: the user
  gets a pool quote rather than a refusal card, because a quote command
  that answers is strictly better than one that scolds. The ceiling still
  does its real job — it caps how much of the owner's DeepSeek budget a
  single user can spend per day.
* **A tiny dedicated system prompt**, not the bot-wide
  :data:`~telegram_invite_bot.services.ai_service.SYSTEM_PROMPT` (a
  ~2k-token command catalogue). A quote needs none of it, and every
  request pays for that prompt in the owner's tokens. ``max_tokens`` is
  capped at 120 for the same reason.
* **Group-capable.** The previous private-only gate cited legacy's
  ``require_group_feature(message, "ai", ...)``. That call cannot deny:
  ``is_feature_enabled_for_chat`` (bot.py:7992) returns ``True`` in
  ``full`` mode and ``feature == "ai"`` in ``restricted`` mode — which is
  ``True`` here — and no other mode is ever written. The group half of
  ``/quote`` was withheld to protect a no-op. The real per-command group
  off-switch in this pipeline is ``/cmdcfg`` (``CommandAccessMiddleware``,
  min-rank 6 = disabled). Same finding as :mod:`handlers.jokes`.
* **Anti-repeat pool.** When the pool is used it goes through
  :class:`RecentPicker`, keyed per chat, so two consecutive fallbacks
  don't hand out the same quote (``random.choice`` over seven entries
  collides 14% of the time).

Cosmetics: the reply is a 💭-led italic line. Legacy shipped the quote as
naked text, which in a busy group is indistinguishable from someone
talking — a quotation is the one kind of content that genuinely wants to
be set apart. No header line above it, though: a "📜 Цитата дня" banner
would fight the quote's own attribution and double the height of a
one-line reply.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.core.recent_picker import RecentPicker
from telegram_invite_bot.middlewares.ai_rate_limit import AiRateLimitMiddleware
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.ai_context import markdown_to_html
from telegram_invite_bot.services.ai_quota_service import (
    AiQuotaConfig,
    AiQuotaService,
    QuotaOutcome,
)
from telegram_invite_bot.services.ai_service import AiService
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.language import pick_by_language

log = logger.bind(component="handlers.quotes")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import AiConfig, AiQuotaSettings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.ai_quota_repo import AiQuotaRepo
    from telegram_invite_bot.repositories.vip_repo import VipRepo
    from telegram_invite_bot.services.user_service import UserService


# RU pool: verbatim copy of the dead ``QUOTES_RU`` list at bot.py:17266.
# Keep in sync if legacy edits.
_QUOTES_RU: tuple[str, ...] = (
    "Учиться никогда не поздно. (Народная мудрость)",
    "Дело не в том, чтобы делать много, а в том, чтобы делать нужное. (Сенека)",
    "Путь в тысячу ли начинается с одного шага. (Лао-цзы)",
    "Знание — сила. (Фрэнсис Бэкон)",
    "Мы — то, что мы делаем повторно. Совершенство — не действие, а привычка. (Аристотель)",  # noqa: E501
    "Не откладывай на завтра то, что можно сделать сегодня. (Бенджамин Франклин)",
    "Единственный способ сделать великую работу — любить то, что делаешь. (Стив Джобс)",
)

# EN pool: the English originals of the same authors as the RU pool, in
# the same author-in-parentheses format. ZERO Cyrillic (enforced by
# tests/unit/i18n/test_ru_en_convergence.py spirit + the e2e test below).
_QUOTES_EN: tuple[str, ...] = (
    "It is never too late to learn. (Folk wisdom)",
    "It is not that we have a short time to live, but that we waste a lot of it. (Seneca)",  # noqa: E501
    "A journey of a thousand miles begins with a single step. (Lao Tzu)",
    "Knowledge is power. (Francis Bacon)",
    "We are what we repeatedly do. Excellence, then, is not an act, but a habit. (Aristotle)",  # noqa: E501
    "Don't put off until tomorrow what you can do today. (Benjamin Franklin)",
    "The only way to do great work is to love what you do. (Steve Jobs)",
)

_PICKER = RecentPicker()

#: Legacy's prompt, byte-identical (bot.py:17330). The EN variant is new
#: — legacy had no English path at all, and asking a Russian prompt on
#: behalf of an English-speaking user is how Cyrillic leaks into EN
#: output.
_PROMPT_RU = (
    "Ты бот в чате. Ответь одним коротким сообщением: дай одну короткую "
    "мудрую цитату (можно с именем автора). Без вступлений."
)
_PROMPT_EN = (
    "You are a chat bot. Reply with one short message: give one short wise "
    "quote (an author name is fine). No preamble."
)

#: Deliberately minimal — see the module docstring on token cost.
_SYSTEM_RU = "Ты лаконичный бот. Отвечай ровно одной строкой, без пояснений."
_SYSTEM_EN = "You are a terse bot. Reply with exactly one line, no explanations."

#: A "short wise quote" that runs longer than this is the model ignoring
#: the instruction (an essay, a numbered list, a refusal). Falling back to
#: the pool beats truncating mid-sentence, which reads as a bug.
_MAX_QUOTE_CHARS = 500

#: Tokens are the owner's money and a one-line quote needs very few.
_MAX_TOKENS = 120


def _pick(lang: str, key: object) -> str:
    """Pool pick keyed off language. ``en`` → EN pool; everything else →
    RU — same contract as the jokes module's ``_pick``.
    """
    pool = pick_by_language(lang, ru=_QUOTES_RU, en=_QUOTES_EN)
    return _PICKER.pick((key, lang), pool)


def _plausible(text: str) -> bool:
    """Is this actually a one-line quote and not the model rambling?"""
    stripped = text.strip()
    return bool(stripped) and len(stripped) <= _MAX_QUOTE_CHARS


async def _consume_quota(
    uid: int,
    *,
    ai_quota_repo: AiQuotaRepo,
    vip_repo: VipRepo,
    quota_settings: AiQuotaSettings,
) -> bool:
    """Spend one daily AI slot. ``False`` → caller must use the pool.

    Consumed BEFORE the upstream call, matching :mod:`handlers.ai`: a
    failing upstream must not become a free retry loop around the ceiling.
    """
    dev_ids = quota_settings.parsed_dev_user_ids()
    # #1539: the ``uid in dev_ids`` half is a SHORT-CIRCUIT, not a gate.
    # ``AiQuotaService`` receives the same ``dev_user_ids`` below and
    # decides developers on its own, so this term changes no outcome —
    # it only spares a developer the wallet round-trip on the right of
    # the ``or``. Read it as an optimisation; deleting it would add a
    # query, not close a hole.
    is_vip = (
        uid in dev_ids
        or (await vip_repo.get_active_profile(uid, now=datetime.now(UTC))) is not None
    )
    service = AiQuotaService(
        ai_quota_repo,
        config=AiQuotaConfig(
            free_daily_limit=quota_settings.free_daily_limit,
            vip_daily_limit=quota_settings.vip_daily_limit,
            dev_user_ids=dev_ids,
        ),
    )
    decision = await service.check_and_consume(uid, is_vip=is_vip)
    return decision.outcome is not QuotaOutcome.EXCEEDED


async def _generate(lang: str, ai_config: AiConfig) -> str | None:
    """One AI-generated quote, or ``None`` on any failure at all."""
    prompt = _PROMPT_EN if lang == "en" else _PROMPT_RU
    system_prompt = _SYSTEM_EN if lang == "en" else _SYSTEM_RU
    async with httpx.AsyncClient() as client:
        answer = await AiService(ai_config, client).complete_or_none(
            prompt, system_prompt=system_prompt, max_tokens=_MAX_TOKENS
        )
    if answer is None or not _plausible(answer):
        return None
    return answer.strip()


async def handle_quote(
    message: Message,
    user_service: UserService,
    ai_config: AiConfig,
    ai_quota_repo: AiQuotaRepo,
    vip_repo: VipRepo,
    quota_settings: AiQuotaSettings,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Touch the user (keeps ``last_seen`` fresh, mirrors legacy's
    ``update_user_info`` at handler top) then render a quote.
    """
    user = await user_service.touch(require_from_user(message))
    generated: str | None = None
    # No key configured → don't spend a quota slot discovering that.
    if ai_config.api_key is not None and await _consume_quota(
        user.user_id,
        ai_quota_repo=ai_quota_repo,
        vip_repo=vip_repo,
        quota_settings=quota_settings,
    ):
        # The touch and the quota slot are both final now; commit them
        # before the model call so ``users.db`` isn't locked for its
        # duration (see :class:`db.session.Checkpoint`).
        if checkpoint is not None:
            await checkpoint()
        generated = await _generate(user.language, ai_config)

    if generated is not None:
        # Model text is arbitrary: escape ``< > &`` first, then promote
        # ``**``/`` ` `` markers, or the markers leak literally under the
        # bot-wide HTML parse mode.
        body = markdown_to_html(generated)
    else:
        body = html.escape(_pick(user.language, message.chat.id))
    await message.answer(f"💭 <i>{body}</i>")
    log.bind(
        uid=user.user_id,
        lang=user.language,
        ai=generated is not None,
    ).info("/quote rendered")


def build_router(
    registry: EngineRegistry,
    ai_config: AiConfig,
    quota_settings: AiQuotaSettings,
    ai_rate_limit: AiRateLimitMiddleware | None = None,
) -> Router:
    """Factory — ``user_service`` / ``ai_quota_repo`` come from the
    dispatcher-level :class:`SessionMiddleware`; ``vip_repo`` needs
    :class:`EconomyMiddleware`, attached here.

    ``ai_rate_limit`` is the one limiter shared with ``/ai`` and
    ``/ask`` (#1103). Production passes the instance built in
    :mod:`routers.main_router`; the ``None`` default keeps standalone
    tests self-contained.
    """

    async def _handle_quote(
        message: Message,
        user_service: UserService,
        ai_quota_repo: AiQuotaRepo,
        vip_repo: VipRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_quote(
            message,
            user_service,
            ai_config,
            ai_quota_repo,
            vip_repo,
            quota_settings,
            checkpoint,
        )

    limiter = ai_rate_limit if ai_rate_limit is not None else AiRateLimitMiddleware()

    router = Router(name="quotes")
    # The daily quota is a per-DAY ceiling and it does not cover
    # everyone: developer ids bypass the counter entirely
    # (:meth:`AiQuotaService.check_and_consume`), so on that path the
    # only thing between a held-down key and the owner's DeepSeek bill
    # was nothing at all. A per-minute bucket is the missing half, and
    # it is literally the same gate as ``/ai`` and ``/ask``: one
    # instance, one bucket table, shared from main_router (#1103).
    # Before that it was the same *numbers* on a separate table, which
    # let one user spend the allowance twice against one paid key.
    # Registered FIRST so a rejected request never pays for a wallet
    # session it is not going to use.
    router.message.middleware(limiter)
    # ``vip_repo`` only — the session repos already arrive from the
    # dispatcher-level outer middleware, so a second SessionMiddleware
    # here would open a nested session for nothing.
    router.message.middleware(EconomyMiddleware(registry))
    # Any chat: the legacy "ai" group-feature gate this router used to
    # cite can never deny (see the module docstring). ``/cmdcfg`` is the
    # real per-group off-switch.
    router.message.register(
        _handle_quote,
        Command(
            "quote",
            "цитата",
            "kom_quote",
            ignore_case=True,
        ),
        F.from_user,
    )
    return router
