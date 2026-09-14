"""End-to-end ``/admin_hashlib``.

Pins:

* Non-developer → silent drop.
* Card renders required / guaranteed / available counts.
* Missing required algorithm (FIPS-host simulation) → ⚠.
* Missing-from-guaranteed → ⚠ row (the FIPS smoking gun).
* pbkdf2/scrypt unavailable → ⚠ on the respective row.
* Healthy state (all required + guaranteed available, probes ok)
  → bare. Cry-wolf prevention.
* Extra host-specific algorithms listed but not flagged (purely
  informational — sm3, ripemd160 etc).
* Capture against the real hashlib succeeds on this host (every
  test host CPython supports at least the guaranteed set
  *unless* FIPS is enforced on the runner — accept either).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.hashlib_info import (
    _capture,
    _HashlibSnapshot,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(**overrides: Any) -> _HashlibSnapshot:
    guaranteed = {
        "md5",
        "sha1",
        "sha224",
        "sha256",
        "sha384",
        "sha512",
        "blake2b",
        "blake2s",
        "sha3_256",
        "sha3_512",
    }
    available = set(guaranteed)
    defaults: dict[str, Any] = {
        "guaranteed": guaranteed,
        "available": available,
        "missing_guaranteed": set(),
        "extra": set(),
        "missing_required": set(),
        "pbkdf2_ok": True,
        "scrypt_ok": True,
    }
    defaults.update(overrides)
    return _HashlibSnapshot(**defaults)


def _row_warn_count(rendered: str) -> int:
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_hashlib", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_hashlib", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "hashlib" in text
    assert "required available" in text
    assert "guaranteed" in text
    assert "available" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_hashlib",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_missing_required_warns() -> None:
    """FIPS-host simulation: md5 unavailable → ⚠ on the required row.
    This is the production-down warning the card exists to surface."""
    rendered = _render(_snap(missing_required={"md5"}))
    assert _row_warn_count(rendered) >= 1
    assert "md5" in rendered


def test_render_missing_from_guaranteed_warns() -> None:
    """A guaranteed-by-CPython hash absent from algorithms_available
    is the FIPS smoking gun. Surfaced as its own ⚠ row so the
    operator doesn't have to mentally diff two counts."""
    rendered = _render(_snap(missing_guaranteed={"blake2b"}, available=set()))
    assert "missing-from-guaranteed" in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_pbkdf2_unavailable_warns() -> None:
    rendered = _render(_snap(pbkdf2_ok=False))
    assert "unavailable" in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_scrypt_unavailable_warns() -> None:
    rendered = _render(_snap(scrypt_ok=False))
    assert "unavailable" in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_healthy_state_no_warnings() -> None:
    """All required hashes present, no missing-from-guaranteed,
    pbkdf2 + scrypt ok → bare. Cry-wolf prevention; mirrors the
    posture of every other admin card."""
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


def test_render_extras_listed_but_not_flagged() -> None:
    """Host-specific OpenSSL builds (sm3, ripemd160, whirlpool) are
    informational — listed, NOT marked. A ⚠ on a perfectly healthy
    extra would dilute the triage signal."""
    rendered = _render(_snap(extra={"sm3", "ripemd160"}))
    assert "host extras" in rendered
    assert "sm3" in rendered
    assert "ripemd160" in rendered
    assert _row_warn_count(rendered) == 0


def test_capture_on_real_host() -> None:
    """End-to-end ``_capture`` against the real ``hashlib`` on the
    test host. We assert structural invariants only — not specific
    algorithm presence — so the test stays green on FIPS-enforced
    CI runners too."""
    snap = _capture()
    # algorithms_guaranteed is a CPython compile-time constant
    # and must always be a non-empty set.
    assert snap.guaranteed
    # available ⊇ (guaranteed - missing_guaranteed) by definition.
    assert (snap.guaranteed - snap.missing_guaranteed).issubset(snap.available)
    # missing_required must be a subset of the required set.
    assert snap.missing_required <= {"md5", "sha1", "sha256", "sha512"}
