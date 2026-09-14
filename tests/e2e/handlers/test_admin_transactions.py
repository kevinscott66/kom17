"""End-to-end ``/admin_transactions``.

Pins:

* Non-developer → silent drop.
* Developer + empty ledger → friendly "ledger is empty" line.
* Developer + rows → newest-first by ``id``, magnitude amount, formatted
  date, escaped reason/type.
* Cap at 10 (12-row ledger shows exactly 10 bullets).
* HTML-escape on ``reason`` (user-typed via /send) — load-bearing.
* NULL counterparty (system credit, e.g. daily bonus) renders as em-dash.
* LEGACY counterparty ``0`` renders as em-dash too (#1586) — legacy
  ``record_transaction`` spells the system side ``0``, not NULL
  (bot.py:10343-10344), and 95% of the production ledger uses that
  spelling.
* The two spellings of one spend render identically (#1586 + #1587):
  a legacy ``(user, 0, -25)`` row and a new ``(user, NULL, +25)`` row
  produce the same bullet body.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import Transaction
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(registry: EngineRegistry, rows: list[dict[str, Any]]) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(Transaction(**r))
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
        make_message_update("/admin_transactions", user_id=42, chat_type="private"),
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
        make_message_update("/admin_transactions", user_id=555, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Recent transactions" in text
    assert "Ledger is empty" in text


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
                "from_id": 100,
                "to_id": 200,
                "amount": 50,
                "reason": "transfer-a-canary",
                "type": "transfer",
                "date": datetime(2026, 1, 1, 12, 0),
            },
            {
                "from_id": 100,
                "to_id": None,
                "amount": -25,
                "reason": "shop-b-canary",
                "type": "shop",
                "date": datetime(2026, 1, 2, 13, 0),
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_transactions", user_id=777, chat_type="private"),
    )
    text = sent[0]["text"]
    # Magnitude only — direction is carried by the ``from → to`` arrow.
    # The legacy row below is stored negative and the new pipeline writes
    # the same spend positive, so echoing the raw sign would show two
    # identical spends differently.
    assert "50" in text
    assert "-25" not in text
    assert "25" in text
    assert "transfer-a-canary" in text
    assert "shop-b-canary" in text
    # Date formatted, not raw isoformat with microseconds.
    assert "2026-01-01 12:00" in text


@pytest.mark.asyncio
async def test_newest_first(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "from_id": 1,
                "to_id": 2,
                "amount": 10,
                "reason": "older-tx-canary",
                "type": "transfer",
                "date": datetime(2026, 1, 1),
            },
            {
                "from_id": 1,
                "to_id": 2,
                "amount": 20,
                "reason": "newer-tx-canary",
                "type": "transfer",
                "date": datetime(2026, 1, 2),
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_transactions", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # id=2 (newer) must appear before id=1 (older).
    assert text.index("newer-tx-canary") < text.index("older-tx-canary")


@pytest.mark.asyncio
async def test_capped_at_ten(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "from_id": 1,
                "to_id": 2,
                "amount": 1,
                "reason": f"r{i}",
                "type": "transfer",
                "date": datetime(2026, 1, 1),
            }
            for i in range(12)
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_transactions", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert text.count("\n• <code>#") == 10


@pytest.mark.asyncio
async def test_html_escape_on_reason(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "from_id": 1,
                "to_id": 2,
                "amount": 1,
                "reason": "<script>alert(1)</script> & more",
                "type": "transfer",
                "date": datetime(2026, 1, 1),
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_transactions", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp; more" in text


@pytest.mark.asyncio
async def test_null_counterparty_renders_as_dash(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """System credits (daily bonus, etc.) write NULL into from_id —
    the card must render those as em-dash, not crash and not show
    'None'. The legacy ledger has thousands of such rows."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "from_id": None,
                "to_id": 100,
                "amount": 100,
                "reason": "daily",
                "type": "daily",
                "date": datetime(2026, 1, 1),
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_transactions", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "None" not in text
    assert "— → <code>100</code>" in text


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
            "/admin_transactions",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"from_id": 0, "to_id": 100}, "— → <code>100</code>"),
        ({"from_id": 100, "to_id": 0}, "<code>100</code> → —"),
        ({"from_id": 0, "to_id": 0}, "— → —"),
    ],
    ids=["legacy-system-payer", "legacy-system-payee", "both-sides-system"],
)
async def test_legacy_zero_counterparty_renders_as_dash(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    row: dict[str, Any],
    expected: str,
) -> None:
    """#1586: legacy spells the system side ``0``, not NULL.

    ``record_transaction`` documents it (bot.py:10343-10344) and its
    writers pass it positionally (bot.py:14757, bot.py:14776). On
    production 1062 rows carry ``from_id=0`` and 227 carry ``to_id=0``
    against 44 and 12 NULLs, so handling only NULL rendered 95% of the
    ledger's system side as ``<code>0</code>`` — a plausible-looking
    user id an operator would go and look up. ``0`` is never a real
    user: ``user_id`` is the SQLite rowid (starts at 1) and
    ``economy.users`` has no such row.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                **row,
                "amount": 100,
                "reason": "zero-party-canary",
                "type": "daily",
                "date": datetime(2026, 1, 1),
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_transactions", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert expected in text
    assert "<code>0</code>" not in text


@pytest.mark.asyncio
async def test_both_spellings_of_one_spend_render_alike(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#1586 + #1587 together: the same 25-coin spend, written by the
    legacy pipeline as ``(100, 0, -25)`` and by the new one as
    ``(100, NULL, +25)``, must produce a byte-identical bullet body.

    That is the whole point of both fixes. With the ``0`` bug the legacy
    row read ``<code>100</code> → <code>0</code>``; with a signed amount
    it would read ``-25`` against the other row's ``25``. An operator
    comparing two rows of the same ledger would see a data bug that is
    not there.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "from_id": 100,
                "to_id": 0,
                "amount": -25,
                "reason": "spend-canary",
                "type": "shop",
                "date": datetime(2026, 1, 1, 12, 0),
            },
            {
                "from_id": 100,
                "to_id": None,
                "amount": 25,
                "reason": "spend-canary",
                "type": "shop",
                "date": datetime(2026, 1, 1, 12, 0),
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_transactions", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    bullets = [ln for ln in text.split("\n") if ln.startswith("• <code>#")]
    assert len(bullets) == 2, bullets
    # Drop the ``#<id></code> `` prefix — the row ids necessarily differ.
    bodies = {ln.split("</code> ", 1)[1] for ln in bullets}
    assert bodies == {
        "<code>100</code> → — <code>25</code> DLAB [<i>shop</i>] spend-canary (2026-01-01 12:00)"
    }
