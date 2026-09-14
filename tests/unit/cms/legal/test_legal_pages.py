"""The three public documents an acquiring bank has to be able to open.

These pages are not a feature — they are a precondition for taking
money. So the properties defended here are the ones whose failure a
reviewer would see as "this project is not ready": a page that renders
half a document, a placeholder token that leaked into published prose, a
contact block with a dead link in it, or six routes that all serve the
same page because of a late-binding closure.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.cms.legal.documents import (
    BY_SLUG,
    DOCUMENTS,
    REVISION,
    LegalDoc,
    build_contacts_md,
    render_body,
)
from telegram_invite_bot.cms.legal.router import build_router
from telegram_invite_bot.cms.paths import doc_path

LANGS = ("ru", "en")

#: Any ``[[...]]`` left in output is a token the renderer failed to
#: substitute — the one defect a reader is guaranteed to notice.
_TOKEN_RE = re.compile(r"\[\[[A-Z_]+\]\]")


def _ctx(**overrides: object) -> LegalContext:
    base: dict[str, object] = {
        "site_title": "ком17",
        "operator": "ИП Иванов Иван Иванович",
        "operator_details": "ИНН 000000000000",
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


# --- documents ------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("doc", DOCUMENTS, ids=lambda d: d.slug)
def test_every_document_has_a_title_and_a_body(doc: LegalDoc, lang: str) -> None:
    assert doc.title(lang).strip()
    assert len(doc.body(lang).strip()) > 500, "a stub is worse than no document"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("doc", DOCUMENTS, ids=lambda d: d.slug)
def test_no_placeholder_token_survives_rendering(doc: LegalDoc, lang: str) -> None:
    body = render_body(
        doc,
        lang,
        service="ком17",
        operator="ИП Иванов",
        contacts_md=build_contacts_md(
            lang,
            support_url="https://t.me/x",
            support_email="a@b.ru",
            operator="ИП Иванов",
            operator_details=None,
        ),
    )
    leaked = _TOKEN_RE.findall(body)
    assert not leaked, f"unsubstituted token(s) in {doc.slug}/{lang}: {leaked}"


@pytest.mark.parametrize("lang", LANGS)
def test_contacts_always_name_the_in_bot_ticket_system(lang: str) -> None:
    """The one contact route that exists in every deployment.

    A bank accepts a ticket system OR a handle OR an email; with none of
    the optional env vars set, the ticket system must still be printed,
    or an unconfigured deploy would publish a contacts section with no
    contacts in it.
    """
    md = build_contacts_md(
        lang, support_url=None, support_email=None, operator="ком17", operator_details=None
    )
    assert "/support" in md
    assert "t.me" not in md, "no support handle configured — no dead t.me link"
    assert "mailto" not in md


def test_terms_state_the_lifetime_payout_cap() -> None:
    """R6 in prose.

    The withdrawal desk caps lifetime payout at lifetime deposits. That
    is the single rule that separates this from an unlicensed gambling
    payout, and a user who is refused a withdrawal must be able to find
    it written down beforehand — otherwise the refusal is a dispute, and
    disputes are what the acquirer is underwriting.
    """
    body = BY_SLUG["terms"].body("ru")
    assert "не может превышать" in body
    assert "пополнени" in body


# --- routes ---------------------------------------------------------


def test_all_six_routes_exist() -> None:
    paths = {route.path for route in build_router(_ctx()).routes}  # type: ignore[attr-defined]
    assert paths == {doc_path(d.slug, lang) for d in DOCUMENTS for lang in LANGS}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("slug", [d.slug for d in DOCUMENTS])
def test_each_route_serves_its_own_document(slug: str, lang: str) -> None:
    """The late-binding guard.

    Registering six routes in a nested loop closes over the loop
    variables; without explicit default-arg binding every path serves the
    *last* document in the last language, and a smoke test that hits one
    URL passes anyway. Hitting all six and asserting each carries its own
    title is what actually catches it.
    """
    response = _client().get(doc_path(slug, lang))
    assert response.status_code == 200
    html = response.text
    assert f"<title>{BY_SLUG[slug].title(lang)}" in html
    for other in DOCUMENTS:
        if other.slug != slug:
            assert f"<title>{other.title(lang)}" not in html


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("slug", [d.slug for d in DOCUMENTS])
def test_rendered_pages_leak_no_tokens_and_carry_the_revision(slug: str, lang: str) -> None:
    html = _client().get(doc_path(slug, lang)).text
    assert not _TOKEN_RE.findall(html)
    assert REVISION in html


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("slug", [d.slug for d in DOCUMENTS])
def test_headings_do_not_skip_a_level(slug: str, lang: str) -> None:
    """The masthead supplies the page's single ``<h1>``.

    Sections are written with one ``#``, which renders as ``<h2>``;
    writing ``##`` instead would emit an ``<h3>`` with no ``<h2>`` above
    it and break heading navigation for screen-reader users. The same
    property the home page defends, on the pages a bank actually opens.
    """
    html = _client().get(doc_path(slug, lang)).text
    # The masthead's own ``<h1>`` is the page's only one. (Counting it
    # over the whole document would also match the stylesheet, whose
    # comment explains this very rule.)
    assert html[html.index('class="wrap masthead"') :].count("<h1>") == 1
    body = html[html.index('<article class="guide') :]
    assert "<h2" in body
    assert "<h3" not in body


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("slug", [d.slug for d in DOCUMENTS])
def test_the_table_of_contents_lists_the_sections(slug: str, lang: str) -> None:
    """The other half of the heading fix.

    ``extract_headings`` drops level 1 by default — that is the guide's
    document title. A legal document has no title line of its own, so
    the same default would silently empty the TOC the moment the
    sections became ``#``; the router passes ``min_level=1``.
    """
    html = _client().get(doc_path(slug, lang)).text
    toc = html[html.index('<details class="toc"') :]
    toc = toc[: toc.index("</details>")]
    body = html[html.index('<article class="guide') :]
    # One row per section, no more and no fewer.
    assert toc.count("<li ") == body.count("<h2")
    assert toc.count("<li ") >= 4
    # Every entry must point somewhere: an anchor-less row is a link to
    # the top of the page dressed up as navigation.
    assert 'href="#"' not in toc


def test_language_switch_points_at_the_same_document() -> None:
    """The RU/EN toggle must stay on the page the reader is on."""
    html = _client().get(doc_path("terms", "ru")).text
    assert 'href="https://tgbot.delabs.space/terms/en"' in html
    assert 'href="https://tgbot.delabs.space/terms"' in html
    # The sibling-document nav stays in the reader's language: from the
    # Russian offer, "privacy policy" must lead to the Russian policy.
    assert 'href="https://tgbot.delabs.space/privacy"' in html
    assert "/privacy/en" not in html


def test_links_stay_relative_without_a_configured_origin() -> None:
    """Polling deployments have no ``WEBHOOK_URL``.

    The pages must still work for someone already on them — the absolute
    form only matters when the URL is pasted into a bank's form.
    """
    html = _client(_ctx(url_prefix="")).get(doc_path("privacy", "ru")).text
    assert 'href="/privacy/en"' in html
    assert "https://tgbot.delabs.space" not in html


def test_operator_falls_back_to_the_site_title_and_never_to_an_empty_name() -> None:
    html = _client(_ctx(operator="", operator_details=None)).get(doc_path("terms", "ru")).text
    assert not _TOKEN_RE.findall(html)
    assert "ком17" in html


def test_pages_are_rendered_once_and_not_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """These are unauthenticated endpoints on a memory-capped box.

    Re-parsing the Markdown on every GET is cheap to trigger from
    outside and expensive inside, so the render must happen while the
    router is being built. Building the client first and then making a
    render *impossible* is what keeps that from silently regressing into
    a per-request render during a later refactor.
    """
    import telegram_invite_bot.cms.legal.router as router_mod

    client = _client()

    def forbidden(ctx: LegalContext, doc: LegalDoc, lang: str) -> str:
        raise AssertionError("a document was rendered while serving a request")

    monkeypatch.setattr(router_mod, "render_document", forbidden)
    for _ in range(3):
        assert client.get(doc_path("terms", "ru")).status_code == 200


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("slug", [d.slug for d in DOCUMENTS])
def test_pages_are_cacheable_at_the_edge(slug: str, lang: str) -> None:
    """The origin shares a small host with other services.

    Cloudflare does not cache HTML unless the response says so, so
    without this header every reader costs an origin request.
    """
    headers = _client().get(doc_path(slug, lang)).headers
    assert "max-age" in headers.get("cache-control", "")


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("slug", [d.slug for d in DOCUMENTS])
def test_head_is_answered_not_rejected(slug: str, lang: str) -> None:
    """These are the URLs handed to a bank.

    FastAPI does not derive HEAD from GET, and an automated link check
    that opens with HEAD would report all six documents as unreachable.
    """
    response = _client().head(doc_path(slug, lang))
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] != "0"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("slug", [d.slug for d in DOCUMENTS])
def test_the_page_ships_its_own_content_policy(slug: str, lang: str) -> None:
    """A scanner run against these pages is part of onboarding."""
    policy = _client().get(doc_path(slug, lang)).headers["content-security-policy"]
    assert "unsafe-inline" not in policy
    assert "sha256-" in policy


def test_html_is_escaped_at_the_boundary() -> None:
    """Operator name is free-form operator input, rendered into HTML."""
    html = _client(_ctx(operator="<script>alert(1)</script>")).get(doc_path("terms", "ru")).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
