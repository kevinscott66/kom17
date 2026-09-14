"""End-to-end ``/rate`` + ``/convert``: dispatcher → handler → mocked FX.

Wires a real ``Dispatcher`` with the production main router, then pins
the ``CurrencyService`` at its ``build_main_router`` instantiation site
to a subclass whose ``_fetch`` returns a canned upstream payload (or
simulates a failure) without touching the network. ``make_request`` is
intercepted by ``capture_outgoing`` to record the reply.

Mirrors ``test_weather.py``'s patch-at-the-instantiation-site approach
(the shared service is captured in the handler closure at router-build
time, so the patch MUST land before ``make_wired``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import currency as currency_handler
from telegram_invite_bot.keyboards.builders.currency import (
    CurrencyBack,
    CurrencyPick,
    CurrencyRates,
)
from telegram_invite_bot.routers import main_router as main_router_module
from telegram_invite_bot.services.currency_service import CurrencyService
from telegram_invite_bot.services.payments.rates import FALLBACK_USD_TO_RUB
from telegram_invite_bot.utils.economy import _MAX_AMOUNT
from tests.e2e.handlers.conftest import (
    assert_only_the_stale_tail_answered,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

# Canned upstream payload: USD→RUB = 100 → base["USD"] = 0.1/100 = 0.001.
# EUR rate 0.9 → base["EUR"] = 0.001 * 0.9 = 0.0009.
_RATES = {"RUB": 100.0, "EUR": 0.9, "USD": 1.0}


def _message_update(
    text: str, *, language_code: str | None = None, chat_type: str = "private"
) -> Any:
    return make_message_update(
        text,
        user_id=777,
        first_name="Cur",
        message_id=300,
        language_code=language_code,
        chat_type=chat_type,
        chat_id=777 if chat_type == "private" else -100_500,
    )


def _callback_update(data: str, *, chat_type: str = "private") -> Any:
    """A tap on the ``/currency`` picker, from the same user as above."""
    return make_callback_update(
        data,
        user_id=777,
        first_name="Cur",
        chat_type=chat_type,
        chat_id=777 if chat_type == "private" else -100_500,
        chat_title=None if chat_type == "private" else "T",
    )


async def _read_stored(registry: EngineRegistry, user_id: int = 777) -> str | None:
    """The raw ``display_currency`` cell — what the LIVE telebot reads."""
    from sqlalchemy import select

    from telegram_invite_bot.db.models.economy import EconomyUser

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        stmt = select(EconomyUser.display_currency).where(EconomyUser.user_id == user_id)
        return (await session.execute(stmt)).scalar_one_or_none()


async def _seed_stored(registry: EngineRegistry, code: str, *, user_id: int = 777) -> None:
    from telegram_invite_bot.db.models.economy import EconomyUser

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=0, display_currency=code))
        await session.commit()


def _install_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any] | None,
    counter: list[int] | None = None,
) -> None:
    """Pin a ``CurrencyService`` whose ``_fetch`` returns ``payload``.

    ``payload=None`` simulates an upstream failure (the service falls
    back to the hardcoded table). ``counter`` (if given) is incremented
    on each ``_fetch`` call so a test can assert the TTL cache prevents
    a second fetch.
    """

    class _Pinned(CurrencyService):
        async def _fetch(self) -> dict[str, Any] | None:
            if counter is not None:
                counter.append(1)
            return payload

    monkeypatch.setattr(currency_handler, "CurrencyService", _Pinned)
    monkeypatch.setattr(main_router_module, "CurrencyService", _Pinned)


async def test_rate_usd_shows_computed_six_decimals(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update("/rate USD"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    # T-019 (R4): USD is the anchor — 1 / 900 coins-per-USDT = 0.001111,
    # independent of the fixture's USD/RUB = 100. Under the old pinned
    # RUB it was 0.1/100 = 0.001, i.e. the quoted dollar price of a coin
    # moved whenever the *rouble* fix moved, which it has no business
    # doing.
    assert "0.001111" in sent[0]["text"]
    assert "USD" in sent[0]["text"]


async def test_rate_no_arg_uses_default_rub(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    # No arg, no en language → defaults to RUB. T-019 (R4): RUB is now
    # derived, (1 / 900) * 100 = 0.111111 at the fixture's USD/RUB = 100.
    # The retired flat 0.1 was only ever right at USD/RUB = 90.
    await dispatcher.feed_update(bot, _message_update("/rate"))
    assert len(sent) == 1
    assert "0.111111" in sent[0]["text"]
    assert "RUB" in sent[0]["text"]


async def test_rate_api_failure_falls_back_no_error(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream failure → hardcoded fallback, user still gets a reply."""
    _install_service(monkeypatch, payload=None)
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/rate USD"))
    assert len(sent) == 1
    # Hardcoded COM_TO_CURRENCY["USD"] = 0.001111 → "0.001111".
    assert "0.001111" in sent[0]["text"]


