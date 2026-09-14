"""The site's front page: ``/`` and ``/en``.

The bare domain used to 404 — every other public page had a path of its
own and nothing served the root. See
:mod:`telegram_invite_bot.cms.home.router` for why that mattered more
than it looks.

Public API: :func:`build_router` for the FastAPI sub-app. It is wired
with the same :class:`~telegram_invite_bot.cms.legal.context
.LegalContext` the documents use.
"""

from __future__ import annotations

from telegram_invite_bot.cms.home.router import build_router, render_home

__all__ = ["build_router", "render_home"]
