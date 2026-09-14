"""The page every wrong address gets.

Until #182 a miss answered ``{"detail":"Not Found"}`` as
``application/json`` — no navigation row, no wordmark, no way back. The
misses are not hypothetical: the English documents live on
``/privacy/en``, so ``/en/privacy`` is the first thing an English reader
tries, and ``/commands/en`` existing makes ``/commands/ru`` look like it
should too.

The properties defended here are the ones whose failure is invisible in
a smoke test: an API client silently served HTML instead of the JSON it
has always parsed, a page marked ``aria-current`` on a row it is not in,
a leaked substitution token, and a page whose own policy is missing so
the middleware's ``style-src 'none'`` fallback strips it to bare text.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms import notfound
from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.cms.paths import home_path

LANGS = ("ru", "en")

_TOKEN_RE = re.compile(r"\[\[[A-Z_]+\]\]")

_BROWSER_ACCEPT: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

#: The same header as a ready-made ``headers=`` mapping.
_H: dict[str, str] = {"Accept": _BROWSER_ACCEPT}


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
    notfound.install(app, ctx or _ctx())
    return TestClient(app)


# --- language ------------------------------------------------------


@pytest.mark.parametrize("path", ["/privacy/en", "/en/privacy", "/en", "/commands/en/"])
def test_an_en_segment_anywhere_in_the_path_is_english(path: str) -> None:
    """Both orders count.

    ``/privacy/en`` is the real English URL and ``/en/privacy`` is the
    mirror-image guess that brought the reader here in the first place;
    a reader who typed either was reading English.
    """
    assert notfound.language_for(path) == "en"


@pytest.mark.parametrize("path", ["/enterprise", "/en-gb/privacy", "/tender", "/"])
def test_a_word_that_merely_starts_with_en_is_not_english(path: str) -> None:
    """The guard against a prefix test.

    ``startswith("/en")`` would read ``/enterprise`` as English and hand
    a Russian reader an English apology.
    """
    assert notfound.language_for(path) == "ru"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("en", "en"),
        ("en-GB,en;q=0.9", "en"),
        ("EN-US", "en"),
        ("ru-RU,ru;q=0.9,en;q=0.8", "ru"),
        ("de", "ru"),
        ("", "ru"),
    ],
)
def test_the_header_decides_when_the_path_says_nothing(header: str, expected: str) -> None:
    """Only the first-listed language counts.

    A Russian browser lists ``en`` as a lower-weighted fallback almost
    always; treating a mere mention as English would serve the English
    page to most of the audience.
    """
    assert notfound.language_for("/nope", header) == expected


def test_the_path_outranks_the_header() -> None:
    """A typed URL is evidence about this reader; the header is a default."""
    assert notfound.language_for("/en/privacy", "ru-RU,ru;q=0.9") == "en"


# --- content negotiation -------------------------------------------


@pytest.mark.parametrize("accept", [_BROWSER_ACCEPT, "text/html", "TEXT/HTML"])
def test_a_browser_is_offered_html(accept: str) -> None:
    assert notfound.wants_html(accept) is True


@pytest.mark.parametrize("accept", ["*/*", "application/json", "", "text/plain"])
def test_everyone_else_keeps_json(accept: str) -> None:
    """The same app serves the Telegram webhook and three payment callbacks.

    Those clients send ``*/*`` or ``application/json``; handing them a
    page of HTML where a JSON body used to be would break parsing on a
    path nobody watches.
    """
    assert notfound.wants_html(accept) is False


# --- the page ------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_page_declares_its_language(lang: str) -> None:
    assert f'<html lang="{lang}"' in notfound.render_not_found(_ctx(), lang)


@pytest.mark.parametrize("lang", LANGS)
def test_no_token_survives_rendering(lang: str) -> None:
    assert not _TOKEN_RE.search(notfound.render_not_found(_ctx(), lang))


@pytest.mark.parametrize("lang", LANGS)
def test_the_way_back_is_a_link(lang: str) -> None:
    """The one thing the JSON blob did not offer."""
    assert f'href="{home_path(lang)}"' in notfound.render_not_found(_ctx(), lang)


@pytest.mark.parametrize("lang", LANGS)
def test_the_navigation_row_is_there(lang: str) -> None:
    """The apology is only useful if it also shows where the pages are."""
    page = notfound.render_not_found(_ctx(guide_enabled=True, contact_enabled=True), lang)
    assert '<nav class="docnav"' in page
    assert "/privacy" in page
    assert "/contact" in page


@pytest.mark.parametrize("lang", LANGS)
def test_nothing_in_the_row_is_marked_current(lang: str) -> None:
    """This page is not in the row, so no entry may claim to be it.

    ``site_nav_html`` marks the current page with ``aria-current`` and
    the stylesheet greys it out as unclickable. Passing any real page
    here would tell a screen reader the reader is on a page they are
    not, and grey out the very link they need.

    Sliced to the documents row specifically. The shell's inline
    ``<style>`` carries an ``aria-current`` selector, so counting over
    the whole page would pass on the stylesheet alone — and the shell
    opens with a *language* ``<nav>`` that never marks anything, so
    slicing at the first ``<nav>`` would pass on that one instead.
    """
    page = notfound.render_not_found(_ctx(guide_enabled=True, contact_enabled=True), lang)
    start = page.index('<nav class="docnav"')
    row = page[start : page.index("</nav>", start)]
    assert "aria-current" not in row


@pytest.mark.parametrize("lang", LANGS)
def test_the_row_omits_what_the_deployment_does_not_serve(lang: str) -> None:
    """An apology that hands the reader a second dead link is worse than none."""
    page = notfound.render_not_found(_ctx(guide_enabled=False, contact_enabled=False), lang)
    assert "/commands" not in page
    assert "/contact" not in page


def test_the_two_languages_do_not_share_copy() -> None:
    ru = notfound.render_not_found(_ctx(), "ru")
    en = notfound.render_not_found(_ctx(), "en")
    assert "Такого адреса нет" in ru
    assert "Такого адреса нет" not in en
    assert "No such address" in en
    assert "No such address" not in ru


@pytest.mark.parametrize("lang", ["EN", "en-GB", "de", ""])
def test_an_unknown_language_falls_back_to_russian(lang: str) -> None:
    """``render_not_found`` is public; it must not render a third language."""
    page = notfound.render_not_found(_ctx(), lang)
    expected = "en" if lang == "EN" else "ru"
    assert f'<html lang="{expected}"' in page


def test_two_deployments_do_not_share_a_cached_page() -> None:
    """The page is cached; the cache is keyed by context as well as language.

    Rendering is cached because a 404 is the one response a stranger can
    ask for without limit. Keying it by language alone would be a real
    bug in tests and in any process that builds two apps: the second
    deployment would serve the first one's navigation row, advertising
    pages it does not mount.

    Asserted through the handler, because that is the path the cache is
    on; ``render_not_found`` is uncached and would pass either way.
    """
    with_form = _client(_ctx(contact_enabled=True)).get("/nope", headers=_H).text
    without = _client(_ctx(contact_enabled=False)).get("/nope", headers=_H).text
    assert "/contact" in with_form
    assert "/contact" not in without


# --- the handler ---------------------------------------------------


def test_a_reader_gets_the_page() -> None:
    response = _client().get("/en/privacy", headers={"Accept": _BROWSER_ACCEPT})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "No such address" in response.text


def test_an_api_client_keeps_the_json_body() -> None:
    """The regression that would be invisible until a payment callback broke."""
    response = _client().get("/nope", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_the_default_client_still_gets_json() -> None:
    """``curl`` and every HTTP library send ``*/*`` unless told otherwise."""
    assert _client().get("/nope").json() == {"detail": "Not Found"}


def test_a_miss_is_not_cached() -> None:
    """The edge must not serve this page for an address that later exists."""
    response = _client().get("/nope", headers={"Accept": _BROWSER_ACCEPT})
    assert response.headers["cache-control"] == "no-store"


def test_the_page_carries_its_own_policy() -> None:
    """Without it the middleware's ``style-src 'none'`` fallback applies.

    The shell styles itself from an inline ``<style>`` block, so a page
    that leaves the policy to the fallback arrives as unstyled text —
    which looks exactly like the broken site the reader already thinks
    they found.
    """
    response = _client().get("/nope", headers={"Accept": _BROWSER_ACCEPT})
    policy = response.headers["content-security-policy"]
    assert "sha256-" in policy
    assert "style-src 'none'" not in policy


def test_other_statuses_keep_the_framework_handler() -> None:
    """The handler is registered for the exception class, not for 404.

    Every other status — a 405 on ``HEAD``, a 403 from the metrics guard
    — must keep answering as it did, or #131's fix regresses silently.
    """
    app = FastAPI()

    @app.get("/only-get")
    async def _only_get() -> dict[str, str]:
        return {"ok": "yes"}

    notfound.install(app, _ctx())
    response = TestClient(app).post("/only-get", headers={"Accept": _BROWSER_ACCEPT})
    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}
