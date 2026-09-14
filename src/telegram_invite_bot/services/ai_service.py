"""DeepSeek chat-completion client for ``/ask`` — Stage 13.

Async port of legacy's ``AIAssistant`` (``bot.py:36943``). Key
differences from legacy:

(#1657: this line used to open by naming ``bot/services/ai_service.py``
as the thing being ported — the very path the note forty lines down
already records as non-existent. Two more citations in this file said
so while the first line kept asserting it.)

* **Native async** end-to-end. Legacy opened a whole event loop per
  question inside a sync telebot handler — ``bot.py:37293``, the sole
  ``new_event_loop()`` in the monolith, followed by
  ``loop.run_until_complete`` — the canonical antipattern the
  strangler migration was meant to fix. (#1631: the citation here
  used to name a ``bot/handlers/ai.py`` that does not exist in this
  repo, forty lines above a note recording that the same mistake had
  already been caught once.)
* **Stateless at this layer** — the service keeps no history of its
  own. Legacy stored conversation context on the assistant instance
  (``AIAssistant.histories``, ``bot.py:36949``) and lost it on every
  process restart. Multi-turn did land later, above this layer:
  :mod:`~telegram_invite_bot.services.ai_memory` owns the per-user
  history and the handler passes it in through
  :meth:`ask_with_context`'s ``history`` argument.
* **httpx instead of aiohttp**. Both are async, but the rest of the
  new stack already uses httpx (FastAPI tests). One client library
  → smaller dependency footprint.
* **Typed config in**, not ``os.getenv`` at instantiation. Tests
  can build a service with any ``AiConfig`` instance; production
  reads from ``.env`` via pydantic-settings.

The system prompt is reproduced byte-identically from legacy so
answers from the new path don't visibly differ from the old.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

import httpx
from loguru import logger

from telegram_invite_bot.utils.http_read import send_capped

log = logger.bind(component="services.ai")

if TYPE_CHECKING:
    from collections.abc import Sequence

    from telegram_invite_bot.config.settings import AiConfig


# Verbatim from legacy's DeepSeek assistant prompt — every line is
# byte-identical, including emoji and trailing whitespace, so users
# can't tell the new path from the old. (The old citation named a
# ``bot/services/ai_service.py`` that does not exist in this repo.)
#
# DEAD IN PRODUCTION: the only reader is :meth:`AiService.ask`, and the
# only caller of ``ask`` is its unit test — every live path goes
# through :meth:`ask_with_context`, which takes its system prompt from
# :mod:`core.ai_modes` plus the dynamic context block. Kept because the
# test pins the legacy text, which is still the fallback answer to
# "what does this bot do".
SYSTEM_PROMPT = """Ты — умный ИИ-помощник в Telegram боте. Твоя задача — помогать пользователям.

Ты знаешь ВСЕ команды бота:

ПОГОДА И ВРЕМЯ:
- /weather [город] — погода (например: /weather Москва)
- /forecast [город] — прогноз на неделю
- /time [город] — время в городе (например: /time Магадан)
- /timezone — установить часовой пояс
- /city — указать город для погоды/времени по умолчанию

ПРОФИЛЬ И ЭКОНОМИКА:
- /profile — профиль пользователя
- /balance — баланс монет
- /shop — магазин товаров
- /buy [товар] — покупка
- /inventory — инвентарь
- /daily — ежедневный бонус
- /referral — реферальная ссылка
- /gift [@user] [сумма] — подарить монеты
- /rate — курсы валют
- /crypto — курсы криптовалют
- /convert [сумма] [из] [в] — конвертер
- /currency — выбор валюты отображения
- /withdraw — вывод средств
- /p2p — P2P-маркет
- /commission — комиссии и рефералы

ИГРЫ:
- /games — список игр
- /dice — бросить кубик
- /duel [@user] — дуэль
- /casino [сумма] — казино
- /rating — рейтинг игроков

ОТНОШЕНИЯ И БРАК:
- /relations — меню отношений
- /marry [@user] — предложение брака
- /accept, /decline — принять/отклонить предложение
- /divorce — развод
- /family — информация о семье
- РП-команды: обнять, поцеловать, погладить и т.д. (по уровням отношений)

ГРУППЫ И МОДЕРАЦИЯ:
- /groups — мои группы
- /admin — админ-панель (в ЛС)
- /ban, /unban, /mute, /unmute, /warn, /unwarn — модерация
- /rules — правила группы
- /setrules [текст] — установить правила
- /filter — настройка фильтра слов

ПОДДЕРЖКА:
- /feedback [текст] — обратная связь
- /faq — частые вопросы
- /donate — донаты
- /help, /commands — помощь и список команд

VIP И ЭМОДЗИ:
- /vip — информация о VIP
- /vip_shop — VIP магазин
- /emojis — кастомные эмодзи (VIP)

Если пользователь спрашивает про конкретную команду — объясни её кратко и дружелюбно.
Если спрашивает «как сделать X» — предложи соответствующую команду.
Если спрашивает погоду/время — подскажи использовать /weather или /time.
Если просто общается — поддерживай беседу.

