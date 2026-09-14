"""Security headers on every response the app produces.

The gap this closes was visible from outside: a GET of the front page
came back with ``content-type``, ``cache-control``, ``vary`` and nothing
else. What makes it worth a test file rather than a one-line assertion
is *where* the headers have to survive — an error raised inside a
handler, a response a route built by hand, a HEAD with no body — because
the way header middleware usually fails is by covering the happy path
and quietly missing the rest.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.csp import FALLBACK_CSP
from telegram_invite_bot.webhook.http_headers import BASE_HEADERS, SecurityHeadersMiddleware

_OWN_CSP = "default-src 'none'; script-src 'sha256-abc'"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.api_route("/html", methods=["GET", "HEAD"])
    async def html() -> HTMLResponse:
        return HTMLResponse("<p>hi</p>")

    @app.get("/html-with-policy")
    async def html_with_policy() -> HTMLResponse:
        return HTMLResponse("<p>hi</p>", headers={"Content-Security-Policy": _OWN_CSP})

    @app.get("/json")
    async def json_route() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/boom")
    async def boom() -> JSONResponse:
        raise HTTPException(status_code=404)

    @app.get("/framed")
    async def framed() -> PlainTextResponse:
        # A route that deliberately states its own value for one of the
        # headers — the middleware must defer to it.
        return PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    return TestClient(app)


@pytest.mark.parametrize("name", [name for name, _ in BASE_HEADERS])
@pytest.mark.parametrize("path", ["/html", "/json", "/boom", "/framed"])
def test_every_response_carries_every_base_header(client: TestClient, path: str, name: str) -> None:
    assert name.lower() in {k.lower() for k in client.get(path).headers}


def test_the_values_are_the_ones_a_reviewer_looks_for(client: TestClient) -> None:
    headers = client.get("/html").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in headers["strict-transport-security"]


def test_a_route_that_states_its_own_value_keeps_it(client: TestClient) -> None:
    """Otherwise the middleware becomes a thing to work around."""
    assert client.get("/framed").headers["x-frame-options"] == "SAMEORIGIN"


def test_headers_survive_an_exception_raised_in_the_handler(client: TestClient) -> None:
    response = client.get("/boom")
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"


# --- the content policy ----------------------------------------------


def test_html_without_a_policy_gets_the_locked_down_fallback(client: TestClient) -> None:
    assert client.get("/html").headers["content-security-policy"] == FALLBACK_CSP


def test_a_page_that_brought_its_own_policy_keeps_it(client: TestClient) -> None:
    """The real pages compute a hash policy; overwriting it would break them."""
    assert client.get("/html-with-policy").headers["content-security-policy"] == _OWN_CSP


def test_non_html_gets_no_content_policy(client: TestClient) -> None:
    """A policy on a JSON body protects nothing and confuses log readers."""
    assert "content-security-policy" not in client.get("/json").headers


def test_head_carries_the_same_headers_as_get(client: TestClient) -> None:
    """Monitors probe with HEAD; a scanner that only sees HEAD must not
    conclude the headers are missing."""
    head = client.head("/html")
    get = client.get("/html")
    assert head.status_code == 200
    assert head.content == b""
    for name, _ in BASE_HEADERS:
        assert head.headers[name] == get.headers[name]
    assert head.headers["content-security-policy"] == get.headers["content-security-policy"]
