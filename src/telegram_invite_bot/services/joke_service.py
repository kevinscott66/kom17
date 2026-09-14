"""Online joke source for ``/joke`` — RR-6 #72.

Legacy ``cmd_joke`` (bot.py:17277) tries the network first and only
falls back to its static pool when the network is down:

1. **JokeAPI v2** (``v2.jokeapi.dev``) with safe-mode and the whole
   nsfw/religious/political/racist/sexist/explicit blacklist, over a
   rotating subset of the Misc / Pun / Spooky categories.
2. **icanhazdadjoke** as the reserve when JokeAPI has nothing.
3. For a Russian-speaking user, an **EN→RU translation** through the
   keyless public Lingva endpoint.

The port kept only step 0 — the offline pool — so ``/joke`` served the
same finite list forever. This module restores 1-3 as an async httpx
client shaped like :class:`~telegram_invite_bot.services.weather_service.WeatherService`
(injectable client, one service instance per process). The ``client``
seam is exercised by tests only: no production call site passes one, so
every online fetch still opens its own connection (#423).

Deliberate divergences from legacy:

* **Every failure is ``None``**, never a raised exception and never a
  user-facing error string. The caller always has a local pool, so the
  correct degradation is "an offline joke", not "❌". That also means an
  outage of a free third-party humour API can never take down a command.
* **A Russian user gets the pool, not an English joke**, when the
  translator fails. Legacy had the same effect by accident (its
  ``_fetch_joke_from_internet`` returns ``None`` on that branch); making
  it explicit matters because the alternative — shipping the untranslated
  English text — is the kind of "technically answered" reply that reads
  as a bug.
* **Bounded output.** Legacy caps at 3500 chars, which is under
  Telegram's 4096 but says nothing about what a hostile or broken
  upstream could return in the meantime; we cap the same way AND require
  the text to survive a whitespace/control-character normalisation, so a
  payload of newlines can't render as a blank message.
* **No retry storm.** Legacy retries JokeAPI three times in a loop with
  a 12s timeout each — up to 36 seconds of a user staring at nothing
  before the reserve is even tried. Two attempts at 6s each, then the
  reserve, keeps the worst case bounded to roughly a Telegram
  send-timeout.

``enabled=False`` (wired from ``JOKE_OFFLINE_ONLY``) reproduces legacy's
kill switch: the service short-circuits to ``None`` and ``/joke`` is
purely offline again, which is what an operator wants when the upstream
starts misbehaving and a redeploy is not on the table.
"""

from __future__ import annotations

import random
import re
from typing import Any, Final
from urllib.parse import quote

import httpx
from loguru import logger

from telegram_invite_bot.utils.http_read import send_capped

log = logger.bind(component="services.joke")

_JOKEAPI_URL: Final = "https://v2.jokeapi.dev/joke/"
_ICANHAZ_URL: Final = "https://icanhazdadjoke.com/"
_LINGVA_URL: Final = "https://lingva.ml/api/v1/en/ru/"

# Legacy's category rotation (bot.py:17163) minus nothing — Programming
# is excluded by never being listed, same as legacy.
_CATEGORIES: Final = ("Misc,Pun", "Pun", "Misc", "Spooky", "Misc,Spooky")

_BLACKLIST: Final = "nsfw,religious,political,racist,sexist,explicit"

# One UA for every outbound call. Both APIs are public and keyless;
# identifying the client is politeness, not authentication.
_USER_AGENT: Final = "TelegramBot/1.0 (KomBot/joke; +https://telegram.org)"

#: Legacy's ceiling (bot.py:17142). Telegram's own limit is 4096.
_MAX_CHARS: Final = 3500

#: Anything the translator is asked to handle is bounded first — the
#: endpoint takes the text in the URL PATH, and a multi-kilobyte path is
#: rejected by intermediaries long before it reaches Lingva.
_MAX_TRANSLATE_CHARS: Final = 1200

#: Control characters that have no business in a chat message. ``\n`` and
#: ``\t`` are deliberately absent — a two-part joke is two lines.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize(text: str | None) -> str | None:
    """Normalise upstream text, or ``None`` if nothing usable is left.

    Runs before the caller ever sees the string, so a broken upstream
    can only ever cost us a fallback to the local pool.
    """
    if not text:
        return None
    cleaned = _CONTROL_RE.sub("", text.replace("\r\n", "\n")).strip()
    # Collapse the runs of blank lines some APIs use as padding; a
    # deliberate setup/delivery break is exactly one blank line.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if not cleaned:
        return None
    if len(cleaned) > _MAX_CHARS:
        cleaned = cleaned[: _MAX_CHARS - 3] + "..."
    return cleaned


