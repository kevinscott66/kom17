"""No public CMS route may accept a request parameter it did not ask for.

The four public routers — legal, home, contact and discovery — build
their pages once and then serve constants. None of their handlers wants
a query string, and only the contact POST wants a body, which it reads
itself from the ``Request``. So the safe state is simple to state and
simple to check: FastAPI must find *nothing* to bind on any of them.

It was not the state we were in. Three of the routers bound their loop
variables with defaulted parameters::

    async def _page(_html: str = page, _headers: dict[str, str] = headers) -> HTMLResponse:

FastAPI inspects handler signatures and turns every parameter into a
request parameter, so ``_html`` became a query parameter and ``_headers``
a body field. ``GET /privacy?_html=<h1>PWNED</h1>`` answered 200 with
that markup as the entire page and ``Cache-Control: public, max-age=3600``
on it — attacker-authored, edge-cacheable content on the domain handed
to the acquiring bank (#1584).

This file pins the shape rather than the symptom. A future author who
reaches for a defaulted parameter to carry a loop variable — the obvious
move, and the one three routers independently made — fails here instead
of in production.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.contact.form import FIELD_MESSAGE, FIELD_REPLY_TO
from telegram_invite_bot.cms.contact.router import build_router as build_contact_router
from telegram_invite_bot.cms.discovery import build_router as build_discovery_router
from telegram_invite_bot.cms.home.router import build_router as build_home_router
from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.cms.legal.router import build_router as build_legal_router

_INJECTED = "<h1>PWNED</h1>"


def _ctx() -> LegalContext:
    return LegalContext(
        site_title="ком17",
        operator="оператор",
        operator_details="",
        support_url="https://t.me/kom17_support",
        support_email="support@example.com",
        bot_username="kom17_bot",
        url_prefix="https://tgbot.delabs.space",
    )


async def _deliver(_text: str) -> None:
    """Stand-in for the operator DM; never called by these tests."""


def _routers() -> dict[str, APIRouter]:
    ctx = _ctx()
    return {
        "legal": build_legal_router(ctx),
        "home": build_home_router(ctx),
        "contact": build_contact_router(ctx, deliver=_deliver),
        "discovery": build_discovery_router(ctx),
    }


def _routes() -> list[tuple[str, str, APIRoute]]:
    return [
        (name, f"{sorted(route.methods or [])} {route.path}", route)
        for name, router in _routers().items()
        for route in router.routes
        if isinstance(route, APIRoute)
    ]


@pytest.mark.parametrize(("router_name", "label", "route"), _routes(), ids=str)
def test_public_cms_routes_bind_no_request_parameters(
    router_name: str, label: str, route: APIRoute
) -> None:
    """Every public route must expose an empty binding surface.

    ``dependant`` is what FastAPI derived from the handler's signature,
    so this is the authoritative answer to "what can a caller set?" —
    not a guess from reading the source.
    """
    dependant = route.dependant
    assert dependant.query_params == [], (router_name, label, dependant.query_params)
    assert dependant.body_params == [], (router_name, label, dependant.body_params)
    assert dependant.header_params == [], (router_name, label, dependant.header_params)
    assert dependant.cookie_params == [], (router_name, label, dependant.cookie_params)
    assert dependant.path_params == [], (router_name, label, dependant.path_params)


@pytest.mark.parametrize("path", ["/privacy", "/privacy/en", "/terms", "/support"])
def test_query_string_cannot_replace_a_legal_page(path: str) -> None:
    """The exact request that used to return attacker markup (#1584)."""
    app = FastAPI()
    app.include_router(build_legal_router(_ctx()))
    with TestClient(app) as client:
        response = client.get(path, params={"_html": _INJECTED})

    assert response.status_code == 200
    assert _INJECTED not in response.text
    assert "ком17" in response.text


def test_query_string_cannot_replace_the_front_page() -> None:
    """Same request against ``/``, the other prerendered surface."""
    app = FastAPI()
    app.include_router(build_home_router(_ctx()))
    with TestClient(app) as client:
        response = client.get("/", params={"_html": _INJECTED})

    assert response.status_code == 200
    assert _INJECTED not in response.text


def test_contact_post_language_is_not_caller_controlled() -> None:
    """The submitter must not choose the language of the operator's DM.

    ``_lang`` used to be a defaulted parameter on the POST handler, so
    ``?_lang=en`` restamped the notification the operator receives. The
    delivered text is captured here rather than the response body: the
    response is what the sender sees, the notification is what the
    operator sees, and only the second one was ever in doubt.
    """
    delivered: list[str] = []

    async def capture(text: str) -> None:
        delivered.append(text)

    app = FastAPI()
    app.include_router(build_contact_router(_ctx(), deliver=capture))
    with TestClient(app) as client:
        response = client.post(
            "/contact",
            params={"_lang": "en"},
            data={
                FIELD_MESSAGE: "проверка связи, сообщение достаточной длины",
                FIELD_REPLY_TO: "@kom17_owner",
            },
        )

    assert response.status_code == 200
    assert len(delivered) == 1
    assert "Язык страницы:</b> RU" in delivered[0], delivered[0][:200]
