"""Which client a public site request is bucketed as.

Shared by every rate limiter on the public site — the contact form
(#137) and the guide editor's failed-secret counter (#189) — so the two
cannot drift apart on what "one client" means, and so a future third
limiter has an obvious place to reach for rather than a third copy.

Lives in ``cms`` rather than under either endpoint because it belongs to
neither: it reads only the request's headers and socket address.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from starlette.requests import Request

#: The header Cloudflare sets on every proxied request. The origin also
#: answers on its own address, where anyone can set this header
#: themselves — which is why it only ever selects a *bucket* and never
#: grants anything.
_CF_HEADER: Final[str] = "cf-connecting-ip"

#: What a request with no resolvable address is bucketed as. Shared by
#: all of them on purpose: "unknown" should not be an unlimited lane.
_UNKNOWN_CLIENT: Final[str] = "-"


def client_key(request: Request) -> str:
    """The throttle key for one request.

    Prefers Cloudflare's ``CF-Connecting-IP`` because every real visitor
    arrives through it and the socket address would otherwise be the
    edge's, collapsing the entire internet into one bucket. A request
    that reaches the origin directly can forge the header and pick its
    own bucket; that is expected, documented and bounded by the global
    limit rather than papered over with a trusted-proxy list that would
    have to be kept in step with Cloudflare's ranges.
    """
    forwarded = request.headers.get(_CF_HEADER, "").strip()
    if forwarded:
        # One header, one address — but a forged header can carry a
        # list, and an unbounded key is an unbounded cache entry.
        return forwarded.split(",")[0].strip()[:64] or _UNKNOWN_CLIENT
    client = request.client
    return client.host if client and client.host else _UNKNOWN_CLIENT
