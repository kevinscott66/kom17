"""Per-tier daily ``/voice`` (TTS) quota enforcement (L-90).

Background — why this is a "minimal honest" port, not a 1:1 legacy port
----------------------------------------------------------------------
The legacy monolith (``/Users/owner/Desktop/telegram_invite_bot/bot.py``)
has NO TTS daily quota. Its only "voice" surfaces are group voice
*transcription* settings (Whisper speech-to-text): the
``voice_transcriptions`` table and the ``voice_settings_*`` callback
menu (bot.py:5902, 27955-28205). There is no text-to-speech synthesis
command in legacy at all — TTS (``vip_emoji_voice`` / ``/voice``) is a
strangler-era feature (T-023). A scout of bot.py for ``tts`` / ``озвуч``
/ ``синтез`` / ``unlimited`` / ``vip_tier`` near any voice command turns
up only the transcription menu and the ``/calc`` flavour text
(bot.py:17610) — never a synthesis quota.

So there is no legacy quota number to port exactly. Per the L-90 brief
("if legacy itself never enforced a quota (flag-only), implement the
minimal honest version: per-tier daily quota with sensible
legacy-derived values and document the derivation") this service
derives its defaults from the one adjacent legacy-parity precedent that
DOES exist: the AI daily quota.

Legacy-derived defaults
-----------------------
:class:`telegram_invite_bot.services.ai_quota_service.AiQuotaService`
restored legacy parity (backlog L-69) at ``free=10/day``,
``vip=unlimited (0)``, with a ``0`` ceiling meaning "unlimited, no
counter, no rejection" — the exact shape legacy used for VIP in
``_check_ai_daily_limit_non_vip`` where VIPs bypassed the gate.

``/voice`` is already VIP-gated at the handler (non-VIPs never reach
synthesis), so the "free" AI tier has no analogue here. The two tiers
that DO reach synthesis are:

* ``vip`` — an ordinary active VIP grant. TTS is a metered upstream
  cost (OpenAI bills per output character — see :class:`TtsConfig`), so
  unlike AI/DeepSeek (which legacy left unlimited for VIPs because it
  was effectively free) an unbounded VIP voice allowance is an
  operator-cost abuse vector. We set a default ceiling of
  ``vip_daily_limit = 20`` — double the legacy free-AI allowance of 10,
  reflecting that voice is a paid VIP perk while AI was the free tier,
  while still bounding daily upstream spend. Operator-tunable via
  ``OPENAI_TTS_VIP_DAILY_LIMIT`` (#1963 — this line claimed as much
  from the start, but the alias did not exist and the handler passed
  only ``unlimited_user_ids``, so the number was reachable one way:
  edit this file and redeploy).
* ``unlimited`` — the top-tier cohort the M-P-4 flag already singles
  out (``FeatureFlags.vip_unlimited_voice`` AND membership in
  ``VIP_UNLIMITED_VOICE_USER_IDS``). These users get free synthesis
  (``skip_billing``) AND now an unbounded daily allowance, expressed
  the legacy way as a ceiling of ``0``. This is the "unlimited tier"
  the L-90 brief calls out as bypassing.

Counting source — ledger COUNT, no migration
---------------------------------------------
Per L-90 design guidance, the daily count comes from
:meth:`TransactionsRepo.voice_today_count` — net ``type='tts'`` debit
rows for the user on the current UTC day (refunds of failed syntheses
subtracted, so a refunded failure does not burn a slot). This is the
same ledger ``/voice_stats`` reads; no counter table / migration is
added.

Pre-check, not consume-then-check
---------------------------------
Unlike :class:`AiQuotaService` (which owns a dedicated counter and
increments it), this gate is a PURE READ. The quota "increment" is the
``type='tts'`` debit row that :class:`TtsService` writes on a
successful synthesis — the very row the NEXT call's
:meth:`voice_today_count` will see. So the flow is:

    check_quota() -> ALLOWED -> TtsService.synthesize() writes the row

A consequence: simultaneous ``/voice`` calls at ``count == limit-1``
all read ``limit-1`` and all synthesise, landing the user over the
daily cap. The window is not tight — ``get_or_create``, client
construction and the OpenAI round-trip all sit between this read and
the ledger row that would have stopped the next caller
(``handlers/vip_emoji_voice.py``), so it is seconds wide, not
milliseconds.

#1961: this paragraph used to bound the overrun at "a single extra
synthesis, self-limited by the per-user
:class:`VoiceRateLimitMiddleware` 5-burst". That reads the burst
backwards. A token bucket with capacity 5 *permits* five calls back to
back, so the burst is what makes the overrun possible rather than what
limits it, and the real bound is ``capacity - 1`` — four extra
syntheses at the shipped default, not one.

Still accepted rather than closed: the fix is the dedicated-counter
shape :class:`AiQuotaService` uses (``AiQuotaRepo`` increments inside
an ``INSERT … ON CONFLICT DO UPDATE`` and compares afterwards), which
means a counter table and a migration on a cost-control surface. That
belongs with the global daily STT budget in the owner's queue, not in
a passing refactor. Nothing here is a money leak — every synthesis is
paid for with a guarded ``hold`` — what leaks is upstream spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

log = logger.bind(component="services.voice_quota")

if TYPE_CHECKING:
    from datetime import datetime

    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


class VoiceQuotaTier(StrEnum):
    VIP = "vip"
    UNLIMITED = "unlimited"


class VoiceQuotaOutcome(StrEnum):
    ALLOWED = "allowed"
    EXCEEDED = "exceeded"


@dataclass(frozen=True, slots=True)
class VoiceQuotaResult:
    outcome: VoiceQuotaOutcome
    tier: VoiceQuotaTier
    used: int = 0
    """Net syntheses already made today (the count this gate read).
    On EXCEEDED this is ``>= limit``; on ALLOWED it is the count BEFORE
    the synthesis this call is about to authorise."""
    limit: int = 0
    """The ceiling that applied for the user's tier. ``0`` for the
    UNLIMITED tier (no cap)."""


@dataclass(frozen=True, slots=True)
class VoiceQuotaConfig:
    """Per-tier daily ``/voice`` ceilings. Legacy-derived defaults.

    See the module docstring for the full derivation. ``0`` for any
    field means *unlimited* (no cap, no rejection) — the legacy idiom
    for VIP unlimited, mirrored from :class:`AiQuotaConfig`.

    ``unlimited_user_ids`` is the top-tier cohort that bypasses the
    counter entirely (the M-P-4 ``vip_unlimited_voice`` allowlist).
    Default empty so the service can be constructed before the handler
    wires the flag-derived set; an empty set means "no implicit
    unlimited tier" (fail-closed, matching the flag's own posture).
    """

    vip_daily_limit: int = 20
    unlimited_user_ids: frozenset[int] = field(default_factory=frozenset)


class VoiceQuotaService:
    """Resolve tier + read today's ledger count into an allow/refuse."""

    def __init__(
        self,
        transactions_repo: TransactionsRepo,
        *,
        config: VoiceQuotaConfig | None = None,
    ) -> None:
        self._ledger = transactions_repo
        self._config = config or VoiceQuotaConfig()

    def _resolve_tier(self, user_id: int) -> VoiceQuotaTier:
        if user_id in self._config.unlimited_user_ids:
            return VoiceQuotaTier.UNLIMITED
        return VoiceQuotaTier.VIP

    async def check(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> VoiceQuotaResult:
        """Pre-synthesis quota gate. PURE READ (no counter write).

        Returns ALLOWED when the user's net syntheses today are below
        their tier ceiling (or the tier is unlimited), EXCEEDED
        otherwise. The caller proceeds to :class:`TtsService` only on
        ALLOWED; the successful synthesis's ``type='tts'`` ledger row
        is what the next call counts (see module docstring on why this
        is a pre-check rather than a consume).
        """
        tier = self._resolve_tier(user_id)
        bound = log.bind(uid=user_id, tier=tier.value)

        if tier is VoiceQuotaTier.UNLIMITED:
            bound.info("voice quota: unlimited tier bypass")
            return VoiceQuotaResult(outcome=VoiceQuotaOutcome.ALLOWED, tier=tier)

        limit = self._config.vip_daily_limit

        # A ceiling of 0 means unlimited — ALLOWED with no read needed.
        # Mirrors AiQuotaService: an unlimited tier never counts or
        # rejects. Keeps ``limit=0`` in the result (== unlimited).
        if limit <= 0:
            bound.info("voice quota: unlimited (limit=0)")
            return VoiceQuotaResult(outcome=VoiceQuotaOutcome.ALLOWED, tier=tier)

        used = await self._ledger.voice_today_count(user_id, now=now)
        if used >= limit:
            bound.bind(used=used, limit=limit).info("voice quota exceeded")
            return VoiceQuotaResult(
                outcome=VoiceQuotaOutcome.EXCEEDED,
                tier=tier,
                used=used,
                limit=limit,
            )

        bound.bind(used=used, limit=limit).info("voice quota ok")
        return VoiceQuotaResult(
            outcome=VoiceQuotaOutcome.ALLOWED,
            tier=tier,
            used=used,
            limit=limit,
        )
