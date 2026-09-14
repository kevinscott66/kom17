"""Security response headers for every route this app serves.

Until this middleware existed a GET of the front page came back with
three headers — ``content-type``, ``cache-control``, ``vary`` — and
nothing else. That is not a theoretical gap: this origin is the address
handed to an acquiring bank next to the offer and the privacy policy,
and "no security headers" is the first line of every automated report
such a reviewer runs. It is also a real one — without ``nosniff`` a
browser is free to re-interpret a response as something it is not, and
without ``frame-ancestors`` the documents can be reframed inside someone
else's page and captioned however that page likes.

Applied to *every* response rather than only to the site's HTML: the
payment callbacks and the Telegram webhook cost nothing to cover, and a
rule with an exception list is a rule that grows a hole the first time
someone adds a route.

The per-page :mod:`~telegram_invite_bot.cms.csp` policy is set by the
routers themselves, because it is derived from the exact page they
rendered. This middleware only fills in a policy where none arrived, and
never overwrites one that did.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.datastructures import MutableHeaders

from telegram_invite_bot.cms.csp import FALLBACK_CSP

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Sent on every response.
#:
#: ``Strict-Transport-Security`` goes out unconditionally although the
#: origin itself speaks plain HTTP behind Cloudflare: browsers are
#: required to ignore the header when it arrives over an insecure
#: transport, so the local-development case is covered by the spec, and
#: putting the header in the application rather than in the proxy means
#: it survives someone changing a setting in a dashboard. No ``preload``
#: — that is a one-way door on a domain the owner also uses elsewhere.
BASE_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    # The modern spelling is CSP's ``frame-ancestors``, which the site's
    # pages carry; this stays for the older clients that ignore it.
    ("X-Frame-Options", "DENY"),
    (
        "Permissions-Policy",
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()",
    ),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
)


class SecurityHeadersMiddleware:
    """Pure-ASGI header stamping.

    Not a ``BaseHTTPMiddleware`` subclass: that one wraps every response
    in an anyio task group and a memory-object stream, which for a
    header edit is a measurable amount of machinery on a box that also
    runs other services. Touching ``http.response.start`` is
    the whole job.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in BASE_HEADERS:
                    # Never clobber a route that stated its own intent —
                    # a future endpoint that must be embeddable, say.
                    if name not in headers:
                        headers.append(name, value)
                content_type = headers.get("content-type", "")
                if content_type.startswith("text/html") and (
                    "Content-Security-Policy" not in headers
                ):
                    headers.append("Content-Security-Policy", FALLBACK_CSP)
            await send(message)

        await self.app(scope, receive, send_with_headers)
