"""The one navigation row every public page renders.

The site grew a page at a time, and each package built its own row: the
front page listed home, the guide and the documents; the documents
listed only each other; the contact form listed the documents and
itself. The guide — the page ``/help`` and ``/faq`` send every user to,
and so the most visited one — listed nothing at all, which made it the
only page from which the privacy policy and the contact form were
unreachable.

Three builders that agree on the markup and disagree on the contents is
how a reader learns the row cannot be trusted. So there is one builder
here, it takes the same arguments from every caller, and the only thing
a page decides is which entry is its own.

Imports run one way — this module reads
:mod:`telegram_invite_bot.cms.paths` and the document table, and the
page packages read this. It deliberately does not import
:mod:`telegram_invite_bot.cms.guide_site`, whose ``__init__`` pulls in
the router that needs this row: that edge would close the cycle.
"""

from __future__ import annotations

import html as html_lib
from typing import TYPE_CHECKING, Final

from telegram_invite_bot.cms.legal.documents import DOCUMENTS
from telegram_invite_bot.cms.paths import (
    absolute,
    commands_path,
    contact_path,
    doc_path,
    home_path,
)
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Marks the page the reader is already on, for assistive tech; the
#: visual cue is the ``.docnav a[aria-current]`` rule in the stylesheet,
#: so the two can't drift apart into a highlight nobody announces.
_CURRENT_ATTR: Final[str] = ' aria-current="page"'

#: What a caller passes as ``current`` for the two pages that are not
#: documents. Document pages pass their slug, and the contact form
#: passes :data:`CONTACT`.
HOME: Final[str] = "home"
COMMANDS: Final[str] = "commands"
CONTACT: Final[str] = "contact"


def _render(items: Sequence[tuple[str, str, bool]]) -> str:
    """``items`` is ``(url, title, is_current)``, none of it escaped yet."""
    # The ``aria-current`` attribute is built outside the f-string: it
    # contains double quotes, and a backslash inside an f-string
    # expression is a syntax error before Python 3.12 — which the lint
    # target still is.
    links = "".join(
        f'<a href="{html_lib.escape(url, quote=True)}"'
        f"{_CURRENT_ATTR if current else ''}>{html_lib.escape(title)}</a>"
        for url, title, current in items
    )
    return f'<nav class="docnav" aria-label="documents">{links}</nav>'


def site_nav_html(
    *,
    url_prefix: str,
    lang: str,
    current: str | None = None,
    guide_enabled: bool = True,
    contact_enabled: bool = False,
) -> str:
    """Every public page of the site, in reading order.

    ``current`` is :data:`HOME`, :data:`COMMANDS`, a document slug,
    :data:`CONTACT`, or ``None`` for a page that is not in the row at
    all. An unknown value simply marks nothing, which is the right
    failure: a row with no highlight is still a working row.

    The two flags mirror what the deployment actually serves.
    ``guide_enabled`` follows ``GUIDE_SITE_ENABLED`` and
    ``contact_enabled`` follows a configured admin chat — a link to a
    page that answers 404 is worse than no link, and on the legal pages
    it is a promise of a contact channel that isn't there.

    Contact goes last on purpose: it is an action, not a document, and a
    reader who came to read a policy should reach the form on the way
    out rather than instead of the text.
    """
    items: list[tuple[str, str, bool]] = [
        (absolute(url_prefix, home_path(lang)), t("site_home_kind", lang), current == HOME)
    ]
    if guide_enabled:
        items.append(
            (
                absolute(url_prefix, commands_path(lang)),
                t("site_home_nav_commands", lang),
                current == COMMANDS,
            )
        )
    items.extend(
        (absolute(url_prefix, doc_path(doc.slug, lang)), doc.title(lang), current == doc.slug)
        for doc in DOCUMENTS
    )
    if contact_enabled:
        items.append(
            (
                absolute(url_prefix, contact_path(lang)),
                t("site_contact_nav", lang),
                current == CONTACT,
            )
        )
    return _render(items)
