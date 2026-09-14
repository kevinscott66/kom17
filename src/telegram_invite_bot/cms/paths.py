"""The public site's URL map, in one importable place.

Three packages under :mod:`telegram_invite_bot.cms` now link at each
other's pages: the front page lists the guide and the documents, the
documents' header links home, and the guide's header does too. Letting
each one import the others' path helpers would close an import cycle
(home needs ``doc_path``, and legal would need ``home_path``), and
hardcoding ``"/commands"`` in four modules is how a renamed route
becomes four dead links.

So the literals live here, in a module that imports nothing from
``cms``. Anything that needs to *build* a page still lives in its own
package; this is only the map.
"""

from __future__ import annotations

from typing import Final

#: Front page. ``/`` used to 404 — nothing served it, while the guide
#: and the documents sat on their own paths — so anyone who trimmed a
#: shared link down to the bare domain (a compliance reviewer typing it
#: by hand, most of all) got an error page for the whole service.
HOME_PATH_RU: Final[str] = "/"
HOME_PATH_EN: Final[str] = "/en"

COMMANDS_PATH_RU: Final[str] = "/commands"
COMMANDS_PATH_EN: Final[str] = "/commands/en"
COMMANDS_PATH_EDIT: Final[str] = "/commands/edit"

#: The public contact form. The legal documents have to name a way to
#: reach the operator that does not require installing the bot first —
#: a regulator, an acquiring bank or a person asking about their own
#: data all arrive without a Telegram account — and this is that way.
#: The path lives here rather than in the contact package because the
#: legal and home routers link to it and must not import it.
CONTACT_PATH_RU: Final[str] = "/contact"
CONTACT_PATH_EN: Final[str] = "/contact/en"

#: The suffix every English page carries. The documents are the only
#: pages whose paths are built rather than spelled out — their slugs
#: come from ``DOCUMENTS``, so adding a fourth document is one entry
#: there and nothing here.
_EN_SUFFIX: Final[str] = "/en"


def home_path(lang: str) -> str:
    """The front page in ``lang``; anything but ``en`` reads Russian."""
    return HOME_PATH_EN if lang == "en" else HOME_PATH_RU


def contact_path(lang: str) -> str:
    """The contact form in ``lang``."""
    return CONTACT_PATH_EN if lang == "en" else CONTACT_PATH_RU


def commands_path(lang: str) -> str:
    """The command reference in ``lang``."""
    return COMMANDS_PATH_EN if lang == "en" else COMMANDS_PATH_RU


def doc_path(slug: str, lang: str) -> str:
    """The site-relative path for one document in one language."""
    return f"/{slug}{_EN_SUFFIX}" if lang == "en" else f"/{slug}"


def absolute(url_prefix: str, path: str) -> str:
    """``path`` behind the configured origin, or as-is when unset.

    Relative URLs work fine for someone already on the page; they do not
    work when the link is pasted into a bank's onboarding form, which is
    part of what this surface exists for. A deployment with no
    ``WEBHOOK_URL`` (polling mode, and every test) keeps the relative
    form rather than growing a broken ``https://`` prefix.
    """
    base = url_prefix.rstrip("/")
    return base + path if base else path