def parse_jokeapi_payload(data: Any) -> str | None:
    """JokeAPI's two response shapes → one string (legacy bot.py:17147).

    Public because the shape is worth pinning in a unit test without
    standing up a transport.
    """
    if not isinstance(data, dict) or data.get("error"):
        return None
    if data.get("type") == "single":
        return (data.get("joke") or "").strip() or None
    if data.get("type") == "twopart":
        setup = (data.get("setup") or "").strip()
        delivery = (data.get("delivery") or "").strip()
        if setup and delivery:
            return f"{setup}\n\n{delivery}"
        return (setup or delivery) or None
    return None


class JokeService:
    """Network joke fetcher with a hard "never raise" contract."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 6.0,
        enabled: bool = True,
        attempts: int = 2,
    ) -> None:
        # Tests pass an ``AsyncClient`` bound to a ``MockTransport``.
        # Production passes nothing (#423), so ``fetch`` takes the
        # ``async with httpx.AsyncClient(...)`` branch below and pays a
        # TLS handshake for a one-line joke. The seam is kept for the
        # day a lifespan-scoped client is wired; that is a separate
        # change.
        self._client = client
        self._timeout = timeout
        self._enabled = enabled
        self._attempts = max(1, attempts)

    async def fetch(self, lang: str) -> str | None:
        """A fresh joke in ``lang``, or ``None`` — caller falls back."""
        if not self._enabled:
            return None
        try:
            if self._client is not None:
                return await self._fetch_with(self._client, lang)
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await self._fetch_with(client, lang)
        except Exception as exc:  # noqa: BLE001 — the pool is the fallback
            # A joke is never worth an exception reaching the dispatcher.
            # The per-call helpers already swallow httpx errors; this is
            # the backstop for anything they cannot anticipate (a broken
            # JSON decoder, a client closed under us at shutdown).
            log.warning("joke fetch failed: {e!r}", e=exc)
            return None

    async def _fetch_with(self, client: httpx.AsyncClient, lang: str) -> str | None:
        english = await self._from_jokeapi(client) or await self._from_icanhaz(client)
        if english is None:
            return None
        if lang == "en":
            return english
        # Any non-English UI language means the joke has to be
        # translated; only RU has a translation route today, and a
        # failed translation degrades to the local pool rather than
        # shipping English text to a Russian speaker.
        return await self._translate_to_ru(client, english)

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, str] | None = None,
        accept: str | None = None,
    ) -> Any | None:
        """GET + JSON-decode, or ``None`` on any transport/parse failure."""
        headers = {"User-Agent": _USER_AGENT}
        if accept:
            headers["Accept"] = accept
        try:
            response = await send_capped(
                client, "GET", url, params=params, headers=headers, timeout=self._timeout
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("joke upstream {u} failed: {e!r}", u=url, e=exc)
            return None

    async def _from_jokeapi(self, client: httpx.AsyncClient) -> str | None:
        for _ in range(self._attempts):
            data = await self._get_json(
                client,
                _JOKEAPI_URL + random.choice(_CATEGORIES),  # noqa: S311 — not crypto
                params={
                    "blacklistFlags": _BLACKLIST,
                    # JokeAPI reads the mere PRESENCE of this key; the
                    # empty value is the documented form and is what
                    # legacy sent.
                    "safe-mode": "",
                    "type": random.choice(("single", "twopart")),  # noqa: S311
                },
            )
            text = _sanitize(parse_jokeapi_payload(data))
            if text:
                return text
        return None

    async def _from_icanhaz(self, client: httpx.AsyncClient) -> str | None:
        data = await self._get_json(client, _ICANHAZ_URL, accept="application/json")
        if not isinstance(data, dict):
            return None
        return _sanitize(data.get("joke"))

    async def _translate_to_ru(self, client: httpx.AsyncClient, text: str) -> str | None:
        """EN→RU through the keyless public Lingva instance.

        The joke goes in the URL path, so it is length-bounded and
        percent-encoded with ``safe=""`` — a joke containing ``/`` or
        ``?`` would otherwise change which endpoint we call.
        """
        payload = text if len(text) <= _MAX_TRANSLATE_CHARS else text[:_MAX_TRANSLATE_CHARS]
        data = await self._get_json(client, _LINGVA_URL + quote(payload, safe=""))
        if not isinstance(data, dict):
            return None
        return _sanitize(data.get("translation"))
