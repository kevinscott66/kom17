"""Daily AI quota enforcement for ``/ai``, ``/ask``, ``/voice``.

The audit (``audits/01_profile_stats_ai_vip.md`` M-P-2) flagged that
the new pipeline never read or wrote ``ai_daily_requests`` — wallet
balance was the only ceiling, so a user with funds could spam the
upstream provider at line-speed.

This service is the abuse-limit gate. It is intentionally separate
from wallet billing (``EconomyService.debit``): the counter exists
to cap operator-side cost, not user-side cost. A failed upstream
call still counts against the quota (we already burned the latency
and possibly the metered request); only the wallet debit is
contingent on success.

Tier ceiling
------------
The audit specified three tiers via :class:`AiQuotaConfig`:

* ``free`` — non-VIP users; default 10/day.
* ``vip`` — any active VIP grant; default 200/day. Generous, but
  FINITE: VIP buys a bigger allowance, not an open tab on the
  operator's provider key (see :class:`~telegram_invite_bot.config.settings.AiQuotaSettings`
  for why the legacy "VIP = unlimited" parity was dropped).
* ``dev`` — unlimited (bypasses the gate); used by ops-side dev
  identities. Selection is by user_id membership in
  :attr:`AiQuotaConfig.dev_user_ids`.

A ceiling of ``0`` for any tier means *unlimited* — ALLOWED with no
counter write, same posture as DEV. It stays available as an explicit
operator opt-out; it is no longer any tier's default.

Returning ``QuotaResult`` rather than raising lets the caller decide
how to render the refusal (i18n key, log line) — same posture as
the rest of the service layer (``TtsResult``, ``OpenAiResult``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

log = logger.bind(component="services.ai_quota")

if TYPE_CHECKING:
    from datetime import datetime

    from telegram_invite_bot.repositories.ai_quota_repo import AiQuotaRepo


class QuotaTier(StrEnum):
    FREE = "free"
    VIP = "vip"
    DEV = "dev"


class QuotaOutcome(StrEnum):
    ALLOWED = "allowed"
    EXCEEDED = "exceeded"


@dataclass(frozen=True, slots=True)
class QuotaResult:
    outcome: QuotaOutcome
    tier: QuotaTier
    count: int = 0
    """Post-increment count for both outcomes. On EXCEEDED the
    counter HAS been bumped (see :meth:`AiQuotaService.check_and_consume`
    inline comment "abuse ledger, not just a cost counter") — a
    rejected attempt still consumes a slot so a hostile loop can't
    re-read the same below-limit value on the next call. The field
    surfaces the value the user / operator sees in logs and replies."""
    limit: int = 0
    """The ceiling that applied for the user's tier. 0 for DEV
    (unlimited)."""


@dataclass(frozen=True, slots=True)
class AiQuotaConfig:
    """Per-tier daily ceilings. Audit-specified defaults.

    ``dev_user_ids`` is a frozenset for O(1) membership; default is
    empty so production behaves as documented (no implicit dev
    bypass) and ops set the value via ``Settings.ai_quota`` when the
    config field lands. The service accepts None for the set so
    callers that have not migrated config can construct the service
    without knowing about dev IDs.
    """

    free_daily_limit: int = 10
    vip_daily_limit: int = 200
    """Per-tier daily ceiling. ``0`` means *unlimited* (no cap, no
    rejection, no DB write).

    The dataclass default tracks
    :class:`~telegram_invite_bot.config.settings.AiQuotaSettings` on
    purpose: a caller that constructs the service without config must
    land on the safe ceiling, not on the unbounded one."""
    dev_user_ids: frozenset[int] = field(default_factory=frozenset)


class AiQuotaService:
    """Compose tier resolution + counter increment into one decision."""

    def __init__(
        self,
        repo: AiQuotaRepo,
        *,
        config: AiQuotaConfig | None = None,
    ) -> None:
        self._repo = repo
        self._config = config or AiQuotaConfig()

    def _resolve_tier(self, user_id: int, *, is_vip: bool) -> QuotaTier:
        if user_id in self._config.dev_user_ids:
            return QuotaTier.DEV
        if is_vip:
            return QuotaTier.VIP
        return QuotaTier.FREE

    async def check_and_consume(
        self,
        user_id: int,
        *,
        is_vip: bool,
        now: datetime | None = None,
    ) -> QuotaResult:
        """Atomically consume one quota slot if available.

        Returns ALLOWED with the new count on success, EXCEEDED with
        the current (un-incremented) count on rejection. DEV always
        ALLOWED (no DB write — dev IDs bypass the counter entirely
        so an operator's smoke-test doesn't inflate user-visible
        ledger rows).
        """
        tier = self._resolve_tier(user_id, is_vip=is_vip)
        bound = log.bind(uid=user_id, tier=tier.value, is_vip=is_vip)

        if tier is QuotaTier.DEV:
            bound.info("ai quota: dev bypass")
            return QuotaResult(outcome=QuotaOutcome.ALLOWED, tier=tier)

        limit = (
            self._config.vip_daily_limit if tier is QuotaTier.VIP else self._config.free_daily_limit
        )

        # A ceiling of 0 means unlimited (legacy VIP bypassed the AI
        # daily gate entirely). Treat it like DEV: ALLOWED with no DB
        # write, so an unlimited tier never inflates the counter or
        # gets rejected. limit stays 0 in the result (== unlimited).
        if limit <= 0:
            bound.info("ai quota: unlimited tier (limit=0)")
            return QuotaResult(outcome=QuotaOutcome.ALLOWED, tier=tier)

        # Increment first, then compare. SQLite holds the writer
        # lock for the duration of the UPSERT, so two concurrent
        # calls cannot both see "4 < 5" and both land at 5; the
        # second one reads 6 and is rejected.
        new_count = await self._repo.get_and_increment(user_id, now=now)
        if new_count > limit:
            bound.bind(count=new_count, limit=limit).info("ai quota exceeded")
            # NB: we did increment — a quota-busting attempt still
            # consumes the slot (so a 6th call doesn't read 5 again
            # next time and slip through). The counter is the abuse
            # ledger, not just a cost counter.
            return QuotaResult(
                outcome=QuotaOutcome.EXCEEDED,
                tier=tier,
                count=new_count,
                limit=limit,
            )

        bound.bind(count=new_count, limit=limit).info("ai quota consumed")
        return QuotaResult(
            outcome=QuotaOutcome.ALLOWED,
            tier=tier,
            count=new_count,
            limit=limit,
        )

    async def release(self, user_id: int, *, now: datetime | None = None) -> bool:
        """Hand back a slot :meth:`check_and_consume` took. #1965.

        Narrow on purpose: the only caller is the ``CancelledError``
        path in ``handlers/ai.py``, where the update is being torn down
        mid-upstream and Telegram will redeliver it to a fresh process.
        A slot spent on a call that *failed* stays spent — see the
        repo's module docstring for why that is load-bearing, and the
        comment on the :class:`~AiRequestError` branch in the handler
        for what handing those back would cost the owner.

        Callers must only release a slot they know was written: DEV and
        the unlimited tier consume nothing at all, and both come back
        with ``count == 0``.
        """
        return await self._repo.release(user_id, now=now)
