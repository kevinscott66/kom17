"""Weather lookup via the free Open-Meteo API.

Matches the data source the legacy monolith uses (see ``bot.py`` around
the ``maybe_send_weather_reply`` block). Two HTTP calls:

1. Geocoding — ``https://geocoding-api.open-meteo.com/v1/search`` →
   ``(latitude, longitude, name, admin1, country, timezone)`` for a
   free-form query.
2. Forecast — ``https://api.open-meteo.com/v1/forecast`` → current
   conditions + today's min/max, or a multi-day daily series.

Everything the Stage 5 docstring said was missing has since landed: the
forecast call is served from a per-instance TTL cache (M-P-9), the saved
``/city`` is the default the handlers fall back to (RR-6 #74), and the
geocoder is retried across spelling variants so an oblique-case city name
out of a free-form phrase still resolves (RR-6 #68 —
:func:`telegram_invite_bot.core.weather_query.geocode_variants`).

Parsing free-form phrases is deliberately NOT this module's job; it does
network I/O and nothing else. The regexes live in ``core.weather_query``,
where they can be tested without a transport.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from telegram_invite_bot.core.weather_query import geocode_variants
from telegram_invite_bot.utils.http_read import send_capped
from telegram_invite_bot.utils.keyed_locks import KeyedLocks

log = logger.bind(component="services.weather")

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Hard cap on the in-memory forecast cache. At ~1 KB per WeatherReport
# this bounds the cache at well under a megabyte while comfortably
# covering every realistic (city, day) working set — a bot serving even
# a few hundred distinct cities a day stays warm. Oldest-written entry
# is evicted first (see ``_cache_put``).
_CACHE_MAX_ENTRIES = 512

# Subset of the Open-Meteo WMO weather codes we surface in replies.
# Reference: https://open-meteo.com/en/docs#weathervariables
_WMO_RU: dict[int, str] = {
    0: "Ясно ☀️",
    1: "Преимущественно ясно 🌤",
    2: "Переменная облачность ⛅",
    3: "Пасмурно ☁️",
    45: "Туман 🌫",
    48: "Изморозь 🌫",
    51: "Лёгкая морось 🌦",
    53: "Морось 🌦",
    55: "Сильная морось 🌧",
    56: "Ледяная морось 🌧",
    57: "Сильная ледяная морось 🌧",
    61: "Небольшой дождь 🌦",
    63: "Дождь 🌧",
    65: "Сильный дождь 🌧",
    66: "Ледяной дождь 🌧",
    67: "Сильный ледяной дождь ❄️",
    71: "Небольшой снег 🌨",
    73: "Снег 🌨",
    75: "Сильный снег ❄️",
    77: "Снежные зёрна 🌨",
    80: "Ливень 🌧",
    81: "Сильный ливень 🌧",
    82: "Очень сильный ливень ⛈",
    85: "Снежный заряд 🌨",
    86: "Сильный снежный заряд ❄️",
    95: "Гроза ⛈",
    96: "Гроза с градом ⛈",
    99: "Сильная гроза с градом ⛈",
}

# English labels for the same WMO codes, with the SAME emoji as the RU
# table. Used by :func:`wmo_label` so English users never see the
# pre-rendered Russian ``condition`` (the ru/en convergence bug).
_WMO_EN: dict[int, str] = {
    0: "Clear ☀️",
    1: "Mainly clear 🌤",
    2: "Partly cloudy ⛅",
    3: "Overcast ☁️",
    45: "Fog 🌫",
    48: "Rime fog 🌫",
    51: "Light drizzle 🌦",
    53: "Drizzle 🌦",
    55: "Heavy drizzle 🌧",
    56: "Freezing drizzle 🌧",
    57: "Heavy freezing drizzle 🌧",
    61: "Light rain 🌦",
    63: "Rain 🌧",
    65: "Heavy rain 🌧",
    66: "Freezing rain 🌧",
    67: "Heavy freezing rain ❄️",
    71: "Light snow 🌨",
    73: "Snow 🌨",
    75: "Heavy snow ❄️",
    77: "Snow grains 🌨",
    80: "Rain showers 🌧",
    81: "Heavy rain showers 🌧",
    82: "Violent rain showers ⛈",
    85: "Snow showers 🌨",
    86: "Heavy snow showers ❄️",
    95: "Thunderstorm ⛈",
    96: "Thunderstorm with hail ⛈",
    99: "Heavy thunderstorm with hail ⛈",
}


def wmo_label(code: int | None, lang: str) -> str:
    """Localized human label for a WMO weather code.

    Returns the ``lang``-appropriate label (with emoji). ``None`` →
    "Нет данных"/"No data". Any code missing from the tables falls back
    to the same no-data label so an unexpected upstream code never leaks
    a bare integer or the wrong language.
    """
    if lang == "en":
        if code is None:
            return "No data"
        return _WMO_EN.get(code, "No data")
    if code is None:
        return "Нет данных"
    return _WMO_RU.get(code, "Нет данных")


def _geo_text(geo: dict[str, Any], key: str) -> str | None:
    """A non-empty string field off a geocoding hit, else ``None``.

    Open-Meteo omits ``admin1`` for city-states and occasionally returns
    it as a non-string; both must read as "no region" rather than as the
    literal ``None`` in a rendered card.
    """
    value = geo.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _finite(value: object) -> float | None:
    """A finite float off an upstream JSON value, else ``None``.

    Python's ``json`` decoder accepts the non-standard ``NaN``,
    ``Infinity`` and ``-Infinity`` tokens by default, so an upstream
    hiccup can hand us a float that is *numeric* by ``isinstance`` and
    still unusable: ``int(nan)`` raises ``ValueError``, ``int(inf)``
    raises ``OverflowError``, and either one formats into a card as
    literal "nan"/"inf". Every numeric read from a weather payload goes
    through here so those land as "no data" — the same outcome as a
    missing field — instead of a 500 on a `/weather` call.

    ``bool`` is rejected explicitly: it passes ``isinstance(x, int)``
    and would silently read as 0/1 degrees.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _finite_int(value: object) -> int | None:
    """:func:`_finite`, truncated to ``int``. ``None`` stays ``None``."""
    number = _finite(value)
    return None if number is None else int(number)


