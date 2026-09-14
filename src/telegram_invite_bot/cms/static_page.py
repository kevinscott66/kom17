"""Handler factory for pages that are rendered once, when the router is built.

Three public routers — legal, home and contact — render every page at
router-build time and then hand back the same string for the life of the
process. Each builds its pages in a ``for lang in ...`` loop, so each
needs the loop variable bound per iteration rather than looked up when
the request arrives; without that, all routes close over the last value
and every path serves the English page.

The obvious way to bind it is a default argument, and all three routers
used to do exactly that::

    async def _page(_html: str = page, _headers: dict[str, str] = headers) -> HTMLResponse:
        return HTMLResponse(_html, headers=_headers)

The binding is correct. What is not correct is the assumption that
FastAPI leaves a handler's parameters alone: it inspects the signature
and turns *every* parameter into a request parameter. ``_html: str``
became a query parameter and ``_headers: dict[str, str]`` a body field,
so ``GET /privacy?_html=<h1>PWNED</h1>`` returned that markup as the
entire page — 200, ``text/html``, ``Cache-Control: public, max-age=3600``
— on the domain handed to the acquiring bank (#1584). The CSP keeps
script from running, but a cacheable attacker-authored page on the
bot's own certificate is the whole of the damage, not a footnote to it.

A closure has no such surface: the handler below takes no parameters at
all, so there is nothing for FastAPI to bind a request to. This is the
shape :mod:`telegram_invite_bot.cms.discovery` already uses — it builds
its one response outside a loop, so it never reached for a default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def html_page(html: str, headers: dict[str, str]) -> Callable[[], Awaitable[HTMLResponse]]:
    """Build a parameterless handler serving one prerendered page.

    Both arguments are captured by the closure, which is what makes this
    safe to call from inside a loop: the returned coroutine function
    reads them from its own cell, not from the enclosing scope, and
    exposes neither of them to FastAPI's signature introspection.
    """

    async def _page() -> HTMLResponse:
        return HTMLResponse(html, headers=headers)

    return _page
