"""Crypto Pay (CryptoBot) webhook adapter (T-025).

Provider spec: https://help.crypt.bot/crypto-pay-api#webhooks

Signature scheme:
    secret = sha256(api_token)
    expected = hmac_sha256(secret, body).hexdigest()
    compare against header ``crypto-pay-api-signature``

The token is hashed once to derive the HMAC key, then the raw body
is HMAC-SHA256'd with that derived key. Both casings of the header
are supported because legacy code accepted either and we don't want
to silently drop signed payloads from a provider that flips header
case.

Coin conversion: legacy read the live exchange rate from
``get_exchange_rate("USD", "COINS")`` with a 900.0 fallback. The
exchange-rate table was never ported, so we take the fallback (900
coins per USD) directly. This is documented in ADR 0014, where the gap
was called acceptable because the legacy table had in practice held 900
throughout. That argument no longer stands on its own: T-011 removed
the process that maintained the table, so 900 is not a fallback that
tracks a rate — it is the rate, hard-coded, on a path that charges real
money. Changing it is a pricing decision and belongs to the owner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from typing import Any, Final

from loguru import logger

from telegram_invite_bot.services.payments.base import ParsedEvent, Provider, fiat_or_none
from telegram_invite_bot.services.payments.rates import (
    INVOICE_FIAT,
    USD_PEGGED_ASSETS,
    coins_for_usd,
    is_priceable,
)

log = logger.bind(component="payments.crypto")

#: The only update type that mints coins. Crypto Pay sends several
#: others (``invoice_expired`` above all) for every abandoned checkout.
_UPDATE_PAID: Final[str] = "invoice_paid"

#: The invoice's own state, one level down under ``payload``. Read only
#: by :meth:`CryptoAdapter.describes_paid_money`, never by the credit
#: path — see the asymmetry documented there.
_STATUS_PAID: Final[str] = "paid"

#: ``currency_type`` of an invoice denominated in fiat rather than in
#: units of a crypto asset. #1232: the two shapes carry ``amount`` in
#: different units, so the credit path has to branch on this. An absent
#: key means crypto — both the provider default and the shape of every
#: invoice this bot minted before #1232 shipped.
_CURRENCY_FIAT: Final[str] = "fiat"


def _usd_rate(raw: Any, asset: str) -> float | None:
    """Asset→USD rate for a crypto-denominated invoice, or ``None``.

    #1233: this used to be ``float(raw or 1)``, and that ``or 1`` fires
    on an absent key, ``null``, ``""``, ``0`` and ``"0"`` alike. For a
    dollar stablecoin parity is the right answer and the bug never
    showed; for anything else it discards the entire conversion — 0.05
    BTC credited as five cents, or the same mistake running the other
    way and paying out of the owner's wallet. Parity is granted only to
    the assets that hold it, and every other asset refuses the credit
    rather than guessing at it. The rouble leg has priced money behind
    a plausibility check since R11 (:func:`sane_usd_to_rub`); this was
    the leg still assuming.

    A rate that is present but unreadable raises for the caller's
    handler: malformed is not the same as absent, and the two deserve
    different log lines.
    """
    parity = 1.0 if asset in USD_PEGGED_ASSETS else None
    if raw is None or raw == "":
        return parity
    rate = float(raw)
    if not math.isfinite(rate) or rate <= 0:
        return parity
    return rate


class CryptoAdapter:
    """Crypto Pay signature verifier + event parser.

    Constructed with the API token (``CRYPTO_PAY_TOKEN`` / the
    ``PaymentsConfig.crypto_api_secret`` SecretStr). Stateless past
    that: tests build one per case, prod builds one per webhook.

    The token-typed constructor (rather than reading env at use time)
    keeps the adapter testable without ``monkeypatch.setenv``.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Return True iff the signature on ``body`` matches our token.

        Fail-closed on missing token (caller should have gated this
        path on :attr:`PaymentsConfig.crypto_configured` already, but
        defense in depth), on a missing/empty header, and on a
        non-ASCII one — see the comment at the comparison. Uses
        :func:`hmac.compare_digest` to avoid a timing oracle.
        """
        if not self._token:
            return False
        # Case-insensitive lookup. Starlette's Headers already do
        # this, but we accept a plain Mapping here for testability —
        # so we normalise both spellings explicitly.
        signature = (
            headers.get("crypto-pay-api-signature") or headers.get("Crypto-Pay-API-Signature") or ""
        ).strip()
        if not signature:
            return False
        secret = hashlib.sha256(self._token.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        # ASCII before constant-time, the same order as the RollyPay
        # twin and ``webhook/security.py:61``. Starlette decodes header
        # values with latin-1, so a 0x80-0xFF byte reaches here as a
        # non-ASCII ``str`` and ``compare_digest`` raises ``TypeError``
        # instead of returning False. Nothing registers an ``Exception``
        # handler on this app, so before #916 that escaped the route as
        # a 500 with a traceback to an anonymous caller. A hex digest is
        # ASCII, so this refuses nothing that could have matched.
        if not signature.isascii():
            return False
        return hmac.compare_digest(expected, signature)

    def parse_event(self, body: bytes) -> ParsedEvent | None:
        """Parse a verified body into a credit-relevant event.

        Returns ``None`` for any non-credit update type (e.g.
        ``invoice_expired``) or a malformed payload — the router
        answers 200 in both cases so the provider stops retrying.

        Crypto Pay stuffs the originating user_id into
        ``payload.payload`` (yes, doubly-nested) per the legacy
        ``on_crypto_payment_webhook`` reading order. We mirror that
        exactly.
        """
        try:
            data: Any = json.loads(body) if body else {}
        except Exception:
            log.warning("crypto: invalid JSON body")
            return None
        if not isinstance(data, dict):
            return None
        if data.get("update_type") != _UPDATE_PAID:
            return None
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        try:
            user_id = int(payload.get("payload") or 0)
            invoice_id = str(payload.get("invoice_id") or "")
            amount_str = str(payload.get("amount") or "0")
            # #1232: what ``amount`` MEANS depends on how the invoice was
            # minted. A fiat invoice already quotes it in ``fiat``, and
            # its ``paid_usd_rate`` describes whichever coin the payer
            # settled in — multiplying the two would turn a $5 top-up
            # into $500 000 of coins.
            if str(payload.get("currency_type") or "").lower() == _CURRENCY_FIAT:
                # #239: recorded in the invoice's own denomination, which
                # here is the fiat currency rather than a ticker.
                asset = str(payload.get("fiat") or "").upper()
                if asset != INVOICE_FIAT:
                    # We mint dollars and nothing else, so a fiat invoice
                    # in another currency is not one of ours — and
                    # reading it as dollars would misprice it by the
                    # whole cross rate.
                    log.warning(
                        "crypto: refusing {inv} — fiat invoice in {a!r}, not {f}",
                        inv=invoice_id,
                        a=asset,
                        f=INVOICE_FIAT,
                    )
                    return None
                paid_usd_rate = None
                amount_usd = float(amount_str)
            else:
                # #239: the ticker the invoice was actually settled in
                # (USDT, TON, ...) — recorded, and since #1233 also what
                # decides whether a missing rate may be read as parity.
                asset = str(payload.get("asset") or "").upper()
                paid_usd_rate = _usd_rate(payload.get("paid_usd_rate"), asset)
                if paid_usd_rate is None:
                    log.warning(
                        "crypto: refusing {inv} — no usable USD rate for {a!r}",
                        inv=invoice_id,
                        a=asset,
                    )
                    return None
                amount_usd = float(amount_str) * paid_usd_rate
        except (TypeError, ValueError) as exc:
            log.warning("crypto: invalid payload fields: {exc}", exc=exc)
            return None
        # ``float()`` happily parses "inf"/"nan" — NaN falls out at the
        # ``> 0`` test but infinity passes it and then overflows inside
        # ``int()``. The body is HMAC-signed so this is provider
        # malformation rather than an attack, but a 500 here would have
        # Crypto Pay retrying the same broken invoice forever.
        #
        # #1698: the guard used to check the operand and not the
        # product. ``1e308`` is finite, so it passed here and then
        # multiplied to ``inf`` inside :func:`coins_for_usd`, where
        # ``math.floor`` raised the OverflowError this branch was
        # written to prevent. ``is_priceable`` bounds the magnitude as
        # well as testing finiteness.
        if not is_priceable(amount_usd):
            log.warning(
                "crypto: unusable amount for {inv}: {amt!r}",
                inv=invoice_id,
                amt=amount_str,
            )
            return None
        if user_id <= 0 or not invoice_id or amount_usd <= 0:
            # #1183: this refusal used to be the silent one. The body is
            # signed, so the invoice is real; what is missing is
            # somebody to credit. Naming the three fields is what turns
            # the owner's alert into something actionable.
            log.warning(
                "crypto: refusing a signed invoice — user_id={uid!r} "
                "invoice_id={inv!r} amount_usd={amt!r}",
                uid=user_id,
                inv=invoice_id,
                amt=amount_usd,
            )
            return None
        # Rounding policy: floor (truncate toward zero). Mirrors
        # legacy ``bot.py:on_crypto_payment_webhook`` (line 18664),
        # which computes ``coins = int(amount_usd * rate)`` — Python's
        # ``int()`` floors positive floats, so legacy effectively
        # floored. A fractional cent of rounding stays with the
        # service (never credited as a phantom coin to the user); the
        # ceiling that an earlier comment described was never the
        # legacy posture. Boundary: ``0.5 USD * 900 = 450.0`` ⇒
        # exactly 450 coins; ``0.0005 USD * 900 = 0.45`` ⇒ 0 coins ⇒
        # event is dropped at the ``coins <= 0`` guard below
        # (provider considers the invoice paid, but we won't credit
        # a sub-coin top-up). See M-E-1 in audits/01_economy.md.
        coins = coins_for_usd(amount_usd)
        if coins <= 0:
            # #1183: deliberate (a phantom coin is worse), but the payer
            # was still debited, so it cannot stay unlogged.
            log.warning(
                "crypto: sub-coin invoice {inv} for {amt} USD — no credit",
                inv=invoice_id,
                amt=amount_usd,
            )
            return None
        # #1232: a fiat invoice applied no conversion, so there is no
        # rate to record. ``None`` says that; a ``1`` would claim a
        # parity nobody quoted.
        fx_rate = fiat_or_none(str(paid_usd_rate)) if paid_usd_rate is not None else None
        return ParsedEvent(
            provider=Provider.CRYPTO,
            external_id=invoice_id,
            user_id=user_id,
            coins=coins,
            reason="Покупка криптой (Crypto Pay)",
            # #239: recorded in the provider's own units — the asset
            # amount and the asset→USD rate it quoted — rather than
            # the derived ``amount_usd``. That product is a float and
            # would land in the audit column as 10.000000000000002;
            # the two operands are exactly what Crypto Pay sent, and
            # the USD figure is recoverable from them.
            fiat_amount=fiat_or_none(amount_str),
            fiat_currency=asset or None,
            fx_rate=fx_rate,
        )

    @staticmethod
    def describes_paid_money(body: bytes) -> bool:
        """Did this callback report money that actually moved?

        Deliberately looser than :meth:`parse_event`, and in the
        opposite direction. That method is strict because it mints
        coins: an update type we have never seen must not credit
        anybody. This one decides whether a refusal is worth waking the
        owner for, where the expensive mistake is the other one — an
        update Crypto Pay adds tomorrow, carrying a paid invoice, would
        otherwise be refused a credit *and* pass in silence, leaving a
        payer debited with nothing to show for it. So a paid
        ``update_type`` OR a paid invoice ``status`` is enough. The
        same asymmetry, for the same reason, as
        :meth:`RollyPayAdapter.describes_paid_money`.

        An amount that will not parse resolves to ``True`` for the same
        reason: unreadable is not zero, and guessing "probably nothing"
        is guessing in the direction that loses a real payment. Only a
        readable, non-positive amount is refused.
        """
        try:
            data: Any = json.loads(body) if body else {}
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        raw = data.get("payload")
        payload: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
        update_type = str(data.get("update_type") or "").lower()
        status = str(payload.get("status") or "").lower()
        if update_type != _UPDATE_PAID and status != _STATUS_PAID:
            return False
        amount = payload.get("amount")
        if amount is None:
            return True
        try:
            return float(str(amount)) > 0
        except (TypeError, ValueError):
            return True
