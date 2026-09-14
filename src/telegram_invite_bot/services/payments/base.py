"""Shared types for payment provider adapters (T-025).

:class:`ParsedEvent` is the boundary value class between the
adapter layer (verify + parse) and :class:`PaymentsService`
(idempotency + credit). Keeping it minimal and provider-agnostic
forces each adapter to do the per-provider semantic translation up
front rather than scattering ``if provider == "stripe"`` branches
through the service.

Why one type for three providers
--------------------------------
The three providers ship genuinely different payloads but the
credit path only needs four things:

* ``provider`` — for the idempotency key.
* ``external_id`` — provider's own id (invoice / session / payment).
* ``user_id`` — who to credit.
* ``coins`` — how many coins (already converted from USD/RUB where
  applicable, so the service is currency-agnostic).

A unified shape means the service has one credit path and tests
exercise it once. Provider-specific quirks (Crypto Pay's USD→coin
conversion, YooKassa's reverify roundtrip, Stripe's metadata
extraction) live in the adapter that produces the ``ParsedEvent``.

#239: why the money itself is carried too
-----------------------------------------
The four fields above are everything the *credit* needs, and for a
long time that was the whole type. They are not everything an
*audit* needs. The adapter is the last place in the process that
knows what the customer was actually charged: it holds the
provider's amount and currency, converts them to coins, and then
drops both on the floor. Nothing downstream — not ``ParsedEvent``,
not ``processed_webhooks``, not the ledger row, which is
denominated in coins — records the rouble figure or the rate it was
converted at. Reconciling our books against a RollyPay settlement
report was therefore impossible from our side alone: the coin
counts are known, the roubles are not, and the rate moves daily so
they cannot be recovered after the fact.

Hence ``fiat_amount`` / ``fiat_currency`` / ``fx_rate``: three
optional fields that carry the charge as the provider stated it.
They are deliberately inert — the service does no arithmetic with
them and no decision depends on them. They exist to be written
down, and they default to ``None`` so that a caller with nothing
useful to say (or a test constructing a minimal event) is not
forced to invent a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum


def fiat_or_none(raw: str | None) -> Decimal | None:
    """Parse an audit-only money string, or give up quietly (#239).

    The fiat fields are a record, not an input: nothing downstream
    branches on them and no coin count depends on them. So a value
    this function cannot make sense of must not be allowed to fail
    a payment that the adapter has already verified and priced —
    the correct outcome is a ``NULL`` column and a credited
    customer, never an exception on the credit path.

    Non-finite values are refused along with unparseable ones.
    ``Decimal("Infinity")`` and ``Decimal("NaN")`` both parse
    happily, and either one written into the audit column would be
    worse than an empty cell: it reads as a figure.
    """
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


class Provider(StrEnum):
    """Stable provider identifiers used in the processed_webhooks PK.

    Values are the strings written to ``ProcessedWebhook.provider``
    and rendered in log lines — short, lowercase, no punctuation so
    a future SQL audit query (``WHERE provider = 'stripe'``) is
    unsurprising.
    """

    CRYPTO = "crypto"
    YOOKASSA = "yookassa"
    STRIPE = "stripe"
    ROLLYPAY = "rollypay"
    # L-80: Telegram Stars top-ups credit through the SAME
    # PaymentsService pipeline as the webhook providers, keyed on
    # ``telegram_payment_charge_id`` — legacy used the prefixed
    # ``stars_{charge_id}`` as its idempotency tx_id (bot.py:18255);
    # the (provider, external_id) composite PK makes that prefix
    # redundant here.
    STARS = "stars"


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    """A credit-relevant payment event after verify + parse.

    Frozen because the service must not mutate it (any normalisation
    happens in the adapter), and slotted because we allocate one
    per webhook delivery and the slot table is smaller than __dict__.

    ``coins`` is the coin amount to credit AFTER any
    currency-to-coin conversion — Crypto Pay's USD × 900, Stripe's
    cents × 900 ÷ 100, YooKassa's roubles through the live USD/RUB
    fix (T-020 R11). None of the three trusts a coin count carried in
    provider metadata: that field is set at checkout time and never
    re-signed, so it is only ever logged on mismatch (R-FIX-003). The
    service does NOT do any math on ``coins`` — it just calls
    ``EconomyService.credit(user_id, coins, ...)``.

    ``reason`` is the ledger row's ``reason`` column — provider-
    specific text matching the legacy strings so a `/cstats` view
    that filters by reason keeps working across pipelines.

    ``fiat_amount`` / ``fiat_currency`` / ``fx_rate`` are the audit
    trail (#239) and never feed the credit. ``fiat_amount`` is what
    the provider says the customer paid, in ``fiat_currency`` — a
    ``Decimal`` because it is money and a float round-trip would
    turn 1000.00 ₽ into something that no longer matches a
    settlement report. ``fx_rate`` is the rate that carried
    ``fiat_amount`` to the USD figure the coin count came off — and
    it is NOT one fixed currency pair (#1531). The two rouble
    providers put a USD/RUB fix there — see the ``fx_rate=``
    argument in :meth:`YooKassaAdapter.parse_event` and
    :meth:`RollyPayAdapter.parse_event`; Crypto Pay puts the
    ASSET-to-USD rate its own invoice quoted, so a BTC top-up
    records a five-figure number against ``fiat_currency="BTC"``
    (:meth:`CryptoAdapter.parse_event`). Read as "multiply
    ``fiat_amount`` by this to get USD" both are one rule, and the
    pair is always printed together, never mixed — the sole reader
    appends the rate to the amount it belongs to
    (``services/payments_service._format_fiat``). ``None`` means no
    conversion happened: Stripe charges in USD, so
    :meth:`StripeAdapter.parse_event` passes ``fx_rate=None``, and a
    fiat Crypto Pay invoice quotes no rate. Telegram Stars
    report ``XTR``, which is not fiat at all; it rides the same
    fields because "what the customer was charged, in the units the
    provider charged it" is the question all five answer.
    """

    provider: Provider
    external_id: str
    user_id: int
    coins: int
    reason: str
    fiat_amount: Decimal | None = None
    fiat_currency: str | None = None
    fx_rate: Decimal | None = None


class UncreditedCause(StrEnum):
    """Why a payment the provider itself confirmed was refused a credit.

    #1643. Machine keys, not prose: the adapter knows which gate
    fired, the router owns what the owner reads. Keeping those apart
    is what lets the adapters stay pure value-producers (see the
    package docstring) while the owner-facing wording stays next to
    the other alert tables in ``webhook/payments.py``.

    Every member names a refusal reached AFTER the provider confirmed
    the payment: the money is on the merchant account and the payer's
    balance never moved. The refusals decided from an unauthenticated
    body are deliberately absent — those describe no money at all, and
    a card raised off one of them would be a stranger ringing the
    owner's phone.
    """

    BAD_METADATA = "bad_metadata"
    NO_USER_ID = "no_user_id"
    NOT_RUB = "not_rub"
    BAD_AMOUNT = "bad_amount"
    BELOW_ONE_COIN = "below_one_coin"


@dataclass(frozen=True, slots=True)
class UncreditedPayment:
    """A confirmed payment the parser refused — an adapter's second output.

    #1643. ``parse_event`` used to answer ``ParsedEvent | None``, and
    that ``None`` covered two unrelated outcomes: "this notification is
    not money" and "this is money we could not credit". Only the second
    is the owner's problem — the customer paid, the coins never
    arrived, and the sole trace was a WARNING nobody reads — and the
    router had no way to tell it from the first.

    Deliberately not a subclass of :class:`ParsedEvent` and deliberately
    not an exception: a caller that forgets this outcome gets a mypy
    error on the union rather than a 500 on a public route.

    ``payer`` and ``amount`` are strings because they are printed, not
    computed. A field the provider will not give up stays empty and the
    alert renders it as ``?``, which is usually the diagnosis itself —
    an empty ``payer`` *is* :attr:`UncreditedCause.NO_USER_ID`.

    Note the name: ``payer``, not ``user_id``. Reusing
    :class:`ParsedEvent`'s field name would let a caller that forgot to
    narrow the union read the wrong member's id and still type-check.
    """

    provider: Provider
    external_id: str
    cause: UncreditedCause
    amount: str = ""
    payer: str = ""
