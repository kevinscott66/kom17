"""Public legal documents: privacy policy, terms of use, support.

Three pages an acquiring bank requires to exist and stay reachable
before it will approve a payment integration. Served by the bot's own
web process, from text held in the repository — see
:mod:`telegram_invite_bot.cms.legal.documents` for why neither of those
choices is incidental.

Import from the submodules: :mod:`~telegram_invite_bot.cms.legal.router`
for the FastAPI sub-app, :mod:`~telegram_invite_bot.cms.legal.context`
for the object that wires it, and
:mod:`telegram_invite_bot.cms.paths` for the URL of a document.

This file re-exported those three names until the shared navigation row
landed in :mod:`telegram_invite_bot.cms.nav`. The row needs the document
table, the table lives in this package, and importing anything from a
package runs its ``__init__`` — so the convenience re-export of the
*router* was enough to drag the router into the import of the *table*,
and back into ``nav``, closing a cycle. Keeping this file empty is what
makes the dependency one-directional in fact and not just on paper.
"""

from __future__ import annotations
