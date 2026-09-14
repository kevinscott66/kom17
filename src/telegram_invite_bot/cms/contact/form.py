"""Validation and HTML for the contact form.

Pure functions over strings: nothing here reads a clock, a request or a
setting, so the whole accept/reject decision is testable without an app.
The router composes these with a throttle and a delivery callable.

Two design notes worth keeping in view:

* **The honeypot is not an error.** A submission that fills the decoy
  field is answered with the success page and dropped silently. Telling
  a bot which check it failed is how the check stops working.
* **Escaping happens here, at the boundary.** Every value that reaches
  the HTML is passed through :func:`html.escape`, including the ones
  echoed back into the form after a rejection — that echo is the only
  place on this site where attacker-controlled text is re-rendered, and
  it is on an unauthenticated public endpoint.
"""

from __future__ import annotations

import html as html_lib
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from telegram_invite_bot.cms.contact.content import ContactCopy

#: Field names on the wire. ``HONEYPOT_FIELD`` is named after something
#: an autofill heuristic will happily complete and a human will never
#: see — the point is that a naive form-filler treats it as real.
FIELD_MESSAGE: Final[str] = "message"
FIELD_REPLY_TO: Final[str] = "reply_to"
HONEYPOT_FIELD: Final[str] = "website"

#: Ceilings, in characters. The message limit is a Telegram message with
#: room for the header; the contact limit is far above any real address
#: and exists only so the field cannot be used as a second message body.
MAX_MESSAGE_CHARS: Final[int] = 2000
MAX_REPLY_TO_CHARS: Final[int] = 200


class Rejection(StrEnum):
    """Why a submission was not delivered."""

    EMPTY = "empty"
    TOO_LONG = "too_long"
    REPLY_TOO_LONG = "reply_too_long"
    #: Decoy field filled. Reported to the caller so it can drop the
    #: submission, never to the sender.
    HONEYPOT = "honeypot"


def validate(*, message: str, reply_to: str, honeypot: str) -> Rejection | None:
    """``None`` when the submission may be delivered.

    Order matters: the honeypot is checked first so a bot's oversized
    payload is answered with the success page rather than with a length
    complaint that tells it what to fix.
    """
    if honeypot.strip():
        return Rejection.HONEYPOT
    body = message.strip()
    contact = reply_to.strip()
    if not body or not contact:
        return Rejection.EMPTY
    if len(body) > MAX_MESSAGE_CHARS:
        return Rejection.TOO_LONG
    if len(contact) > MAX_REPLY_TO_CHARS:
        return Rejection.REPLY_TOO_LONG
    return None


def rejection_text(rejection: Rejection, copy: ContactCopy) -> str:
    """The sender-facing sentence for a rejection.

    :data:`Rejection.HONEYPOT` has no text on purpose — a caller that
    asks for one has confused "drop silently" with "report", so this
    raises rather than inventing a message.
    """
    if rejection is Rejection.EMPTY:
        return copy.err_empty
    if rejection is Rejection.TOO_LONG:
        return copy.err_too_long
    if rejection is Rejection.REPLY_TOO_LONG:
        return copy.err_reply_too_long
    msg = "the honeypot rejection is never shown to the sender"
    raise ValueError(msg)


