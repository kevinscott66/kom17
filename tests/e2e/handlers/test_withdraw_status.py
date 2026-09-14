"""End-to-end ``/withdraw_status``.

Pins:

* Empty state → friendly "no requests" line.
* Returns ONLY the caller's rows (other users' rows must not leak).
* Newest-first ordering by ``id`` DESC.
* Cap at 10 — an 11-row history shows exactly 10 lines.
* Status is localised, not printed raw (#245(a)); an unrecognised
  status still falls back to the escaped column.
* Currency HTML-escaped (defence-in-depth).
* Works in group chats too — user-scoped query, no privacy hazard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import WithdrawalRequest
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(registry: EngineRegistry, rows: list[dict[str, Any]]) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(WithdrawalRequest(**r))
        await session.commit()


@pytest.mark.asyncio
async def test_empty_state(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw_status", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Ваши заявки на вывод" in text
    assert "нет заявок" in text


@pytest.mark.asyncio
async def test_card_answers_in_the_callers_language(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The whole card — header, empty line and the per-row template —
    was Russian literals, so an English user asking "did my withdrawal
    clear?" got an answer they couldn't read. Both branches (empty and
    populated) are checked: the empty one is its own return path.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/withdraw_status", user_id=42, chat_type="private", language_code="en"
        ),
    )
    empty = sent[0]["text"]
    assert "Your withdrawal requests" in empty
    assert "no withdrawal requests" in empty
    assert not any("Ѐ" <= ch <= "ӿ" for ch in empty), empty

    await _seed(
        registry,
        [
            {
                "user_id": 42,
                "amount_com": 1000,
                "amount_fiat": 1050,
                "currency": "RUB",
                "status": "pending",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent.clear()
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/withdraw_status", user_id=42, chat_type="private", language_code="en"
        ),
    )
    filled = sent[0]["text"]
    assert "Your withdrawal requests" in filled
    # The row still carries the data, not just the translated chrome.
    assert "10.50" in filled and "pending" in filled
    assert not any("Ѐ" <= ch <= "ӿ" for ch in filled), filled


@pytest.mark.asyncio
async def test_lists_only_callers_rows(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 42,
                "amount_com": 1000,
                "amount_fiat": 1050,
                "currency": "RUB",
                "status": "pending",
                "created_at": "2026-01-01",
            },
            {
                "user_id": 42,
                "amount_com": 2000,
                "amount_fiat": 2000,
                "currency": "RUB",
                "status": "completed",
                "created_at": "2026-01-02",
            },
            # Different user — must NOT leak into caller's card.
            {
                "user_id": 99,
                "amount_com": 9999,
                "amount_fiat": 9999,
                "currency": "RUB",
                "status": "pending",
                "payment_details": "other-user-leak-canary",
                "created_at": "2026-01-03",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw_status", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    # #245(a): the card shows localised status words, not the raw
    # ``withdrawal_requests.status`` column.
    assert "ожидает" in text
    assert "выплачена" in text
    assert "10.50" in text
    assert "20.00" in text
    # Cross-user isolation — the whole point of the user_id scope.
    assert "other-user-leak-canary" not in text
    assert "99.99" not in text


@pytest.mark.asyncio
async def test_newest_first(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """id DESC matches the question users actually ask first — "did
    my latest one go through?". Pin the order so a future refactor
    that switches to ``id ASC`` (or sorts on the TEXT created_at)
    fails loudly."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 7,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "status": "first-row",
                "created_at": "2026-01-01",
            },
            {
                "user_id": 7,
                "amount_com": 200,
                "amount_fiat": 200,
                "currency": "RUB",
                "status": "last-row",
                "created_at": "2026-01-02",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw_status", user_id=7, chat_type="private")
    )
    text = sent[0]["text"]
    # Newest (id=2, "last-row") must appear before oldest (id=1).
    assert text.index("last-row") < text.index("first-row")


@pytest.mark.asyncio
async def test_cap_at_ten(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 1,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "status": "pending",
                "created_at": f"2026-01-{i + 1:02d}",
            }
            for i in range(11)
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw_status", user_id=1, chat_type="private")
    )
    text = sent[0]["text"]
    # Exactly 10 bullet lines regardless of history size.
    assert text.count("\n• <code>#") == 10


@pytest.mark.asyncio
async def test_html_escape_on_currency(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 1,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "<b>X</b>",
                "status": "pending&special",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw_status", user_id=1, chat_type="private")
    )
    text = sent[0]["text"]
    assert "<b>X</b>" not in text
    assert "&lt;b&gt;X&lt;/b&gt;" in text
    assert "pending&amp;special" in text


@pytest.mark.asyncio
async def test_works_in_group_chat(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """Legacy answers in groups too — the query is user-scoped, so
    rendering in a group doesn't leak other users' withdrawals. Pin
    this so a future "tighten to private" change is a conscious
    decision, not a drive-by."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 42,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "status": "pending",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/withdraw_status",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert sent and "ожидает" in sent[0]["text"]


@pytest.mark.asyncio
async def test_the_two_spellings_of_a_refusal_read_as_one_word(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#245(a): one event, two stored literals, one word on the card.

    Legacy's admin reject wrote ``'cancelled'`` (``bot.py:20754``); this
    port's writes ``'rejected'``. Prod carries a row of each era, so
    before this fix a user scrolling their own history saw two different
    words for the same thing and had no way to know it wasn't two
    different things.

    Both rows are seeded together on purpose: asserting each spelling
    renders *something* would pass with two labels as easily as with
    one. The claim is that the card contains exactly one distinct
    refusal word, and the raw literals are gone from it.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 42,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "status": "rejected",
                "created_at": "2026-01-01",
            },
            {
                "user_id": 42,
                "amount_com": 200,
                "amount_fiat": 200,
                "currency": "RUB",
                "status": "cancelled",
                "created_at": "2026-01-02",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw_status", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]

    assert text.count("отклонена") == 2
    assert "rejected" not in text
    assert "cancelled" not in text


@pytest.mark.asyncio
async def test_an_unknown_status_is_still_shown_rather_than_swallowed(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The map is a lookup, not a whitelist.

    ``status`` is operator-set free text, so a value nobody anticipated
    must stay legible — escaped, but present. A ``KeyError`` or a bare
    dash would turn "we didn't translate this" into "your request has no
    status", which is the more alarming of the two lies.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 42,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "status": "on_hold_manual_review",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/withdraw_status", user_id=42, chat_type="private")
    )

    assert "on_hold_manual_review" in sent[0]["text"]


@pytest.mark.asyncio
async def test_the_english_card_reconciles_the_two_spellings_too(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """A second locale is where a half-applied mapping shows up.

    ``'rejected'`` is *also* the English label, so an English card would
    look correct even if ``'cancelled'`` fell through to the raw column.
    Seeding only the legacy spelling is what makes this test able to
    fail: the word has to come from the catalogue, not from the row.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed(
        registry,
        [
            {
                "user_id": 42,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "status": "cancelled",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/withdraw_status", user_id=42, chat_type="private", language_code="en"
        ),
    )
    text = sent[0]["text"]

    assert "rejected" in text
    assert "cancelled" not in text
