"""Per-user AI rate-limit middleware (M-P-1).

Audit ``audits/01_profile_stats_ai_vip.md`` flagged ``/ai``, ``/ask``,
and ``/voice`` as having zero per-user rate limit. At the time the
wallet balance was the only ceiling; ``/ai`` no longer charges coins at
all, so without this middleware the daily quota would be the single
gate and a user could still burn it at line-speed in one burst. The
global
:class:`ThrottlingMiddleware` covers raw update bursts, not per-command
quotas.

This module pins two thin subclasses of
:class:`BucketRateLimitMiddleware`:

* :class:`AiRateLimitMiddleware` — for ``/ai`` and ``/ask``. Both run
  through DeepSeek on the same free, quota-gated posture (the briefly
  OpenAI-billed ``/ai`` was re-aligned with legacy in
  :mod:`handlers.ai`), so the wording "billed vs unmetered" no longer
  distinguishes them. Default 10 tokens / 60 s refill (one call per
  ~6 s sustained, with a burst of 10).
* :class:`VoiceRateLimitMiddleware` — for ``/voice`` (OpenAI TTS).
  Default 5 tokens / 60 s refill (TTS is heavier than chat tokens
  per call and the audio upload back to Telegram dominates user
  experience under bursts).

Why two subclasses instead of one tuned per router: each domain has
its own UX copy (emoji prefix signals to the user what they hit),
each has its own log tag for SRE filtering, and the defaults differ
materially. Following the pattern set by
:class:`RateLimitMiddleware` (gaming) and
:class:`TransferRateLimitMiddleware` (transfers).
"""

from __future__ import annotations

from typing import Any, ClassVar

from aiogram.filters.command import CommandObject
from aiogram.types import TelegramObject

from telegram_invite_bot.middlewares._bucket_rate_limit import BucketRateLimitMiddleware

_AI_DEFAULT_CAPACITY = 10
_AI_DEFAULT_REFILL_PER_SECOND = 10.0 / 60.0

_VOICE_DEFAULT_CAPACITY = 5
_VOICE_DEFAULT_REFILL_PER_SECOND = 5.0 / 60.0

# M-P-9: /weather is cheap per-call (cached upstream JSON) but the
# upstream Open-Meteo API still has fair-use limits, and a single
# user looping /weather Москва would otherwise dominate the
# shared client's connection pool. 5/min sustained / burst-5 is
# generous (a user genuinely curious about weather hits maybe
# 1-2 cities per session) and matches the legacy bot's posture.
_WEATHER_DEFAULT_CAPACITY = 5
_WEATHER_DEFAULT_REFILL_PER_SECOND = 5.0 / 60.0


class AiRateLimitMiddleware(BucketRateLimitMiddleware):
    """Token-bucket gate for ``/ai`` and ``/ask``.

    Attach BEFORE :class:`EconomyMiddleware` / :class:`SessionMiddleware`
    so a rejected request doesn't pay the cost of opening a wallet
    session. Anonymous updates bypass the gate — the handler itself
    requires ``from_user`` via the router filter.
    """

    _REJECT_RU: ClassVar[str] = "🤖 Слишком часто. Подожди {seconds} сек."
    _REJECT_EN: ClassVar[str] = "🤖 Too fast. Wait {seconds}s."
    _LOG_MESSAGE: ClassVar[str] = "ai rate-limit reject"
    _ZERO_REFILL_FALLBACK_SECONDS: ClassVar[int] = 60

    def __init__(
        self,
        capacity: int = _AI_DEFAULT_CAPACITY,
        refill_per_second: float = _AI_DEFAULT_REFILL_PER_SECOND,
    ) -> None:
        super().__init__(capacity=float(capacity), refill_per_second=refill_per_second)


