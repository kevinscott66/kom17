"""``VoiceQuotaService`` — per-tier daily ``/voice`` quota (L-90).

Legacy had no TTS quota (its "voice" surfaces are Whisper transcription
settings, not synthesis — see the service module docstring). These tests
pin the minimal-honest policy the strangler adds: ordinary VIPs are
capped at ``VoiceQuotaConfig.vip_daily_limit``/day; the top-tier
allowlist cohort bypasses; a ceiling of 0 means unlimited.

The repo is a hand-written fake returning a fixed "used today" count so
the tests assert policy, not SQLAlchemy plumbing (same posture as
``test_ai_quota_service``). The quota gate is a PURE READ — it never
mutates the fake — so a single ``check`` call against a given count is
the whole contract.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from telegram_invite_bot.services.voice_quota_service import (
    VoiceQuotaConfig,
    VoiceQuotaOutcome,
    VoiceQuotaService,
    VoiceQuotaTier,
)


@dataclass
class _FakeLedger:
    """Mimics ``TransactionsRepo.voice_today_count`` — fixed daily count."""

    today: int = 0

    async def voice_today_count(self, user_id: int, *, now: object = None) -> int:
        return self.today


async def test_vip_allowed_below_limit() -> None:
    svc = VoiceQuotaService(_FakeLedger(today=4), config=VoiceQuotaConfig(vip_daily_limit=5))
    result = await svc.check(42)
    assert result.outcome is VoiceQuotaOutcome.ALLOWED
    assert result.tier is VoiceQuotaTier.VIP
    assert result.used == 4
    assert result.limit == 5


async def test_vip_rejected_at_limit() -> None:
    """``used >= limit`` rejects — the 6th synthesis after 5 today."""
    svc = VoiceQuotaService(_FakeLedger(today=5), config=VoiceQuotaConfig(vip_daily_limit=5))
    result = await svc.check(42)
    assert result.outcome is VoiceQuotaOutcome.EXCEEDED
    assert result.tier is VoiceQuotaTier.VIP
    assert result.used == 5
    assert result.limit == 5


async def test_vip_rejected_above_limit() -> None:
    svc = VoiceQuotaService(_FakeLedger(today=9), config=VoiceQuotaConfig(vip_daily_limit=5))
    result = await svc.check(42)
    assert result.outcome is VoiceQuotaOutcome.EXCEEDED


async def test_unlimited_cohort_bypasses_even_over_limit() -> None:
    """A user in the allowlist is UNLIMITED tier — never rejected, and
    the ledger is never even read (count of 999 is irrelevant)."""
    svc = VoiceQuotaService(
        _FakeLedger(today=999),
        config=VoiceQuotaConfig(vip_daily_limit=5, unlimited_user_ids=frozenset({42})),
    )
    result = await svc.check(42)
    assert result.outcome is VoiceQuotaOutcome.ALLOWED
    assert result.tier is VoiceQuotaTier.UNLIMITED
    assert result.limit == 0


async def test_zero_ceiling_means_unlimited() -> None:
    """A ``vip_daily_limit`` of 0 is the legacy idiom for unlimited —
    ALLOWED with no read and no rejection, mirroring AiQuotaService."""
    svc = VoiceQuotaService(_FakeLedger(today=999), config=VoiceQuotaConfig(vip_daily_limit=0))
    result = await svc.check(42)
    assert result.outcome is VoiceQuotaOutcome.ALLOWED
    assert result.tier is VoiceQuotaTier.VIP
    assert result.limit == 0


async def test_default_config_vip_limit_is_twenty() -> None:
    """Pin the shipped default so a silent change to the legacy-derived
    ceiling surfaces here (derivation documented in the service)."""
    assert VoiceQuotaConfig().vip_daily_limit == 20
    assert VoiceQuotaConfig().unlimited_user_ids == frozenset()


async def test_the_shipped_default_and_the_settings_default_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1963: the env ceiling and the dataclass fallback must match.

    This is the guard ``test_ai_quota_service`` has carried since it was
    written and this module did not: a caller that builds
    :class:`VoiceQuotaService` without a config falls back to the
    dataclass default, so the two have to stay in lockstep or the
    operator's number applies only on the wired path. Its absence is
    part of why the ceiling stayed unreachable — nothing asserted that
    a settings alias existed at all.
    """
    from telegram_invite_bot.config.settings import TtsConfig

    # A developer machine may export the var; the subject is the code
    # default, not the local environment.
    monkeypatch.delenv("OPENAI_TTS_VIP_DAILY_LIMIT", raising=False)
    settings = TtsConfig(_env_file=None)

    assert settings.vip_daily_limit > 0
    assert VoiceQuotaConfig().vip_daily_limit == settings.vip_daily_limit
