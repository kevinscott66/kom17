"""Every public page reaches every other public page (#179).

The site is five kinds of page written by four packages, and each
package used to build its own navigation row. They disagreed. ``/`` and
``/commands`` listed the guide; the documents and the contact form did
not. And ``/commands`` — the page ``/help`` and ``/faq`` send every user
to, so the most visited one on the site — carried no row at all: its
only outbound links were the bot, the wordmark and the RU/EN switch.
From the page most people land on, the privacy policy and the contact
form were unreachable.

That matters beyond tidiness. The documents exist because an acquiring
bank requires them to be permanently reachable, and "reachable" is a
property of the whole site, not of three pages that happen to link each
other.

So the row has one builder now, and this is the guard that keeps it one:
it renders every shell the site actually serves and asserts the same set
of links comes out of each. A new page kind that forgets the row fails
here rather than in a compliance review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from telegram_invite_bot.cms.contact.router import render_page as render_contact
from telegram_invite_bot.cms.guide_site.context import GuideSiteContext
from telegram_invite_bot.cms.guide_site.router import _render_page as render_guide
from telegram_invite_bot.cms.home.router import render_home
from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.cms.legal.documents import DOCUMENTS
from telegram_invite_bot.cms.legal.router import render_document
from telegram_invite_bot.cms.paths import commands_path, contact_path, doc_path, home_path

LANGS = ("ru", "en")

#: The origin the deploy actually sets. Present so the assertions
#: compare absolute URLs — the form the pages ship, and the form a link
#: pasted into a bank's onboarding form has to be in.
_PREFIX = "https://tgbot.delabs.space"

_GUIDE_MD = "# Гайд\n\n## Раздел\n\n- пункт\n"


def _legal_ctx(*, contact_enabled: bool = True, guide_enabled: bool = True) -> LegalContext:
    return LegalContext(
        site_title="ком17",
        operator="ком17",
        bot_username="kom17bot",
        url_prefix=_PREFIX,
        contact_enabled=contact_enabled,
        guide_enabled=guide_enabled,
    )


def _guide_ctx(*, contact_enabled: bool = True) -> GuideSiteContext:
    # The markdown is passed to the renderer, so neither file is opened
    # and the paths never have to exist.
    return GuideSiteContext(
        guide_file_ru=Path("unused_ru.md"),
        guide_file_en=Path("unused_en.md"),
        site_title="ком17",
        version="1.2.3",
        bot_username="kom17bot",
        url_prefix=_PREFIX,
        contact_enabled=contact_enabled,
    )


#: The row itself, cut out of the page. Some assertions are about the
#: row and not about the document that carries it — the wordmark and the
#: RU/EN pair live in the topbar, and the stylesheet mentions
#: ``aria-current`` too. Matching the page instead of the row is how a
#: test starts reporting the header's links as the row's.
_ROW_RE = re.compile(r'<nav class="docnav".*?</nav>', re.DOTALL)

#: The RU/EN switch, the one link on the site that is *supposed* to
#: cross languages.
_LANG_SWITCH_RE = re.compile(r'<nav class="nav".*?</nav>', re.DOTALL)

#: The ``<head>`` identity links added by #185 — ``canonical`` and the
#: ``hreflang`` pair. They cross languages for the same reason the
#: switch above does, and are cut for the same reason: naming the other
#: language is their whole job. Neither is navigable — no reader can
#: follow a ``<link>`` — so they cannot strand anyone in the wrong
#: language, which is the property below.
_HEAD_LINKS_RE = re.compile(r'<link rel="(?:canonical|alternate)"[^>]*/>')


def _both_forms(path: str) -> tuple[str, str]:
    """Every way one page can be linked from another.

    The navigation row spells its hrefs out in full — those get copied
    out of the page and pasted into an acquirer's form. The body copy
    keeps them site-relative on purpose, so the reader is not thrown
    into a second tab. A guard that knows only one of the two forms
    passes while the other half of the page says the opposite.
    """
    return f'href="{_PREFIX + path}"', f'href="{path}"'


def _row(path: str, lang: str, html: str) -> str:
    """The navigation row of one page, or a failure naming the page."""
    match = _ROW_RE.search(html)
    assert match is not None, f"{path} ({lang}) renders no navigation row at all"
    return match.group(0)


def _pages(
    lang: str, *, contact_enabled: bool = True, guide_enabled: bool = True
) -> dict[str, str]:
    """One rendered page of every kind the site serves, by its path.

    With the guide switched off ``/commands`` is not mounted, so it is
    not among the pages either — a page that 404s cannot be expected to
    carry the row.
    """
    legal = _legal_ctx(contact_enabled=contact_enabled, guide_enabled=guide_enabled)
    pages = {home_path(lang): render_home(legal, lang)}
    if guide_enabled:
        guide = _guide_ctx(contact_enabled=contact_enabled)
        pages[commands_path(lang)] = render_guide(guide, lang, _GUIDE_MD)
    for doc in DOCUMENTS:
        pages[doc_path(doc.slug, lang)] = render_document(legal, doc, lang)
    if contact_enabled:
        pages[contact_path(lang)] = render_contact(legal, lang)
    return pages


def _expected_links(
    lang: str, *, contact_enabled: bool = True, guide_enabled: bool = True
) -> set[str]:
    links = {
        _PREFIX + home_path(lang),
        *(_PREFIX + doc_path(doc.slug, lang) for doc in DOCUMENTS),
    }
    if guide_enabled:
        links.add(_PREFIX + commands_path(lang))
    if contact_enabled:
        links.add(_PREFIX + contact_path(lang))
    return links


@pytest.mark.parametrize("lang", LANGS)
def test_every_public_page_links_to_every_other_one(lang: str) -> None:
    """The property #179 restored, stated once for all five page kinds."""
    expected = _expected_links(lang)
    for path, html in _pages(lang).items():
        row = _row(path, lang, html)
        missing = sorted(url for url in expected if f'href="{url}"' not in row)
        assert not missing, f"{path} ({lang}) does not link: {', '.join(missing)}"


