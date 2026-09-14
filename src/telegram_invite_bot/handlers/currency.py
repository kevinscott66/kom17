"""``/rate`` ``/convert`` ``/crypto`` ``/currency`` — COM↔currency (A-09, RR-6 #66/#67).

Ports legacy ``cmd_rate`` (bot.py:17732), ``cmd_convert`` (bot.py:17783),
``cmd_crypto`` (bot.py:17760) and ``cmd_currency`` (bot.py:17633). All
four turn the bot's INTERNAL game currency COM (🪙) into a fiat/crypto
unit via :class:`CurrencyService`.

RR-6 #66/#67 — what this module restores
----------------------------------------
The first port dropped the user's saved **display currency** in two
places at once:

* ``/currency`` stopped being the interactive setter and became a
  read-only rate list, so the preference had no way in from the new
  pipeline — while the legacy process, then still running, kept reading
  and honouring the very same column.
* ``/rate`` and ``/convert`` derived their default from the Telegram
  ``language_code`` alone, so a Russian-speaking user who had chosen TON
  was quoted rubles, and ``/convert`` printed a bare number with no
  currency symbol and no K/M abbreviation (``1500`` where legacy said
  ``1.50K 🪙 (~$1.67)``).

Both are back. The preference lives in ``economy.users.display_currency``
— deliberately the same column legacy wrote — so a user's pre-cutover
choice is still their choice here rather than something a fresh column
would have silently discarded.
See :func:`~telegram_invite_bot.services.currency_service.effective_currency`
for how a stored value is read, quirk and all.

Chat-type split, deliberate
---------------------------
The setter card is PRIVATE-only. In a group the message is shared but the
preference is per-user, so legacy's group picker re-drew the ✅ for
everyone whenever a single member tapped — the card became a lie for
every other reader, and the ``MainMenu`` footer button would be dead
besides (that router is private-filtered). In a group ``/currency``
answers with the rate table plus a pointer to DM, which is what the
command did in this pipeline yesterday: no group surface is lost.

The handler mirrors the Stage-5 weather closure pattern: the shared
:class:`CurrencyService` is injected into ``build_router`` and captured in
the handler closures so its 1h TTL cache is reused across requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.currency import (
    CurrencyBack,
    CurrencyPick,
    CurrencyRates,
    build_back_markup,
    build_picker_markup,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.currency_service import (
    AVAILABLE_CURRENCIES,
    CurrencyService,
    currency_label,
    effective_currency,
    format_display_amount,
)
from telegram_invite_bot.utils.aiogram import (
    command_body,
    edit_card,
    require_from_user,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT
from telegram_invite_bot.utils.numbers import parse_int_token

log = logger.bind(component="handlers.currency")

_COM_EMOJI = "🪙"

#: Worked examples on the post-tap confirmation, straight from legacy
#: (bot.py:17709). The ladder is chosen to cross the K threshold twice so
#: the abbreviation is visible in the card that introduces it.
_EXAMPLE_AMOUNTS = (100, 1000, 10_000, 100_000)

#: Rate precision: crypto needs 8 places before it stops being zeroes,
#: fiat is legible at 6 (both match legacy).
_CRYPTO_CODES = ("BTC", "ETH", "TON")
_CRYPTO_PRECISION = 8
_FIAT_PRECISION = 6

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, InlineKeyboardMarkup

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo


async def _stored_currency(economy_repo: EconomyRepo | None, user_id: int) -> str | None:
    """The raw ``display_currency`` cell, or ``None`` if unreadable.

    ``economy_repo`` is ``None`` only when ``build_router`` was called
    without a registry — a test-wiring convenience.

    The ``SQLAlchemyError`` swallow is deliberate and load-bearing:
    before RR-6 #67 ``/rate``, ``/convert`` and ``/currency`` touched no
    database at all, so an ``economy.db`` that is locked, missing or
    mid-migration used to cost the user nothing. Restoring a *preference*
    must not turn a read-only FX lookup into a command that can fail
    outright — the worst a lost read may do is quote the locale default,
    which is precisely what these commands did yesterday.
    """
    if economy_repo is None:
        return None
    try:
        return await economy_repo.get_display_currency(user_id)
    except SQLAlchemyError as exc:  # pragma: no cover - defensive
        log.bind(uid=user_id).warning(f"display currency unreadable, using default: {exc}")
        return None


async def _display_currency(economy_repo: EconomyRepo | None, user_id: int, lang: str) -> str:
    """The user's effective display currency (RR-6 #67)."""
    return effective_currency(await _stored_currency(economy_repo, user_id), lang)


