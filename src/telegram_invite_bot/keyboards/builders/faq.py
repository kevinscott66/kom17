"""FAQ inline-keyboard CallbackData factories.

The only callback in the FAQ surface is the "Continue → part 2"
button rendered under the ``/faq`` reply: clicking it should edit the
prompt into the part-2 body (already keyed as ``h_faq_part2`` in YAML
from Stage 22). The legacy wire format is the bare literal
``"faq_part2"`` (bot.py:35428, bot.py:35460); we deliberately do NOT
reuse that string because:

* Legacy-rendered buttons outlive the legacy process. T-011 stopped
  ``bot.py`` from running, but an inline keyboard it sent is still
  sitting in scrollback and is still tappable years later — Telegram
  keeps the markup on the message, not in the sender. So the wire is
  not clear of ``"faq_part2"`` and will not be: reusing that literal
  would route those old taps into today's handler, which never
  authored their payload. A distinct prefix (``faq_cont``) means an
  old tap resolves to nothing, which is the honest outcome.
* This is the reason the rest of the package calls prefix isolation
  the "strangler invariant". The invariant outlived its original
  justification: it is now enforced package-wide, on a stronger
  argument than legacy co-existence, by
  ``tests/regression/test_prose_names_real_things.py`` (#1997), which
  fails if any two ``CallbackData`` factories share a prefix.

``FaqContinue`` carries no fields — the part-2 body is rendered from
``callback.from_user.language_code`` alone, matching the Stage 22
``/faq2`` resolution. Adding a ``user_id`` field would invite a
spoofing class (the wire is user-controlled) and would not change
behaviour: aiogram populates ``callback.from_user`` from the same
Telegram payload, and that field is authenticated by Telegram's
signature on the update envelope.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class FaqContinue(CallbackData, prefix="faq_cont"):
    """Marker for the "Continue → part 2" button under ``/faq``.

    Empty payload — see module docstring for why no fields are
    carried. The prefix is 8 chars; aiogram's CallbackData encoder
    joins ``prefix:field1:field2:...`` and Telegram caps the whole
    string at 64 bytes, so leaving 56 bytes of headroom matters once
    a future revision adds fields. Pinned narrow on purpose.
    """