class WeatherLookupError(Exception):
    """Raised when the upstream API is unreachable or returns garbage.

    Distinct from :class:`CityNotFoundError` so the handler can phrase
    the user-facing error appropriately (transient vs. user-input).
    """


class CityNotFoundError(Exception):
    """Raised when geocoding returns zero results for the query."""


@dataclass(frozen=True, slots=True)
class WeatherReport:
    city: str
    country: str | None
    # Optional like every other reading (#422). It used to be a bare
    # ``float`` degraded to 0.0, which is indistinguishable from a real
    # freezing reading and, worse, reached the AI prompt as a fact.
    temperature_c: float | None
    feels_like_c: float | None
    condition: str
    humidity_pct: int | None
    wind_kmh: float | None
    temp_min_c: float | None
    temp_max_c: float | None
    # Raw WMO code so the handler can localize the condition via
    # ``wmo_label`` for the resolved language. ``condition`` stays the
    # pre-rendered RU label for back-compat with any reader that still
    # reads it.
    weather_code: int | None = None
    # First-level administrative area ("Краснодарский край"), when the
    # geocoder knows one. RR-6 #68: legacy titled the card
    # ``City (region, country)`` and the port dropped the region, which
    # is the half that disambiguates the dozen Springfields.
    region: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedCity:
    """What the geocoder knows about a city name (RR-6 #74).

    ``/city`` stores :attr:`name` — the geocoder's canonical spelling —
    rather than the raw input, so ``/city краснодар`` and
    ``/city  КРАСНОДАР `` both end up as ``Краснодар`` and the profile
    card reads like a person wrote it.

    :attr:`timezone` rides along because the same geocoding response
    carries it; ``/city`` offers it as a hint rather than writing it,
    since silently changing a second setting the user didn't ask about
    is the kind of helpfulness nobody thanks you for.
    """

    name: str
    country: str | None
    timezone: str | None
    # First-level admin area, when the geocoder knows one. Not stored by
    # ``/city`` — the column holds the city, and a region that changed
    # spelling upstream would silently invalidate the saved value — but
    # the confirmation card can name it, which is how a user notices
    # they just saved the Krasnodar in the wrong country.
    region: str | None = None


