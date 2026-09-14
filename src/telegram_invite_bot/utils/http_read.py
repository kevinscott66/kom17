"""Bounded reads of third-party HTTP responses (#119).

Every outbound integration in this bot — Open-Meteo, JokeAPI,
icanhazdadjoke, exchangerate-api, DeepSeek, Whisper, RollyPay, Crypto
Pay — used to be called as ``await client.get(...)`` / ``.post(...)``,
which reads the body to completion into memory before returning. httpx
has a connect/read *timeout* but no size ceiling: a peer that answers
``200`` and then streams gigabytes at a steady trickle never trips the
read timeout (each chunk arrives well inside it) and the process grows
until the OOM killer takes it. One process serves every chat here, so
that is a full outage triggered by a single upstream — a compromised or
merely broken partner, or anything that can MITM one plain hostname.

:func:`send_capped` is the replacement call shape. It streams the
response and stops the moment the body passes ``max_bytes``, raising
:class:`ResponseTooLarge`.

Why the exception subclasses ``httpx.HTTPError``
================================================
Every call site already wraps its request in ``except httpx.HTTPError``
and degrades — a weather lookup that fails is "нет данных", a joke that
fails falls back to the local pool, a payment that fails is reported as
unavailable rather than credited. An over-long body IS that same
category of event ("the response could not be read"), so inheriting
from ``httpx.HTTPError`` routes it into the handling each service
already has, instead of adding a second failure path to eight modules
that would each need its own test.

The returned object is a plain :class:`httpx.Response` carrying the
bytes we did read, so callers keep using ``.status_code``, ``.json()``
and ``.text`` unchanged. Headers describing the *encoding* of the
original byte stream are dropped when rebuilding it — ``aiter_bytes``
yields content-decoded bytes, so carrying ``Content-Encoding: gzip``
forward would tell httpx to gunzip an already-gunzipped body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Default ceiling. Every response this bot reads is a JSON document:
#: the largest in normal operation is exchangerate-api's full rate table
#: at roughly 30 KB, and a maximal DeepSeek completion is of the same
#: order. 4 MiB is ~100x headroom over anything legitimate while still
#: being a rounding error against the process's memory — the point is to
#: bound the damage, not to police payload sizes.
DEFAULT_MAX_RESPONSE_BYTES: Final[int] = 4 * 1024 * 1024

#: Headers that describe how the *wire* bytes were framed or encoded.
#: They do not survive the read: what we hand back is the decoded body
#: with a length of its own.
_STRIPPED_HEADERS: Final[frozenset[str]] = frozenset(
    {"content-encoding", "content-length", "transfer-encoding"}
)


class ResponseTooLarge(httpx.HTTPError):
    """Upstream body passed the cap and the read was abandoned."""


def _carried_headers(headers: httpx.Headers) -> Sequence[tuple[str, str]]:
    """Response headers minus the framing/encoding ones.

    ``Content-Type`` is deliberately kept: ``Response.text`` reads the
    charset off it, so dropping it would silently change how a non-UTF-8
    body decodes.
    """
    return [
        (name, value)
        for name, value in headers.multi_items()
        if name.lower() not in _STRIPPED_HEADERS
    ]


async def send_capped(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    **kwargs: Any,
) -> httpx.Response:
    """``client.<method>(url, **kwargs)`` with the body read bounded.

    Accepts the same keyword arguments as the client's verb methods
    (``params``, ``json``, ``headers``, ``content``, ``data``,
    ``files``) — they are forwarded to ``build_request`` verbatim.

    Raises :class:`ResponseTooLarge` as soon as the accumulated body
    exceeds ``max_bytes``; the connection is released by the ``finally``
    below, so an abandoned read costs one aborted stream rather than a
    leaked connection. Note this is the *decoded* size — a compressed
    body is measured after decompression, which is the number that
    matters for memory (and makes a zip bomb cost us ``max_bytes``, not
    its expanded size).
    """
    request = client.build_request(method, url, **kwargs)
    response = await client.send(request, stream=True)
    try:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                # HOST only, never the full URL. Call sites log this
                # exception verbatim (``currency_service._fetch_with``
                # does), and exchangerate-api carries its API key in the
                # URL *path* (``currency_service._url``), so
                # interpolating ``url`` wrote a live credential into
                # journald the first time that upstream misbehaved.
                raise ResponseTooLarge(
                    f"{method} {httpx.URL(url).host}: response body exceeded {max_bytes} bytes"
                )
            chunks.append(chunk)
    finally:
        await response.aclose()

    return httpx.Response(
        response.status_code,
        headers=_carried_headers(response.headers),
        content=b"".join(chunks),
        request=request,
    )
