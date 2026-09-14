"""Stripe webhook adapter (T-025).

Provider spec: https://stripe.com/docs/webhooks/signatures

Stripe signs every webhook with HMAC-SHA256 over a timestamped
payload, and the official ``stripe-python`` library packages the
whole verify+parse into one call: ``stripe.Webhook.construct_event``.
We use that directly — re-implementing the verifier would be a
maintenance liability against a moving spec (stripe rotates their
signature scheme on a multi-year cadence and the SDK absorbs it).

Quirks of this adapter compared to Crypto / YooKassa:

* ``verify_signature`` and ``parse_event`` would naturally be one
  operation in Stripe's API, but to keep the adapter interface
  uniform across providers we split them: the SDK call lives in
  ``parse_event`` and ``verify_signature`` just gate-checks the
  secret. The two-step shape matches the router's "verify-first,
  then parse" loop and the unit tests for the service can mock
  each side independently.

* Stripe-specific 400 (not 403) on signature failure is the
  router's concern — the adapter just returns ``None`` from
  ``parse_event`` when the SDK raises. The router maps "None from
  Stripe's parse_event after passing verify_signature gate" to 400.
  Crypto's None means 200 (event-not-credit-relevant), which is
  why the mapping has to be per-provider in the router rather than
  in the adapter.

  Practically: to disambiguate "bad signature" from "non-credit
  event" within ``parse_event``, this adapter returns ``None`` on
  both — and the router calls ``verify_signature`` ahead of time.
  Stripe's case is handled by routing the SDK exception through
  ``verify_signature`` instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Final

from loguru import logger

from telegram_invite_bot.services.payments.base import ParsedEvent, Provider
from telegram_invite_bot.services.payments.rates import coins_for_usd_cents, is_priceable

log = logger.bind(component="payments.stripe")

#: Events that move money *away* from the merchant account after a
#: charge already settled — and, for a top-up, after coins were already
#: minted. Not credit-relevant, but the router alerts the owner on them
#: (see :meth:`StripeAdapter.classify`), as it does for the two rouble
#: providers.
#:
#: ``charge.dispute.closed`` is deliberately absent: a dispute can close
#: *won*, which is money coming back, and an alert that fires on good
#: news teaches the owner to skim past the one that matters.
REVERSAL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "charge.refunded",
        "charge.dispute.created",
        "charge.dispute.funds_withdrawn",
    }
)

#: Events that may credit a top-up. Two, not one, because a Checkout
#: Session completes at the moment the payer *commits* — which for a
#: delayed-notification method (SEPA debit, Boleto, Konbini, a bank
#: transfer) is days before the money actually clears. Stripe reports
#: that by sending ``checkout.session.completed`` with
#: ``payment_status: "unpaid"`` and then, when the funds land,
#: ``checkout.session.async_payment_succeeded`` carrying the same
#: session with ``"paid"``. Listening to the first event alone means
#: minting coins for money that may never arrive (and, if it never
#: does, never hearing about it); listening to the second alone means
#: card payers — who are paid the instant they complete — never get
#: credited at all. So: both events, and the ``payment_status`` gate
#: below decides which one actually moves a balance.
#:
#: A double credit is not a risk here: both events carry the same
#: session id, and the idempotency key is (provider, external_id).
_CREDIT_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    }
)


class StripeAdapter:
    """Stripe ``construct_event`` wrapper.

    Holds the webhook signing secret. The SDK is imported lazily
    inside ``verify_signature`` so unit tests can run without the
    ``stripe`` package, and so an operator with all-non-Stripe
    providers doesn't pay the import cost.
    """

    def __init__(self, webhook_secret: str) -> None:
        self._secret = webhook_secret
        # Cached event from the last successful verify_signature call.
        # Stripe's SDK does verify+parse in one call, so re-running
        # construct_event in parse_event would double the work.
        #
        # Keyed by the body *bytes*, not by ``id(body)``. The identity
        # key worked as long as every entry was popped, but an entry
        # stranded by an exception between verify and parse outlives
        # its body, and CPython reuses ``id()`` after collection — a
        # later, unrelated request could then land on a stale event
        # and parse someone else's session. Bytes are hashable and
        # immutable, so a content key can only ever match the same
        # body (a duplicate delivery, which the idempotency row
        # already handles). The cache is a correctness GATE, not an
        # optimisation, and the difference matters to anyone moving
        # this code (#1444): on a miss ``parse_event`` does NOT verify
        # again, it logs a warning and returns ``None``, which the
        # route reads as "not a crediting event" and answers 200 —
        # after which Stripe never redelivers. A paid checkout would
        # vanish with a WARNING as its only trace. The miss is
        # unreachable today only because the route hands the very same
        # ``body`` object to both calls and builds a fresh adapter per
        # request; anything that breaks either property breaks
        # crediting, silently.
        self._last_event: dict[bytes, dict[str, Any]] = {}

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Run ``stripe.Webhook.construct_event`` — verify + cache event.

        Returns False on any exception from the SDK (signature
        mismatch, timestamp skew, malformed payload, missing
        secret). The router maps that to 400 per Stripe's spec.
        """
        if not self._secret:
            return False
        sig = headers.get("Stripe-Signature") or headers.get("stripe-signature") or ""
        if not sig:
            return False
        try:
            import stripe

            event = stripe.Webhook.construct_event(body, sig, self._secret)
        except Exception as exc:
            log.warning("stripe: signature failure: {exc}", exc=exc)
            return False
        # Stash for parse_event. ``construct_event`` returns a
        # dict-alike (stripe.Event).
        self._last_event[body] = dict(event)
        return True

    @staticmethod
    def classify(body: bytes) -> str:
        """``type`` off the event body, or ``""``.

        Read from the raw body rather than the cached event because
        ``parse_event`` pops that cache — and re-parsing costs nothing
        next to the verification that already happened. Safe precisely
        because it *did* happen: the router only reaches this after
        ``verify_signature`` accepted the body's HMAC, so this reads a
        signed document, not an attacker's.

        Never raises: a body that will not parse has no type.
        """
        try:
            data = json.loads(body) if body else {}
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("type") or "").lower()

    @staticmethod
    def describes_paid_money(body: bytes) -> bool:
        """Did this verified event report money that actually moved?

        The third case hiding behind ``None`` from
        :meth:`parse_event`, and the expensive one. ``None`` covers an
        event this route does not credit, a Checkout Session the payer
        has committed to but not yet cleared, and a session that
        genuinely cleared and was still refused a credit: settled in a
        currency other than USD, carrying no ``metadata.user_id``, or
        priced below a single coin. In that third case the money sits
        on the merchant account and the payer's balance never moved —
        a state no retry fixes and no user can escalate past a support
        message.

        Answers only "did money move", never "why was it refused". The
        reason is already in the WARNING :meth:`parse_event` logged.

        Unlike :meth:`RollyPayAdapter.describes_paid_money` this is
        NOT an exception list over event names, and the difference is
        forced by the provider. Stripe delivers hundreds of event
        types to one endpoint, almost none of which say anything
        about a top-up, and ``charge.dispute.closed`` is deliberately
        outside :data:`REVERSAL_EVENTS` because a dispute can close
        *won* — so "not a reversal, therefore paid money" would fire a
        paid-but-uncredited alert on good news. The discriminator is
        the object instead: a Checkout Session is the only shape this
        route ever credits, and it names itself. That still keeps the
        asymmetry the alert exists for — a session event Stripe adds
        tomorrow is refused a credit by :data:`_CREDIT_EVENTS` and is
        still reported here.

        ``payment_status`` gates exactly as it does in
        :meth:`parse_event`, mirrored on purpose: anything but
        ``paid`` is a payer who committed and has not cleared, so
        nobody is out any money yet. Absent is treated as paid there
        and is treated as paid here.

        An ``amount_total`` that will not parse resolves to ``True``:
        unreadable is not zero, and guessing "probably nothing"
        guesses in the direction that loses a real payment. Only a
        readable non-positive amount — a fully discounted session — is
        refused.

        ``livemode: false`` returns ``False``. A test-mode event is
        signed exactly like a live one whenever the operator pointed
        the route at the test secret, but nobody is out any money —
        the same call RollyPay's sandbox flag gets, and mirrored in
        :meth:`parse_event` so the two halves cannot disagree about
        whether an event moved money.

        Never raises, and reads the raw body rather than the cached
        event for the same reason :meth:`classify` does: the router
        only reaches this after the HMAC passed, and
        :meth:`parse_event` has already popped the cache by then.
        """
        try:
            data: Any = json.loads(body) if body else {}
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        if data.get("livemode") is False:
            return False
        raw_data = data.get("data")
        wrapper: Mapping[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        raw_obj = wrapper.get("object")
        sess: Mapping[str, Any] = raw_obj if isinstance(raw_obj, dict) else {}
        event_type = str(data.get("type") or "").lower()
        is_session = str(sess.get("object") or "") == "checkout.session"
        if event_type not in _CREDIT_EVENTS and not is_session:
            return False
        payment_status = str(sess.get("payment_status") or "").lower()
        if payment_status and payment_status != "paid":
            return False
        amount = sess.get("amount_total")
        if amount is None:
            return True
        try:
            return int(amount) > 0
        except (TypeError, ValueError):
            return True

    def parse_event(self, body: bytes) -> ParsedEvent | None:
        """Extract the credit-relevant fields from the cached event.

        Returns None on:
        - Cache miss (verify_signature wasn't called or didn't pass).
        - Event type outside :data:`_CREDIT_EVENTS`.
        - ``livemode: false`` — a test-mode event (#1609).
        - A session whose ``payment_status`` is anything but ``paid``.
        - Missing/malformed metadata.

        Stripe ships a coin count in ``data.object.metadata.coins``
        (set at session-creation time by the bot's checkout page), but
        it is NOT what gets credited. Metadata is caller-supplied and
        travels outside the signature's guarantees, so the credit is
        derived from the signed ``amount_total`` instead (:387), and
        ``metadata.coins`` survives only as a mismatch warning (:390-397).
        That is R-FIX-003; the docstring used to claim the opposite.
        """
        event = self._last_event.pop(body, None)
        if event is None:
            log.warning("stripe: parse_event called without prior verify")
            return None
        if event.get("type") not in _CREDIT_EVENTS:
            return None
        # #1609: test-mode guard, the exact counterpart of
        # RollyPay's ``test: true`` refusal. A test-mode event is
        # signed with the test secret, so the HMAC passes and
        # nothing upstream of this line tells it apart from a live
        # one — but no money moved. Until now the check lived only
        # in ``describes_paid_money``, which the router reaches
        # ONLY after this method already returned None; on the
        # crediting path there was no gate at all, so a test
        # endpoint pointed at the live URL was a coin faucet.
        #
        # ``is False``, not falsy, and absent means live: the same
        # call ``describes_paid_money`` makes, mirrored on purpose.
        # Refusing on absence would turn a provider-side omission
        # into a payer who paid and got nothing.
        if event.get("livemode") is False:
            log.warning(
                "stripe: refusing to credit TEST-mode event {eid} — "
                "test webhooks are signed but no money moved",
                eid=event.get("id"),
            )
            return None
        sess = event.get("data", {}).get("object") or {}
        if not isinstance(sess, dict):
            return None
        # "Completed" is the payer finishing the checkout, not the
        # money arriving — see :data:`_CREDIT_EVENTS`. Everything the
        # credit is derived from is signed, so this field is as
        # trustworthy as the amount next to it; what it is *not* is
        # optional. A session that says "unpaid" here and is credited
        # anyway is coins minted against a payment that may still fail,
        # and the failure event (``async_payment_failed``) would arrive
        # long after they had been spent.
        #
        # Absent is treated as paid: only Stripe can put a body past
        # the HMAC, older API versions did not always send the field,
        # and refusing on absence would turn a provider-side omission
        # into a payer who paid and got nothing. ``no_payment_required``
        # (a full discount) is refused here and would be refused again
        # by the non-positive amount gate below.
        payment_status = str(sess.get("payment_status") or "").lower()
        if payment_status and payment_status != "paid":
            log.warning(
                "stripe: session {sid} is {ps!r}, not paid — no credit",
                sid=sess.get("id"),
                ps=payment_status,
            )
            return None
        session_id = str(sess.get("id") or "")
        metadata = sess.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        try:
            user_id = int(metadata.get("user_id") or 0)
            # Metadata coins is parsed only for the post-derive
            # mismatch warning. The credit amount is derived from
            # the signed ``amount_total`` (R-FIX-003 — metadata is
            # user-influenced at session-creation time and never
            # re-signed by Stripe; the HMAC only covers the whole
            # event body, but the metadata content inside it was
            # set by whatever code created the Checkout Session).
            metadata_coins = int(metadata.get("coins") or 0)
        except (TypeError, ValueError):
            log.warning("stripe: invalid metadata: {meta!r}", meta=metadata)
            return None
        if not session_id or user_id <= 0:
            # #1982: ``<= 0``, not ``not user_id``. A Telegram chat id
            # below zero is a group, never a payer, and the credit path
            # below does not check the sign — ``EconomyRepo.get_or_create``
            # would open a wallet for the supergroup and put the coins
            # in it, leaving the person who paid with nothing and no
            # alert, because from the code's side the credit worked.
            # This is the same refusal ``yookassa`` and ``rollypay``
            # have made since #1698; Stripe is the provider where it
            # matters most, since the id is typed into the dashboard by
            # hand rather than built by the bot.
            log.warning(
                "stripe: session {sid} carries no usable user_id ({uid!r}) — refusing to credit",
                sid=session_id,
                uid=user_id,
            )
            return None

        # R-FIX-003: derive coins from the *signed* ``amount_total``
        # (minor units = USD cents) using the legacy 900-coins/USD
        # rate. For any honest /buy invoice this equals
        # ``metadata_coins``; mismatches are logged and the server
        # value wins.
        # T-020 R11-b, applied here too: ``coins_for_usd_cents`` says
        # USD in its name and nowhere in its arithmetic. A session
        # settled in RUB would have its minor units priced as cents —
        # 900 coins for what is really about a dollar's worth of
        # roubles at a ~90x discount. Refuse rather than guess.
        currency = str(sess.get("currency") or "usd").lower()
        if currency != "usd":
            log.warning(
                "stripe: session {sid} settled in {cur}, not USD — refusing to credit",
                sid=session_id,
                cur=currency,
            )
            return None
        amount_total_raw = sess.get("amount_total")
        try:
            amount_total_cents = int(amount_total_raw or 0)
        except (TypeError, ValueError):
            log.warning(
                "stripe: invalid amount_total for sid={sid}: {amt!r}",
                sid=session_id,
                amt=amount_total_raw,
            )
            return None
        if amount_total_cents <= 0:
            log.warning(
                "stripe: non-positive amount_total for sid={sid}: {amt}",
                sid=session_id,
                amt=amount_total_cents,
            )
            return None
        # #1698: a magnitude bound, and the one refusal in this module
        # that must not print the number it refuses — loguru formats
        # eagerly, and ``str()`` on an integer past CPython's
        # 4300-digit conversion limit raises the very error the branch
        # exists to prevent. ``bit_length`` says how absurd it was
        # without converting it.
        if not is_priceable(amount_total_cents):
            log.warning(
                "stripe: amount_total for sid={sid} is {bits} bits — refusing to price it",
                sid=session_id,
                bits=amount_total_cents.bit_length(),
            )
            return None
        # Cents → USD → coins. Integer math throughout to avoid the
        # float-loss class of bug (``amount_total_cents * rate // 100``).
        coins = coins_for_usd_cents(amount_total_cents)
        if coins <= 0:
            return None
        if metadata_coins and metadata_coins != coins:
            log.warning(
                "stripe: metadata.coins={meta} != server-derived={srv} "
                "for sid={sid} amount_total={amt}c — crediting server value",
                meta=metadata_coins,
                srv=coins,
                sid=session_id,
                amt=amount_total_cents,
            )

        return ParsedEvent(
            provider=Provider.STRIPE,
            external_id=session_id,
            user_id=user_id,
            coins=coins,
            reason="Покупка (Stripe)",
            # #239: Stripe quotes an integer number of cents, so the
            # exact dollar figure is a Decimal division by 100 and
            # never a float. ``fx_rate`` stays None: the guard above
            # has already rejected anything but USD, and USD is the
            # currency coins are priced in — nothing was converted.
            fiat_amount=Decimal(amount_total_cents) / 100,
            fiat_currency=currency.upper(),
            fx_rate=None,
        )