def _rate_line(code: str, rate: float, lang: str) -> str:
    """One ``• <rate> <emoji> <name> (CODE)`` row of a rate table."""
    prec = _CRYPTO_PRECISION if code in _CRYPTO_CODES else _FIAT_PRECISION
    return f"• <b>{rate:.{prec}f}</b> {currency_label(code, lang)}"


def _rate_precision(code: str) -> int:
    return _CRYPTO_PRECISION if code in _CRYPTO_CODES else _FIAT_PRECISION


# ── cards ────────────────────────────────────────────────────────────


async def _render_picker(
    service: CurrencyService, lang: str, stored: str | None
) -> tuple[str, InlineKeyboardMarkup]:
    """The ``/currency`` setter card + its grid.

    When the stored code and the effective one disagree — the English
    ``RUB``→``USD`` fallback is the only way that happens — the card says
    so out loud instead of showing a ✅ the user never chose. Silently
    check-marking USD over a stored RUB is how legacy made this feel like
    a bug.
    """
    effective = effective_currency(stored, lang)
    rate = await service.get_rate(effective)
    lines = [
        t("h_cur_title", lang),
        "",
        t("h_cur_current", lang, cur=currency_label(effective, lang)),
        t(
            "h_cur_rate_now",
            lang,
            rate=f"{rate:.{_rate_precision(effective)}f}",
            code=effective,
        ),
    ]
    normalised = (stored or "").strip().upper()
    if normalised in AVAILABLE_CURRENCIES and normalised != effective:
        lines += ["", t("h_cur_en_default_note", lang, stored=normalised)]
    lines += ["", t("h_cur_choose", lang)]
    return "\n".join(lines), build_picker_markup(lang, effective)


async def _render_rates(service: CurrencyService, lang: str) -> str:
    """``1 COM = …`` for every supported currency (the old ``/currency``)."""
    rows = [
        _rate_line(code, await service.get_rate(code), lang)
        for code in AVAILABLE_CURRENCIES
        if code != "COM"
    ]
    return f"{t('h_cur_rates_title', lang)}\n\n1 {_COM_EMOJI} =\n" + "\n".join(rows)


async def _render_saved(service: CurrencyService, lang: str, code: str) -> str:
    """Post-tap confirmation with legacy's worked examples (bot.py:17713)."""
    rate = await service.get_rate(code)
    lines = [
        t("h_cur_saved_title", lang),
        "",
        t("h_cur_saved_body", lang, cur=currency_label(code, lang)),
        "",
        t("h_cur_examples_title", lang),
    ]
    lines += [f"• {format_display_amount(com, code, rate)}" for com in _EXAMPLE_AMOUNTS]
    lines += ["", t("h_cur_legend", lang)]
    return "\n".join(lines)


# ── commands ─────────────────────────────────────────────────────────


async def handle_rate(
    message: Message,
    service: CurrencyService,
    lang: str,
    economy_repo: EconomyRepo | None = None,
) -> None:
    tg_user = require_from_user(message)

    parts = command_body(message).split()
    if len(parts) >= 2:
        code = parts[1].upper()
    else:
        code = await _display_currency(economy_repo, tg_user.id, lang)

    if code not in AVAILABLE_CURRENCIES:
        await message.answer(t("rate_unknown_currency", lang))
        return

    rate = await service.get_rate(code)
    body = (
        f"{t('h_cur_rate_title', lang)}\n\n"
        f"1 {_COM_EMOJI} = <b>{rate:.{_rate_precision(code)}f}</b> "
        f"{currency_label(code, lang)}"
    )
    # A rate is an abstraction; one worked amount underneath it is what
    # actually answers "so what is my balance worth?".
    body += f"\n{format_display_amount(_EXAMPLE_AMOUNTS[1], code, rate)}"
    await message.answer(body)
    log.bind(uid=tg_user.id, code=code).info("/rate rendered")