#: Scoped to ``.contact``/``.formnote`` so it cannot reach the document
#: chrome it is injected into. Inlined like every other block on this
#: site; :func:`~telegram_invite_bot.cms.csp.csp_for_html` hashes it, so
#: an edit here changes the page's policy automatically.
_FORM_CSS: Final[str] = """
.contact { margin-top: 1.6rem; display: grid; gap: 1.35rem; max-width: 46rem; }
.contact .field { display: grid; gap: 0.4rem; }
.contact label {
  font-family: var(--mono); font-weight: 700; font-size: 0.74rem;
  letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink);
}
.contact .hint { margin: 0; font-size: 0.9rem; color: var(--muted); }
.contact textarea, .contact input[type="text"] {
  width: 100%; box-sizing: border-box;
  padding: 0.7rem 0.8rem; min-height: 2.9rem;
  font: inherit; font-size: 1rem; line-height: 1.5;
  color: var(--ink); background: var(--panel);
  border: 1px solid var(--rule-strong); border-radius: 8px;
}
.contact textarea { min-height: 11rem; resize: vertical; }
.contact textarea:focus-visible, .contact input[type="text"]:focus-visible {
  outline: 2px solid var(--gold); outline-offset: 2px; border-color: var(--gold);
}
.contact button {
  justify-self: start; padding: 0.7rem 1.5rem; min-height: 2.9rem;
  font-family: var(--mono); font-weight: 700; font-size: 0.8rem;
  letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--bg); background: var(--gold);
  border: 1px solid var(--gold); border-radius: 8px; cursor: pointer;
}
.contact button:hover { filter: brightness(1.08); }
.contact button:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
/* The decoy. Off-canvas rather than display:none — a form-filler that
   skips hidden inputs would otherwise skip it too. */
.contact .decoy {
  position: absolute; left: -10000px; width: 1px; height: 1px; overflow: hidden;
}
.contact .note { margin: 0; font-size: 0.82rem; color: var(--muted); }
.formnote {
  margin-top: 1.6rem; padding: 0.9rem 1.1rem;
  border: 1px solid var(--rule-strong); border-left-width: 4px; border-radius: 8px;
  background: var(--panel);
}
.formnote p { margin: 0.3rem 0 0; }
.formnote p:first-child { margin-top: 0; }
.formnote .head {
  font-family: var(--mono); font-weight: 700; font-size: 0.74rem;
  letter-spacing: 0.12em; text-transform: uppercase;
}
.formnote.is-ok { border-left-color: var(--gold); }
.formnote.is-ok .head { color: var(--gold); }
.formnote.is-bad { border-left-color: #d2604a; }
.formnote.is-bad .head { color: #d2604a; }
"""


def render_notice(*, head: str, body: str, ok: bool) -> str:
    """A result panel. ``head`` and ``body`` are raw text; escaped here.

    ``role="status"`` rather than ``role="alert"``: the panel is present
    in the freshly-loaded POST response, so a screen reader announces it
    as part of reading the page. An assertive live region would
    interrupt that reading to say the same thing twice.
    """
    tone = "is-ok" if ok else "is-bad"
    return (
        f'<div class="formnote {tone}" role="status">'
        f'<p class="head">{html_lib.escape(head)}</p>'
        f"<p>{html_lib.escape(body)}</p>"
        "</div>"
    )


def render_form(
    copy: ContactCopy,
    *,
    action: str,
    message_value: str = "",
    reply_to_value: str = "",
) -> str:
    """The form itself, with the style block it needs.

    ``action`` must already be escaped for an attribute; everything else
    is escaped here. The values are echoed back so a rejected
    submission does not cost the sender their text — the single most
    common reason a contact form goes unanswered.
    """
    return f"""<style>{_FORM_CSS}</style>
<form class="contact" method="post" action="{action}" accept-charset="utf-8">
  <div class="field">
    <label for="cf-message">{html_lib.escape(copy.label_message)}</label>
    <p class="hint" id="cf-message-hint">{html_lib.escape(copy.hint_message)}</p>
    <textarea id="cf-message" name="{FIELD_MESSAGE}" rows="9" required
      maxlength="{MAX_MESSAGE_CHARS}" aria-describedby="cf-message-hint"
      >{html_lib.escape(message_value)}</textarea>
  </div>
  <div class="field">
    <label for="cf-reply">{html_lib.escape(copy.label_reply_to)}</label>
    <p class="hint" id="cf-reply-hint">{html_lib.escape(copy.hint_reply_to)}</p>
    <input id="cf-reply" name="{FIELD_REPLY_TO}" type="text" required
      maxlength="{MAX_REPLY_TO_CHARS}" aria-describedby="cf-reply-hint"
      value="{html_lib.escape(reply_to_value, quote=True)}"/>
  </div>
  <div class="decoy" aria-hidden="true">
    <label for="cf-website">Website</label>
    <input id="cf-website" name="{HONEYPOT_FIELD}" type="text" tabindex="-1"
      autocomplete="off"/>
  </div>
  <button type="submit">{html_lib.escape(copy.submit_label)}</button>
  <p class="note">{html_lib.escape(copy.privacy_note)}</p>
</form>"""
