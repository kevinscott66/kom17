"""End-to-end ``/admin_withdrawals``.

Pins:

* Non-developer → silent drop.
* Developer + empty DB → ``pending: 0`` and NO fiat-total line.
* Developer + rows → counts, fiat-sum, oldest-first sample.
* Sample capped at 5 (6 pending rows → exactly 5 bullets).
* ``payment_details`` HTML-escaped (user-typed → load-bearing).
* ``payment_details`` truncated past 20 chars (matches legacy shape).
* #172 — a card-shaped ``payment_details`` renders masked (BIN +
  last 4) in all three separator spellings, while a phone or a
  crypto address stays readable.
* #169 ageing — per-row age, ⚠️ past the threshold, queue-wide stale
  counter, and ``?`` (never ⚠️, never an invented age) for a row whose
  ``created_at`` is missing or unparseable.
* #236 — a port-written row (``amount_crypto`` set, ``amount_fiat``
  NULL, ``currency`` = the ASSET) renders its payout figure instead of
  ``—``, and the structurally-zero fiat total is not printed.
* #283 — a ``processing`` row is listed with the ⏳ marker, counted
  separately in the header, and carries NO ✅/🚫 buttons.
* #1400 — a payee with a reversed payment on record is marked, and the
  row beside it is not.
* Group invocation → router-level private filter rejects (UNHANDLED).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import ProcessedWebhook, WithdrawalRequest
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.utils.time import db_now
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(
    registry: EngineRegistry,
    rows: list[dict[str, Any]],
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(WithdrawalRequest(**r))
        await session.commit()


async def _seed_webhooks(
    registry: EngineRegistry,
    rows: list[dict[str, Any]],
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(ProcessedWebhook(**r))
        await session.commit()


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_empty(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=555, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Pending withdrawals" in text
    assert "<code>0</code>" in text
    # #236: the total sums ``amount_fiat``, a column the port's own
    # writer never fills — so it is printed only when it means
    # something. An empty queue means nothing to total.
    assert "fiat total" not in text
    # No sample block when count == 0.
    assert "Latest" not in text


@pytest.mark.asyncio
async def test_renders_with_rows(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=777),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 1000,
                "amount_fiat": 1050,
                "currency": "RUB",
                "payment_details": "card 1234",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00",
            },
            {
                "user_id": 200,
                "amount_com": 2000,
                "amount_fiat": 2025,
                "currency": "RUB",
                "payment_details": "wallet abc",
                "status": "pending",
                "created_at": "2026-01-02T00:00:00",
            },
            # A non-pending row must NOT contribute to the count or sum.
            {
                "user_id": 300,
                "amount_com": 9999,
                "amount_fiat": 99999,
                "currency": "RUB",
                "payment_details": "should-not-appear",
                "status": "completed",
                "created_at": "2026-01-03T00:00:00",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=777, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "pending: <code>2</code>" in text
    # 10.50 + 20.25 = 30.75 — completed row excluded.
    assert "30.75" in text
    assert "card 1234" in text
    assert "wallet abc" in text
    assert "should-not-appear" not in text


@pytest.mark.asyncio
async def test_sample_capped_at_five(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=111),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100 + i,
                "amount_com": 1000,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": f"d{i}",
                "status": "pending",
                "created_at": f"2026-01-{i + 1:02d}T00:00:00",
            }
            for i in range(6)
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=111, chat_type="private"),
    )
    text = sent[0]["text"]
    # Count is the truth (6), sample is capped at 5.
    assert "pending: <code>6</code>" in text
    assert text.count("\n  • ") == 5


@pytest.mark.asyncio
async def test_escapes_payment_details(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 1,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": "<b>x</b>&y",
                "status": "pending",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "<b>x</b>" not in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "&amp;y" in text


@pytest.mark.asyncio
async def test_truncates_long_details(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """Legacy renders ``details[:20] + "…"`` when over 20 chars — pin
    the boundary so an operator comparing the new card against legacy
    sees the same shape."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    long_details = "x" * 30
    await _seed(
        registry,
        [
            {
                "user_id": 1,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": long_details,
                "status": "pending",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # 20 x's followed by an ellipsis; the 21st char must NOT appear as
    # part of the truncated string.
    assert ("x" * 20 + "…") in text
    assert ("x" * 21) not in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_withdrawals",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


@pytest.mark.asyncio
async def test_paging_navigates_and_clamps(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    """L-97 paging: 7 pending rows → page 1/2 with a ▶️ button; the
    page callback edits the card to page 2/2 (the remaining 2 rows);
    an out-of-range page from a stale button clamps to the last page."""
    from telegram_invite_bot.keyboards.builders.withdrawals import WithdrawPage
    from tests.e2e.handlers.conftest import make_callback_update

    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=111),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100 + i,
                "amount_com": 1000,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": f"d{i}",
                "status": "pending",
                "created_at": f"2026-01-{i + 1:02d}T00:00:00",
            }
            for i in range(7)
        ],
    )
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=111, chat_type="private"),
    )
    text = sink[0]["text"]
    assert "Page 1/2" in text
    assert text.count("\n  • ") == 5

    # ▶️ to page 2 — the card is edited in place with the last 2 rows.
    await dispatcher.feed_update(
        bot, make_callback_update(WithdrawPage(page=1).pack(), user_id=111)
    )
    edits = [e for e in sink if e["kind"] == "edit"]
    assert edits, "page callback must edit the card"
    assert "Page 2/2" in edits[-1]["text"]
    assert edits[-1]["text"].count("\n  • ") == 2

    # A stale ▶️ pointing past the end clamps to the last page.
    await dispatcher.feed_update(
        bot, make_callback_update(WithdrawPage(page=99).pack(), user_id=111)
    )
    edits = [e for e in sink if e["kind"] == "edit"]
    assert "Page 2/2" in edits[-1]["text"]


