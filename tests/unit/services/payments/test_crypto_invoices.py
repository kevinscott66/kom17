"""Unit tests for the user-side Crypto Pay invoice service (A1: L-81).

Pins the degraded-mode contract (missing token → typed
``NOT_CONFIGURED``, never an exception) and the createInvoice wire
format (user_id rides the invoice ``payload`` — the credit-routing
contract with ``CryptoAdapter.parse_event``).

#1232/#1236 added the denomination half: what the wire format says
``amount`` is measured in, and a round trip proving the coins the
button promises are the coins the webhook credits.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from telegram_invite_bot.services.payments.crypto import CryptoAdapter
from telegram_invite_bot.services.payments.crypto_client import CryptoPayClient
from telegram_invite_bot.services.payments.crypto_invoices import (
    COINS_PER_USD,
    CRYPTO_USD_AMOUNTS,
    TOPUP_ASSETS,
    CryptoTopupService,
    TopupInvoiceOutcome,
)


def _created(**result: Any) -> httpx.Response:
    """A minimal ``ok:true`` createInvoice response."""
    return httpx.Response(
        200,
        json={
            "ok": True,
            "result": {
                "invoice_id": 321,
                "pay_url": "https://t.me/CryptoBot?start=IVxyz",
                **result,
            },
        },
    )


def _refused() -> httpx.Response:
    """The provider deciding against us before anything was minted."""
    return httpx.Response(
        200, json={"ok": False, "error": {"code": 400, "name": "CURRENCY_TYPE_INVALID"}}
    )


def _service_with_transport(
    token: str | None, handler: Any
) -> tuple[CryptoTopupService, list[httpx.Request]]:
    """Service whose HTTP layer is an httpx MockTransport."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_record)
    http_client = httpx.AsyncClient(transport=transport)

    async def _resolver() -> str | None:
        return token

    service = CryptoTopupService(
        _resolver,
        client_factory=lambda tok: CryptoPayClient(tok, client=http_client),
    )
    return service, seen


