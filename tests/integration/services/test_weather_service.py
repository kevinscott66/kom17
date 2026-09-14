"""``WeatherService`` against a mocked Open-Meteo upstream.

Uses ``httpx.MockTransport`` rather than monkeypatching — keeps the
service code untouched and exercises the real JSON parsing path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from telegram_invite_bot.services.weather_service import (
    CityNotFoundError,
    WeatherLookupError,
    WeatherService,
)


def _geo_hit() -> dict[str, object]:
    return {
        "results": [
            {
                "name": "Москва",
                "country": "Россия",
                "latitude": 55.75,
                "longitude": 37.62,
            }
        ]
    }


def _forecast_hit() -> dict[str, object]:
    return {
        "current": {
            "temperature_2m": 7.4,
            "apparent_temperature": 4.1,
            "relative_humidity_2m": 72,
            "weather_code": 3,
            "wind_speed_10m": 14.5,
        },
        "daily": {
            "temperature_2m_min": [3.2, 1.0],
            "temperature_2m_max": [10.1, 8.0],
        },
    }


def _make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_lookup_returns_full_report() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        report = await service.lookup("Москва")

    assert report.city == "Москва"
    assert report.country == "Россия"
    assert report.temperature_c == pytest.approx(7.4)
    assert report.feels_like_c == pytest.approx(4.1)
    assert report.humidity_pct == 72
    assert report.wind_kmh == pytest.approx(14.5)
    assert report.temp_min_c == pytest.approx(3.2)
    assert report.temp_max_c == pytest.approx(10.1)
    assert "Пасмурно" in report.condition  # WMO code 3


async def test_lookup_unknown_city_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(CityNotFoundError):
            await service.lookup("Atlantis-XYZ-нету")


async def test_geocoding_http_error_raises_lookup_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")


async def test_forecast_network_error_raises_lookup_error() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            calls.append("geo")
            return httpx.Response(200, json=_geo_hit())
        calls.append("forecast")
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")
    assert calls == ["geo", "forecast"]


async def test_missing_optional_fields_collapse_to_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return httpx.Response(
            200,
            json={
                "current": {"temperature_2m": 5.0, "weather_code": 0},
                "daily": {},
            },
        )

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        report = await service.lookup("Москва")

    assert report.temperature_c == pytest.approx(5.0)
    assert report.feels_like_c is None
    assert report.humidity_pct is None
    assert report.wind_kmh is None
    assert report.temp_min_c is None
    assert report.temp_max_c is None
    assert "Ясно" in report.condition


async def test_empty_query_raises_not_found() -> None:
    service = WeatherService()
    with pytest.raises(CityNotFoundError):
        await service.lookup("   ")


# ── Defensive error-paths ────────────────────────────────────────────────
#
# Open-Meteo is a third-party API that returns 200 OK for "no results"
# (empty list), 200 OK with malformed JSON during outages, occasionally
# returns lat/lon as ``null``, and on overload status codes drift between
# 5xx and 4xx. The WeatherService validates every shape because a
# misparsed response would surface to users as a stack-trace reply
# instead of "город не найден". The branches below lock each guard.


async def test_geocoding_network_error_raises_lookup_error() -> None:
    """Symmetric to the forecast-network-error test: a transport failure
    on the *geocoding* call must surface as WeatherLookupError, not let
    the raw httpx exception escape to the handler.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns down")

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")


async def test_geocoding_non_json_body_raises_lookup_error() -> None:
    """A 200 OK with a non-JSON body (HTML error page from a proxy)
    must be caught at parse time and converted, not propagate raw
    json.JSONDecodeError.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>nope</html>")

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")


async def test_geocoding_first_result_not_dict_raises_not_found() -> None:
    """Defensive shape-check: if ``results[0]`` is a scalar / list rather
    than a dict, treat it as "no usable result" — CityNotFoundError, not
    AttributeError at the next ``.get`` call.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": ["not-a-dict"]})

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(CityNotFoundError):
            await service.lookup("Москва")