Отвечай на языке пользователя (русский или английский). Будь дружелюбным, используй эмодзи где уместно. Кратко — Telegram ограничивает длину сообщений."""  # noqa: E501


class AiResponseCache:
    """Optional MD5 cache of short, history-less prompts — Cluster J L-65.

    Legacy cached answers to short (<200 char) prompts asked with an
    empty conversation history, keyed by ``md5(prompt.lower())`` and
    scoped per-user, with a 1-hour TTL (``bot.py:37102-37245``). Caching
    only history-less prompts is the key correctness property: once a
    conversation has context, the same surface text can warrant a
    different answer, so it must never be served from cache.

    In-process, bounded (LRU), and thread-unsafe-by-design is fine here:
    the worst case of a race is a duplicate upstream call or a slightly
    stale answer, never a wrong-user leak (the key includes ``user_id``).
    A process restart clears it — identical to legacy.
    """

    def __init__(self, *, ttl_seconds: float = 3600.0, max_entries: int = 1024) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()

    @staticmethod
    def _key(user_id: int, prompt: str) -> str:
        digest = hashlib.md5(prompt.strip().lower().encode()).hexdigest()  # noqa: S324
        return f"{user_id}:{digest}"

    def get(self, user_id: int, prompt: str) -> str | None:
        key = self._key(user_id, prompt)
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts > self._ttl:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return value

    def put(self, user_id: int, prompt: str, answer: str) -> None:
        key = self._key(user_id, prompt)
        self._store.pop(key, None)
        self._store[key] = (time.monotonic(), answer)
        while len(self._store) > self._max:
            self._store.popitem(last=False)


class AiNotConfiguredError(RuntimeError):
    """Raised when ``AiConfig.api_key`` is empty.

    Legacy returns an apologetic string in this case; the new path
    raises so the handler can produce the same string from a single
    error-format function without ``ai_service.ask`` having to know
    about user-facing copy.
    """


class AiRequestError(RuntimeError):
    """A completion that did not produce usable text.

    Exists so a caller can tell "upstream failed" from "upstream said
    this" WITHOUT sniffing the reply for a leading ``❌``. #1597 took
    that to its conclusion: :meth:`AiService.ask` and
    :meth:`AiService.ask_with_context` now let this escape instead of
    mapping it to a hardcoded Russian sentence, so the one caller that
    knows the reader's language (:mod:`handlers.ai`, which already has
    ``lang`` and ``t()`` in scope) picks the copy. A service layer has
    no business owning user-facing wording, and an exception is a
    stronger typed result than a marker a caller can forget to test —
    a degraded reply can no longer reach the caller's ``answer``
    variable at all. :meth:`AiService.complete_or_none` still swallows
    it as ``None`` so a handler with a local fallback (``/quote``'s
    static pool) can use the fallback instead of printing an error.

    ``reason`` is one of ``timeout`` / ``network`` / ``http`` /
    ``bad_response`` / ``empty_choices`` / ``empty_content``; ``status``
    carries the HTTP code when ``reason == "http"``. ``bad_response``
    means the upstream answered 200 with a body we could not parse into
    a completion — see :meth:`AiService._complete`.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(f"{reason}{'' if status is None else f' {status}'}")
        self.reason = reason
        self.status = status


