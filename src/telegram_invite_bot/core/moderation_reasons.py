"""Machine-written reasons for ``moderation_log.reason``.

The column is otherwise free text typed by a moderator, and both
renderers (``handlers/groupadmin.py``'s stats page and the
``handlers/profile.py`` "last actions" block) print it verbatim. That is
right for a human-typed reason and wrong for the one row the BOT writes
itself: the captcha timeout kick used to store the Russian literal
``"Капча: не пройдена"``, which an English-speaking operator then read
in Russian on an otherwise English card (#1346).

The fix is the same shape the action column already uses: store a
language-neutral slug and resolve it to a label at render time.
:func:`reason_label` passes anything it does not recognise straight
through, so a moderator's own wording is untouched — and so are the
pre-#1346 rows still sitting in production with the Russian literal in
them. There is no backfill: those age out of the 90-day profile window
on their own, and the audit log is append-only by design.

:func:`reason_label` returns a string that is NOT html-escaped, and
that is the trap #1639 was filed about: for a moderator's free text
the caller must escape, and for a recognised slug it must not —
``t()`` already returns HTML. A caller holding one string cannot
tell the two apart, so ``handlers/profile.py`` escaped both and
clipped both to 50 characters, which for a label would have cut a
tag or an entity in half the day one carried markup.

:func:`reason_html` is the answer: it makes the decision here,
where the two cases are still distinguishable, and hands back a
string that is finished HTML either way. New call sites should use
it; :func:`reason_label` stays for readers that want the words
without the dressing.
"""

from __future__ import annotations

import html

from telegram_invite_bot.i18n import t

#: The join captcha timed out and the bot removed the user
#: (``handlers/group_events._record_captcha_kick``). The machine-readable
#: half of that event lives in ``details`` as ``captcha_timeout:<outcome>``;
#: this slug is only what the operator-facing label is looked up by.
CAPTCHA_FAILED = "captcha_failed"

#: Slug -> i18n key. Deliberately tiny: a slug earns a place here only
#: when the bot itself writes the row, never for moderator input.
_REASON_KEYS: dict[str, str] = {
    CAPTCHA_FAILED: "h_mod_reason_captcha_failed",
}


def reason_label(reason: str, lang: str) -> str:
    """Render ``reason`` for an operator reading in ``lang``.

    A recognised bot slug becomes its localized label; everything else —
    moderator free text, and the legacy Russian literals — is returned
    unchanged.
    """
    key = _REASON_KEYS.get(reason.strip())
    return t(key, lang) if key is not None else reason


def reason_html(reason: str, lang: str, *, limit: int) -> str:
    """``reason`` as finished HTML for a caption, clipped to ``limit``.

    Two branches and not one (#1639):

    * A recognised slug resolves to its label and is returned as is.
      ``t()`` already yields HTML, so escaping it would show the
      reader ``&amp;`` where the label says ``&``; and the label is
      our own text, short by construction, so ``limit`` — a budget
      written for a moderator who can type a paragraph — does not
      apply to it. Clipping it is what would break it, by cutting a
      tag or an entity in half.
    * Anything else is a moderator's free text: clipped FIRST, then
      escaped. That order is legacy parity (``bot.py:39802``) and it
      is deliberate — counting escaped length would silently show
      less text for a reason containing ``<`` than for one without.

    An empty reason returns an empty string; the caller decides what
    a row with no reason looks like.
    """
    text = reason.strip()
    key = _REASON_KEYS.get(text)
    if key is not None:
        return t(key, lang)
    return html.escape(text[:limit])