async def test_geocoding_non_numeric_coords_raises_lookup_error() -> None:
    """A geo result without numeric lat/lon must not be fed to the
    forecast call — that would either 422 or return garbage. We bail
    early with WeatherLookupError.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Nowhere",
                        "country": "??",
                        "latitude": None,
                        "longitude": None,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Nowhere")


async def test_forecast_http_non_200_raises_lookup_error() -> None:
    """Forecast endpoint returning 503 — the parallel to the geocoding
    503 test, but on the second hop.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return httpx.Response(503, json={"error": "overload"})

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")


async def test_forecast_non_json_body_raises_lookup_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return httpx.Response(200, content=b"oops not json")

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")


async def test_lookup_without_injected_client_opens_short_lived_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production callers construct ``WeatherService()`` without a
    client; the service then opens a per-call ``httpx.AsyncClient`` and
    closes it via ``async with``. Every other test injects a long-lived
    client bound to ``MockTransport`` — which means the
    ``self._client is None`` branch (and the ``async with httpx.AsyncClient(...)
    as client`` it guards) never executes in CI. A regression that
    leaks the client (e.g. dropping the ``async with`` and returning the
    naked client) would only surface in production as an open-socket
    leak.

    We patch the module's ``httpx.AsyncClient`` to a factory that
    always binds a ``MockTransport``, so the service still gets the
    fake upstream but its own short-lived-client codepath runs end to
    end. The fixture wraps ``httpx.AsyncClient`` rather than replacing
    it, so ``async with`` and ``aclose`` semantics stay real.
    """
    from telegram_invite_bot.services import weather_service as ws

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return httpx.Response(200, json=_forecast_hit())

    transport = _make_transport(handler)
    real_cls = httpx.AsyncClient

    def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("telegram_invite_bot.services.weather_service.httpx.AsyncClient", factory)
    _ = ws  # keep the import — documents WHERE we're patching

    service = WeatherService()  # NB: no client injected
    report = await service.lookup("Москва")
    assert report.city == "Москва"
    assert report.temperature_c == pytest.approx(7.4)


# ── M-P-9: TTL cache ─────────────────────────────────────────────────────


async def test_cache_hit_short_circuits_forecast_call() -> None:
    """M-P-9: a second lookup of the same city within the TTL window
    must NOT issue a second forecast HTTP request. Geocoding still
    runs (it's cheap and gives us the cache key), but the heavier
    forecast call is served from memory.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            calls.append("geo")
            return httpx.Response(200, json=_geo_hit())
        calls.append("forecast")
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        first = await service.lookup("Москва")
        second = await service.lookup("Москва")

    assert first == second
    # Two geos (one per lookup), but only ONE forecast — the second
    # was served from the TTL cache.
    assert calls == ["geo", "forecast", "geo"]


async def test_cache_evicts_oldest_when_over_capacity() -> None:
    """The cache must stay bounded: a long-running process that looks up
    many cities once each can't grow without limit. Past the hard cap,
    the oldest-written entry is evicted (LRU-by-write)."""
    from telegram_invite_bot.services import weather_service as _ws

    service = WeatherService()

    def _report(city: str) -> _ws.WeatherReport:
        return _ws.WeatherReport(
            city=city,
            country="RU",
            temperature_c=0.0,
            feels_like_c=None,
            condition="x",
            humidity_pct=None,
            wind_kmh=None,
            temp_min_c=None,
            temp_max_c=None,
        )

    # Fill exactly to capacity. Keys are distinct (lat varies).
    for i in range(_ws._CACHE_MAX_ENTRIES):
        service._cache_put((float(i), 0.0, "2026-06-05"), _report(f"c{i}"))
    assert len(service._cache) == _ws._CACHE_MAX_ENTRIES

    first_key = (0.0, 0.0, "2026-06-05")
    assert service._cache_get(first_key) is not None

    # One more write must evict the oldest (the first key) and keep the
    # cache exactly at the cap — never larger.
    new_key = (9999.0, 0.0, "2026-06-05")
    service._cache_put(new_key, _report("new"))
    assert len(service._cache) == _ws._CACHE_MAX_ENTRIES
    assert service._cache_get(first_key) is None
    assert service._cache_get(new_key) is not None


async def test_cache_rewrite_refreshes_lru_position() -> None:
    """Re-writing an existing key moves it to newest so it survives the
    next eviction — proves the pop-then-set LRU refresh, not plain FIFO."""
    from telegram_invite_bot.services import weather_service as _ws

    service = WeatherService()

    def _report(city: str) -> _ws.WeatherReport:
        return _ws.WeatherReport(
            city=city,
            country=None,
            temperature_c=0.0,
            feels_like_c=None,
            condition="x",
            humidity_pct=None,
            wind_kmh=None,
            temp_min_c=None,
            temp_max_c=None,
        )

    for i in range(_ws._CACHE_MAX_ENTRIES):
        service._cache_put((float(i), 0.0, "d"), _report(f"c{i}"))

    # Re-write the oldest key → it becomes newest.
    refreshed = (0.0, 0.0, "d")
    service._cache_put(refreshed, _report("refreshed"))
    # Now adding a new key evicts the *next*-oldest (key 1), not key 0.
    service._cache_put((10000.0, 0.0, "d"), _report("new"))

    assert service._cache_get(refreshed) is not None
    assert service._cache_get((1.0, 0.0, "d")) is None


async def test_cache_expires_after_ttl() -> None:
    """Past the TTL, the entry must be re-fetched."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        calls.append("forecast")
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client, cache_ttl_seconds=10.0)
        # Pin the clock so we can step past the TTL deterministically.
        now = [1_000_000.0]
        service._now = lambda: now[0]  # type: ignore[method-assign]

        await service.lookup("Москва")
        now[0] += 11.0  # past TTL
        await service.lookup("Москва")

    assert calls == ["forecast", "forecast"]


async def test_forecast_non_object_payload_raises_lookup_error() -> None:
    """JSON valid but top-level is a list/scalar instead of an object.
    Without the guard, downstream ``.get`` calls would AttributeError.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return httpx.Response(200, json=["not", "an", "object"])

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")


# ----------------------------------------------------------------------
# Non-finite upstream numbers
# ----------------------------------------------------------------------
# Python's ``json`` decoder accepts the non-standard ``NaN`` /
# ``Infinity`` / ``-Infinity`` tokens by default, and httpx decodes
# response bodies with it — so an upstream hiccup hands the parser a
# float that is numeric by ``isinstance`` and still unusable:
# ``int(nan)`` raises ``ValueError`` and ``int(inf)`` raises
# ``OverflowError``, both of which surfaced as a 500 on /weather before
# the fix. These bodies are written as raw text because httpx's ``json=``
# kwarg serialises with ``allow_nan=False`` and so cannot express what a
# real upstream can actually put on the wire.

_JSON_HEADERS = {"content-type": "application/json"}


def _raw(body: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers=_JSON_HEADERS)


async def test_lookup_survives_non_finite_current_conditions() -> None:
    body = """
    {"current": {"temperature_2m": NaN,
                 "apparent_temperature": Infinity,
                 "relative_humidity_2m": NaN,
                 "weather_code": Infinity,
                 "wind_speed_10m": -Infinity},
     "daily": {"temperature_2m_min": [NaN], "temperature_2m_max": [Infinity]}}
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return _raw(body)

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        report = await service.lookup("Москва")

    assert report.city == "Москва"
    assert report.temperature_c is None
    assert report.feels_like_c is None
    assert report.humidity_pct is None
    assert report.wind_kmh is None
    assert report.temp_min_c is None
    assert report.temp_max_c is None
    assert report.weather_code is None
    assert report.condition == "Нет данных"


async def test_forecast_survives_non_finite_daily_series() -> None:
    body = """
    {"daily": {"time": ["2026-05-15", "2026-05-16"],
               "weather_code": [NaN, 3],
               "temperature_2m_min": [Infinity, 1.0],
               "temperature_2m_max": [NaN, 8.0]}}
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        return _raw(body)

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        result = await service.forecast("Москва", days=2)

    assert [day.date_iso for day in result.days] == ["2026-05-15", "2026-05-16"]
    # The poisoned day degrades field-by-field; the clean one is untouched.
    assert result.days[0].temp_min_c is None
    assert result.days[0].temp_max_c is None
    assert result.days[0].weather_code is None
    assert result.days[1].temp_min_c == pytest.approx(1.0)
    assert result.days[1].temp_max_c == pytest.approx(8.0)
    assert result.days[1].weather_code == 3


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
async def test_non_finite_coordinates_raise_lookup_error(bad: str) -> None:
    """A non-finite coordinate must not reach the forecast URL — it
    would be serialised into the query string as literal "nan"/"inf".
    """
    geo = f'{{"results": [{{"name": "M", "latitude": {bad}, "longitude": 37.62}}]}}'

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return _raw(geo)
        raise AssertionError("forecast must not be reached")

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")


# ── #139: cache stampede ─────────────────────────────────────────────────


def _geo_hit_at(name: str, lat: float, lon: float) -> dict[str, object]:
    """A geocoding hit for a named city at chosen coordinates."""
    return {"results": [{"name": name, "country": "Россия", "latitude": lat, "longitude": lon}]}


async def test_concurrent_misses_of_one_city_make_one_forecast_call() -> None:
    """#139: an expiring entry must not turn into N upstream calls.

    The popular cities are exactly the ones several people ask about
    inside the same second, and they share a cache key. Without the
    per-key fill lock each of those lookups pays the full forecast
    latency and spends a slot of a rate limit the whole bot shares;
    with it, one call fills the entry and the rest read it.
    """
    forecasts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal forecasts
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        forecasts += 1
        # Long enough that every other coroutine is already waiting by
        # the time this one answers — the shape of a real stampede.
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        reports = await asyncio.gather(*(service.lookup("Москва") for _ in range(8)))

    assert forecasts == 1
    # Everyone gets the same answer, not just the winner of the race.
    assert all(report == reports[0] for report in reports)


async def test_two_cities_do_not_queue_behind_each_other() -> None:
    """The lock is per key, so Москва must not wait for Владивосток.

    Both forecasts meet at a barrier: if the fill lock were global one
    of them could never arrive and the barrier would hang, which the
    timeout turns into a failure rather than a hung suite.
    """
    barrier = asyncio.Barrier(2)

    async def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            name = request.url.params.get("name", "")
            lat, lon = (55.75, 37.62) if name == "Москва" else (43.11, 131.87)
            return httpx.Response(200, json=_geo_hit_at(name, lat, lon))
        async with asyncio.timeout(3):
            await barrier.wait()
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        async with asyncio.timeout(5):
            moscow, vladivostok = await asyncio.gather(
                service.lookup("Москва"), service.lookup("Владивосток")
            )

    assert moscow.city == "Москва"
    assert vladivostok.city == "Владивосток"


async def test_a_failed_fill_does_not_wedge_the_next_caller() -> None:
    """A raising fill must release the lock and leave the key cold.

    Otherwise the first upstream blip would either deadlock every
    later lookup of that city or, worse, cache the failure.
    """
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json=_geo_hit())
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        with pytest.raises(WeatherLookupError):
            await service.lookup("Москва")
        async with asyncio.timeout(5):
            report = await service.lookup("Москва")

    assert attempts == 2
    assert report.city == "Москва"


