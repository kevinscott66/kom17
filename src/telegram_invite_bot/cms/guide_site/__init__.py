"""HTML guide for bot commands, and the editor behind it.

Ports :mod:`legacy.guide_site` from Flask onto a FastAPI ``APIRouter``.
The editor (GET/POST ``/commands/edit``) IS ported — see
:mod:`telegram_invite_bot.cms.guide_site.editor` and the
``guide_edit_get`` / ``guide_edit_post`` handlers in ``router.py``.
Named rather than cited by line: the range this paragraph used to give
(``472-548``) had drifted off both ends of them, and a name does not
rot. This paragraph also used to say the opposite of its first
sentence, describing a stage in which the editor was deliberately left
behind because its writes would race the still-running Flask handler;
that stage is over, and the writes now go through the bridge on this
side. Both halves answer 404 unless ``GUIDES_EDIT_SECRET`` is set, so a
deployment that does not want the editor gets the same surface as
before by leaving the variable unset — which is the state production is
in today.

Public API: :func:`build_router` for the FastAPI sub-app,
:class:`GuideSiteContext` to wire it.
"""

from __future__ import annotations

from telegram_invite_bot.cms.guide_site.context import GuideSiteContext
from telegram_invite_bot.cms.guide_site.router import build_router

__all__ = ["GuideSiteContext", "build_router"]
