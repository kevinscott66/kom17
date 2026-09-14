"""Content-Security-Policy for the pages this package renders.

The site ships its CSS and its one script *inside* the HTML — no
stylesheet files, no bundles, no CDN — because a page that is one
request is a page that survives a cold visitor on mobile data. The cost
is that the obvious policy for inline content is ``'unsafe-inline'``,
which is precisely the string an acquiring bank's scanner reports as a
finding, and which does genuinely re-open injected-script execution for
every escaping bug anywhere in the renderer.

So the policy is built from **hashes of the exact blocks the page
carries**, taken from the rendered page itself.

Be clear about what that does and does not buy, because the note that
stood here was wrong about it. It said a block the renderer did not
produce "has no hash, and therefore does not run", and that escaping
was thereby backed by a second layer. It is not. The hashes are derived
from the finished HTML, *after* any interpolation — so a ``<script>``
that reached the page through an escaping bug is part of the page this
function reads, gets hashed like everything else, and is permitted by
the policy built to describe it. A hash policy computed from its own
output cannot distinguish the script the renderer wrote from the one an
input smuggled in.

What it does buy is the thing it was adopted for: the pages carry no
``'unsafe-inline'``, so an injection that lands *outside* a block this
function hashes — an ``onclick=`` attribute, a ``javascript:`` href, a
script appended by something other than the renderer after the header
was computed — does not run. Escaping is therefore the only thing
standing between a reflected value and script execution inside the
page, and it has to be treated that way: every reflection escapes, and
that is a property of the renderers, pinned by their own tests, not
something this module can enforce on their behalf.

Everything else is denied outright — ``default-src 'none'`` — because
the page fetches nothing: no fonts, no images, no XHR, no frames. The
three exceptions state themselves: ``img-src`` for the favicon a browser
asks for on its own, ``form-action 'self'`` for the guide editor's POST,
and ``frame-ancestors 'none'`` so the documents cannot be reframed under
someone else's domain.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Final

#: Inline ``<style>``/``<script>`` blocks. Non-greedy on purpose: the
#: renderer guarantees a block never contains its own closing tag (the
#: JSON label block escapes ``<`` for exactly that reason), so the first
#: closer is always the right one.
_INLINE_RE: Final[re.Pattern[str]] = re.compile(
    r"<(script|style)\b[^>]*>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE
)

#: The CSP keyword for "nothing at all", quotes included.
_NONE: Final[str] = "'none'"

#: Directives that do not depend on the page's contents.
_STATIC_DIRECTIVES: Final[tuple[str, ...]] = (
    "default-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "img-src 'self' data:",
)

#: Used by the ASGI middleware for any HTML response that arrives
#: without a policy of its own — an error page, or a page added later by
#: someone who did not read this module. Deliberately harsher than what
#: the real pages get: nothing inline runs at all.
FALLBACK_CSP: Final[str] = "; ".join(
    (*_STATIC_DIRECTIVES, f"script-src {_NONE}", f"style-src {_NONE}")
)


def _sha256_source(text: str) -> str:
    """One CSP hash source expression over a block's exact contents."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def csp_for_html(html: str) -> str:
    """The policy that permits exactly this page's inline blocks.

    Order is preserved and duplicates are dropped so that two renders of
    the same page produce byte-identical headers — a header that changes
    per request is a cache key that changes per request.
    """
    sources: dict[str, list[str]] = {"script": [], "style": []}
    for kind, body in _INLINE_RE.findall(html):
        bucket = sources[kind.lower()]
        source = _sha256_source(body)
        if source not in bucket:
            bucket.append(source)

    # A page with no blocks of a given kind gets ``'none'`` rather than
    # an empty directive: an empty value is a parse error in some
    # browsers, and a policy that fails to parse is a policy that is not
    # applied.
    scripts = " ".join(sources["script"]) or _NONE
    styles = " ".join(sources["style"]) or _NONE
    return "; ".join((*_STATIC_DIRECTIVES, f"script-src {scripts}", f"style-src {styles}"))
