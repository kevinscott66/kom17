"""``AiQuotaService`` — tier resolution + ALLOWED/EXCEEDED branches.

Policy: free=10/day, vip=200/day, dev=unlimited; a ceiling of 0 means
unlimited for that tier. This module pins each branch so a future
change to ``AiQuotaConfig`` defaults (or to ``_resolve_tier``'s
priority order) surfaces here. Tests pass explicit configs so they
assert behaviour, not the shipped defaults — with one deliberate
exception at the bottom, where the shipped VIP default IS the subject
(it must stay finite; ``0`` there is an unbounded provider bill).

Repo is a hand-written fake — a stateful counter keyed by user_id —
because the service treats the repo as an atomic primitive and we
want the test to fail on policy-level regressions, not SQLAlchemy
plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from telegram_invite_bot.services.ai_quota_service import (
    AiQuotaConfig,
    AiQuotaService,
    QuotaOutcome,
    QuotaTier,
)


@dataclass
class _FakeQuotaRepo:
    """Mimics ``AiQuotaRepo.get_and_increment`` — one in-memory counter."""

    counts: dict[int, int] = field(default_factory=dict)

    async def get_and_increment(self, user_id: int, *, now: object = None) -> int:
        self.counts[user_id] = self.counts.get(user_id, 0) + 1
        return self.counts[user_id]


async def test_free_user_rejected_on_sixth_call() -> None:
    repo = _FakeQuotaRepo()
    svc = AiQuotaService(repo, config=AiQuotaConfig(free_daily_limit=5))

    outcomes = []
    for _ in range(6):
        outcomes.append((await svc.check_and_consume(42, is_vip=False)).outcome)

    assert outcomes[:5] == [QuotaOutcome.ALLOWED] * 5
    assert outcomes[5] is QuotaOutcome.EXCEEDED


async def test_vip_can_do_fifty() -> None:
    repo = _FakeQuotaRepo()
    svc = AiQuotaService(repo, config=AiQuotaConfig(free_daily_limit=5, vip_daily_limit=50))

    for _ in range(50):
        result = await svc.check_and_consume(7, is_vip=True)
        assert result.outcome is QuotaOutcome.ALLOWED
        assert result.tier is QuotaTier.VIP

    # 51st rejected
    rej = await svc.check_and_consume(7, is_vip=True)
    assert rej.outcome is QuotaOutcome.EXCEEDED
    assert rej.limit == 50
    assert rej.count == 51


async def test_vip_unlimited_when_ceiling_zero() -> None:
    """An explicit ceiling of 0 is unlimited: never rejected, never
    touches the repo counter — same posture as DEV. This is the
    operator opt-out (it used to be the VIP default, backlog L-69);
    the ceiling still has to behave this way when someone sets it."""
    repo = _FakeQuotaRepo()
    svc = AiQuotaService(repo, config=AiQuotaConfig(free_daily_limit=10, vip_daily_limit=0))

    for _ in range(200):
        result = await svc.check_and_consume(7, is_vip=True)
        assert result.outcome is QuotaOutcome.ALLOWED
        assert result.tier is QuotaTier.VIP
        assert result.limit == 0  # 0 == unlimited
    assert repo.counts == {}  # unlimited tier never writes the ledger


async def test_free_user_rejected_on_eleventh_call_legacy_default() -> None:
    """Legacy-parity free default is 10/day → 11th call rejected."""
    repo = _FakeQuotaRepo()
    svc = AiQuotaService(repo, config=AiQuotaConfig(free_daily_limit=10))

    outcomes = []
    for _ in range(11):
        outcomes.append((await svc.check_and_consume(42, is_vip=False)).outcome)

    assert outcomes[:10] == [QuotaOutcome.ALLOWED] * 10
    assert outcomes[10] is QuotaOutcome.EXCEEDED


async def test_dev_bypasses_counter_entirely() -> None:
    repo = _FakeQuotaRepo()
    svc = AiQuotaService(
        repo,
        config=AiQuotaConfig(
            free_daily_limit=5,
            vip_daily_limit=50,
            dev_user_ids=frozenset({999}),
        ),
    )

    # 100 calls — DEV must never be rejected and must never touch repo
    for _ in range(100):
        result = await svc.check_and_consume(999, is_vip=False)
        assert result.outcome is QuotaOutcome.ALLOWED
        assert result.tier is QuotaTier.DEV
    assert repo.counts == {}


async def test_rejected_call_still_consumes_slot() -> None:
    """A 6th-attempt rejection still increments — abuse ledger, not cost counter."""
    repo = _FakeQuotaRepo()
    svc = AiQuotaService(repo, config=AiQuotaConfig(free_daily_limit=5))

    for _ in range(5):
        await svc.check_and_consume(42, is_vip=False)
    rej = await svc.check_and_consume(42, is_vip=False)

    assert rej.outcome is QuotaOutcome.EXCEEDED
    # Counter advanced to 6 even though the call was refused, so a
    # retry won't read "5" and slip through.
    assert repo.counts[42] == 6


async def test_vip_takes_precedence_over_free() -> None:
    repo = _FakeQuotaRepo()
    svc = AiQuotaService(repo, config=AiQuotaConfig(free_daily_limit=1, vip_daily_limit=10))
    # As a VIP, the 2nd call (which would exceed free=1) is fine.
    assert (await svc.check_and_consume(1, is_vip=True)).outcome is QuotaOutcome.ALLOWED
    assert (await svc.check_and_consume(1, is_vip=True)).outcome is QuotaOutcome.ALLOWED


def test_quota_settings_parses_dev_ids() -> None:
    """Operator-edited env var → frozenset[int]."""
    from telegram_invite_bot.config.settings import AiQuotaSettings

    assert AiQuotaSettings(dev_user_ids="").parsed_dev_user_ids() == frozenset()
    assert AiQuotaSettings(dev_user_ids="111, 222 ,333").parsed_dev_user_ids() == frozenset(
        {111, 222, 333}
    )
    with pytest.raises(ValueError):
        AiQuotaSettings(dev_user_ids="abc").parsed_dev_user_ids()


def test_shipped_vip_ceiling_is_finite(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SHIPPED VIP default must stay > 0 — this one asserts defaults
    on purpose.

    ``/ai``, ``/ask``, ``/voice`` and ``/quote`` cost the user nothing,
    so every request is billed to the operator's provider key and this
    counter is the only per-day ceiling in the system. A ceiling of 0
    means unlimited (see ``test_vip_unlimited_when_ceiling_zero``), so
    shipping 0 here would be an unbounded bill per VIP. Operators can
    still opt into it via the env var; the default may not.

    ``AiQuotaConfig`` is asserted alongside because a caller that
    constructs :class:`AiQuotaService` without config falls back to it —
    the two defaults have to stay in lockstep or the safe number only
    applies on the wired path.
    """
    from telegram_invite_bot.config.settings import AiQuotaSettings

    # A developer machine may export the var; the subject is the code
    # default, not the local environment.
    monkeypatch.delenv("AI_QUOTA_VIP_DAILY_LIMIT", raising=False)
    settings = AiQuotaSettings(_env_file=None)

    assert settings.vip_daily_limit > 0
    assert AiQuotaConfig().vip_daily_limit == settings.vip_daily_limit
    # And the free tier is finite too — same reasoning, cheaper blast
    # radius.
    assert settings.free_daily_limit > 0
