"""Per-user rate limiting middleware for gaming commands — Stage 31.

Provides token-bucket rate limiting for commands like ``/cpc`` that
might be spammed or cause disruption in group chats. The bucket
arithmetic lives in
:class:`telegram_invite_bot.middlewares._bucket_rate_limit.BucketRateLimitMiddleware`
— this class just pins gaming-domain defaults (5-burst, 1/30s
refill) and the "🎮 Слишком часто играешь" copy.

Why a separate class from :class:`TransferRateLimitMiddleware`
--------------------------------------------------------------
The arithmetic is identical, but each domain has its own UX
contract:

* the *copy* differs ("⏳ Слишком часто" for transfers vs.
  "🎮 Слишком часто играешь" for games — the emoji signals the
  domain to the user before they read the body);
* the *log tag* differs so an SRE filtering for transfer abuse
  doesn't get drowned in /cpc spam events;
* the *defaults* differ — games are bursty by nature (a /cpc
  challenge is "play a round", not "send money once an hour").

Keeping two thin subclasses makes each domain self-describing
while collapsing the implementation into a single arithmetic core.

Usage::

    router.message.middleware(
        RateLimitMiddleware(capacity=5, refill_per_second=1.0 / 30.0)
    )
"""

from __future__ import annotations

from typing import ClassVar

from telegram_invite_bot.middlewares._bucket_rate_limit import BucketRateLimitMiddleware

# Defaults for gaming commands: more permissive than transfers but
# still protective against spam in group chats.
_DEFAULT_CAPACITY = 5
_DEFAULT_REFILL_PER_SECOND = 1.0 / 30.0


class RateLimitMiddleware(BucketRateLimitMiddleware):
    """Token-bucket rate limiter for gaming commands.

    Tuned for ``/cpc`` and future game commands: a 5-token burst
    refilling at 1 token / 30 seconds gives a fluent player room to
    chain matches while still cutting off a script that fires once a
    second. Anonymous updates bypass the gate (the handler itself
    rejects ``sender_chat``).
    """

    _REJECT_RU: ClassVar[str] = "🎮 Слишком часто играешь. Подожди {seconds} сек."
    _REJECT_EN: ClassVar[str] = "🎮 Too many games. Wait {seconds}s."
    _LOG_MESSAGE: ClassVar[str] = "gaming rate-limit reject"
    _ZERO_REFILL_FALLBACK_SECONDS: ClassVar[int] = 30

    def __init__(
        self,
        capacity: int = _DEFAULT_CAPACITY,
        refill_per_second: float = _DEFAULT_REFILL_PER_SECOND,
    ) -> None:
        super().__init__(capacity=float(capacity), refill_per_second=refill_per_second)
