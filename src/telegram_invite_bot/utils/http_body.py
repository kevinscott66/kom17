"""Ceiling on the size of an *inbound* request body.

The mirror image of :mod:`telegram_invite_bot.utils.http_read`, which
bounds what this bot reads *from* a third party. This module bounds
what a third party may make it read *in*.

Every public POST endpoint here — the four payment webhooks, the
contact form and the guide editor — ends in a call that materialises
the whole body in memory (``await request.body()`` or ``await
request.form()``). None of those bodies is legitimately large: a
provider callback is a small JSON document and the form's own ceilings
are 2200 characters. Without a check the only limit is nginx's
``client_max_body_size`` default of 1 MB, which is the wrong place for
it twice over — it is a deployment setting rather than a property of
the endpoint, and it is not in the repository, so nothing here notices
when it changes. One process serves every chat, so a body that costs
us a megabyte of RSS per concurrent request is a shared resource an
anonymous caller controls.

The check reads ``Content-Length`` and decides *before* the body is
touched, which is the entire point: refusing after the read has already
paid for it.

Two shapes of request declare nothing this can judge:

* **A chunked request declares no length at all.** Nothing about the
  size is knowable before the body arrives.
* **A header that is not a plain digit run** — ``parse_int_token``
  rather than ``str.isdigit`` because the latter accepts 128 code
  points that then raise inside ``int()`` (#102), and this value is
  read straight off the wire on unauthenticated endpoints.

What happens to those depends on whether the caller has a second line
of defence. The four payment webhooks have one: they want raw bytes,
so #229 handed the gap to :func:`read_body_capped`, which counts the
chunks as they arrive and stops at the same ceiling. The header check
still runs first there because it is the cheaper of the two — it
refuses without reading a byte.

The two form POSTs — the contact form and the guide editor — have
none. Both end in ``await request.form()``, and Starlette's urlencoded
``FormParser`` carries no size check whatsoever: it accumulates the
stream into a ``bytearray`` until the stream ends. (Its multipart
sibling caps a single *part* at 1 MB, which bounds neither the request
nor the encoding a hand-built one would choose.) With nothing to defer
to, those two pass ``require_declared_length=True`` and an undeclared
body is refused unread (#1633). A browser always declares the length
of a form POST, so what this turns away is a request built by hand to
slip past the gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from loguru import logger
from starlette.exceptions import HTTPException
from starlette.status import HTTP_400_BAD_REQUEST

from telegram_invite_bot.utils.numbers import is_digit_run, parse_int_token

if TYPE_CHECKING:
    from starlette.requests import Request

log = logger.bind(module="http_body")

#: Ceiling for a payment provider's callback. The largest real payload
#: across the four providers is a Stripe ``checkout.session.completed``
#: with expanded line items, which lands in the low tens of KB; the
#: other three are under 2 KB. 256 KiB is an order of magnitude of
#: headroom over the worst legitimate case and still a quarter of the
#: nginx default, so this is the layer that says no first.
PAYMENT_WEBHOOK_MAX_BYTES: Final[int] = 256 * 1024


def declared_body_too_large(
    request: Request, *, max_bytes: int, require_declared_length: bool = False
) -> bool:
    """Whether the declared body is past the point of reading it.

    ``parse_int_token`` refuses a digit run past the 64-bit ceiling
    (#1042) because no such value can reach SQLite. For *this* gate
    that refusal must not read as "nothing declared": a length too
    large to represent is the strongest possible "too large", so the
    digit-run-but-unparseable case is answered ``True`` here rather
    than deferred to :func:`read_body_capped`.

    ``require_declared_length`` settles the *undeclared* case, and it
    defaults to off so that a caller has to state that it has no
    second gate. Off, an undeclared body is deferred — the chunked gap
    the module docstring describes, closed for those callers by
    :func:`read_body_capped`. On, it is refused here, because for that
    caller nothing downstream will bound it (#1633).
    """
    raw = request.headers.get("content-length", "")
    declared = parse_int_token(raw)
    if declared is None:
        return require_declared_length or is_digit_run(raw)
    return declared > max_bytes


async def read_body_capped(request: Request, *, max_bytes: int) -> bytes | None:
    """Read the body, or return ``None`` the moment it exceeds the cap.

    ``await request.body()`` buffers whatever arrives; on a chunked
    request there is no declared length for
    :func:`declared_body_too_large` to refuse, so the header gate lets
    it through and the process pays for every byte the sender chooses
    to send. This counts instead, and abandons the read as soon as the
    running total passes ``max_bytes`` — the caller answers 413 with
    at most one chunk of overshoot held.

    ``>`` and not ``>=``: a body of exactly ``max_bytes`` is inside the
    ceiling, which is what "max" says and what the header gate already
    does. The two must agree or the same payload is accepted or
    refused depending only on whether the sender declared a length.

    The body is consumed from the stream, so ``request.body()`` and
    ``request.json()`` are not available afterwards. Every caller here
    wants the raw bytes anyway — the signature is computed over them.

    A read that FAILS raises ``HTTPException(400)`` rather than joining
    the ``None`` channel. The two are different verdicts and only one of
    them is the caller's to shape: 413 is a decision about a body we
    successfully read the start of, and each route logs it with its own
    response shape, while a failed read has no live client left to
    receive a body. Starlette raises ``ClientDisconnect`` out of
    ``stream()`` the moment it sees ``http.disconnect`` (#813), and
    nothing registers an ``Exception`` handler on this app
    (``webhook/security.py:61-72`` says the same, for the same
    reason), so letting it escape turned three dozen bytes from an
    anonymous caller into an ``Exception in ASGI application``
    traceback and a 500 — on a small host whose vhosts
    carry no rate limit. The Telegram route runs the same loop with
    the same guard inline — see the ``async for chunk in
    request.stream()`` loop in :mod:`telegram_invite_bot.webhook.server`
    (#1471: the line range this used to cite had drifted onto the
    ``Content-Length`` gate, which is a different check); this is that
    guard, put where every payment route inherits it and a fifth one
    cannot forget it.
    """
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:
                return None
            chunks.append(chunk)
    except Exception as exc:
        # Deliberately broad, like the sibling loop it mirrors: whatever
        # goes wrong reading a body an anonymous caller controls is that
        # caller's problem, not a server fault worth a traceback.
        log.warning("request body read failed after {n} bytes: {exc!r}", n=size, exc=exc)
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST, detail="failed to read request body"
        ) from exc
    return b"".join(chunks)
