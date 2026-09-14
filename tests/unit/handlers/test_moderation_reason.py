"""Unit tests for the moderation ``_extract_reason`` helper.

M-M-4 follow-up (re-audit): the reason text persisted on every
``/ban /kick /mute /warn /unwarn /fine`` must be bounded so that a
4 KB blob (PII, accidental log paste) does not land verbatim in
``moderation_log.reason``. The cap is enforced at extraction —
this file pins both the cap value and the truncation marker so a
refactor that drops the clamp surfaces here, not in audit-log
spelunking months later.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers.moderation import (
    _REASON_MAX_LENGTH,
    _clamp_reason,
    _extract_reason,
)


def _msg(text: str) -> Message:
    """Build a minimal ``Message``-shaped stub.

    ``_extract_reason`` only reads ``message.text`` — a typed cast
    over ``SimpleNamespace`` is enough and keeps the test free of
    aiogram's heavy ``Message.model_validate`` boilerplate.
    """
    return cast("Message", SimpleNamespace(text=text))


def test_extract_reason_under_cap_returned_verbatim() -> None:
    """Short reasons pass through unchanged — the clamp is invisible
    on the common path (typical reason: a sentence or two)."""
    text = "/ban spam in #general"
    assert _extract_reason(_msg(text), from_reply=True) == "spam in #general"


def test_extract_reason_clamps_4000_char_blob_to_cap() -> None:
    """The audit reproducer: a 4000-char paste lands as exactly the
    cap (256 chars) with a single ``…`` marker as the terminating
    character. Total length is bounded by ``_REASON_MAX_LENGTH``."""
    blob = "x" * 4000
    text = f"/ban {blob}"
    result = _extract_reason(_msg(text), from_reply=True)
    assert len(result) == _REASON_MAX_LENGTH
    assert result.endswith("…")
    # Ensure the truncation preserves the prefix of the input.
    assert result[:-1] == "x" * (_REASON_MAX_LENGTH - 1)


def test_extract_reason_exactly_at_cap_no_ellipsis() -> None:
    """Boundary: a reason exactly at the cap is not marked truncated
    (there's nothing to mark). Off-by-one defence: the truncation
    branch fires on ``len > cap``, not ``len >= cap``."""
    body = "y" * _REASON_MAX_LENGTH
    text = f"/ban {body}"
    result = _extract_reason(_msg(text), from_reply=True)
    assert result == body
    assert not result.endswith("…")


def test_extract_reason_one_over_cap_truncates() -> None:
    """One char over the cap → truncated. Pins the boundary."""
    body = "z" * (_REASON_MAX_LENGTH + 1)
    text = f"/ban {body}"
    result = _extract_reason(_msg(text), from_reply=True)
    assert len(result) == _REASON_MAX_LENGTH
    assert result.endswith("…")


@pytest.mark.parametrize("from_reply", [True, False])
def test_extract_reason_clamp_applies_to_both_paths(from_reply: bool) -> None:
    """Reply form AND arg form both go through the clamp — a future
    refactor that splits the two branches must keep the cap on both."""
    blob = "w" * 1000
    if from_reply:
        text = f"/ban {blob}"
        result = _extract_reason(_msg(text), from_reply=True)
    else:
        text = f"/ban @target {blob}"
        result = _extract_reason(_msg(text), from_reply=False, skip_arg_tokens=1)
    assert len(result) == _REASON_MAX_LENGTH
    assert result.endswith("…")


def test_clamp_reason_unit() -> None:
    """The helper itself: empty / short / over-cap shapes."""
    assert _clamp_reason("") == ""
    assert _clamp_reason("short") == "short"
    over = "a" * (_REASON_MAX_LENGTH + 50)
    clamped = _clamp_reason(over)
    assert len(clamped) == _REASON_MAX_LENGTH
    assert clamped.endswith("…")