async def test_convert_amount_and_currency(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    # RR-6 #67: 1000 COM abbreviates to "1.00K 🪙" and the converted half
    # carries the currency's own symbol/placement — 1000 * 0.001 = $1.
    result = await dispatcher.feed_update(bot, _message_update("/convert 1000 USD"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "1.00K 🪙" in text, "lost the K/M abbreviation legacy had"
    assert "$1" in text, "lost the currency symbol"
    assert "USD" in text


async def test_convert_no_amount_shows_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/convert"))
    assert len(sent) == 1
    assert "/convert" in sent[0]["text"]


async def test_convert_non_integer_amount_errors(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/convert abc"))
    assert len(sent) == 1
    assert "число" in sent[0]["text"].lower()


@pytest.mark.parametrize(
    "token",
    [
        # CPython's int/str limit is 4300 digits and Telegram allows a
        # 4096-character message, so a 400-digit run parsed fine, passed
        # the sign check and reached the float division inside the
        # compact formatter — ``OverflowError``, surfaced as a generic
        # failure rather than the usage hint (#950).
        "1" + "0" * 400,
        # Below that threshold there was no crash at all: a
        # several-hundred-digit string went out into the chat.
        "1" + "0" * 200,
        # Nothing above the economy's balance ceiling is reachable.
        str(_MAX_AMOUNT + 1),
        # ``int()`` is wider than the gate family it was standing in for.
        "٣",
        "²",
        "-5",
    ],
)
async def test_convert_rejects_amounts_a_bare_int_would_accept(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    """``/convert`` is the one ``parse_int_token`` caller with no balance
    or bet to clamp against afterwards, so the gate itself has to bound
    both the grammar and the magnitude (#950).
    """
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update(f"/convert {token} USD"))
    assert len(sent) == 1
    assert "число" in sent[0]["text"].lower()


async def test_rate_unknown_currency(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/rate ZZZ"))
    assert len(sent) == 1
    assert "Неизвестная валюта" in sent[0]["text"]


async def test_ttl_cache_avoids_second_fetch(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second /rate within the TTL window must not re-fetch upstream."""
    counter: list[int] = []
    _install_service(monkeypatch, payload={"rates": _RATES}, counter=counter)
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/rate USD"))
    await dispatcher.feed_update(bot, _message_update("/rate EUR"))
    assert len(sent) == 2
    # One fetch populated the cache; the second call served it warm.
    assert len(counter) == 1


# ---- service-level: endpoint selection + payload-shape normalisation --


def test_url_selects_v4_without_key() -> None:
    """No API key → keyless v4 endpoint."""
    svc = CurrencyService()
    assert svc._url == "https://api.exchangerate-api.com/v4/latest/USD"


def test_url_selects_keyed_v6_with_key() -> None:
    """API key → keyed v6 endpoint with the key interpolated."""
    svc = CurrencyService(api_key="SECRET123")
    assert svc._url == "https://v6.exchangerate-api.com/v6/SECRET123/latest/USD"


async def test_v6_conversion_rates_payload_is_parsed() -> None:
    """A v6-shaped payload (``conversion_rates`` + ``result: success``)
    derives the same table as the v4 ``rates`` shape."""

    class _V6(CurrencyService):
        async def _fetch(self) -> dict[str, Any] | None:
            return {"result": "success", "conversion_rates": dict(_RATES)}

    svc = _V6(api_key="K")
    # T-019 (R4): USD = 1 / 900 (the payout rate); everything else hangs
    # off it — EUR = USD * 0.9, RUB = USD * 100.
    assert await svc.get_rate("USD") == pytest.approx(1 / 900)
    assert await svc.get_rate("EUR") == pytest.approx(0.9 / 900)
    assert await svc.get_rate("RUB") == pytest.approx(100 / 900)


async def test_v6_error_result_falls_back_to_hardcoded() -> None:
    """``result: error`` (e.g. invalid key / quota) → hardcoded table."""
    from telegram_invite_bot.services.currency_service import COM_TO_CURRENCY

    class _V6Err(CurrencyService):
        async def _fetch(self) -> dict[str, Any] | None:
            return {"result": "error", "error-type": "invalid-key"}

    svc = _V6Err(api_key="BAD")
    # The USD/RUB legs are re-anchored even in the degraded path, so a
    # configured spread can't be quietly ignored when the API is down.
    # ``COM_TO_CURRENCY`` holds the same value rounded to six decimals.
    assert await svc.get_rate("USD") == pytest.approx(COM_TO_CURRENCY["USD"], abs=1e-6)
    assert await svc.get_rate("RUB") == pytest.approx(0.1)
    # Crypto has no upstream — still served verbatim from the table.
    assert await svc.get_rate("TON") == COM_TO_CURRENCY["TON"]


# ── /crypto + /currency listings (CMD-3) ─────────────────────────────


async def test_crypto_lists_btc_eth_ton(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/crypto"))
    text = sent[-1]["text"]
    assert "Курсы криптовалют" in text
    assert "(BTC)" in text and "(ETH)" in text and "(TON)" in text


async def test_currency_lists_all_and_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    # English user, DM → the picker card, English names, NO Cyrillic.
    await dispatcher.feed_update(bot, _message_update("/currency", language_code="en"))
    text = sent[-1]["text"]
    assert "Display currency" in text
    assert "(USD)" in text and "US Dollar" in text
    assert not re.search("[А-Яа-яЁё]", text), "EN /currency leaked Cyrillic"

    # ...and the grid itself, which is half of what the card says.
    rows = sent[-1]["markup"].inline_keyboard
    labels = [btn.text for row in rows for btn in row]
    assert "✅ 🇺🇸 USD" in labels, "no check-mark on the effective currency"
    assert sum(label.startswith("✅") for label in labels) == 1
    assert not any(label.startswith("h_") for label in labels), "untranslated key on a button"
    assert not any(re.search("[А-Яа-яЁё]", label) for label in labels)


async def test_rate_en_uses_english_currency_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: /rate to an English user must show the English name
    ('US Dollar'), never the Russian 'Доллар США' (ru/en convergence)."""
    import re

    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/rate USD", language_code="en"))
    text = sent[-1]["text"]
    assert "US Dollar" in text
    assert not re.search("[А-Яа-яЁё]", text)


# ── RR-6 #66/#67: the saved display currency ─────────────────────────


async def test_rate_uses_saved_display_currency(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #67: a bare ``/rate`` honours the stored preference.

    This is the whole regression in one assertion — the port derived the
    default from ``language_code`` alone, so a user who had chosen TON
    was quoted rubles by the new pipeline and TON by the legacy one.
    """
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_stored(registry, "TON")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/rate"))
    text = sent[-1]["text"]
    assert "(TON)" in text
    assert "(RUB)" not in text


async def test_rate_explicit_arg_beats_saved_currency(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit argument is a one-off question, not a preference change."""
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_stored(registry, "TON")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/rate USD"))
    assert "(USD)" in sent[-1]["text"]
    # The one-off must not have rewritten the preference.
    assert await _read_stored(registry) == "TON"


async def test_rate_survives_unreadable_economy_db(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``economy.db`` tables → the locale default, never an error.

    ``/rate`` needed no database before the preference was restored;
    wiring one in must not hand it a brand-new way to fail.
    """
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired()  # deliberately no schemas
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _message_update("/rate"))
    assert result is not UNHANDLED
    assert "(RUB)" in sent[-1]["text"]


async def test_currency_in_group_lists_rates_and_points_at_dm(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setter is DM-only, so the group keeps the read-only table.

    A per-user preference on a shared card would re-draw the ✅ for
    every other reader; the group loses no surface it had yesterday.
    """
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/currency", chat_type="supergroup"))
    text = sent[-1]["text"]
    assert "Курсы валют" in text
    assert "(USD)" in text and "(TON)" in text
    assert "/currency" in text  # the DM pointer
    assert sent[-1]["markup"] is None, "group card must not carry private-only buttons"


async def test_currency_pick_saves_and_confirms(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #66: a tap writes the shared column and redraws the card."""
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(bot, _callback_update(CurrencyPick(code="usd").pack()))
    assert result is not UNHANDLED
    # Written to the very column the LIVE telebot reads, upper-cased.
    assert await _read_stored(registry) == "USD"

    toast = next(e for e in sent if e["kind"] == "callback_answer")
    assert "USD" in (toast["text"] or "")
    edit = next(e for e in sent if e["kind"] == "edit")
    assert "Валюта сохранена" in edit["text"]
    # Legacy's worked-examples ladder, K/M abbreviation included.
    assert "100 🪙" in edit["text"] and "100.00K 🪙" in edit["text"]
    assert "$" in edit["text"]


async def test_currency_pick_does_not_hold_the_write_lock_across_the_fx_fetch(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two writes land before the confirmation card is priced.

    That card quotes the rate ladder off the FX provider, which on a
    cold cache is an HTTP round-trip — ten seconds allowed, twice
    SQLite's ``busy_timeout``. Under ``BEGIN IMMEDIATE`` the pick would
    own ``economy.db`` for all of it, so one currency tap would park
    every wallet, game, transfer and shop write behind it and they would
    all come back ``database is locked``.

    Two independent signals: the probe *writes* from a separate session
    (that write blocks and then raises if the lock is still held), and
    it *reads* the picked currency (WAL readers never block, so an
    uncommitted write shows as ``None`` rather than raising).
    """
    from telegram_invite_bot.db.models.economy import EconomyUser

    registry_box: list[EngineRegistry] = []
    other_updates_could_write: list[bool] = []
    stored_during_fetch: list[str | None] = []

    class _Probing(CurrencyService):
        async def _fetch(self) -> dict[str, Any] | None:
            registry = registry_box[0]
            async with registry.session(DBName.ECONOMY)() as other:
                other.add(EconomyUser(user_id=999, balance=7))
                await other.commit()
            other_updates_could_write.append(True)
            stored_during_fetch.append(await _read_stored(registry))
            return {"rates": _RATES}

    monkeypatch.setattr(currency_handler, "CurrencyService", _Probing)
    monkeypatch.setattr(main_router_module, "CurrencyService", _Probing)

    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    registry_box.append(registry)
    sent = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(bot, _callback_update(CurrencyPick(code="usd").pack()))
    assert result is not UNHANDLED

    assert other_updates_could_write == [True]
    # ``None`` here would mean the pick was still sitting in the update's
    # open transaction — the exact state that holds the writer lock.
    assert stored_during_fetch == ["USD"]
    # Committing early cost nothing: the pick stands and the card drew.
    assert await _read_stored(registry) == "USD"
    edit = next(e for e in sent if e["kind"] == "edit")
    assert "Валюта сохранена" in edit["text"]


async def test_currency_pick_seeds_a_missing_wallet(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An UPDATE against a row that was never created is a silent no-op.

    Legacy called ``register_user`` first (bot.py:3392) for exactly this
    reason: the picker is reachable before anything seeds the wallet.
    """
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    assert await _read_stored(registry) is None  # no row at all yet
    capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _callback_update(CurrencyPick(code="TON").pack()))
    assert await _read_stored(registry) == "TON"


async def test_currency_pick_refuses_unknown_code_without_writing(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forged payload must not poison the column the OTHER bot reads."""
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_stored(registry, "TON")
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _callback_update(CurrencyPick(code="ZZZ").pack()))
    assert await _read_stored(registry) == "TON", "junk code reached the shared column"
    toast = next(e for e in sent if e["kind"] == "callback_answer")
    assert toast["show_alert"] is True
    assert not [e for e in sent if e["kind"] == "edit"], "refused tap still redrew the card"


async def test_currency_pick_in_group_never_reaches_the_picker(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picker callbacks are private-filtered, so a group tap dies.

    Nothing legitimate produces one — the group card carries no buttons
    (see the test above) — so a tap claiming otherwise is a stale or
    forged card and must never reach the setter.

    Until #159 "dies" meant literally nothing happened: ``UNHANDLED``,
    an empty sink, and a button that spun for fifteen seconds before
    Telegram gave up on it. The tail now acknowledges it instead, so
    what pins the gate is no longer the silence but the two facts
    around it — the only reply is the stale-card toast, and the shared
    column is still empty.
    """
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, _callback_update(CurrencyPick(code="USD").pack(), chat_type="supergroup")
    )
    assert_only_the_stale_tail_answered(sent, "a group tap must not reach the picker")
    assert await _read_stored(registry) is None


async def test_currency_rates_and_back_round_trip(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """📊 → rate table → ◀️ → the picker, with the ✅ re-read from disk."""
    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_stored(registry, "EUR")
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(bot, _callback_update(CurrencyRates().pack()))
    table = next(e for e in sent if e["kind"] == "edit")
    assert "Курсы валют" in table["text"]

    sent.clear()
    await dispatcher.feed_update(bot, _callback_update(CurrencyBack().pack()))
    picker = next(e for e in sent if e["kind"] == "edit")
    assert "Валюта отображения" in picker["text"]
    labels = [btn.text for row in picker["markup"].inline_keyboard for btn in row]
    assert "✅ 🇪🇺 EUR" in labels


async def test_en_user_with_stored_rub_is_told_about_the_usd_fallback(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy quirk, surfaced instead of hidden.

    ``display_currency`` defaults to the literal ``'RUB'``, so legacy
    could not tell "chose RUB" from "never chose" and read an English
    user back as USD. The rule is ported verbatim (the LIVE bot reads
    the same cell), but the card says so rather than check-marking a
    currency the user never picked.
    """
    import re

    _install_service(monkeypatch, payload={"rates": _RATES})
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_stored(registry, "RUB")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _message_update("/currency", language_code="en"))
    text = sent[-1]["text"]
    assert "RUB" in text and "USD" in text
    assert not re.search("[А-Яа-яЁё]", text)
    labels = [btn.text for row in sent[-1]["markup"].inline_keyboard for btn in row]
    assert "✅ 🇺🇸 USD" in labels, "check-mark must follow the EFFECTIVE currency"


# ---------------------------------------------------------------------------
# T-020 R11 — the raw USD/RUB fix, recovered for the payments layer
# ---------------------------------------------------------------------------


async def test_usd_to_rub_recovers_the_raw_fix_from_the_com_table() -> None:
    """``base["RUB"] / base["USD"]`` cancels the shared coin anchor.

    The service only ever exposed COM-denominated rates; R11 needs the
    fix itself to price a rouble top-up. Recovering it from the same
    cached table — rather than fetching again — is what guarantees the
    rouble a payment is priced at equals the rouble ``/rate`` quoted.
    """

    class _Pinned(CurrencyService):
        async def _fetch(self) -> dict[str, Any] | None:
            return {"rates": dict(_RATES)}

    svc = _Pinned()
    # ``_RATES`` fixes USD/RUB at 100; the coin anchor divides out.
    assert await svc.usd_to_rub() == pytest.approx(100.0)


async def test_usd_to_rub_is_independent_of_the_withdraw_anchor() -> None:
    """Opening a buy/sell spread (audit R6) must not move the FX fix."""

    class _Pinned(CurrencyService):
        async def _fetch(self) -> dict[str, Any] | None:
            return {"rates": dict(_RATES)}

    wide = _Pinned(coins_per_usdt=450.0)
    assert await wide.usd_to_rub() == pytest.approx(100.0)


async def test_usd_to_rub_falls_back_when_the_upstream_is_down() -> None:
    """The degraded table re-anchors both legs, so the recovered fix is
    the offline anchor — not a divide-by-zero into a payment path."""

    class _Down(CurrencyService):
        async def _fetch(self) -> dict[str, Any] | None:
            return None

    assert await _Down().usd_to_rub() == pytest.approx(FALLBACK_USD_TO_RUB)
