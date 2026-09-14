"""The Content-Security-Policy the site's pages carry.

The policy permits inline blocks *by hash*, which buys a real property —
a ``<script>`` the renderer did not produce cannot run, whatever
escaping bug let it onto the page — but only while every hash is
correct. A policy whose hashes do not match the page it ships with does
not fail loudly: the browser silently drops the stylesheet and the page
arrives unstyled, or drops the script and the search box stops
filtering. So the load-bearing test here re-derives the hashes from the
rendered HTML with a *different* parser than the one the module uses,
and would catch the regex drifting away from the markup.
"""

from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser

import pytest

from telegram_invite_bot.cms.csp import FALLBACK_CSP, csp_for_html


class _InlineBlocks(HTMLParser):
    """Collect inline ``<script>``/``<style>`` bodies via html.parser.

    Deliberately not the module's regex: two implementations that agree
    is evidence, one implementation checked against itself is not.
    ``HTMLParser`` switches to raw-text mode inside these two elements
    on its own, so ``handle_data`` receives the body verbatim — which is
    exactly what CSP hashes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.styles: list[str] = []
        #: Tags carrying a ``style="..."`` attribute — see the test at
        #: the bottom of this module for why they matter.
        self.styled_tags: list[str] = []
        self._kind: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._kind = tag
        if any(name == "style" for name, _ in attrs):
            self.styled_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._kind:
            self._kind = None

    def handle_data(self, data: str) -> None:
        if self._kind == "script":
            self.scripts.append(data)
        elif self._kind == "style":
            self.styles.append(data)


def _sha256(text: str) -> str:
    return f"'sha256-{base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()}'"


def _directive(policy: str, name: str) -> str:
    for part in policy.split("; "):
        if part.startswith(f"{name} "):
            return part
    raise AssertionError(f"{name} missing from {policy!r}")


# --- the directives that do not depend on the page -------------------


@pytest.mark.parametrize(
    "directive",
    [
        "default-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ],
)
def test_the_page_may_fetch_nothing_and_be_framed_nowhere(directive: str) -> None:
    assert directive in csp_for_html("<p>hi</p>").split("; ")


def test_nothing_inline_is_allowed_by_keyword() -> None:
    """``'unsafe-inline'`` would give up the entire point of hashing."""
    assert "unsafe-inline" not in csp_for_html("<style>a{}</style>")
    assert "unsafe-inline" not in FALLBACK_CSP


def test_a_page_with_no_inline_blocks_denies_both() -> None:
    policy = csp_for_html("<p>plain</p>")
    assert _directive(policy, "script-src") == "script-src 'none'"
    assert _directive(policy, "style-src") == "style-src 'none'"


def test_no_directive_is_ever_left_empty() -> None:
    """An empty value makes the whole policy unparseable — i.e. absent."""
    for policy in (csp_for_html(""), FALLBACK_CSP):
        for part in policy.split("; "):
            assert len(part.split(" ", 1)) == 2, part


# --- hashing ---------------------------------------------------------


def test_a_block_is_hashed_into_its_own_directive() -> None:
    html = "<style>body{color:red}</style><script>var a = 1;</script>"
    policy = csp_for_html(html)
    assert _sha256("body{color:red}") in _directive(policy, "style-src")
    assert _sha256("var a = 1;") in _directive(policy, "script-src")
    # And not into the other one.
    assert _sha256("body{color:red}") not in _directive(policy, "script-src")


def test_attributes_on_the_tag_do_not_change_the_hash() -> None:
    """The guide's label block is ``<script id="i18n" type="...">``.

    CSP hashes the element's contents, not its tag, so an attribute must
    not leak into the digest — and a naive ``<script>``-literal match
    would skip the block entirely and produce a policy that blocks it.
    """
    policy = csp_for_html('<script id="i18n" type="application/json">{"a":1}</script>')
    assert _sha256('{"a":1}') in _directive(policy, "script-src")


def test_the_same_page_always_produces_the_same_header() -> None:
    """A header that varies per request is a cache key that varies."""
    html = "<style>a{}</style><script>b()</script><style>c{}</style>"
    assert csp_for_html(html) == csp_for_html(html)


def test_a_repeated_block_is_listed_once() -> None:
    policy = csp_for_html("<style>a{}</style><style>a{}</style>")
    assert _directive(policy, "style-src").count("sha256-") == 1


def test_changing_one_byte_changes_the_policy() -> None:
    assert csp_for_html("<script>a()</script>") != csp_for_html("<script>a();</script>")


# --- against the pages actually shipped ------------------------------


def _real_pages() -> list[str]:
    """One of each shell the site serves.

    The guide page is the interesting one: it is the only page with
    scripts, and one of them is a JSON data block.
    """
    from pathlib import Path

    from telegram_invite_bot.cms.guide_site import GuideSiteContext
    from telegram_invite_bot.cms.guide_site.editor import render_editor_html
    from telegram_invite_bot.cms.guide_site.router import _render_page
    from telegram_invite_bot.cms.home import render_home
    from telegram_invite_bot.cms.legal.context import LegalContext
    from telegram_invite_bot.cms.legal.documents import DOCUMENTS
    from telegram_invite_bot.cms.legal.router import render_document

    # The markdown is passed in, so the files are never opened — this
    # renders the real shell without needing a tmp_path fixture.
    guide_ctx = GuideSiteContext(
        guide_file_ru=Path("unused_ru.md"),
        guide_file_en=Path("unused_en.md"),
        site_title="ком17",
        version="1.2.3",
        bot_username="kom17_bot",
        url_prefix="https://tgbot.delabs.space",
    )
    ctx = LegalContext(
        site_title="ком17",
        operator="ком17",
        operator_details=None,
        support_url="https://t.me/kom17_support",
        support_email="support@example.com",
        bot_username="kom17_bot",
        url_prefix="https://tgbot.delabs.space",
    )
    return [
        render_home(ctx, "ru"),
        render_document(ctx, DOCUMENTS[0], "ru"),
        _render_page(guide_ctx, "ru", "# Гайд\n\n## Раздел\n\n- пункт\n"),
        render_editor_html("ru text", "en text", ""),
    ]


def test_every_inline_block_of_every_real_page_is_permitted() -> None:
    """The property the whole module exists for.

    Re-extracted with ``html.parser`` rather than with the module's own
    regex, so a hash can only match if the policy really does describe
    the markup that ships.
    """
    seen_scripts = 0
    for page in _real_pages():
        policy = csp_for_html(page)
        blocks = _InlineBlocks()
        blocks.feed(page)
        assert blocks.styles, "no <style> found — the extraction is broken, not the policy"
        seen_scripts += len(blocks.scripts)
        for body in blocks.styles:
            assert _sha256(body) in _directive(policy, "style-src")
        for body in blocks.scripts:
            assert _sha256(body) in _directive(policy, "script-src")

    # The guide page carries two of them (the search script and the JSON
    # label block). Zero here would mean the script half of the policy
    # was never actually exercised by this test.
    assert seen_scripts >= 2


def test_no_page_relies_on_a_style_attribute() -> None:
    """``style="..."`` is not coverable by a hash — it would just die.

    Under this policy an inline style attribute is silently dropped, so
    it must not be how anything on the page is coloured or positioned.
    """
    for page in _real_pages():
        blocks = _InlineBlocks()
        blocks.feed(page)
        assert blocks.styled_tags == []
