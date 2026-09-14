"""Unit tests for RateLimitMiddleware.

Tests the token bucket logic and rejection behavior without aiogram
dependency by monkey-patching the clock and using a minimal event mock.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.types import User

from telegram_invite_bot.middlewares.rate_limit import RateLimitMiddleware


class MockEvent:
    """Minimal event mock for testing."""

    def __init__(self, user_id: int, language_code: str = "ru"):
        self.from_user = User(
            id=user_id, is_bot=False, first_name="Test", language_code=language_code
        )
        self.answer = AsyncMock()


@pytest.mark.asyncio
async def test_rate_limit_admits_first_request():
    """Fresh user should be admitted immediately."""
    middleware = RateLimitMiddleware(capacity=3, refill_per_second=0.1)
    middleware._now = lambda: 100.0  # Fixed time

    handler = AsyncMock(return_value="handled")
    event = MockEvent(user_id=123)

    result = await middleware(handler, event, {})

    assert result == "handled"
    handler.assert_called_once()
    event.answer.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_rejects_when_bucket_empty():
    """Empty bucket should cause rejection with friendly message."""
    middleware = RateLimitMiddleware(capacity=1, refill_per_second=0.1)
    middleware._now = lambda: 100.0  # Fixed time

    handler = AsyncMock(return_value="handled")
    event = MockEvent(user_id=123, language_code="en")

    # First request consumes the only token
    result1 = await middleware(handler, event, {})
    assert result1 == "handled"

    # Second request should be rejected
    result2 = await middleware(handler, event, {})
    assert result2 is None

    # Should have sent rejection message
    event.answer.assert_called_once()
    call_args = event.answer.call_args[0][0]
    assert "Too many games" in call_args
    assert "Wait" in call_args


@pytest.mark.asyncio
async def test_rate_limit_russian_rejection_message():
    """Russian users should get Russian rejection message."""
    middleware = RateLimitMiddleware(capacity=1, refill_per_second=0.1)
    middleware._now = lambda: 100.0

    handler = AsyncMock(return_value="handled")
    event = MockEvent(user_id=123, language_code="ru")

    # Consume token
    await middleware(handler, event, {})

    # Trigger rejection
    await middleware(handler, event, {})

    call_args = event.answer.call_args[0][0]
    assert "Слишком часто играешь" in call_args
    assert "Подожди" in call_args


@pytest.mark.asyncio
async def test_rate_limit_token_refill():
    """Tokens should refill over time."""
    middleware = RateLimitMiddleware(capacity=2, refill_per_second=1.0)  # 1 token per second

    # Mock clock to advance time
    time_now = 100.0
    middleware._now = lambda: time_now

    handler = AsyncMock(return_value="handled")
    event = MockEvent(user_id=123)

    # Consume 2 tokens
    result1 = await middleware(handler, event, {})
    assert result1 == "handled"

    result2 = await middleware(handler, event, {})
    assert result2 == "handled"

    # Bucket should now be empty
    result3 = await middleware(handler, event, {})
    assert result3 is None

    # Advance time by 1 second
    time_now = 101.0

    # Should have 1 token available
    result4 = await middleware(handler, event, {})
    assert result4 == "handled"


@pytest.mark.asyncio
async def test_rate_limit_per_user_isolation():
    """Different users should have separate buckets."""
    middleware = RateLimitMiddleware(capacity=1, refill_per_second=0.1)
    middleware._now = lambda: 100.0

    handler = AsyncMock(return_value="handled")

    # User 1 consumes their token
    event1 = MockEvent(user_id=123)
    result1 = await middleware(handler, event1, {})
    assert result1 == "handled"

    # User 1's second request should be rejected
    result2 = await middleware(handler, event1, {})
    assert result2 is None

    # User 2 should still be admitted
    event2 = MockEvent(user_id=456)
    result3 = await middleware(handler, event2, {})
    assert result3 == "handled"


@pytest.mark.asyncio
async def test_rate_limit_bypasses_anonymous_events():
    """Events without from_user should bypass rate limiting."""
    middleware = RateLimitMiddleware(capacity=0, refill_per_second=0.0)  # No tokens

    handler = AsyncMock(return_value="handled")

    # Event without from_user
    class AnonymousEvent:
        pass

    event = AnonymousEvent()
    result = await middleware(handler, event, {})

    assert result == "handled"
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_rate_limit_wait_time_calculation():
    """Wait time should be calculated correctly based on deficit."""
    middleware = RateLimitMiddleware(capacity=1, refill_per_second=0.1)  # 10 seconds per token
    middleware._now = lambda: 100.0

    handler = AsyncMock()
    event = MockEvent(user_id=123)

    # Consume token
    await middleware(handler, event, {})

    # Trigger rejection
    await middleware(handler, event, {})

    # Should suggest waiting about 10 seconds (rounded up)
    call_args = event.answer.call_args[0][0]
    assert "10" in call_args or "Wait 10" in call_args
