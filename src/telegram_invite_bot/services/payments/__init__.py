"""Provider adapters for payment webhooks (T-025).

One adapter per provider. Each exposes:

* ``verify_signature(headers, body) -> bool`` — fail-closed signature
  check. False means the router responds with the provider's expected
  4xx and does NOT touch the database.
* ``parse_event(body) -> ParsedEvent | None`` — pure transform from
  the raw verified body to a typed value the service consumes. None
  means "not a credit-relevant event" (e.g. Crypto Pay's
  ``invoice_expired``, Stripe's ``payment_intent.created``), which
  the router answers 200 to ack without crediting. The YooKassa
  adapter widens that to ``ParsedEvent | UncreditedPayment | None``
  (#1643): its reverify is the only point in the process that knows a
  refused payment was confirmed by the provider, and ``None`` cannot
  carry that. The other three keep the narrow type — their refusals
  are read off a body an HMAC already vouched for, which the router
  reaches directly through ``describes_paid_money``.

The adapters know nothing about the wallet, the ledger, or
idempotency. They are pure value-producers; :class:`PaymentsService`
owns side effects. This split is what makes the unit tests for the
service trivial — mock the three adapters and assert the credit
decision.
"""

from telegram_invite_bot.services.payments.base import (
    ParsedEvent,
    Provider,
    UncreditedCause,
    UncreditedPayment,
)
from telegram_invite_bot.services.payments.crypto import CryptoAdapter
from telegram_invite_bot.services.payments.rollypay import RollyPayAdapter
from telegram_invite_bot.services.payments.stripe import StripeAdapter
from telegram_invite_bot.services.payments.yookassa import YooKassaAdapter

__all__ = [
    "CryptoAdapter",
    "ParsedEvent",
    "Provider",
    "RollyPayAdapter",
    "StripeAdapter",
    "UncreditedCause",
    "UncreditedPayment",
    "YooKassaAdapter",
]