class AiService:
    """Single-shot DeepSeek chat-completion caller.

    Takes an :class:`httpx.AsyncClient` so a caller *could* reuse a
    connection pool. Nothing does today: the live path in
    :mod:`handlers.ai` builds the service inside
    ``async with httpx.AsyncClient()`` per request, so each answer pays
    its own TLS handshake and the client closes with the block. There
    is no dishka provider for this service (#423).
    """

    def __init__(self, config: AiConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        extra_payload: dict[str, object] | None = None,
        tag: str,
    ) -> str:
        """POST one chat completion and return its text, or raise.

        The single place where the HTTP shape of the DeepSeek call
        lives. Every failure mode leaves as an :class:`AiRequestError`
        so a caller can branch on ``reason`` / ``status`` instead of
        reading prose — before this existed, the two wrappers each
        carried a full copy of the request/parse/branch chain and had
        already drifted apart (``ask`` never grew the 401/402 split
        ``ask_with_context`` had). Since #1597 neither wrapper catches
        it: the exception travels up to :mod:`handlers.ai`, the only
        layer that knows which language to fail in.

        Raises :class:`AiNotConfiguredError` when no API key is set.
        """
        if self._config.api_key is None:
            raise AiNotConfiguredError("DEEPSEEK_API_KEY is not set")

        payload: dict[str, object] = {
            "model": self._config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self._config.temperature,
        }
        if extra_payload:
            payload.update(extra_payload)
        headers = {
            "Authorization": f"Bearer {self._config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }

        try:
            response = await send_capped(
                self._client,
                "POST",
                self._config.api_url,
                json=payload,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            log.warning("deepseek timeout ({t})", t=tag)
            raise AiRequestError("timeout") from exc
        except httpx.HTTPError as exc:
            log.warning("deepseek network error ({t}): {e!r}", t=tag, e=exc)
            raise AiRequestError("network") from exc

        if response.status_code != 200:
            log.bind(status=response.status_code).warning(
                "deepseek http error ({t}): {b}", t=tag, b=response.text[:200]
            )
            raise AiRequestError("http", status=response.status_code)

        # #1532: from here down we are parsing a body we do not
        # control, so every step is guarded. An OpenAI-compatible
        # endpoint answers a filtered or tool-call-only completion with
        # ``"content": null``, and a proxy in front of one can answer
        # 200 with a non-JSON body or a top-level list. The previous
        # version called ``.strip()`` on whatever came back, so those
        # shapes escaped as ``AttributeError`` / ``JSONDecodeError`` —
        # and NO caller catches either: :meth:`complete_or_none` only
        # catches :class:`AiRequestError`, so it broke its own
        # documented "returns ``None`` on failure" contract and ``/quote``
        # showed the error card after already committing the user's
        # daily AI slot. Legacy guarded the same case at
        # ``bot.py:37236-37238``; the sibling
        # :mod:`services.whisper_stt_service` still does (``:142-147``).
        try:
            data = response.json()
        except ValueError as exc:  # json.JSONDecodeError subclasses it
            log.warning("deepseek unparseable body ({t}): {b}", t=tag, b=response.text[:200])
            raise AiRequestError("bad_response") from exc
        if not isinstance(data, dict):
            log.warning("deepseek non-object body ({t}): {ty}", t=tag, ty=type(data).__name__)
            raise AiRequestError("bad_response")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AiRequestError("empty_choices")
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        raw = message.get("content") if isinstance(message, dict) else None
        # ``content: null`` and a non-string content both land here as
        # "no text", which is exactly what ``empty_content`` means — the
        # caller's degraded copy is already right for it.
        content = raw.strip() if isinstance(raw, str) else ""
        if not content:
            raise AiRequestError("empty_content")
        return content

    async def ask(self, prompt: str) -> str:
        """Send a one-shot prompt, return DeepSeek's text response.

        Raises :class:`AiNotConfiguredError` when no API key is set
        and :class:`AiRequestError` for every other failure; the
        caller turns both into copy in the reader's language.

        This used to swallow ``AiRequestError`` and return one of
        four hardcoded Russian sentences (#1597). They were printed
        verbatim to English readers, and the difference between "the
        model said this" and "the call failed" was carried by a
        leading ``❌`` that a caller had to remember to test for.

        DEAD IN PRODUCTION: the only caller is this module's unit
        test — see the note above :data:`SYSTEM_PROMPT`.
        """
        return await self._complete(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=self._config.max_tokens,
            tag="ask",
        )

    async def complete_or_none(
        self,
        prompt: str,
        *,
        system_prompt: str,
        max_tokens: int | None = None,
    ) -> str | None:
        """One completion, or ``None`` if it did not produce text.

        For callers that own a local fallback and would rather use it
        than show the user an error — ``/quote`` degrades to its static
        pool, so an outage reads as an ordinary quote instead of a red
        cross. A missing API key is a ``None`` here too: an operator who
        never configured DeepSeek should get the offline pool, not a
        configuration complaint on every ``/quote``.
        """
        try:
            return await self._complete(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens or self._config.max_tokens,
                tag="complete_or_none",
            )
        except (AiRequestError, AiNotConfiguredError):
            return None

    async def ask_with_context(
        self,
        prompt: str,
        *,
        system_prompt: str,
        history: Sequence[dict[str, str]] | None = None,
        extra_system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Single-shot completion with an injected system prompt + history.

        Restores the legacy injected-context experience (Cluster J): the
        caller builds the persona preamble (modes), the dynamic
        ``[...]`` context blocks (time/weather/dice/group), and any
        reply-target instruction, and passes them here. This stays a
        SINGLE completion call — it is prompt construction, not a
        tool-use loop.

        * ``system_prompt`` — the full system content (persona preamble
          plus dynamic context already concatenated by the caller).
        * ``history`` — prior ``{role, content}`` turns (already trimmed
          to the rolling window) inserted between system and the new
          user turn.
        * ``extra_system`` — appended to the system content as the LAST
          block (legacy appended ``extra_system`` after dynamic context,
          e.g. the reply-target instruction).
        * ``max_tokens`` — per-mode override (expert mode raises the cap);
          falls back to the configured default.

        Error handling mirrors :meth:`ask`: :class:`AiNotConfiguredError`
        for a missing key, :class:`AiRequestError` for everything else.
        Neither is caught here — :mod:`handlers.ai` owns the wording
        (#1597), including the decision NOT to tell the end user that
        the failure was a bad key or an empty provider balance.
        """
        full_system = system_prompt
        if extra_system:
            full_system += extra_system

        messages: list[dict[str, str]] = [{"role": "system", "content": full_system}]
        for turn in history or []:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": prompt})

        return await self._complete(
            messages,
            max_tokens=max_tokens or self._config.max_tokens,
            # Legacy's context path nudges the model away from
            # repeating itself across a multi-turn thread; the
            # history-less ``ask`` path has nothing to repeat.
            extra_payload={"frequency_penalty": 0.3, "presence_penalty": 0.3},
            tag="context",
        )
