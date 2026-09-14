"""#1959: the order book's currency filter comes off the wire unchecked.

``P2pOrderBook.currency`` is documented as ``""`` or one of
``P2P_CURRENCIES`` (``keyboards/builders/p2p.py``), and every button the
bot draws honours that. aiogram's ``CallbackData`` validates the *type*
of the field, not its value, so a hand-rolled client can send anything
without a separator in it — and ``_render_order_list`` interpolated the
result straight into an HTML message:

    text = t("p2p_no_orders", lang) + f" ({currency})"
    header += f" — {currency}"

while the neighbouring ``_order_line`` escapes its free text. The blast
radius is small (the card is the sender's own), which is why this is a
hygiene fix rather than an incident — but the invariant the builder
declares should be enforced where the wire crosses into the handler,
the way ``handle_express_currency`` already does one screen over.

Enforcing it also fixes a plain bug: the filter is compared against
``fiat_currency``, which ``P2pService.create_order`` stores upper-cased,
so a lower-cased currency used to match nothing at all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from telegram_invite_bot.handlers import p2p_trade
from telegram_invite_bot.keyboards.builders.p2p import P2pOrderBook

if TYPE_CHECKING:
    from telegram_invite_bot.services.p2p_service import P2pService


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Recorder for ``_edit_or_answer`` — the real one needs a live Message."""
    seen: list[str] = []

    async def _record(target: object, text: str, markup: object = None) -> None:
        seen.append(text)

    monkeypatch.setattr(p2p_trade, "_edit_or_answer", _record)
    return seen


@pytest.fixture
def service() -> Any:
    return SimpleNamespace(order_book=AsyncMock(return_value=[]))


async def _book(service: Any, currency: str) -> list[Any]:
    """Drive the handler with a forged callback; return its acks."""
    acks: list[Any] = []
    callback = SimpleNamespace(
        message=None,
        answer=AsyncMock(side_effect=lambda *a, **k: acks.append((a, k))),
    )
    await p2p_trade.handle_order_book(
        cast("Any", callback),
        P2pOrderBook(currency=currency, page=0),
        cast("P2pService", service),
        "ru",
    )
    return acks


async def test_a_forged_currency_never_reaches_the_card(service: Any, rendered: list[str]) -> None:
    """The injection itself: ``<b>`` used to land in an HTML message."""
    acks = await _book(service, "<b>pwn</b>")

    assert rendered == []
    assert service.order_book.await_count == 0
    assert acks == [((), {})]  # silent bail, same shape as express currency


async def test_a_nonsense_currency_does_not_query_the_book(service: Any) -> None:
    """Not markup, just not a currency — same answer."""
    await _book(service, "XYZ")

    assert service.order_book.await_count == 0


async def test_a_supported_currency_still_filters(service: Any, rendered: list[str]) -> None:
    """The half that must not change."""
    await _book(service, "USD")

    assert service.order_book.await_args.kwargs["currency"] == "USD"
    assert "USD" in rendered[0]


async def test_a_lower_cased_currency_now_matches_the_column(service: Any) -> None:
    """``fiat_currency`` is stored upper-cased, so the filter must be too."""
    await _book(service, "usd")

    assert service.order_book.await_args.kwargs["currency"] == "USD"


async def test_the_unfiltered_view_is_still_reachable(service: Any, rendered: list[str]) -> None:
    """``""`` is the documented "no filter" value, not an invalid one."""
    await _book(service, "")

    assert service.order_book.await_args.kwargs["currency"] is None
    assert rendered != []
