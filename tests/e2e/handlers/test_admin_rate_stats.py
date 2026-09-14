"""End-to-end ``/admin_rate_stats``.

Pins:

* Non-developers get **no** reply (silent drop — see the module
  docstring for why we don't surface "permission denied").
* Developers see a card listing capacity, refill rate, tracked-users
  total, and a "most pressured" top-N of users sorted ascending by
  remaining tokens.
* When no user has been seen yet the card still renders, with an
  explicit "no users tracked yet" line — distinct from the same
  output a render bug would produce.
* The handler reads from the SAME ``ThrottlingMiddleware`` instance
  the dispatcher uses to gate traffic. Tests inject bucket state
  directly via that instance and assert the snapshot reflects it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _find_throttle(dispatcher: Any) -> ThrottlingMiddleware:
    """Locate the ``ThrottlingMiddleware`` the conftest captured into the
    router closure.

    The conftest constructs one ``ThrottlingMiddleware`` per
    ``make_wired`` call, passes it to ``build_main_router``, and the
    admin handler captures it via closure. The dispatcher doesn't keep
    a reference (the conftest deliberately doesn't attach throttle as
    an outer middleware in tests — see the conftest comment). So we
    seed the middleware via the same closure path the production code
    uses: walk the routers to the admin-rate-stats router's registered
    handler and pull its ``__closure__``.

    Brittle? Yes, but the alternative — making the conftest hand back
    the throttle in the factory return tuple — would change a fixture
    shape used by 30+ other tests for a benefit only this one needs.
    """
    for router in dispatcher.sub_routers:
        if router.name == "main":
            for subrouter in router.sub_routers:
                if subrouter.name == "admin.rate_stats":
                    # The router has one message handler registered via
                    # ``Command(...)``. The callback is a closure that
                    # captured ``throttle`` as its second free var
                    # (see ``handle_admin_rate_stats(message, settings,
                    # throttle)``).
                    callback = subrouter.message.handlers[0].callback
                    closure = callback.__closure__
                    assert closure is not None
                    for cell in closure:
                        candidate = cell.cell_contents
                        if isinstance(candidate, ThrottlingMiddleware):
                            return candidate
    raise AssertionError("throttle middleware not found on dispatcher tree")


@pytest.mark.asyncio
async def test_admin_rate_stats_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)

    update = make_message_update("/admin_rate_stats", user_id=42)
    await dispatcher.feed_update(bot, update)

    assert sent == []


@pytest.mark.asyncio
async def test_admin_rate_stats_is_dropped_in_a_group_even_for_a_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Private-only router: the snapshot names throttled users (#408).

    ``_render`` prints raw user IDs of whoever is currently being rate
    limited. A developer running this in a shared group would publish
    that list to everyone in it, so the venue is gated separately from
    the reader.
    """
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_rate_stats",
            user_id=555,
            chat_id=-1001234567890,
            chat_type="supergroup",
        ),
    )

    assert sent == []


@pytest.mark.asyncio
async def test_admin_rate_stats_renders_for_developer_empty(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Fresh middleware → tracker is enabled but empty. Card must still
    render with the explicit "no users tracked yet" line — distinct
    from a missing section that would look like a render bug.
    """
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_rate_stats", user_id=555))

    assert len(sent) == 1
    text = sent[0]["text"]
    assert "Rate-limit snapshot" in text
    assert "capacity" in text
    assert "no users tracked yet" in text


@pytest.mark.asyncio
async def test_admin_rate_stats_lists_top_pressured_users(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Three users seeded with different remaining-token counts; the
    card must list the lowest-token user first, in ascending order.
    """
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)

    throttle = _find_throttle(dispatcher)
    # Inject buckets directly — ``_allow`` advances real time which is
    # noisy in tests. We pin the raw tokens we expect to see ranked.
    throttle._buckets[111] = (5.0, 0.0)
    throttle._buckets[222] = (0.5, 0.0)  # most pressured
    throttle._buckets[333] = (2.0, 0.0)

    await dispatcher.feed_update(bot, make_message_update("/admin_rate_stats", user_id=42))

    assert len(sent) == 1
    text = sent[0]["text"]
    # All three IDs appear.
    assert "222" in text
    assert "333" in text
    assert "111" in text
    # Ascending by remaining tokens.
    assert text.index("222") < text.index("333") < text.index("111")
    # Tracked-user count surfaces the table size.
    assert "<code>3</code>" in text


@pytest.mark.asyncio
async def test_admin_rate_stats_silent_when_user_field_missing(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Defence in depth — channel posts with no ``from`` field must not
    reach the developer branch. Same posture as ``/admin_status``.
    """
    from aiogram.types import Update

    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=111),
    )
    sent = capture_outgoing(bot)

    update = Update.model_validate(
        {
            "update_id": 7,
            "channel_post": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": -100, "type": "channel", "title": "C"},
                "text": "/admin_rate_stats",
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    assert sent == []
