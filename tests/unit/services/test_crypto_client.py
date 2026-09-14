"""``CryptoPayClient`` against a mocked Crypto Pay API (T-027).

Uses ``httpx.MockTransport`` so the real request-build + ``ok``/``error``
unwrap path is exercised; nothing is monkeypatched on the client.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from telegram_invite_bot.services.payments.crypto_client import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayInsufficientFunds,
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> CryptoPayClient:
    transport = httpx.MockTransport(handler)
    return CryptoPayClient("tok-123", client=httpx.AsyncClient(transport=transport))


async def test_create_invoice_returns_pay_url() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("Crypto-Pay-API-Token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "invoice_id": 555,
                    "pay_url": "https://t.me/CryptoBot?start=IV555",
                    "asset": "USDT",
                    "amount": "5",
                },
            },
        )

    client = _client(handler)
    result = await client.create_invoice(
        asset="USDT", amount="5", payload="100", description="Top up"
    )

    assert result.invoice_id == 555
    assert result.pay_url == "https://t.me/CryptoBot?start=IV555"
    assert result.asset == "USDT"
    assert str(seen["url"]).endswith("/createInvoice")
    assert seen["token"] == "tok-123"
    assert seen["body"] == {
        "asset": "USDT",
        "amount": "5",
        "payload": "100",
        "description": "Top up",
    }


async def test_transfer_success_returns_transfer_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "transfer_id": 9001,
                    "spend_id": "wd-42",
                    "asset": "USDT",
                    "amount": "5",
                },
            },
        )

    client = _client(handler)
    result = await client.transfer(
        user_id=100, asset="USDT", amount="5", spend_id="wd-42", comment="withdraw #42"
    )

    assert result.transfer_id == 9001
    assert result.spend_id == "wd-42"
    assert str(seen["url"]).endswith("/transfer")
    assert seen["body"] == {
        "user_id": 100,
        "asset": "USDT",
        "amount": "5",
        "spend_id": "wd-42",
        "comment": "withdraw #42",
    }


async def test_transfer_insufficient_app_wallet_raises_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {"code": 400, "name": "NOT_ENOUGH_COINS_TO_TRANSFER"},
            },
        )

    client = _client(handler)
    with pytest.raises(CryptoPayInsufficientFunds) as exc:
        await client.transfer(user_id=100, asset="USDT", amount="5", spend_id="wd-1")
    assert exc.value.name == "NOT_ENOUGH_COINS_TO_TRANSFER"


async def test_transfer_other_api_error_raises_generic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ok": False, "error": {"code": 400, "name": "SPEND_ID_TAKEN"}}
        )

    client = _client(handler)
    with pytest.raises(CryptoPayError) as exc:
        await client.transfer(user_id=100, asset="USDT", amount="5", spend_id="dup")
    assert not isinstance(exc.value, CryptoPayInsufficientFunds)
    assert exc.value.name == "SPEND_ID_TAKEN"


async def test_http_non_200_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = _client(handler)
    with pytest.raises(CryptoPayError):
        await client.create_invoice(asset="USDT", amount="5", payload="1")


async def test_transport_error_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    with pytest.raises(CryptoPayError):
        await client.transfer(user_id=1, asset="USDT", amount="5", spend_id="x")


async def test_ok_true_without_result_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    with pytest.raises(CryptoPayError):
        await client.transfer(user_id=1, asset="USDT", amount="5", spend_id="x")
