"""Bounded reads of third-party HTTP responses (#119).

The defect these pin: ``await client.get(url)`` reads the whole body
into memory before returning, and httpx's read timeout does not bound
it — a peer trickling gigabytes stays inside the per-chunk deadline the
whole way down. One process serves every chat, so an OOM there is a
full outage caused by one upstream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from telegram_invite_bot.utils.http_read import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ResponseTooLarge,
    send_capped,
)


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_body_under_the_cap_comes_back_intact() -> None:
    """The happy path must be indistinguishable from ``client.get`` —
    status, decoded JSON and the content type all survive the rebuild."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "value": 42})

    async with _client(handler) as client:
        response = await send_capped(client, "GET", "https://example.test/api")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "value": 42}
    assert response.headers["content-type"].startswith("application/json")


async def test_oversized_body_raises_instead_of_being_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    async with _client(handler) as client:
        with pytest.raises(ResponseTooLarge):
            await send_capped(client, "GET", "https://example.test/api", max_bytes=4096)


async def test_a_chunked_body_with_no_length_is_still_bounded() -> None:
    """The dangerous shape: ``Transfer-Encoding: chunked`` declares no
    size at all, so nothing before the read can tell how much is coming.
    The cap has to be enforced *during* streaming, not from a header."""
    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(1000):
            sent += 1
            yield b"y" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    async with _client(handler) as client:
        with pytest.raises(ResponseTooLarge):
            await send_capped(client, "GET", "https://example.test/stream", max_bytes=8 * 1024)

    # Abandoned early: we must not have pulled the whole 1 MB down just
    # to discover it was too big.
    assert sent <= 10, sent


async def test_the_cap_is_inclusive_at_the_boundary() -> None:
    """``max_bytes`` bytes is fine; one more is not. Pinned because an
    off-by-one here turns a legitimate exact-size payload into a
    permanent outage for whichever integration produces it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"z" * 1024)

    async with _client(handler) as client:
        exact = await send_capped(client, "GET", "https://example.test/api", max_bytes=1024)
        assert len(exact.content) == 1024
        with pytest.raises(ResponseTooLarge):
            await send_capped(client, "GET", "https://example.test/api", max_bytes=1023)


async def test_too_large_is_an_httpx_error_so_call_sites_already_catch_it() -> None:
    """Every integration wraps its request in ``except httpx.HTTPError``
    and degrades. The whole point of the inheritance is that #119 needed
    no new failure path in eight modules."""
    assert issubclass(ResponseTooLarge, httpx.HTTPError)


async def test_request_keywords_are_forwarded() -> None:
    """``params``/``json``/``headers`` have to reach the wire — the
    helper builds the request itself, so a dropped kwarg would silently
    send a different request than the call site wrote."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = request.content
        seen["header"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        await send_capped(
            client,
            "POST",
            "https://example.test/api",
            params={"q": "moscow"},
            json={"a": 1},
            headers={"X-Api-Key": "secret"},
        )

    assert seen["method"] == "POST"
    assert seen["url"] == "https://example.test/api?q=moscow"
    assert seen["body"] == b'{"a":1}'
    assert seen["header"] == "secret"


async def test_default_cap_is_generous_enough_for_real_payloads() -> None:
    """The largest body the bot legitimately reads is a full fiat rate
    table, tens of kilobytes. The default must not be tight enough to
    turn a normal day into a degraded one."""
    assert DEFAULT_MAX_RESPONSE_BYTES >= 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}" + b" " * (256 * 1024))

    async with _client(handler) as client:
        response = await send_capped(client, "GET", "https://example.test/rates")
    assert response.status_code == 200


async def test_the_message_names_the_host_not_the_whole_url() -> None:
    """#540: this exception is logged verbatim by its call sites, and
    ``currency_service`` builds a URL that carries the exchangerate API
    key in the *path* — so quoting ``url`` wrote a live credential into
    journald the first time that upstream misbehaved. The host is what
    makes the log line useful; the rest of the URL never is."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    async with _client(handler) as client:
        with pytest.raises(ResponseTooLarge) as excinfo:
            await send_capped(
                client,
                "GET",
                "https://example.test/v6/secret-api-key/latest/RUB",
                max_bytes=4096,
            )

    message = str(excinfo.value)
    assert "secret-api-key" not in message
    assert "example.test" in message
