"""``EffectsService.resolve_daily_effects`` — DB → typed bundle.

The pinned guarantees:

* ``double`` reflects the live state of the privileges table.
* ``vip_percent`` is hard-zero in Stage 11 (regression guard
  against an accidental "I implemented VIP" PR that bypasses the
  legacy /daily routing — the gap is documented and tested).
* ``consume_double_daily`` is a delegating one-liner; the test
  pins it so a future refactor that drops the wrapper would fail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, UserGroupVip, UserPrivilege
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.services.effects_service import EffectsService
from tests.integration.repositories._session import build_session


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[tuple[EffectsService, AsyncSession]]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield EffectsService(PrivilegesRepo(session), VipRepo(session)), session


async def _seed_wallet(
    session: AsyncSession, user_id: int, *, vip_till: float | None = None
) -> None:
    session.add(EconomyUser(user_id=user_id, balance=100, language="ru", vip_till=vip_till))
    await session.commit()


async def test_default_user_gets_zero_effects(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """No privilege rows → no double, no VIP. Pinned because the
    handler renders "you got 10 coins" math directly from this; a
    spurious double=True would silently double everyone's daily."""
    effects_service, _ = service
    effects = await effects_service.resolve_daily_effects(42, now=datetime(2024, 6, 15, tzinfo=UTC))
    assert effects.vip_percent == 0
    assert effects.double is False


