"""#813: a body read that fails must not reach uvicorn as a 500.

The four payment webhooks are the only public POST endpoints that go
through :func:`read_body_capped`, and nothing registers an ``Exception``
handler on the app (as ``webhook.security.verify_secret_token`` notes),
so an escaping
``ClientDisconnect`` used to print a full traceback per request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request

from telegram_invite_bot.utils.http_body import declared_body_too_large, read_body_capped


class _FakeRequest:
    """Only ``stream()`` is touched by the function under test."""

    def __init__(self, chunks: list[bytes], *, raises: Exception | None = None) -> None:
        self._chunks = chunks
        self._raises = raises

    async def stream(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._raises is not None:
            raise self._raises


def _request(chunks: list[bytes], *, raises: Exception | None = None) -> Request:
    return cast("Request", _FakeRequest(chunks, raises=raises))


def _declaring(content_length: str | None) -> Request:
    """Only ``headers`` is touched by :func:`declared_body_too_large`."""
    headers = {} if content_length is None else {"content-length": content_length}
    return cast("Request", SimpleNamespace(headers=headers))


async def test_reads_a_whole_body() -> None:
    assert await read_body_capped(_request([b"ab", b"cd"]), max_bytes=16) == b"abcd"


async def test_a_body_of_exactly_max_bytes_is_inside_the_ceiling() -> None:
    """``>`` and not ``>=`` — it has to agree with the header gate."""
    assert await read_body_capped(_request([b"abcd"]), max_bytes=4) == b"abcd"


async def test_oversized_chunked_body_returns_none() -> None:
    """The 413 channel: a body we read the start of and refused."""
    assert await read_body_capped(_request([b"ab", b"cd", b"ef"]), max_bytes=4) is None


async def test_client_disconnect_becomes_a_400_not_a_traceback() -> None:
    """#813: this used to escape into uvicorn as ``Exception in ASGI
    application`` and answer 500 — for three dozen bytes and a
    ``close()`` from an anonymous caller."""
    with pytest.raises(HTTPException) as excinfo:
        await read_body_capped(_request([b"ab"], raises=ClientDisconnect()), max_bytes=16)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "failed to read request body"


async def test_a_disconnect_past_the_cap_is_still_413() -> None:
    """Order matters: the cap is checked per chunk, so a stream that
    overshoots and *then* dies never reaches the raise."""
    request = _request([b"abcdef"], raises=ClientDisconnect())
    assert await read_body_capped(request, max_bytes=4) is None


def test_a_declared_length_inside_the_cap_is_read() -> None:
    assert not declared_body_too_large(_declaring("4"), max_bytes=4)
    assert declared_body_too_large(_declaring("5"), max_bytes=4)


def test_a_missing_or_junk_length_defers_to_the_capped_read() -> None:
    """No declaration is not a refusal — the chunk counter handles it."""
    assert not declared_body_too_large(_declaring(None), max_bytes=4)
    assert not declared_body_too_large(_declaring(""), max_bytes=4)
    assert not declared_body_too_large(_declaring("abc"), max_bytes=4)
    assert not declared_body_too_large(_declaring("4 "), max_bytes=4)


@pytest.mark.parametrize("raw", [None, "", "abc", "4 "])
def test_require_declared_length_refuses_what_the_default_defers(raw: str | None) -> None:
    """#1633: the caller that has no second gate says so here.

    The contact form and the guide editor both end in
    ``await request.form()``, whose urlencoded parser buffers the
    stream without a ceiling, so for them an undeclared body is not a
    gap to defer — it is the whole attack.
    """
    assert not declared_body_too_large(_declaring(raw), max_bytes=4)
    assert declared_body_too_large(_declaring(raw), max_bytes=4, require_declared_length=True)


def test_require_declared_length_leaves_a_declared_body_alone() -> None:
    """It settles the undeclared case and nothing else."""
    assert not declared_body_too_large(_declaring("4"), max_bytes=4, require_declared_length=True)
    assert declared_body_too_large(_declaring("5"), max_bytes=4, require_declared_length=True)


def test_a_length_too_large_to_represent_is_still_refused() -> None:
    """#1042 narrowed ``parse_int_token`` to what SQLite can store.

    This gate must not read that ``None`` as "nothing declared": a
    length past 2**63-1 is the strongest possible "too large", and
    refusing it here is what keeps the promise of answering without
    reading a byte.
    """
    assert declared_body_too_large(_declaring("9" * 25), max_bytes=4)
    assert declared_body_too_large(_declaring(str(2**63)), max_bytes=4)
