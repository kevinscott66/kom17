"""Unit tests for ``/ai_limits`` (Cluster T1, backlog L-10).

Pins the legacy contract (``bot.py:38376-38396``):

* VIP / dev → unlimited card, and the read path must NOT touch the
  quota counter (checking your limit never consumes a slot);
* free tier → used / remaining / limit for today, remaining clamped
  at zero when the counter overshoots the ceiling;
* tier resolution mirrors ``_answer_with_ai`` (dev ids fold into VIP,
  a tier ceiling of 0 means unlimited — backlog L-69).

Telegram plumbing is faked with duck-typed stubs; the i18n ``t`` is
replaced with a recorder so the assertions pin keys AND kwargs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from telegram_invite_bot.handlers import ai as ai_mod

handle_ai_limits: Any = ai_mod.handle_ai_limits


class FakeMessage:
    def __init__(self, *, user_id: int | None = 100) -> None:
        self.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
        self.chat = SimpleNamespace(id=user_id or 0, type="private")
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


class FakeVipRepo:
    def __init__(self, *, profile: object | None = None) -> None:
        self._profile = profile
        self.calls = 0

    async def get_active_profile(self, user_id: int, *, now: Any = None) -> object | None:
        self.calls += 1
        return self._profile


def _quota_settings(
    *,
    free: int = 10,
    vip: int = 0,
    dev_ids: frozenset[int] = frozenset(),
) -> SimpleNamespace:
    return SimpleNamespace(
        free_daily_limit=free,
        vip_daily_limit=vip,
        parsed_dev_user_ids=lambda: dev_ids,
    )


@pytest.fixture
def recorded_t(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_t(key: str, lang: str, **kwargs: Any) -> str:
        calls.append((key, lang, kwargs))
        return key

    monkeypatch.setattr(ai_mod, "t", fake_t)
    return calls


@pytest.fixture
def no_quota_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(registry: Any, user_id: int) -> int:
        raise AssertionError("unlimited path must not read the quota counter")

    monkeypatch.setattr(ai_mod, "_quota_used_today", boom)


def _patch_used(monkeypatch: pytest.MonkeyPatch, used: int) -> None:
    async def fake_used(registry: Any, user_id: int) -> int:
        return used

    monkeypatch.setattr(ai_mod, "_quota_used_today", fake_used)


@pytest.mark.asyncio
async def test_free_user_sees_used_and_remaining(
    monkeypatch: pytest.MonkeyPatch, recorded_t: list[tuple[str, str, dict[str, Any]]]
) -> None:
    _patch_used(monkeypatch, 3)
    msg = FakeMessage()
    await handle_ai_limits(msg, "ru", FakeVipRepo(profile=None), object(), _quota_settings(free=10))
    assert msg.replies == ["h_ai_limits_card"]
    card_calls = [c for c in recorded_t if c[0] == "h_ai_limits_card"]
    assert card_calls[0][2] == {
        "tier": "h_ai_limits_tier_free",
        "used": 3,
        "left": 7,
        "limit": 10,
    }


@pytest.mark.asyncio
async def test_remaining_clamped_at_zero_when_over_limit(
    monkeypatch: pytest.MonkeyPatch, recorded_t: list[tuple[str, str, dict[str, Any]]]
) -> None:
    # The counter keeps incrementing on rejected attempts (abuse
    # ledger, see AiQuotaService), so used can exceed the ceiling —
    # the card must show 0 remaining, never a negative number.
    _patch_used(monkeypatch, 15)
    msg = FakeMessage()
    await handle_ai_limits(msg, "ru", FakeVipRepo(profile=None), object(), _quota_settings(free=10))
    card_calls = [c for c in recorded_t if c[0] == "h_ai_limits_card"]
    assert card_calls[0][2]["used"] == 15
    assert card_calls[0][2]["left"] == 0


@pytest.mark.asyncio
async def test_vip_default_unlimited_skips_counter_read(
    recorded_t: list[tuple[str, str, dict[str, Any]]], no_quota_read: None
) -> None:
    # vip_daily_limit=0 == unlimited (backlog L-69) → unlimited card,
    # and the read-only path never touches ai_daily_requests.
    msg = FakeMessage()
    await handle_ai_limits(
        msg, "ru", FakeVipRepo(profile=object()), object(), _quota_settings(vip=0)
    )
    assert msg.replies == ["h_ai_limits_unlimited"]


@pytest.mark.asyncio
async def test_vip_with_finite_ceiling_renders_vip_tier_card(
    monkeypatch: pytest.MonkeyPatch, recorded_t: list[tuple[str, str, dict[str, Any]]]
) -> None:
    _patch_used(monkeypatch, 2)
    msg = FakeMessage()
    await handle_ai_limits(
        msg, "en", FakeVipRepo(profile=object()), object(), _quota_settings(vip=50)
    )
    card_calls = [c for c in recorded_t if c[0] == "h_ai_limits_card"]
    assert card_calls[0][1] == "en"
    assert card_calls[0][2] == {
        "tier": "h_ai_limits_tier_vip",
        "used": 2,
        "left": 48,
        "limit": 50,
    }


@pytest.mark.asyncio
async def test_dev_id_unlimited_without_vip_lookup(
    recorded_t: list[tuple[str, str, dict[str, Any]]], no_quota_read: None
) -> None:
    vip_repo = FakeVipRepo(profile=None)
    msg = FakeMessage(user_id=999)
    await handle_ai_limits(msg, "ru", vip_repo, object(), _quota_settings(dev_ids=frozenset({999})))
    assert msg.replies == ["h_ai_limits_unlimited"]
    # ``is_dev or (await vip_repo...)`` short-circuits — no DB lookup.
    assert vip_repo.calls == 0


@pytest.mark.asyncio
async def test_anonymous_message_is_a_noop(
    recorded_t: list[tuple[str, str, dict[str, Any]]], no_quota_read: None
) -> None:
    msg = FakeMessage(user_id=None)
    await handle_ai_limits(msg, "ru", FakeVipRepo(profile=None), object(), _quota_settings())
    assert msg.replies == []
