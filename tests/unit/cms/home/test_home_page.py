"""The page a visitor gets by typing the bare domain.

Until #130 the root returned a 404 — the guide answered on
``/commands``, the documents on ``/privacy`` and friends, and nothing
answered on ``/``. That is the address a compliance reviewer types by
hand and the address anyone gets by trimming a shared link, so the
properties defended here are the ones whose failure looks like "the site
is broken": a dead root, a leaked substitution token, a link to a page
this deployment does not serve, or two routes serving one language
because of a late-binding closure.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.home import build_router, render_home
from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.cms.paths import (
    commands_path,
    contact_path,
    doc_path,
    home_path,
)

LANGS = ("ru", "en")

#: Any ``[[...]]`` left in output is a token the renderer failed to
#: substitute — the one defect a reader is guaranteed to notice.
_TOKEN_RE = re.compile(r"\[\[[A-Z_]+\]\]")


def _ctx(**overrides: object) -> LegalContext:
    base: dict[str, object] = {
        "site_title": "ком17",
        "operator": "ком17",
        "operator_details": None,
        "support_url": "https://t.me/kom17_support",
        "support_email": "support@example.com",
        "bot_username": "kom17_bot",
        "url_prefix": "https://tgbot.delabs.space",
    }
    base.update(overrides)
    return LegalContext(**base)  # type: ignore[arg-type]


def _client(ctx: LegalContext | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(ctx or _ctx()))
    return TestClient(app)


# --- routes ---------------------------------------------------------


def test_both_routes_exist() -> None:
    paths = {route.path for route in build_router(_ctx()).routes}  # type: ignore[attr-defined]
    assert paths == {home_path(lang) for lang in LANGS}


def test_the_bare_root_answers() -> None:
    """The regression this whole module exists for."""
    assert _client().get("/").status_code == 200


@pytest.mark.parametrize("lang", LANGS)
def test_each_route_serves_its_own_language(lang: str) -> None:
    """The late-binding guard.

    Two routes registered in a loop close over the loop variable; without
    explicit default-arg binding both serve the English page, and a smoke
    test that hits ``/`` alone passes anyway.
    """
    response = _client().get(home_path(lang))
    assert response.status_code == 200
    assert f'<html lang="{lang}"' in response.text


def test_the_two_languages_differ() -> None:
    ru = _client().get(home_path("ru")).text
    en = _client().get(home_path("en")).text
    assert ru != en
    assert "Что это" in ru
    assert "What this is" in en


@pytest.mark.parametrize("lang", LANGS)
def test_no_placeholder_token_survives_rendering(lang: str) -> None:
    assert not _TOKEN_RE.findall(_client().get(home_path(lang)).text)


@pytest.mark.parametrize("lang", LANGS)
def test_the_page_names_the_service(lang: str) -> None:
    assert "ком17" in _client().get(home_path(lang)).text


# --- the links the page exists to carry ------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_all_three_documents_are_linked(lang: str) -> None:
    """A reviewer who lands on the root must reach the documents.

    In the reader's own language: sending someone from the Russian front
    page to the English offer is how a legal page gets read as "not
    actually ours".
    """
    html = _client().get(home_path(lang)).text
    for slug in ("privacy", "terms", "support"):
        assert f'href="{doc_path(slug, lang)}"' in html


@pytest.mark.parametrize("lang", LANGS)
def test_the_guide_is_linked_only_when_it_is_mounted(lang: str) -> None:
    """``GUIDE_SITE_ENABLED`` off means ``/commands`` 404s.

    Advertising it anyway would make the front page the source of the
    site's only broken link.
    """
    on = _client(_ctx(guide_enabled=True)).get(home_path(lang)).text
    off = _client(_ctx(guide_enabled=False)).get(home_path(lang)).text
    assert commands_path(lang) in on
    assert "/commands" not in off


@pytest.mark.parametrize("lang", LANGS)
def test_the_documents_list_carries_the_contact_form(lang: str) -> None:
    """The form belongs in the list headed "Documents", not only in the row.

    Its own copy names its audience as an acquiring bank, a regulator, a
    personal-data request or a vulnerability report — the reader this
    page exists for. The nav row alone reaches them only if they read
    the chrome; the list is what they came to the page to read.

    Asserted on the **site-relative** href, which is the form the body
    copy uses. The absolute one would pass on the nav row alone and say
    nothing about whether the bullet is there.
    """
    on = _client(_ctx(contact_enabled=True)).get(home_path(lang)).text
    off = _client(_ctx(contact_enabled=False)).get(home_path(lang)).text
    assert f'href="{contact_path(lang)}"' in on
    assert "/contact" not in off


def test_the_wordmark_leads_home_from_the_front_page_itself() -> None:
    assert 'class="brand" href="https://tgbot.delabs.space/"' in _client().get("/").text


def test_links_stay_relative_without_a_configured_origin() -> None:
    """Polling deployments have no ``WEBHOOK_URL``.

    The page must still work for someone already on it — the absolute
    form only matters when a URL is copied out and pasted elsewhere.
    """
    html = _client(_ctx(url_prefix="")).get("/").text
    assert 'href="/privacy"' in html
    assert "https://tgbot.delabs.space" not in html


def test_the_language_toggle_switches_this_page_and_not_a_document() -> None:
    html = _client().get("/").text
    assert 'href="https://tgbot.delabs.space/en"' in html
    assert 'href="https://tgbot.delabs.space/"' in html


# --- how it is served ------------------------------------------------


def test_pages_are_rendered_once_and_not_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated endpoint on a memory-capped shared host.

    Re-parsing the Markdown on every GET is cheap to trigger from outside
    and expensive inside, so the render must happen while the router is
    being built.
    """
    import telegram_invite_bot.cms.home.router as router_mod

    client = _client()

    def forbidden(ctx: LegalContext, lang: str) -> str:
        raise AssertionError("the front page was rendered while serving a request")

    monkeypatch.setattr(router_mod, "render_home", forbidden)
    for _ in range(3):
        assert client.get("/").status_code == 200