async def handle_convert(
    message: Message,
    service: CurrencyService,
    lang: str,
    economy_repo: EconomyRepo | None = None,
) -> None:
    tg_user = require_from_user(message)

    parts = command_body(message).split()
    if len(parts) < 2:
        saved = await _display_currency(economy_repo, tg_user.id, lang)
        await message.answer(t("convert_amount_hint", lang, cur=saved))
        return

    # #950: a bare ``int()`` here was two bugs. It accepts what the
    # gate family exists to reject (Arabic-Indic «٣», a leading sign,
    # surrounding whitespace), and it accepts a digit run of any length
    # under CPython's 4300-digit limit — Telegram allows 4096 characters
    # per message, so a ~400-digit token parsed fine, passed the sign
    # check and then reached ``format_amount_compact``'s float division,
    # which raises ``OverflowError`` and surfaces as a generic failure
    # instead of the usage hint. Below ~315 digits it did not even raise:
    # it emitted a several-hundred-digit string into the chat. The
    # economy's own balance ceiling is the natural upper bound — nothing
    # above it is reachable in this bot, and nothing above it renders.
    amount = parse_int_token(parts[1])
    if amount is None or amount > _MAX_AMOUNT:
        await message.answer(t("enter_number_example", lang))
        return

    if len(parts) >= 3:
        code = parts[2].upper()
    else:
        code = await _display_currency(economy_repo, tg_user.id, lang)
    if code not in AVAILABLE_CURRENCIES:
        await message.answer(t("rate_unknown_currency", lang))
        return

    rate = await service.get_rate(code)
    body = (
        f"{t('h_cur_convert_title', lang)}\n\n"
        f"{format_display_amount(amount, code, rate)}\n\n"
        + t(
            "h_cur_rate_now",
            lang,
            rate=f"{rate:.{_rate_precision(code)}f}",
            code=code,
        )
    )
    await message.answer(body)
    log.bind(uid=tg_user.id, code=code, amount=amount).info("/convert rendered")


async def handle_crypto(message: Message, service: CurrencyService, lang: str) -> None:
    """``/crypto`` — 1 COM → each supported crypto (BTC/ETH/TON)."""
    tg_user = require_from_user(message)
    rows = [
        _rate_line(code, await service.get_rate(code), lang)
        for code in _CRYPTO_CODES
        if code in AVAILABLE_CURRENCIES
    ]
    header = t("h_cur_crypto_title", lang)
    await message.answer(f"{header}\n\n1 {_COM_EMOJI} =\n" + "\n".join(rows))
    log.bind(uid=tg_user.id).info("/crypto rendered")


async def handle_currency(
    message: Message,
    service: CurrencyService,
    lang: str,
    economy_repo: EconomyRepo | None = None,
) -> None:
    """``/currency`` — the display-currency setter in DM, rates in a group."""
    tg_user = require_from_user(message)

    if message.chat.type != ChatType.PRIVATE:
        table = await _render_rates(service, lang)
        await message.answer(f"{table}\n\n{t('h_cur_group_hint', lang)}")
        log.bind(uid=tg_user.id, chat=message.chat.id).info("/currency rates (group)")
        return

    stored = await _stored_currency(economy_repo, tg_user.id)
    text, markup = await _render_picker(service, lang, stored)
    await message.answer(text, reply_markup=markup)
    log.bind(uid=tg_user.id).info("/currency picker rendered")


# ── callbacks ────────────────────────────────────────────────────────


async def handle_pick(
    callback: CallbackQuery,
    service: CurrencyService,
    lang: str,
    economy_repo: EconomyRepo,
    code: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Store the tapped currency and redraw the card as a confirmation.

    ``code`` is validated here, before the repo sees it. The original
    reason was a second live reader on the same column; T-011 removed
    it, and the reason that outlived it is inside this pipeline: the
    read side deliberately hands back whatever is on disk untouched
    (``EconomyRepo.get_display_currency``), so a junk value from a
    forged payload would be stored and re-served, not normalised away.
    An unknown code is refused with a toast and no write.
    """
    uid = callback.from_user.id
    code = code.upper()
    if code not in AVAILABLE_CURRENCIES:
        await callback.answer(t("h_cur_toast_unknown", lang), show_alert=True)
        log.bind(uid=uid, code=code).warning("/currency unknown code refused")
        return

    # Legacy called ``register_user`` before the UPDATE (bot.py:3392) —
    # a user can reach the picker before anything has ever seeded their
    # wallet, and an UPDATE against a missing row is a silent no-op.
    await economy_repo.get_or_create(uid, language=lang)
    await economy_repo.set_display_currency(uid, code)
    # Those two writes are everything this callback stores, and the
    # confirmation card below prices the rate table off the FX provider
    # — ten seconds on a cold cache, twice SQLite's ``busy_timeout``.
    # End the write transaction here so a currency tap can't lock
    # ``economy.db`` (wallets, games, transfers) behind an HTTP wait.
    # See :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()

    await callback.answer(t("h_cur_toast_saved", lang, code=code))
    # The preference is already saved; an inaccessible card (deleted, or
    # older than Telegram will hand back) costs the user the confirmation
    # view, never the write.
    message = callback.message
    if not isinstance(message, Message):
        return
    await edit_card(
        message, await _render_saved(service, lang, code), reply_markup=build_back_markup(lang)
    )
    log.bind(uid=uid, code=code).info("/currency saved")


async def handle_show_rates(callback: CallbackQuery, service: CurrencyService, lang: str) -> None:
    """Swap the picker for the full rate table."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    await edit_card(
        message, await _render_rates(service, lang), reply_markup=build_back_markup(lang)
    )