@pytest.mark.asyncio
async def test_single_page_has_no_nav(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """≤5 pending rows → the legacy "Latest N" header and no page nav."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=111),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 1000,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": "d",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=111, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Latest 1" in text
    assert "Page" not in text


@pytest.mark.asyncio
async def test_ageing_marks_only_overdue_rows(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """A fresh row shows a bare age; an overdue one is flagged, and the
    stale counter is queue-wide so it stays true on every page."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=321),
    )
    now = db_now()
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 10,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": "fresh",
                "status": "pending",
                "created_at": (now - timedelta(hours=2)).isoformat(sep=" ", timespec="seconds"),
            },
            {
                "user_id": 101,
                "amount_com": 20,
                "amount_fiat": 200,
                "currency": "RUB",
                "payment_details": "old",
                "status": "pending",
                "created_at": (now - timedelta(days=9)).isoformat(sep=" ", timespec="seconds"),
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=321, chat_type="private"),
    )
    text = sent[0]["text"]

    assert "stale (&gt;24h): <code>1</code>" in text
    fresh_line = next(ln for ln in text.splitlines() if "fresh" in ln)
    old_line = next(ln for ln in text.splitlines() if "| old |" in ln)
    assert fresh_line.endswith("| 2h")
    assert "⚠️" not in fresh_line
    assert old_line.endswith("| ⚠️ 9d")