@pytest.mark.parametrize("lang", LANGS)
def test_the_page_you_are_on_is_the_one_marked_current(lang: str) -> None:
    """Exactly one entry per page, and it is that page's own.

    Without this the row renders fine and silently stops orienting
    anyone: ``aria-current`` is what a screen reader announces, and the
    gold underline is what everyone else reads.
    """
    for path, html in _pages(lang).items():
        row = _row(path, lang, html)
        marked = f'href="{_PREFIX + path}" aria-current="page"'
        assert marked in row, f"{path} ({lang}) does not mark itself current"
        assert row.count('aria-current="page"') == 1, (
            f"{path} ({lang}) marks more than one nav entry as current"
        )


@pytest.mark.parametrize("lang", LANGS)
def test_no_page_advertises_a_contact_form_that_is_not_mounted(lang: str) -> None:
    """``/contact`` needs an ADMIN_CHAT_ID to forward to; without one it
    is not mounted, and a row that links it anyway is a 404 promised on
    every page of the site.
    """
    for path, html in _pages(lang, contact_enabled=False).items():
        for href in _both_forms(contact_path(lang)):
            assert href not in html, (
                f"{path} ({lang}) links the contact form while it is switched off"
            )


@pytest.mark.parametrize("lang", LANGS)
def test_no_page_links_the_other_language(lang: str) -> None:
    """A visitor who picked English stays in English until they say
    otherwise.

    Deliberately about the whole page and not just the row: the first
    run of this test found the front page's wordmark pointing at the
    Russian home from ``/en``, which no assertion scoped to the row
    would ever have seen. The two controls whose entire job is to cross
    — the RU/EN pair and the ``<head>`` identity links — are cut out
    first; everything else that names the other language is a leak.
    """
    other = "en" if lang == "ru" else "ru"
    for path, html in _pages(lang).items():
        body = _HEAD_LINKS_RE.sub("", _LANG_SWITCH_RE.sub("", html))
        for url in _expected_links(other):
            # ``/`` is a prefix of every path, and ``/commands`` of
            # ``/commands/en`` — compare the full attribute so a RU page
            # is not reported for linking its own paths.
            assert f'href="{url}"' not in body, f"{path} ({lang}) links {other} page {url}"


@pytest.mark.parametrize("lang", LANGS)
def test_no_page_advertises_a_guide_that_is_not_mounted(lang: str) -> None:
    """``GUIDE_SITE_ENABLED`` off means ``/commands`` 404s.

    The flag lives on the context every page kind reads, so this asks
    all of them at once. It used to reach the front page as a separate
    keyword argument, which meant the page could hide the guide from its
    own body and row while the documents next to it went on linking it —
    the same divergence this file exists to prevent, one boolean lower.
    """
    for path, html in _pages(lang, guide_enabled=False).items():
        for href in _both_forms(commands_path(lang)):
            assert href not in html, f"{path} ({lang}) links the guide while it is switched off"


@pytest.mark.parametrize("lang", LANGS)
def test_the_row_survives_both_switches_being_off(lang: str) -> None:
    """Home and the three documents, and nothing that is not mounted."""
    expected = _expected_links(lang, contact_enabled=False, guide_enabled=False)
    pages = _pages(lang, contact_enabled=False, guide_enabled=False)
    for path, html in pages.items():
        row = _row(path, lang, html)
        assert sorted(re.findall(r'<a href="([^"]+)"', row)) == sorted(expected), (
            f"{path} ({lang}) renders the wrong row with both switches off"
        )
