"""Repository for ``users.ai_daily_requests`` — daily AI request counter.

Tracks per-user, per-calendar-day call counts for ``/ai`` and
``/ask``. ``/voice`` used to count here too and no longer does — it
moved to :class:`VoiceQuotaService` over the ledger, whose day is naive
UTC, so the two features now roll at two different midnights and must
not be described as sharing a counter (#1952). The table is NOT a
strangler-era invention: legacy
bot.py creates it (bot.py:5636), reads it in ``_get_ai_daily_count``
(bot.py:38476) and writes it in ``_increment_ai_daily_count``
(bot.py:38485), all keyed by ``date.today().isoformat()``. What the
new pipeline was missing (``audits/01_profile_stats_ai_vip.md`` SEV-2,
tracked to closure as M-P-2 in ``audits/03_after_iter2.md``) was any
*reader* of it — the wallet balance was the only ceiling — so a user
with funds could spam the upstream provider at line-speed.

The repo exposes two methods. :meth:`get_and_increment` is the atomic
"consume one quota slot for today and return the post-increment count"
primitive; callers (the :class:`AiQuotaService`) compare the result
against the configured ceiling and either let the upstream call proceed
or refuse with a budget-exceeded outcome. :meth:`release` is its narrow
inverse — see #1965 there for the one case that is allowed to use it.

Atomicity
---------
The increment uses SQLite's ``INSERT … ON CONFLICT (PK) DO UPDATE
SET count = count + 1 RETURNING count``. The whole decision happens
inside one statement under SQLite's writer lock, so two concurrent
``/ai`` calls from the same user cannot both read "count == 4" and
both decide they fit under a limit of 5 — the second one sees the
post-first value via the row lock.

Why count BEFORE the upstream call (and not after success): the
quota exists to protect the OpenAI key budget from a single user's
abuse, not to reward them for upstream failures. A retry-storm of
failed calls would burn the OpenAI quota repeatedly if we only
counted successes. The wallet-debit logic continues to bill only on
success — that's the right place for cost-accounting; this counter
is the abuse-limit gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.ai_quota import AiDailyRequest

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


def _today_iso(now: datetime | None = None) -> str:
    """Return today's LOCAL calendar day as ``YYYY-MM-DD``.

    ``ai_daily_requests`` is a legacy-shared table and legacy keys it
    with ``date.today().isoformat()`` — the *host's* local calendar
    day, i.e. MSK on the production host (bot.py:38478 read,
    bot.py:38487 write). The day boundary follows legacy for two
    reasons: keying the same rows on the UTC day would split one MSK
    calendar day across two rows on a DB legacy also wrote, and it
    would move the reset to 03:00 MSK — contradicting the refusal copy
    that tells a Russian-facing user to come back "завтра"
    (bot.py:38506).

    ``now`` may be naive (already local wall-clock, used as-is) or
    aware (converted to the host zone first). This mirrors the
    naive-local convention the other legacy-shared columns use — see
    ``bonds_repo._decayed_experience`` and
    ``InventoryRepo.list_for_user``.
    """
    if now is None:
        now = datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
    if now.tzinfo is not None:
        now = now.astimezone()
    return now.date().isoformat()


class AiQuotaRepo:
    """Read-and-increment access to ``users.ai_daily_requests``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_and_increment(self, user_id: int, *, now: datetime | None = None) -> int:
        """Increment today's counter atomically; return the new value.

        Uses an UPSERT with ``RETURNING count``: on a fresh row the
        insert path lands count=1; on an existing row the DO UPDATE
        bumps it. Either branch returns the post-increment value, so
        the caller can apply ``new_count > ceiling`` uniformly.

        The function is intentionally NOT idempotent within a single
        request: each call consumes one quota slot. Callers must
        invoke it exactly once per ``/ai`` / ``/ask`` / ``/voice``
        update before the upstream call. Counting BEFORE upstream is
        deliberate — see the module docstring.
        """
        date_iso = _today_iso(now)
        insert_stmt = sqlite_insert(AiDailyRequest).values(
            user_id=user_id, date_iso=date_iso, count=1
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["user_id", "date_iso"],
            set_={"count": AiDailyRequest.count + 1},
        ).returning(AiDailyRequest.count)
        result = await self._session.execute(stmt)
        new_count = result.scalar_one()
        return int(new_count)

    async def release(self, user_id: int, *, now: datetime | None = None) -> bool:
        """Hand one consumed slot back. Returns whether a row moved.

        #1965: the deliberate posture above — a slot spent on a call
        that then failed stays spent — is about *failure*. This is for
        the one case that is not a failure: the request was
        ``CancelledError``-ed out from under the handler, so nobody
        ever got an answer and there is no retry loop to protect
        against, because the process is going away. Telegram will
        redeliver the update to the next process, whose in-memory
        dedup ledger is empty, and the user would pay twice for zero
        answers.

        Do NOT reach for this on an upstream error: that is precisely
        the hole the count-before-upstream design closes.

        Guarded on ``count > 0`` so a double release, or one for a day
        whose row was never written, cannot drive the counter negative
        — the same compare-and-set shape the rest of the repo layer
        uses, and the reason the return value is a row count rather
        than the new value.
        """
        stmt = (
            update(AiDailyRequest)
            .where(
                AiDailyRequest.user_id == user_id,
                AiDailyRequest.date_iso == _today_iso(now),
                AiDailyRequest.count > 0,
            )
            .values(count=AiDailyRequest.count - 1)
        )
        result = await self._session.execute(stmt)
        return cast("CursorResult[Any]", result).rowcount > 0