@pytest.mark.parametrize("lang", LANGS)
def test_pages_are_cacheable_at_the_edge(lang: str) -> None:
    headers = _client().get(home_path(lang)).headers
    assert "max-age" in headers.get("cache-control", "")


@pytest.mark.parametrize("lang", LANGS)
def test_head_is_answered_not_rejected(lang: str) -> None:
    """FastAPI does not derive HEAD from GET.

    Uptime monitors, link checkers and Telegram's preview fetcher all
    open with HEAD, and a 405 to any of them reads as a broken site.
    """
    response = _client().head(home_path(lang))
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] != "0"


@pytest.mark.parametrize("lang", LANGS)
def test_the_page_ships_its_own_content_policy(lang: str) -> None:
    policy = _client().get(home_path(lang)).headers["content-security-policy"]
    assert "unsafe-inline" not in policy
    assert "sha256-" in policy


def test_html_is_escaped_at_the_boundary() -> None:
    """The site title is operator-supplied and rendered into HTML."""
    html = _client(_ctx(site_title="<script>alert(1)</script>")).get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --- markup the page's own dialect makes easy to get wrong -----------


@pytest.mark.parametrize("lang", LANGS)
def test_list_items_do_not_spill_into_stray_paragraphs(lang: str) -> None:
    """One source line = one block in this markdown dialect.

    A soft-wrapped bullet publishes its tail as a ``<p>`` wedged between
    the list items — legible in a diff only if you know to look, and
    obvious on the page.
    """
    html = render_home(_ctx(), lang)
    assert "</li>\n<p>" not in html


@pytest.mark.parametrize("lang", LANGS)
def test_headings_do_not_skip_a_level(lang: str) -> None:
    """The hero supplies the page's single ``<h1>``.

    Sections are written with one ``#``, which renders as ``<h2>``;
    writing ``##`` instead would emit an ``<h3>`` with no ``<h2>`` above
    it and break heading navigation for screen-reader users.
    """
    html = render_home(_ctx(), lang)
    body = html[html.index('<article class="guide') :]
    assert "<h2" in body
    assert "<h3" not in body