async def test_double_daily_row_flips_double_flag(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    effects_service, session = service
    session.add(UserPrivilege(user_id=42, privilege_type="double_daily", group_id=0))
    await session.commit()

    effects = await effects_service.resolve_daily_effects(42, now=datetime(2024, 6, 15, tzinfo=UTC))
    assert effects.double is True
    # VIP gap still 0 — see service docstring.
    assert effects.vip_percent == 0


async def test_expired_double_daily_does_not_flip_flag(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """The buster row is still in the DB but past its deadline →
    resolves as not active. Pinned so a future "read all rows
    eagerly" optimisation can't accidentally drop the expiry
    filter."""
    effects_service, session = service
    past = (datetime(2024, 1, 1, tzinfo=UTC)).timestamp()
    session.add(
        UserPrivilege(
            user_id=42,
            privilege_type="double_daily",
            group_id=0,
            expires_at=past,
        )
    )
    await session.commit()

    effects = await effects_service.resolve_daily_effects(42, now=datetime(2024, 6, 15, tzinfo=UTC))
    assert effects.double is False


async def test_active_global_vip_yields_legacy_percent(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """Stage 12: with the VipRepo wired, an active global VIP grant
    surfaces as :data:`VipProfile.daily_bonus_percent` (15). Pins
    the constant — if a future tiering PR changes the percent,
    this test needs an intentional update, not a silent drift."""
    effects_service, session = service
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    await _seed_wallet(session, 42, vip_till=future)

    effects = await effects_service.resolve_daily_effects(42, now=datetime(2024, 6, 15, tzinfo=UTC))
    assert effects.vip_percent == 15


async def test_expired_global_vip_yields_zero_percent(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """``vip_till`` in the past → no VIP — payout matches a regular
    user. Boundary pinned on the repo; this test pins the bundle
    propagates the boundary cleanly through to /daily."""
    effects_service, session = service
    past = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    await _seed_wallet(session, 42, vip_till=past)

    effects = await effects_service.resolve_daily_effects(42, now=datetime(2024, 6, 15, tzinfo=UTC))
    assert effects.vip_percent == 0


async def test_group_scoped_vip_does_not_grant_global_daily_percent(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """Legacy reads the *global* VIP for /daily, not the per-chat
    grant. A user with only group-VIP must read as vip_percent=0
    in this bundle — otherwise users would get per-chat perks on
    a per-user command, breaking the legacy contract."""
    effects_service, session = service
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    await _seed_wallet(session, 42)  # no global VIP
    session.add(UserGroupVip(user_id=42, group_id=-1001, vip_till=future))
    await session.commit()

    effects = await effects_service.resolve_daily_effects(42, now=datetime(2024, 6, 15, tzinfo=UTC))
    assert effects.vip_percent == 0


async def test_consume_double_daily_delegates_to_remove(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    effects_service, session = service
    session.add(UserPrivilege(user_id=42, privilege_type="double_daily", group_id=0))
    await session.commit()

    consumed = await effects_service.consume_double_daily(42)
    await session.commit()
    assert consumed is True

    # Subsequent resolve sees no buster.
    effects = await effects_service.resolve_daily_effects(42, now=datetime(2024, 6, 15, tzinfo=UTC))
    assert effects.double is False


async def test_consume_missing_buster_returns_false(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """Idempotent / race-safe: consuming a missing buster is a no-op
    that reports False. Allows the handler to log "buster consumed"
    only on a real consumption without try/except scaffolding."""
    effects_service, _ = service
    assert await effects_service.consume_double_daily(99999) is False


async def test_active_double_just_before_expiry_resolves_true(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """Time-boundary cross-check: ``now`` strictly before
    ``expires_at`` reads as active. The repo test pins this on the
    repo; this test pins that the service threads ``now`` through
    correctly (no off-by-one introduced at the bundle layer)."""
    effects_service, session = service
    deadline = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    session.add(
        UserPrivilege(
            user_id=42,
            privilege_type="double_daily",
            group_id=0,
            expires_at=deadline.timestamp(),
        )
    )
    await session.commit()

    effects = await effects_service.resolve_daily_effects(42, now=deadline - timedelta(seconds=1))
    assert effects.double is True
    # At the deadline → expired.
    effects = await effects_service.resolve_daily_effects(42, now=deadline)
    assert effects.double is False


# ---------------------------------------------------------------------------
# resolve_transfer_effects (Stage 14) — tax-discount-percent bundle for /send
# ---------------------------------------------------------------------------


async def test_default_user_gets_zero_transfer_discount(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """No VIP grant → no discount. Pinned because the future
    TransferService applies the discount unconditionally to the
    ``COINS_TRANSFER_TAX`` rate; a spurious non-zero here would
    silently shave commission off every non-VIP user."""
    effects_service, _ = service
    effects = await effects_service.resolve_transfer_effects(
        42, now=datetime(2024, 6, 15, tzinfo=UTC)
    )
    assert effects.tax_discount_percent == 0


async def test_active_global_vip_yields_legacy_50_percent_discount(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """Mirrors the daily-percent pinning: legacy ``ItemEffects``
    hardcodes ``tax_discount_percent=50`` (``bot.py:13515``). Pinning
    50 here means a future tiering PR has to update this test
    intentionally — not drift silently and shift commission for
    every VIP user across both pipelines."""
    effects_service, session = service
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    await _seed_wallet(session, 42, vip_till=future)

    effects = await effects_service.resolve_transfer_effects(
        42, now=datetime(2024, 6, 15, tzinfo=UTC)
    )
    assert effects.tax_discount_percent == 50


async def test_expired_global_vip_yields_zero_transfer_discount(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """``vip_till`` in the past → no discount. The boundary itself is
    pinned in test_vip_repo.py; this test pins that the transfer
    bundle propagates the boundary cleanly — same shape as the
    /daily symmetric test."""
    effects_service, session = service
    past = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    await _seed_wallet(session, 42, vip_till=past)

    effects = await effects_service.resolve_transfer_effects(
        42, now=datetime(2024, 6, 15, tzinfo=UTC)
    )
    assert effects.tax_discount_percent == 0


async def test_group_scoped_vip_does_not_grant_transfer_discount(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """Legacy ``get_transfer_tax_discount_percent`` calls
    ``get_vip_profile(user_id)`` with NO ``group_id`` (``bot.py:13553``).
    Per-chat VIP must not leak into a per-user /send command — pinning
    this prevents an accidental ``group_id=chat_id`` argument from
    sneaking into the resolver and silently waiving tax for any user
    who holds VIP in any single chat they happen to be in."""
    effects_service, session = service
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    await _seed_wallet(session, 42)  # no global VIP
    session.add(UserGroupVip(user_id=42, group_id=-1001, vip_till=future))
    await session.commit()

    effects = await effects_service.resolve_transfer_effects(
        42, now=datetime(2024, 6, 15, tzinfo=UTC)
    )
    assert effects.tax_discount_percent == 0


async def test_transfer_and_daily_effects_share_one_vip_grant(
    service: tuple[EffectsService, AsyncSession],
) -> None:
    """A single active VIP row drives BOTH bundles in one round-trip
    each — pinned so a future "consolidate effects into one
    .resolve()" refactor that drops independent reads can't silently
    skip one bundle's percent. Catches the symmetric error where
    daily/transfer divergence would otherwise only surface in
    production after a real VIP user complains."""
    effects_service, session = service
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    await _seed_wallet(session, 42, vip_till=future)

    now = datetime(2024, 6, 15, tzinfo=UTC)
    daily = await effects_service.resolve_daily_effects(42, now=now)
    transfer = await effects_service.resolve_transfer_effects(42, now=now)
    assert daily.vip_percent == 15
    assert transfer.tax_discount_percent == 50
