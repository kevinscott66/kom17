"""Configuration object for the guide-site sub-app.

A frozen dataclass — once the router is built we close over a single
context instance and do not mutate it. Decoupling the router from the
``Settings`` graph means tests can build a context with arbitrary
paths/titles/versions without spinning up the full pydantic loader,
AND the same context can be reused for the future ``cms_dashboard``
sub-app without coupling them through ``Settings``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram_invite_bot.cms.guide_site.editor import EditorBridge


@dataclass(frozen=True, slots=True)
class GuideSiteContext:
    """Everything the guide router needs to render a page.

    Attributes
    ----------
    guide_file_ru / guide_file_en:
        Filesystem paths to the source Markdown. Missing files render
        a stub page (not a 404) — matches legacy behaviour and means a
        partial deploy doesn't break the surviving language.
    site_title:
        Free-form bot name displayed in the topbar and footer. Escaped
        at the call site; here we accept the raw value so the operator
        can configure with unicode/punctuation freely.
    version:
        Stringified version shown next to the page title and in the
        footer. Read once at app startup; we deliberately do NOT
        re-read ``bot.py`` on every request like legacy did — the new
        pipeline has no monolithic ``bot.py`` and the version comes
        from packaging metadata.
    bot_username:
        Telegram username without the ``@`` — used to build the
        "Open bot in Telegram" link. ``None`` falls back to a generic
        ``https://t.me`` (legacy behaviour) so a misconfigured
        environment still renders rather than 500ing.
    url_prefix:
        Public-facing absolute URL prefix (e.g. ``https://bot.com``).
        Used for the RU↔EN nav links so they survive proxying that
        rewrites the request path. Empty string falls back to relative
        URLs (``/commands`` / ``/commands/en``).
    contact_enabled:
        Whether ``/contact`` is mounted — it needs a configured admin
        chat to forward to, so a deployment without one does not serve
        it. The shared navigation row reads this and omits the entry
        rather than linking a 404. Defaults to off for the same reason
        :class:`~telegram_invite_bot.cms.legal.context.LegalContext`
        does: a test that never wires the form should not render a link
        to it.
    """

    guide_file_ru: Path
    guide_file_en: Path
    site_title: str = "Bot"
    version: str = "?"
    bot_username: str | None = None
    url_prefix: str = ""
    contact_enabled: bool = False
    # ``None`` → the editor routes 404 (a deployment that cannot save
    # does not advertise an admin form). In prod the lifespan wires a
    # ``JsonFileEditorBridge`` (#1483: this comment used to name a
    # ``LegacyBotEditorBridge``, which has never existed in this repo
    # under any name); tests can pass an ``InMemoryEditorBridge`` for
    # hermetic checks. Kept as a
    # field rather than a global module-level singleton so multiple
    # apps in one process (a thing the test suite does) get
    # independent editor state.
    editor_bridge: EditorBridge | None = field(default=None, compare=False)

    @property
    def tme_url(self) -> str:
        # Validate the username at read time — a stray space or unicode
        # codepoint slipping in via env would otherwise produce a
        # broken ``https://t.me/...`` link. Legacy did the same check
        # against ``bot.bot.get_me()``; here we apply it to whatever
        # config supplied the value.
        #
        # #1484: ``str.isalnum`` is Unicode-aware, so it answers True
        # for Cyrillic — the comment above promised to refuse a
        # "unicode codepoint" and did not. A Telegram handle is
        # ASCII letters, digits and underscore, and ``@поддержка``
        # used to mint a ``t.me`` link that 404s at tap time, which is
        # worse than no link at all. Same ``.isascii()`` gate the
        # webhook secret check uses (:mod:`webhook.security`), and the
        # order matters: it is cheap and it makes the alphanumeric
        # test mean what it says.
        u = (self.bot_username or "").strip()
        if u and u.isascii() and all((c.isalnum() or c == "_") for c in u):
            return "https://t.me/" + u
        return "https://t.me"