@pytest.mark.asyncio
async def test_clean_queue_renders_no_stale_line(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """A clean queue must read as clean — no ``stale: 0`` line to parse."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=322),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 10,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": "fresh",
                "status": "pending",
                "created_at": db_now().isoformat(sep=" ", timespec="seconds"),
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=322, chat_type="private"),
    )

    assert "stale" not in sent[0]["text"]


@pytest.mark.asyncio
async def test_unusable_created_at_renders_a_question_mark(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Legacy imports really do carry NULL/garbage timestamps. Inventing
    an age would read as *fresh*; inventing a ⚠️ would send the operator
    after a timestamp that does not exist."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=323),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 10,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": "nulldate",
                "status": "pending",
                "created_at": None,
            },
            {
                "user_id": 101,
                "amount_com": 20,
                "amount_fiat": 200,
                "currency": "RUB",
                "payment_details": "junkdate",
                "status": "pending",
                "created_at": "not-a-timestamp",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=323, chat_type="private"),
    )
    text = sent[0]["text"]

    for marker in ("nulldate", "junkdate"):
        line = next(ln for ln in text.splitlines() if marker in ln)
        assert line.endswith("| ?")
        assert "⚠️" not in line
    # Unknown age is not counted as stale — the alert takes the same
    # position, so the two surfaces agree.
    assert "stale" not in text


@pytest.mark.parametrize(
    "details",
    [
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        # Free text around the number — the shape legacy rows actually
        # have, and the one a whole-string check would have missed.
        "Сбербанк 4111111111111111",
    ],
)
@pytest.mark.asyncio
async def test_a_card_number_never_renders_in_full(
    make_wired: WiredFactory, capture_outgoing: Any, details: str
) -> None:
    """#172. ``_truncate`` cuts at 20 and a PAN is 16, so the card used
    to print whole — into a message that stays in the operator's chat
    history and survives any forward or screenshot of it.

    The spellings are one row on purpose: the mask scans for a
    card-length digit run anywhere in the value, so neither a separator
    nor a bank name typed in front of the number may buy the card a way
    past it.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 1,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": details,
                "status": "pending",
                "created_at": "2026-01-01",
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # Neither the raw spelling nor the separator-stripped one.
    assert details not in text
    assert "4111111111111111" not in text
    # The BIN survives and is followed by bullets, so the operator can
    # still tell two queued cards apart. How many bullets — and whether
    # the last 4 fits — depends on how much room the surrounding text
    # leaves before the 20-char truncation, so that is not asserted.
    assert "411111•" in text


@pytest.mark.asyncio
async def test_details_that_are_not_a_card_stay_readable(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The mask is aimed at PANs, not at every string with digits in it.

    A crypto address and an SBP phone are payout *instruments* the
    operator reads off this card, and neither is a secret the way a card
    number is — bulleting them would cost readability and buy nothing.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 1,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "RUB",
                "payment_details": "+79991234567",
                "status": "pending",
                "created_at": "2026-01-01",
            },
            {
                "user_id": 2,
                "amount_com": 100,
                "amount_fiat": 100,
                "currency": "USDT",
                "payment_details": "TQn9Y2kh",
                "status": "pending",
                "created_at": "2026-01-02",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "+79991234567" in text
    assert "TQn9Y2kh" in text
    # Not a bare «•» check: the card bullets its own list rows with one.
    # A mask is always at least two bullets wide (12 digits minimum, 10
    # of them kept), so a run of two is the unambiguous marker.
    assert "••" not in text


@pytest.mark.asyncio
async def test_port_written_row_renders_its_crypto_amount(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#236 — the shape ``WithdrawalsRepo.create`` actually writes.

    Every other test in this file seeds ``amount_fiat`` + a fiat
    ``currency`` + ``payment_details``, which is a LEGACY row: the
    port's own writer sets ``amount_crypto`` and ``currency`` = the
    asset and touches neither of the other two. Before #236 the card
    read those rows off ``amount_fiat`` alone and rendered the payout
    as ``—``, plus a header claiming a fiat total of ``0.00``.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=606),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 4500,
                "amount_crypto": 4.5,
                "currency": "USDT",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=606, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "pending: <code>1</code>" in text
    # The credit side is named, and named exactly: no trailing zeros,
    # no scientific notation, no "0.00" standing in for "unknown".
    assert "<code>4.5</code> USDT" in text
    # And the header does not report a measured zero for a column that
    # is simply absent.
    assert "fiat total" not in text


@pytest.mark.asyncio
async def test_tiny_crypto_amount_is_not_rendered_as_zero(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#236 — a sub-satoshi dust row must not read as ``0``.

    ``0.000000004 USDT`` rounds to eight decimals as ``0.00000000``.
    Printing that as ``0`` would tell an operator a request is free to
    close; ``~0`` says "smaller than we render", which is the truth.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=607),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 1,
                "amount_crypto": 0.000000004,
                "currency": "TON",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=607, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "<code>~0</code> TON" in text


@pytest.mark.asyncio
async def test_processing_row_is_listed_without_buttons(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#283 — a stranded auto-payout lease must be visible.

    ``claim_processing`` flips ``pending`` → ``processing`` before the
    provider call and nothing releases it on a timeout, so a process
    that dies mid-transfer used to strand a real request — user's coins
    already in escrow — in a status no admin surface listed at all.

    It gets no ✅/🚫: ``approve_manual`` and ``reject`` both guard on
    ``pending``, so those buttons could only ever report failure.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=608),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 1000,
                "amount_crypto": 1.0,
                "currency": "USDT",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00",
            },
            {
                "user_id": 200,
                "amount_com": 2000,
                "amount_crypto": 2.0,
                "currency": "USDT",
                "status": "processing",
                "created_at": "2026-01-02T00:00:00",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=608, chat_type="private"),
    )
    text = sent[0]["text"]
    # Both rows listed, but counted apart — a leased row is not queue
    # work an operator can pick up.
    assert text.count("\n  • ") == 2
    assert "pending: <code>1</code>" in text
    assert "processing (leased): <code>1</code>" in text
    assert "⏳ <code>#2</code>" in text
    # The pending row keeps its plain bullet.
    assert "  • <code>#1</code>" in text

    markup = sent[0]["markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["✅ #1", "🚫 #1"]


@pytest.mark.asyncio
async def test_processing_only_queue_still_renders(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#283 — the whole queue leased is the case worth surviving.

    ``pending: 0`` used to be the ONLY thing this card could say when
    every request was stuck in ``processing``: an empty-looking panel
    over a queue of real money. The pager counts the listed queue, not
    the pending subset, so the rows must still render.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=609),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 1000,
                "amount_crypto": 1.0,
                "currency": "USDT",
                "status": "processing",
                "created_at": "2026-01-01T00:00:00",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=609, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "pending: <code>0</code>" in text
    assert "processing (leased): <code>1</code>" in text
    assert "Latest 1" in text
    # No buttons at all → no keyboard rather than an empty one.
    assert sent[0]["markup"] is None


@pytest.mark.asyncio
async def test_a_chargeback_is_marked_on_the_row_that_has_one(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#1400 — the operator sees the reversal instead of inferring it.

    ``lifetime_deposits`` already subtracts a charged-back top-up from
    the R2 threshold and the R6 cap, so the arithmetic can no longer be
    refilled by top-up-then-chargeback. This card is the other half: a
    human presses \u2705 here, and a shrunken headroom figure is not a
    legible way to learn that the money went back.

    The marker is per user, so the clean row beside it must stay clean —
    a flag that lands on every row is a flag nobody reads. A settled
    webhook row (no ``reversed_at``) is not a chargeback either.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=610),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "amount_com": 1000,
                "amount_fiat": 1050,
                "currency": "RUB",
                "payment_details": "clean-payee",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00",
            },
            {
                "user_id": 200,
                "amount_com": 2000,
                "amount_fiat": 2025,
                "currency": "RUB",
                "payment_details": "reversed-payee",
                "status": "pending",
                "created_at": "2026-01-02T00:00:00",
            },
        ],
    )
    await _seed_webhooks(
        registry,
        [
            {
                "provider": "crypto",
                "external_id": "inv-settled",
                "user_id": 100,
                "credited_amount": 9_000,
                "processed_at": db_now(),
            },
            {
                "provider": "crypto",
                "external_id": "inv-reversed",
                "user_id": 200,
                "credited_amount": 9_000,
                "processed_at": db_now(),
                "reversed_at": db_now(),
                "reversed_event": "invoice_reversed",
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_withdrawals", user_id=610, chat_type="private"),
    )
    lines = sent[0]["text"].splitlines()
    clean = next(line for line in lines if "clean-payee" in line)
    flagged = next(line for line in lines if "reversed-payee" in line)
    assert "chargeback" not in clean
    assert flagged.endswith("\u26a0\ufe0f chargeback")