@dataclass(frozen=True, slots=True)
class ForecastDay:
    date_iso: str
    temp_min_c: float | None
    temp_max_c: float | None
    weather_code: int | None


@dataclass(frozen=True, slots=True)
class ForecastReport:
    city: str
    country: str | None
    days: list[ForecastDay]
    region: str | None = None


class WeatherService:
    """Open-Meteo client with an in-memory TTL cache (M-P-9).

    Designed to be shared across requests: the cache is per-instance,
    so one :class:`WeatherService` (constructed in
    :func:`routers.main_router.build_main_router` and closed over by the
    handlers) gives every user access to the same warm entries. A
    short-lived per-call instance would defeat the cache — the audit
    explicitly called this out as a pre-fix bug ("fresh AsyncClient
    per call"). Only the *cache* half of that fix landed: ``client`` is
    never filled in production, so an uncached lookup still opens a
    fresh connection (#423).

    Cache shape: keyed by the resolved ``(lat, lon, utc_date)``
    triple. The geocoding call still happens per query (cheap,
    cached separately by the upstream API), but the heavier
    forecast call is served from memory for any (lat, lon) the
    cache has seen within the TTL window for today's UTC date.
    Crossing into the next UTC day evicts the entry implicitly
    because the key includes the date.

    Default TTL is 1 hour (3600s) per audit M-P-9 spec.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        cache_ttl_seconds: float = 3600.0,
    ) -> None:
        # Tests pass an ``AsyncClient`` bound to a ``MockTransport``.
        # Production callers pass nothing today (#423) — the seam is
        # kept for the separate change that adds a lifespan-scoped
        # client; until then ``None`` means one connection per call.
        self._client = client
        self._timeout = timeout
        self._cache_ttl = cache_ttl_seconds
        # Cache value is (timestamp, WeatherReport). The TTL/date key
        # gives lazy eviction, but only for keys that get RE-queried —
        # a city looked up once and never again would otherwise live
        # until process restart. In a long-running webhook process that
        # set grows without bound. ``_CACHE_MAX_ENTRIES`` caps it with
        # LRU-by-write eviction (insertion-ordered dict → O(1) evict of
        # the oldest entry). The dict is insertion-ordered, so the first
        # key is always the least-recently-written.
        self._cache: dict[tuple[float, float, str], tuple[float, WeatherReport]] = {}
        # Serialises *misses* on one key, so an expiry does not turn
        # into one Open-Meteo call per waiting user. The popular cities
        # are exactly the ones several people ask about within the same
        # second, and they share a cache key — without this, every one
        # of them pays the full forecast latency and spends a slot of a
        # rate limit the whole bot shares. Per key rather than one
        # global lock: a lookup for Москва has no reason to queue
        # behind one for Владивостока. ``KeyedLocks`` frees a slot when
        # its last waiter leaves, so the registry does not grow with the
        # set of cities ever asked about.
        self._fill_locks: KeyedLocks[tuple[float, float, str]] = KeyedLocks()

    def _now(self) -> float:
        """Monotonic time hook — overridable in tests."""
        return datetime.now(UTC).timestamp()

    def _cache_key(self, lat: float, lon: float) -> tuple[float, float, str]:
        """Round coords + take UTC date so close-by queries share."""
        utc_date = datetime.now(UTC).date().isoformat()
        # Round to 2dp (~1km) so "Москва" and "москва, центр" hit
        # the same entry — the forecast resolution is coarser than
        # that anyway.
        return (round(lat, 2), round(lon, 2), utc_date)

    def _cache_get(self, key: tuple[float, float, str]) -> WeatherReport | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, report = entry
        if self._now() - ts > self._cache_ttl:
            # Stale — drop so the cache doesn't grow indefinitely
            # for keys nobody re-queries today.
            self._cache.pop(key, None)
            return None
        return report

    def _cache_put(self, key: tuple[float, float, str], report: WeatherReport) -> None:
        # Pop-then-set so a re-written key moves to the end (newest) of
        # the insertion order — turns the dict into a cheap LRU-by-write.
        self._cache.pop(key, None)
        self._cache[key] = (self._now(), report)
        # Hard cap: evict the oldest-written entries until under the
        # bound. ``next(iter(...))`` is the least-recently-written key.
        while len(self._cache) > _CACHE_MAX_ENTRIES:
            self._cache.pop(next(iter(self._cache)), None)

    async def lookup(self, query: str) -> WeatherReport:
        query = query.strip()
        if not query:
            raise CityNotFoundError("empty query")

        if self._client is not None:
            return await self._lookup_with(self._client, query)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._lookup_with(client, query)

    async def resolve_city(self, query: str) -> ResolvedCity:
        """Geocode ``query`` to its canonical name (RR-6 #74).

        Raises :class:`CityNotFoundError` for a name the geocoder can't
        place and :class:`WeatherLookupError` if the geocoder itself is
        unreachable — the two need different words on screen ("check the
        spelling" vs "try later"), and only the first is the user's
        fault. ``/city`` additionally treats the second as *non-fatal*
        and saves the raw input anyway; see the handler for why.

        No caching: this runs once per ``/city`` call, which a user
        issues roughly never. The forecast endpoint is the one worth
        caching, and :meth:`lookup` already does.
        """
        query = query.strip()
        if not query:
            raise CityNotFoundError("empty query")
        if self._client is not None:
            return await self._resolve_city_with(self._client, query)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._resolve_city_with(client, query)

    async def _resolve_city_with(self, client: httpx.AsyncClient, query: str) -> ResolvedCity:
        geo = await self._geocode(client, query)
        return ResolvedCity(
            # The geocoder is the authority on spelling, but it has
            # returned odd payloads before — every string field goes
            # through :func:`_geo_text`, which is the one place holding
            # the isinstance guard — and the name falls back to the
            # user's own wording rather than storing ``None`` or the
            # literal "None".
            name=_geo_text(geo, "name") or query,
            country=_geo_text(geo, "country"),
            timezone=_geo_text(geo, "timezone"),
            region=_geo_text(geo, "admin1"),
        )

    async def _lookup_with(self, client: httpx.AsyncClient, query: str) -> WeatherReport:
        geo = await self._geocode(client, query)
        lat = _finite(geo.get("latitude"))
        lon = _finite(geo.get("longitude"))
        if lat is None or lon is None:
            raise WeatherLookupError("geocoding returned non-numeric coords")

        # M-P-9: cache hit short-circuits the forecast HTTP call.
        # Geocoding still runs (it gives us the lat/lon key); a
        # future optimisation could cache geocoding too, but its
        # per-query latency is small and the upstream rate limit
        # mostly bites on the forecast endpoint.
        cache_key = self._cache_key(lat, lon)
        cached = self._cache_get(cache_key)
        if cached is not None:
            log.bind(lat=lat, lon=lon).info("weather cache hit")
            return cached

        async with self._fill_locks.acquire(cache_key):
            # Re-read inside the lock: by the time a waiter is admitted
            # the coroutine ahead of it has usually already filled the
            # entry, and that second read is what turns N upstream calls
            # into one. The hit path above stays outside the lock — a
            # warm entry must not queue behind anyone.
            cached = self._cache_get(cache_key)
            if cached is not None:
                log.bind(lat=lat, lon=lon).info("weather cache hit after wait")
                return cached
            report = await self._build_report(
                client, geo=geo, query=query, lat=float(lat), lon=float(lon)
            )
            # M-P-9: populate the cache so a subsequent lookup for the
            # same coords in the same UTC day is served warm.
            self._cache_put(cache_key, report)
            return report

    async def _build_report(
        self,
        client: httpx.AsyncClient,
        *,
        geo: dict[str, Any],
        query: str,
        lat: float,
        lon: float,
    ) -> WeatherReport:
        """Fetch the forecast for already-geocoded coords and shape it.

        Split out of :meth:`_lookup_with` so the whole upstream call
        sits inside the per-key fill lock and nothing else does.
        """
        forecast = await self._forecast(client, float(lat), float(lon))
        current_raw = forecast.get("current", {})
        daily_raw = forecast.get("daily", {})
        current: dict[str, Any] = current_raw if isinstance(current_raw, dict) else {}
        daily: dict[str, Any] = daily_raw if isinstance(daily_raw, dict) else {}

        def _first(seq: object) -> float | None:
            return _finite(seq[0]) if isinstance(seq, list) and seq else None

        code = _finite_int(current.get("weather_code"))
        condition = _WMO_RU.get(code, "Нет данных") if code is not None else "Нет данных"

        return WeatherReport(
            city=_geo_text(geo, "name") or query,
            country=_geo_text(geo, "country"),
            temperature_c=_finite(current.get("temperature_2m")),
            feels_like_c=_finite(current.get("apparent_temperature")),
            condition=condition,
            humidity_pct=_finite_int(current.get("relative_humidity_2m")),
            wind_kmh=_finite(current.get("wind_speed_10m")),
            temp_min_c=_first(daily.get("temperature_2m_min")),
            temp_max_c=_first(daily.get("temperature_2m_max")),
            weather_code=code,
            region=_geo_text(geo, "admin1"),
        )

    async def forecast(self, query: str, *, days: int = 7) -> ForecastReport:
        """Multi-day forecast for a free-form city query.

        Geocodes via :meth:`_geocode` (same path as :meth:`lookup`),
        then hits the daily forecast endpoint. ``days`` is clamped to
        1..10 (Open-Meteo's free daily horizon).

        Caching: the forecast endpoint is hit fresh every call here. The
        per-day cache that :meth:`lookup` uses is keyed on a single
        ``WeatherReport`` for *current* conditions; multi-day responses
        have a different shape and a different ``days`` axis, so reusing
        that cache would complicate the key for little gain (forecast is
        a far lower-traffic command). A dedicated forecast cache can be
        added later if traffic warrants it.
        """
        query = query.strip()
        if not query:
            raise CityNotFoundError("empty query")
        days = max(1, min(10, days))

        if self._client is not None:
            return await self._forecast_lookup_with(self._client, query, days)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._forecast_lookup_with(client, query, days)

    async def _forecast_lookup_with(
        self, client: httpx.AsyncClient, query: str, days: int
    ) -> ForecastReport:
        geo = await self._geocode(client, query)
        lat = _finite(geo.get("latitude"))
        lon = _finite(geo.get("longitude"))
        if lat is None or lon is None:
            raise WeatherLookupError("geocoding returned non-numeric coords")

        payload = await self._daily_forecast(client, lat, lon, days)
        daily_raw = payload.get("daily", {})
        daily: dict[str, Any] = daily_raw if isinstance(daily_raw, dict) else {}

        dates = daily.get("time")
        codes = daily.get("weather_code")
        mins = daily.get("temperature_2m_min")
        maxs = daily.get("temperature_2m_max")
        dates_list = dates if isinstance(dates, list) else []

        def _at(seq: object, idx: int) -> object:
            return seq[idx] if isinstance(seq, list) and idx < len(seq) else None

        def _num(seq: object, idx: int) -> float | None:
            return _finite(_at(seq, idx))

        def _code(seq: object, idx: int) -> int | None:
            return _finite_int(_at(seq, idx))

        result_days: list[ForecastDay] = [
            ForecastDay(
                date_iso=str(date),
                temp_min_c=_num(mins, idx),
                temp_max_c=_num(maxs, idx),
                weather_code=_code(codes, idx),
            )
            for idx, date in enumerate(dates_list)
        ]

        return ForecastReport(
            city=_geo_text(geo, "name") or query,
            country=_geo_text(geo, "country"),
            days=result_days,
            region=_geo_text(geo, "admin1"),
        )

    async def _daily_forecast(
        self, client: httpx.AsyncClient, latitude: float, longitude: float, days: int
    ) -> dict[str, Any]:
        try:
            response = await send_capped(
                client,
                "GET",
                _FORECAST_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "forecast_days": days,
                    "timezone": "auto",
                },
            )
        except httpx.HTTPError as exc:
            log.warning("forecast HTTP error: {e}", e=exc)
            raise WeatherLookupError("forecast failed") from exc

        if response.status_code != 200:
            raise WeatherLookupError(f"forecast HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherLookupError("forecast non-JSON response") from exc
        if not isinstance(payload, dict):
            raise WeatherLookupError("forecast non-object response")
        return payload

    async def _geocode(self, client: httpx.AsyncClient, query: str) -> dict[str, Any]:
        """Resolve ``query`` to a geocoding hit, trying spelling variants.

        RR-6 #68: a city pulled out of a free-form phrase arrives in an
        oblique case ("погода в Казани" → "Казани"), which the geocoder
        does not resolve. :func:`geocode_variants` supplies the fallbacks
        — raw first, so a name that resolves as typed is never rewritten,
        and the heuristic ones only cost a request in the case that was
        already heading for "city not found".

        A transport failure on the FIRST variant still raises
        :class:`WeatherLookupError`: the upstream being down is not a
        "no such city", and hammering it twice more to confirm that would
        be the wrong instinct.
        """
        variants = geocode_variants(query) or [query]
        for index, name in enumerate(variants):
            hit = await self._geocode_once(client, name, allow_network_retry=index > 0)
            if hit is not None:
                return hit
        raise CityNotFoundError(query)

    async def _geocode_once(
        self, client: httpx.AsyncClient, query: str, *, allow_network_retry: bool
    ) -> dict[str, Any] | None:
        """One geocoding call. ``None`` == no results (try the next variant).

        ``allow_network_retry`` decides what an upstream *failure* means
        on this attempt: on the first variant it propagates (the service
        is down — say so), on later ones it is swallowed as "this variant
        did not resolve", because by then we already know the upstream
        answers and a per-variant hiccup should not turn a plausible
        "not found" into a scary "service unavailable".
        """
        try:
            response = await send_capped(
                client,
                "GET",
                _GEOCODING_URL,
                params={"name": query, "count": 1, "language": "ru", "format": "json"},
            )
        except httpx.HTTPError as exc:
            log.bind(query=query).warning("geocoding HTTP error: {e}", e=exc)
            if allow_network_retry:
                return None
            raise WeatherLookupError("geocoding failed") from exc

        if response.status_code != 200:
            if allow_network_retry:
                return None
            raise WeatherLookupError(f"geocoding HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            if allow_network_retry:
                return None
            raise WeatherLookupError("geocoding non-JSON response") from exc

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            return None
        first = results[0]
        return first if isinstance(first, dict) else None

    async def _forecast(
        self, client: httpx.AsyncClient, latitude: float, longitude: float
    ) -> dict[str, Any]:
        try:
            response = await send_capped(
                client,
                "GET",
                _FORECAST_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "weather_code,wind_speed_10m"
                    ),
                    "daily": "temperature_2m_min,temperature_2m_max",
                    "timezone": "auto",
                    "wind_speed_unit": "kmh",
                },
            )
        except httpx.HTTPError as exc:
            log.warning("forecast HTTP error: {e}", e=exc)
            raise WeatherLookupError("forecast failed") from exc

        if response.status_code != 200:
            raise WeatherLookupError(f"forecast HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise WeatherLookupError("forecast non-JSON response") from exc
        if not isinstance(payload, dict):
            raise WeatherLookupError("forecast non-object response")
        return payload
