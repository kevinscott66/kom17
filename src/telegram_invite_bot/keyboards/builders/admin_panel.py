"""``/admin`` panel navigation CallbackData factory (T-024.3).

The unified developer panel (``handlers/admin/panel.py``) renders a
root screen with one button per category; clicking a category edits
the card to that category's command list, and a "⬅️ back" button
returns to root. A single :class:`AdminNav` carries the target
``section`` key — ``"root"`` for the back button, a category key
(``"payments"``, ``"withdrawals"``, …) otherwise.

``section`` is a free-form short string, not a capability token:
every navigation handler re-checks ``is_developer(clicker_id)``
server-side, so a forged payload from a non-dev renders nothing.

Distinct prefix (``adm_panel``) keeps these clear of the per-row
withdraw action payloads (``wd_adm_ok`` / ``wd_adm_no``) and any
legacy ``admin_*`` string — no cross-matching across the bridge.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class AdminNav(CallbackData, prefix="adm_panel"):
    """A navigation tap inside the ``/admin`` panel.

    ``section`` is the target screen key: ``"root"`` for the menu,
    or a category key for a sub-screen. The handler re-checks
    developer authorization before rendering.
    """

    section: str