async def handle_back(
    callback: CallbackQuery,
    service: CurrencyService,
    lang: str,
    economy_repo: EconomyRepo,
) -> None:
    """Back to the picker, re-read so the ✅ reflects the latest write."""
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    stored = await economy_repo.get_display_currency(callback.from_user.id)
    text, markup = await _render_picker(service, lang, stored)
    await edit_card(message, text, reply_markup=markup)


def build_router(
    service: CurrencyService | None = None,
    registry: EngineRegistry | None = None,
) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    The caller injects a shared :class:`CurrencyService` so its 1h TTL
    cache is reused across requests. A fresh instance per
    ``build_router`` call would defeat the cache. The service also takes
    an optional ``httpx.AsyncClient``, but no production call site
    passes one (#423) — sharing buys the cache, not a socket.

    ``registry`` mounts :class:`EconomyMiddleware` so the saved display
    currency can be read and written. It is an INNER middleware on both
    observers, so a chat that never runs one of these commands never pays
    for an ``economy.db`` session. Passing ``None`` (test convenience)
    drops the preference lookup back to the locale default and leaves the
    picker callbacks unregistered — an unregistered callback is a dead
    button, so the picker itself is only offered when the registry is
    there to back it.
    """
    currency_service = service if service is not None else CurrencyService()

    async def _handle_rate(
        message: Message, lang: str, economy_repo: EconomyRepo | None = None
    ) -> None:
        await handle_rate(message, currency_service, lang, economy_repo)

    async def _handle_convert(
        message: Message, lang: str, economy_repo: EconomyRepo | None = None
    ) -> None:
        await handle_convert(message, currency_service, lang, economy_repo)

    async def _handle_crypto(message: Message, lang: str) -> None:
        await handle_crypto(message, currency_service, lang)

    async def _handle_currency(
        message: Message, lang: str, economy_repo: EconomyRepo | None = None
    ) -> None:
        await handle_currency(message, currency_service, lang, economy_repo)

    router = Router(name="currency")
    router.message.register(
        _handle_rate,
        # #501: ``rates``/``exchange`` are legacy spellings
        # (bot.py:42358) that the port dropped from both the catalog
        # and the registration, so they answered nothing.
        Command("rate", "курс", "курсы", "rates", "exchange", ignore_case=True),
        F.from_user,
    )
    router.message.register(
        _handle_convert,
        # #501: ``conversion`` — legacy alias (bot.py:42359).
        Command("convert", "конверт", "конвертация", "conversion", ignore_case=True),
        F.from_user,
    )
    router.message.register(
        _handle_crypto,
        Command("crypto", "крипта", "криптовалюта", "kom_crypto", ignore_case=True),
        F.from_user,
    )
    router.message.register(
        _handle_currency,
        Command("currency", "валюта", "kom_currency", ignore_case=True),
        F.from_user,
    )

    if registry is None:
        return router

    async def _cb_pick(
        callback: CallbackQuery,
        callback_data: CurrencyPick,
        lang: str,
        economy_repo: EconomyRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_pick(
            callback, currency_service, lang, economy_repo, callback_data.code, checkpoint
        )

    async def _cb_rates(callback: CallbackQuery, lang: str) -> None:
        await handle_show_rates(callback, currency_service, lang)

    async def _cb_back(callback: CallbackQuery, lang: str, economy_repo: EconomyRepo) -> None:
        await handle_back(callback, currency_service, lang, economy_repo)

    router.message.middleware(EconomyMiddleware(registry))
    router.callback_query.middleware(EconomyMiddleware(registry))
    # PRIVATE-only, matching where the keyboard is attached. A group card
    # can't carry these buttons (see the module docstring), so a tap
    # claiming otherwise is either a forged payload or a stale card from
    # before this filter existed — either way it belongs unhandled.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    router.callback_query.register(_cb_pick, CurrencyPick.filter())
    router.callback_query.register(_cb_rates, CurrencyRates.filter())
    router.callback_query.register(_cb_back, CurrencyBack.filter())
    return router