async def test_the_lock_registry_does_not_grow_with_the_cities_asked_about() -> None:
    """#139: the per-key registry must free a slot when its last waiter
    leaves, or a bot asked about a thousand cities keeps a thousand
    locks alive for the life of the process.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            name = request.url.params.get("name", "")
            return httpx.Response(200, json=_geo_hit_at(name, float(len(name)), 10.0))
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        service = WeatherService(client=client)
        for name in ("а", "аб", "абв", "абвг", "абвгд"):
            await service.lookup(name)
        assert len(service._fill_locks) == 0  # noqa: SLF001


async def test_non_string_geocoder_fields_never_reach_the_card() -> None:
    """#954: ``country`` was read straight off the payload while ``name``
    and ``admin1`` went through the module's string guard. Open-Meteo's
    geocoder is community-edited and has returned odd payloads before;
    an unexpected shape must read as "absent", not render itself.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            payload = {
                "results": [
                    {
                        "name": 12345,
                        "country": {"unexpected": "shape"},
                        "latitude": 55.75,
                        "longitude": 37.62,
                    }
                ]
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=_forecast_hit())

    async with httpx.AsyncClient(transport=_make_transport(handler)) as client:
        report = await WeatherService(client=client).lookup("Москва")

    assert report.city == "Москва"
    assert report.country is None