class VoiceRateLimitMiddleware(BucketRateLimitMiddleware):
    """Token-bucket gate for ``/voice`` (TTS).

    Tighter default than :class:`AiRateLimitMiddleware` because each
    TTS call produces a Telegram voice upload (heavier than a text
    reply) and the upstream cost-per-call is higher than chat
    completions for an equivalent perceived workload.
    """

    _REJECT_RU: ClassVar[str] = "🎙 Слишком часто. Подожди {seconds} сек."
    _REJECT_EN: ClassVar[str] = "🎙 Too fast. Wait {seconds}s."
    _LOG_MESSAGE: ClassVar[str] = "voice rate-limit reject"
    _ZERO_REFILL_FALLBACK_SECONDS: ClassVar[int] = 60

    def __init__(
        self,
        capacity: int = _VOICE_DEFAULT_CAPACITY,
        refill_per_second: float = _VOICE_DEFAULT_REFILL_PER_SECOND,
    ) -> None:
        super().__init__(capacity=float(capacity), refill_per_second=refill_per_second)

    def _is_free(self, event: TelegramObject, data: dict[str, Any]) -> bool:
        """Bare ``/voice`` (no text) is free — #1537.

        The no-argument form answers a static usage hint from
        :func:`handlers.vip_emoji_voice.handle_voice_usage`: no OpenAI
        call, no audio upload, no wallet write. The 5-per-60s budget
        exists solely for the PAID synthesis, so charging the hint let
        five typos — or one user reading the help twice — lock the real
        command out for the rest of the window.

        Moving the hint to a child router does NOT work as an
        alternative: aiogram 3.28's
        ``TelegramEventObserver._resolve_middlewares`` walks
        ``router.chain_head``, i.e. the router's ANCESTORS, so a child
        inherits every inner middleware of its parents.

        ``data["command"]`` is the :class:`CommandObject` the matched
        ``Command`` filter produced. Anything else reaching this gate
        (a future non-command registration) pays as before.
        """
        command = data.get("command")
        if not isinstance(command, CommandObject):
            return False
        return command.args is None


# RR-6 #72: /joke reaches free third-party humour APIs (JokeAPI,
# icanhazdadjoke, Lingva) that have no contract with us at all. The
# command always answers — an exhausted bucket is the only thing that
# stops a user from looping it — so the ceiling protects the upstreams'
# fair use rather than our own budget. 10/min matches /ask, which is the
# closest neighbour in "one command, one outbound call".
_CONTENT_DEFAULT_CAPACITY = 10
_CONTENT_DEFAULT_REFILL_PER_SECOND = 10.0 / 60.0


class ContentRateLimitMiddleware(BucketRateLimitMiddleware):
    """Token-bucket gate for the network-backed content leaves.

    Attached to ``/joke`` only: ``/joke18`` is a pure local pool pick
    and gating it would cost a user their punchline for nothing.
    ``/quote`` is not here either, but it is not ungated: it carries
    :class:`AiRateLimitMiddleware`, because the upstream it reaches is
    the owner's own paid DeepSeek account rather than a stranger's free
    API. The AI daily quota it also rides is a per-day ceiling and it
    exempts developer ids outright, so it was never a substitute for a
    per-minute one.
    """

    _REJECT_RU: ClassVar[str] = "😄 Слишком часто. Подожди {seconds} сек."
    _REJECT_EN: ClassVar[str] = "😄 Too fast. Wait {seconds}s."
    _LOG_MESSAGE: ClassVar[str] = "content rate-limit reject"
    _ZERO_REFILL_FALLBACK_SECONDS: ClassVar[int] = 60

    def __init__(
        self,
        capacity: int = _CONTENT_DEFAULT_CAPACITY,
        refill_per_second: float = _CONTENT_DEFAULT_REFILL_PER_SECOND,
    ) -> None:
        super().__init__(capacity=float(capacity), refill_per_second=refill_per_second)


class WeatherRateLimitMiddleware(BucketRateLimitMiddleware):
    """M-P-9: token-bucket gate for every command that can geocode.

    Sits BEFORE the handler so a flood doesn't even reach the
    upstream-cached lookup. The TTL cache (see
    :class:`WeatherService`) covers warm hits at the data layer;
    this middleware caps how often a single user can trigger any
    lookup at all (warm or cold).

    ``/weather``, ``/forecast``, ``/city`` and ``/time`` all reach the
    same geocoding host, so production wires ONE instance across all of
    them (#421) — the bucket table is per-instance, and an instance per
    router would have granted each surface its own full allowance.
    """

    _REJECT_RU: ClassVar[str] = "🌤 Слишком часто. Подожди {seconds} сек."
    _REJECT_EN: ClassVar[str] = "🌤 Too fast. Wait {seconds}s."
    _LOG_MESSAGE: ClassVar[str] = "weather rate-limit reject"
    _ZERO_REFILL_FALLBACK_SECONDS: ClassVar[int] = 60

    def __init__(
        self,
        capacity: int = _WEATHER_DEFAULT_CAPACITY,
        refill_per_second: float = _WEATHER_DEFAULT_REFILL_PER_SECOND,
    ) -> None:
        super().__init__(capacity=float(capacity), refill_per_second=refill_per_second)
