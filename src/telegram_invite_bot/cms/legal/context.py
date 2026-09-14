"""Configuration object for the legal-document pages.

Same shape and the same reasoning as
:class:`~telegram_invite_bot.cms.guide_site.context.GuideSiteContext`: a
frozen dataclass built once at startup, so the router closes over it and
tests can render a page without constructing the whole ``Settings``
graph.

The one difference worth naming is that nothing here is optional-by-
accident. A missing support handle is a *supported* state (the in-bot
ticket system is the fallback and is itself an accepted contact form),
not a misconfiguration to paper over — so the fields are typed
``str | None`` and the renderer omits the lines it has no value for.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram_invite_bot.cms.legal.documents import REVISION


@dataclass(frozen=True, slots=True)
class LegalContext:
    """Everything the legal router needs to render a page.

    Attributes
    ----------
    site_title:
        The bot's public name. Doubles as the fallback operator name —
        honest about being a project name rather than inventing a legal
        entity that does not exist.
    operator / operator_details:
        Who the contract is with, and their registration details. Raw
        values; the router escapes at the HTML boundary.
    support_url / support_email:
        Optional contact channels, already validated by
        :class:`~telegram_invite_bot.config.settings.LegalConfig`.
    bot_username:
        Used for the "open the bot" call to action, same fallback rule
        as the guide site.
    url_prefix:
        Absolute public origin (e.g. ``https://bot.example``). Empty
        falls back to relative URLs, which still work for a visitor but
        not for a link pasted into a bank's onboarding form — which is
        why the deploy sets it.
    contact_enabled:
        Whether ``/contact`` is mounted. The documents name the form as
        a contact channel and the nav links to it, so both have to know
        — a policy that promises a page which answers 404 is worse than
        one that never mentioned it.
    guide_enabled:
        Whether ``/commands`` is mounted, for the same reason and with
        the same consequence: the shared navigation row lists it, and a
        deployment that turned the guide off would otherwise advertise
        a 404 from every legal page.
    revision:
        Overridable only so a test can pin a date; production always
        takes :data:`~telegram_invite_bot.cms.legal.documents.REVISION`.
    """

    site_title: str = "Bot"
    operator: str = ""
    operator_details: str | None = None
    support_url: str | None = None
    support_email: str | None = None
    bot_username: str | None = None
    url_prefix: str = ""
    contact_enabled: bool = False
    guide_enabled: bool = True
    revision: str = REVISION

    @property
    def tme_url(self) -> str:
        # #1484, and the reasoning is in
        # :meth:`cms.guide_site.context.GuideSiteContext.tme_url`:
        # ``isalnum`` accepts Cyrillic, a Telegram handle does not, and
        # the link this builds is the one a bank reviewer taps.
        u = (self.bot_username or "").strip()
        if u and u.isascii() and all((c.isalnum() or c == "_") for c in u):
            return "https://t.me/" + u
        return "https://t.me"

    @property
    def operator_name(self) -> str:
        """The operator to print — never empty, never a placeholder."""
        return self.operator.strip() or self.site_title
