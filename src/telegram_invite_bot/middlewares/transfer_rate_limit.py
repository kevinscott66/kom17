"""Per-user transfer rate-limit middleware — Stage 19 of the strangler migration.

Stage 17's ``/send`` port shipped without legacy's per-user anti-flood
gate (``_check_transfer_rate_limit``, ``bot.py``) on the explicit
understanding that the limit would land "uniformly across /send,
/gift, future /tip" as cross-cutting middleware. This module is that
middleware: a token-bucket gate that short-circuits transfer commands
BEFORE the economy session opens, so a spam-bot can't DoS the wallet
table by sending three updates per millisecond and forcing the
service through its full 8-step flow each time.

The bucket arithmetic lives in
:class:`telegram_invite_bot.middlewares._bucket_rate_limit.BucketRateLimitMiddleware`
— this class just pins the transfer-domain defaults and the legacy
"⏳ Слишком часто" copy.

How this relates to legacy's numbers
------------------------------------
Legacy's ``_check_transfer_rate_limit`` (bot.py:18830-18838) was NOT
a token bucket. ``TransferRateTracker`` (bot.py:3953-3976) kept a
sliding window log: every call dropped timestamps older than 60s and
rejected once ``len(calls) >= max_per_minute``. The ceiling came from
the operator setting ``transfer_rate_limit_per_minute``, default 6
(bot.py:2552, read at bot.py:3164).

The defaults below reproduce both of legacy's user-visible numbers —
a burst of 6 and a sustained 6/minute — on the bucket shape the rest
of this package already uses. What is deliberately NOT identical is
the recovery curve: legacy's window let all 6 slots return at once,
60s after the first of them, while the bucket returns one slot every
10s. The bucket is the gentler of the two and is what the sibling
gates do, so the shape stays.

What is NOT ported: the operator knob itself. There is no
``transfer_rate_limit_per_minute`` field on ``Settings`` yet, so the
numbers are the constructor's defaults. The constructor takes them
as parameters precisely so adding that field later is a wiring
change in ``handlers/send.py`` and nothing else.

Why router-scoped not dispatcher-scoped
---------------------------------------
``/gift`` and the future ``/tip`` will get their own routers and
each will tune its own ``(capacity, refill_per_second)`` — a casino
chip tip is order-of-magnitude smaller than a /send transfer and
deserves a looser bucket. A dispatcher-scoped middleware would force
one global config across all three commands. The constructor takes
its parameters so each router can build its own instance.

Reply copy
----------
On reject we reply with a friendly RU/EN "wait N seconds" rather
than dropping silently like the global throttling middleware
(:class:`telegram_invite_bot.middlewares.throttling.ThrottlingMiddleware`):

* the global throttle exists to protect the BOT's outbound API
  budget — silence is correct, the user is flooding;
* THIS gate exists to protect the WALLET TABLE from a single user's
  bursts — a friendly cool-down message is the legacy contract and
  removing it would surprise users who learnt the legacy UX.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from telegram_invite_bot.middlewares._bucket_rate_limit import BucketRateLimitMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

# Legacy posture: 6 transfers per minute (bot.py:2552), enforced as a
# burst of 6 with one slot back every 10s. Keeping the numbers
# explicit (not magic-numbered) so a future operator-tunable Settings
# field can swap them without re-reading the docstring.
_TRANSFERS_PER_MINUTE = 6
_SECONDS_PER_MINUTE = 60.0
_DEFAULT_CAPACITY = _TRANSFERS_PER_MINUTE
_DEFAULT_REFILL_PER_SECOND = _TRANSFERS_PER_MINUTE / _SECONDS_PER_MINUTE


class TransferRateLimitMiddleware(BucketRateLimitMiddleware):
    """Token-bucket gate for transfer commands.

    Attach via ``router.message.middleware(...)`` BEFORE
    :class:`EconomyMiddleware` / :class:`SessionMiddleware` so a
    rejected request doesn't pay the cost of opening an
    ``economy.db`` session. Aiogram applies router middlewares in
    registration order, so position is load-bearing — see
    :func:`telegram_invite_bot.handlers.send.build_router`.

    Anonymous updates (no ``from_user``) bypass the gate, and
    double-charging them here against a synthetic key like ``0``
    would punish the next genuine user to share that bucket. What
    makes the bypass safe for THIS command is the ``F.from_user``
    filter on the registration itself
    (:func:`telegram_invite_bot.handlers.send.build_router`): an
    anonymous update never reaches the handler at all.

    It is NOT made safe by the global throttle — that middleware
    lets anonymous updates through with the same early return
    (``throttling.py:229-231``). Any command that omits
    ``F.from_user`` is therefore ungated, which is exactly how
    ``/weather`` reached an external API unlimited (#489).
    """

    _REJECT_RU: ClassVar[str] = "⏳ Слишком часто. Подожди {seconds} сек."
    _REJECT_EN: ClassVar[str] = "⏳ Too fast. Wait {seconds}s."
    _LOG_MESSAGE: ClassVar[str] = "transfer rate-limit reject"
    _ZERO_REFILL_FALLBACK_SECONDS: ClassVar[int] = 60

    def __init__(
        self,
        capacity: int = _DEFAULT_CAPACITY,
        refill_per_second: float = _DEFAULT_REFILL_PER_SECOND,
        is_exempt: Callable[[int], bool] | None = None,
    ) -> None:
        super().__init__(
            capacity=float(capacity),
            refill_per_second=refill_per_second,
            is_exempt=is_exempt,
        )