@pytest.mark.asyncio
async def test_missing_token_degrades_without_api_call() -> None:
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when unconfigured")

    service, seen = _service_with_transport(None, _boom)
    assert await service.available() is False
    invoice = await service.create_invoice(user_id=7, asset="USDT", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.NOT_CONFIGURED
    assert invoice.pay_url is None
    assert seen == []


@pytest.mark.asyncio
async def test_create_invoice_sends_user_id_payload_and_returns_url() -> None:
    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "invoice_id": 321,
                    "pay_url": "https://t.me/CryptoBot?start=IVxyz",
                    "asset": "USDT",
                    "amount": "5",
                },
            },
        )

    service, seen = _service_with_transport("tok-1", _ok)
    assert await service.available() is True
    invoice = await service.create_invoice(user_id=42, asset="USDT", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.CREATED
    assert invoice.pay_url == "https://t.me/CryptoBot?start=IVxyz"
    assert invoice.invoice_id == 321

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path.endswith("/createInvoice")
    assert request.headers["Crypto-Pay-API-Token"] == "tok-1"
    body = json.loads(request.content)
    # The payload IS the credit-routing contract: CryptoAdapter reads
    # the user_id back from ``payload.payload`` on invoice_paid.
    assert body["payload"] == "42"
    assert body["amount"] == "5"
    # #1232: "5" only means five dollars if the invoice says so. A bare
    # ``asset`` here would have Crypto Pay bill five USDT — right by
    # accident for a stablecoin, and five bitcoin one button over.
    assert body["currency_type"] == "fiat"
    assert body["fiat"] == "USD"
    assert body["accepted_assets"] == "USDT"
    assert "asset" not in body


@pytest.mark.asyncio
async def test_api_error_maps_to_failed_not_raise() -> None:
    def _err(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error": {"code": 400, "name": "ASSET_INVALID"}}
        )

    service, _ = _service_with_transport("tok-1", _err)
    invoice = await service.create_invoice(user_id=42, asset="DOGE", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.FAILED


@pytest.mark.asyncio
async def test_missing_pay_url_maps_to_failed() -> None:
    def _no_url(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"invoice_id": 1}})

    service, _ = _service_with_transport("tok-1", _no_url)
    invoice = await service.create_invoice(user_id=42, asset="USDT", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.FAILED


def test_constants_mirror_legacy() -> None:
    # bot.py:18053-18061 (assets) / bot.py:18334 (USD amounts) /
    # payments/rates.py ``COINS_PER_USD`` (rate parity with the credit
    # side — the button must promise what the webhook pays).
    assert TOPUP_ASSETS == ("USDT", "BTC", "TON")
    assert CRYPTO_USD_AMOUNTS == (5, 10, 25, 50)
    assert COINS_PER_USD == 900


@pytest.mark.asyncio
async def test_bitcoin_invoice_is_priced_in_dollars_not_in_bitcoin() -> None:
    """#1232, the case that made this a live-money bug.

    ``TOPUP_ASSETS`` offers BTC beside USDT, and the amount rows are
    labelled ``$5``/``$10``/``$25``/``$50``. Minted with a bare
    ``asset``, that ``$5`` row produced an invoice for FIVE BITCOIN.
    USDT hid it for the whole strangler window by being worth a dollar.
    """
    service, seen = _service_with_transport("tok-1", lambda _r: _created(asset="BTC"))
    invoice = await service.create_invoice(user_id=42, asset="BTC", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.CREATED
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["currency_type"] == "fiat"
    assert body["fiat"] == "USD"
    assert body["accepted_assets"] == "BTC"
    assert body["amount"] == "5"


@pytest.mark.asyncio
async def test_a_refused_fiat_invoice_retries_a_stablecoin_in_asset_units() -> None:
    """The fallback that keeps the leg which works in production today.

    The fiat form is the new one and this path is live, so an outright
    refusal must not take the whole method down with the change that
    was meant to fix it. A dollar stablecoin is within rounding of the
    dollar, so asset units are an acceptable second choice for it.
    """
    responses = [_refused(), _created(asset="USDT", amount="5")]

    def _handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    service, seen = _service_with_transport("tok-1", _handler)
    invoice = await service.create_invoice(user_id=42, asset="USDT", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.CREATED
    assert len(seen) == 2
    retry = json.loads(seen[1].content)
    assert retry["asset"] == "USDT"
    assert "currency_type" not in retry


@pytest.mark.asyncio
async def test_a_refused_fiat_invoice_is_not_retried_for_bitcoin() -> None:
    """No fallback where the fallback is the bug.

    Retrying BTC in asset units is exactly what #1232 describes, so a
    refusal has to stay a refusal: a correctly-priced invoice or none.
    """
    service, seen = _service_with_transport("tok-1", lambda _r: _refused())
    invoice = await service.create_invoice(user_id=42, asset="BTC", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.FAILED
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_unreadable_outcome_is_never_retried() -> None:
    """A 5xx may land after the invoice was minted.

    ``CryptoPayUnconfirmed`` is the "we don't know" answer, and the
    stablecoin fallback must not turn it into a second invoice the user
    might be handed while the first is already payable.
    """
    service, seen = _service_with_transport(
        "tok-1", lambda _r: httpx.Response(502, text="bad gateway")
    )
    invoice = await service.create_invoice(user_id=42, asset="USDT", amount_usd=5, coins=4500)
    assert invoice.outcome is TopupInvoiceOutcome.FAILED
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", TOPUP_ASSETS)
@pytest.mark.parametrize("usd", CRYPTO_USD_AMOUNTS)
async def test_coins_promised_on_the_button_are_the_coins_credited(asset: str, usd: int) -> None:
    """#1236: the round trip neither half-test could see.

    The button label, the minted invoice body and the crediting webhook
    were each pinned in isolation, and #1232 satisfied all three while
    being catastrophically wrong — the label promised dollars, the
    invoice billed asset units, and the webhook then multiplied by that
    asset's dollar rate. Only a loop catches it, so this mints through
    the real service and feeds the provider's own paid-invoice shape
    back to the real adapter.

    ``paid_usd_rate`` is deliberately absurd: on a fiat invoice it
    describes whichever coin the payer settled in and must not touch
    the credit at all.
    """
    service, seen = _service_with_transport("tok-1", lambda _r: _created())
    invoice = await service.create_invoice(
        user_id=42, asset=asset, amount_usd=usd, coins=usd * COINS_PER_USD
    )
    assert invoice.outcome is TopupInvoiceOutcome.CREATED
    minted = json.loads(seen[0].content)

    paid = json.dumps(
        {
            "update_type": "invoice_paid",
            "payload": {
                "invoice_id": "INV-RT",
                "payload": minted["payload"],
                "currency_type": minted["currency_type"],
                "fiat": minted["fiat"],
                "amount": minted["amount"],
                "paid_asset": minted["accepted_assets"],
                "paid_usd_rate": "104235.5",
            },
        }
    ).encode()
    event = CryptoAdapter("tok-1").parse_event(paid)
    assert event is not None
    assert event.user_id == 42
    assert event.coins == usd * COINS_PER_USD
