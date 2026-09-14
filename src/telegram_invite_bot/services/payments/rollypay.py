"""RollyPay payment webhook adapter.

Provider spec: https://docs.rollypay.io/api/callbacks/

RollyPay is the fourth provider and the first rouble one that actually
*signs* its callbacks. YooKassa does not sign at all — that adapter has
to spend a network roundtrip (``Payment.find_one``) re-asking the
provider whether the event was real, and then re-read every field off
the answer because the body itself is unauthenticated. Here the body IS
the authentication: ``X-Signature`` is an HMAC-SHA256, hex-encoded, over

    X-Timestamp + "." + <raw request body>

keyed with the terminal's ``signing_secret``. A body that verifies came
from RollyPay unmodified, so the amount and the metadata in it are
evidence rather than hints, and the credit can be derived from the
delivery alone. That is why this module has no reverify hop.

Two consequences of that are worth stating, because they are the reason
the checks below exist rather than being a shorter file:

**The signed timestamp is checked for freshness, but it is not what
stops a double credit.** Idempotency does — ``(provider, external_id)``
is the primary key on ``processed_webhooks``, so replaying a captured
delivery credits nothing the second time regardless of its age. The
window here is defence in depth, and it is therefore set *generously*
(:data:`_MAX_CLOCK_SKEW_SECONDS`): RollyPay retries a failed delivery up
to 8 times with exponential backoff reaching ~32 minutes, and a window
tight enough to feel clever would start rejecting the provider's own
legitimate late retries. Losing a real payment to a strict clock check
is a much worse failure than accepting a replay the idempotency table
already neutralises.

**A sandbox payment must never mint a real coin.** ``POST /payments``
takes ``"test": true`` and the resulting callback carries ``"test":
true`` (plus an ``X-Test-Mode: true`` header). Those deliveries are
correctly signed — the signing secret is the same one — so signature
verification alone would happily credit them, and anyone who can reach
the merchant dashboard could mint coins for free by issuing sandbox
payments. :meth:`RollyPayAdapter.parse_event` refuses them explicitly.

Currency: ``amount`` is a decimal string, and the callback names its
currency in either ``currency`` or ``payment_currency`` — the outbound
``POST /payments`` request uses the latter (see
:meth:`~telegram_invite_bot.services.payments.rollypay_client.
RollyPayClient.create_payment`), and this module has always read the
former first. Which one an inbound callback actually carries is the
provider's schema, not ours, and the production credits pass through
that ``or`` either way, so they do not settle the question.
:meth:`RollyPayAdapter.parse_event` therefore accepts whichever is
present and refuses a callback that carries both with *different*
values, rather than ranking two keys it has no grounds to rank. Only
RUB credits — the coin price for roubles is derived through the dollar
anchor by :func:`~telegram_invite_bot.services.payments.rates.
coins_for_rub`, exactly as the YooKassa leg does, so the two rouble
providers cannot drift apart or open a spread against the withdraw desk
(see ``docs/ECONOMY_RATE_AUDIT.md`` §8.6). A payment settled in EUR or
in crypto would be priced as if it were roubles, which is why anything
else is refused rather than best-guessed.

Reversals: unlike Crypto Pay, RollyPay carries real chargeback risk —
cards and SBP can be pulled back after the coins are spent. This adapter
does not credit ``payment.chargeback`` / ``payment.refunded`` (they are
not credit events), but it does not swallow them either:
:meth:`RollyPayAdapter.classify` lets the router recognise a reversal and
raise the alarm. Automatic debiting is deliberately NOT done here — the
wallet may already be empty, and deciding between a negative balance, a
partial claw-back and a manual review is an owner's policy call, not an
adapter's.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from loguru import logger

from telegram_invite_bot.services.payments.base import ParsedEvent, Provider
from telegram_invite_bot.services.payments.rates import (
    FALLBACK_USD_TO_RUB,
    coins_for_rub,
    is_priceable,
    sane_usd_to_rub,
)
from telegram_invite_bot.utils.numbers import parse_int_token

log = logger.bind(component="payments.rollypay")

#: How far the signed ``X-Timestamp`` may sit from our clock.
#:
#: One hour, deliberately loose — see the module docstring. RollyPay's
#: retry ladder ends ~63 minutes after the first attempt, and each
#: attempt is re-signed with a fresh dispatch timestamp, so an hour
#: covers the whole ladder even if a delivery is signed once and
#: re-sent. The value that actually prevents a double credit is the
#: idempotency key, not this number.
_MAX_CLOCK_SKEW_SECONDS: Final[int] = 3600

#: The only event that moves money toward the user.
_EVENT_PAID: Final[str] = "payment.paid"

#: Events that move money *away* after a successful payment. Not
#: credit-relevant, but the router alerts on them (see ``classify``).
REVERSAL_EVENTS: Final[frozenset[str]] = frozenset(
    {"payment.chargeback", "payment.refunded", "refund_request.completed"}
)

#: The event names allowed to reach the credit path (#227). The
#: empty string is the callback that carries no ``event_type`` at
#: all: for those the ``status`` field is the only signal there
#: is, and refusing them would drop real payments. Everything
#: else — a name RollyPay adds tomorrow, a rename, a partial
#: refund — is refused here rather than waved through by a
#: ``status: "paid"``, which a reversal body carries just as
#: legitimately as a payment body does.
_CREDIT_EVENTS: Final[frozenset[str]] = frozenset({"", _EVENT_PAID})

#: Ledger ``reason`` for a RollyPay credit. Follows the YooKassa row's
#: shape ("Покупка (ЮKassa)") so a reason-filtered ``/cstats`` view reads
#: consistently across the rouble providers.
_REASON: Final[str] = "Покупка (RollyPay)"


class RollyPayAdapter:
    """RollyPay callback verifier + event parser."""

    def __init__(
        self,
        signing_secret: str,
        *,
        usd_to_rub: float = FALLBACK_USD_TO_RUB,
        clock: Callable[[], float] = time.time,
        max_skew_seconds: int = _MAX_CLOCK_SKEW_SECONDS,
    ) -> None:
        """``usd_to_rub`` is resolved by the caller, not fetched here.

        Same contract as :class:`~telegram_invite_bot.services.payments.
        yookassa.YooKassaAdapter`: the adapter API is sync, an FX lookup
        is network I/O, so the router awaits the rate and hands it in.
        Defaulted to :data:`FALLBACK_USD_TO_RUB` and re-clamped through
        :func:`sane_usd_to_rub` — this number is about to mint coins, so
        it is checked again even though the router already checked it.

        ``clock`` is injected for the same reason the rate is: a test
        that pins "now" is a test that cannot go flaky at midnight.
        """
        self._secret = signing_secret
        self._usd_to_rub = sane_usd_to_rub(usd_to_rub)
        self._clock = clock
        self._max_skew = max_skew_seconds

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        """Case-insensitive header read.

        ``dict(request.headers)`` on Starlette lowercases keys, but the
        adapter is also called directly from tests with the casing the
        provider documents. Reading both keeps those two callers from
        disagreeing about whether a signature was even present.
        """
        return headers.get(name) or headers.get(name.lower()) or ""

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Constant-time HMAC check over ``timestamp + "." + body``.

        Fail-closed on every branch: no secret, no signature header, no
        timestamp header, an unparseable timestamp, a timestamp outside
        the skew window, a non-ASCII signature header, or a digest
        mismatch all return False and the router answers 403 without
        touching the database.

        That ASCII arm is not decoration, and until #916 it was missing
        — which made the sentence above false. Starlette decodes header
        values with latin-1, so any byte in 0x80-0xFF arrives here as a
        non-ASCII ``str``, and ``hmac.compare_digest`` raises
        ``TypeError`` on those rather than returning False. The only
        exception handler this app registers is for
        ``StarletteHTTPException`` (cms/notfound.py:226), so that
        TypeError escaped the route as a 500 with a full uvicorn
        traceback to an anonymous caller — and a *legitimate* callback
        carrying one mangled signature byte burned RollyPay's whole
        retry ladder against 500s while ``_alert_uncredited`` stayed
        silent, because that helper lives downstream of this check. A
        hex digest is ASCII by construction, so refusing early can only
        ever refuse a request that was going to mismatch anyway.
        ``webhook/security.py:61`` guards the Telegram route the same way.

        The *raw* timestamp header string is what gets signed, so that
        is what goes into the digest — the parsed integer is used only
        for the freshness comparison, and it is bounded as well as
        shaped (#1698, below). Re-serialising the parsed value
        (``str(int(ts))``) would silently break verification for any
        dispatch whose header carried, say, a leading zero, and the
        failure would look like a wrong secret.
        """
        if not self._secret:
            return False
        signature = self._header(headers, "X-Signature")
        timestamp = self._header(headers, "X-Timestamp")
        if not signature or not timestamp:
            log.warning("rollypay: missing X-Signature/X-Timestamp")
            return False
        # #1698: ``int()`` was the wrong parse and ``ValueError`` the
        # wrong sibling to guard it with. A 401-digit run of ASCII
        # digits parses cleanly here — CPython only refuses at 4300 —
        # and the subtraction on the next line then raises
        # ``OverflowError: int too large to convert to float``, which
        # nothing catches, so an anonymous caller with no knowledge of
        # the secret could 500 this route at will from *outside* the
        # signature check, one header long. ``parse_int_token`` bounds
        # the magnitude as well as the character class, and everything
        # the provider actually sends is a plain digit run that parses
        # through it identically.
        sent_at = parse_int_token(timestamp)
        if sent_at is None:
            log.warning("rollypay: non-numeric X-Timestamp {ts!r}", ts=timestamp)
            return False
        skew = abs(self._clock() - sent_at)
        if skew > self._max_skew:
            log.warning(
                "rollypay: timestamp outside window ({skew:.0f}s > {max}s)",
                skew=skew,
                max=self._max_skew,
            )
            return False
        expected = hmac.new(
            self._secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        provided = signature.strip().lower()
        if not provided.isascii() or not hmac.compare_digest(expected, provided):
            log.warning("rollypay: signature mismatch")
            return False
        return True

    @staticmethod
    def classify(body: bytes) -> str:
        """``event_type`` off an already-verified body, or ``""``.

        Exists so the router can tell the two reasons ``parse_event``
        returns None apart: "benign non-credit event" (ack and forget)
        versus "money just went back out of the merchant account" (alert
        the owner). Re-parsing the JSON to answer that is a few
        microseconds on a request that already did network I/O, and it
        keeps the adapter free of per-request mutable state — the
        alternative, stashing the last event type on ``self``, turns a
        pure value-producer into something whose correctness depends on
        call order.

        Never raises: a body that will not parse has no event type.
        """
        try:
            data = json.loads(body) if body else {}
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("event_type") or "").lower()

    @staticmethod
    def describes_paid_money(body: bytes) -> bool:
        """Whether this verified body says a *real* payment cleared.

        The third case hiding behind ``None`` from :meth:`parse_event`,
        and the expensive one. ``None`` covers a benign lifecycle event
        (nothing was lost), a reversal (money went back out — alerted on
        its own), and a payment that genuinely cleared and was still
        refused a credit: settled in a currency other than RUB, carrying
        no ``metadata.user_id``, or priced below a single coin. In that
        third case the money sits on the merchant account and the
        payer's balance never moved — a state no retry fixes and no user
        can escalate past a support message.

        Answers only "did money move", never "why was it refused". The
        reason is already in the WARNING ``parse_event`` logged, and
        re-deriving it here would leave two copies of the credit rules
        free to drift apart. The status and sandbox gates are mirrored
        from :meth:`parse_event` and must keep agreeing.

        The event-type gate deliberately is not (#227).
        :meth:`parse_event` credits an allowlist —
        :data:`_CREDIT_EVENTS` — while this stays an exception list
        that refuses only the three reversal names. The asymmetry is
        the point: an event RollyPay adds tomorrow, arriving with
        ``status: "paid"``, must be refused a credit *and* still be
        reported as money that moved. Mirroring the allowlist here
        would make it fail closed in silence instead.

        A sandbox payment returns False: its callback is signed exactly
        like a real one, but nobody is out any money.
        """
        try:
            data = json.loads(body) if body else {}
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        event_type = str(data.get("event_type") or "").lower()
        if event_type in REVERSAL_EVENTS:
            # A reversal body may legitimately carry ``status: "paid"``
            # (it describes a payment that *was* paid), and reading that
            # as fresh money would turn every chargeback into a second,
            # wrong alert on top of the right one.
            return False
        status = str(data.get("status") or "").lower()
        if event_type != _EVENT_PAID and status != "paid":
            return False
        return not bool(data.get("test"))

    def parse_event(self, body: bytes) -> ParsedEvent | None:
        """Verified body → credit event, or None.

        Returns ``None`` on: non-JSON or non-object body; any event
        type other than ``payment.paid`` — a callback carrying no
        ``event_type`` at all is the one exception, and it is credited
        on ``status: "paid"`` alone; a sandbox (``test: true``) payment;
        a missing ``payment_id``; missing or non-positive
        ``metadata.user_id``; a non-RUB currency; a missing,
        non-numeric, non-finite or non-positive amount; or an amount
        too small to be worth one coin.

        Everything the credit depends on is read from the signed body.
        That is safe *here* specifically because the signature covers
        the whole body — the same code against YooKassa's unsigned
        webhook would be the R11-b vulnerability (anyone who learned a
        real payment id could restate its amount and recipient).
        """
        try:
            data_any: Any = json.loads(body) if body else {}
        except Exception:
            log.warning("rollypay: invalid JSON body")
            return None
        if not isinstance(data_any, dict):
            return None
        data: dict[str, Any] = data_any

        event_type = str(data.get("event_type") or "").lower()
        status = str(data.get("status") or "").lower()
        # An allowlist, not a list of exceptions (#227). A reversal
        # describes a payment that *was* paid, so its body can
        # legitimately carry ``status: "paid"`` — and the status arm
        # below reads that as a credit. Naming the three reversal
        # events and letting everything else through left the arm
        # reachable for any name RollyPay had not invented yet:
        # ``payment.partially_refunded``, a renamed refund event, a
        # dispute. Two things break when one of those credits. The
        # router raises its reversal alert off ``parse_event``
        # returning None, so the owner never hears that money left the
        # merchant account; and if the reversal is keyed by a refund id
        # rather than the payment id, the idempotency check has nothing
        # to match and the refund *mints coins*.
        #
        # Unknown names now fail closed — and not silently. They are
        # outside :data:`REVERSAL_EVENTS`, so the router falls through
        # to :meth:`describes_paid_money`, which stays permissive on
        # purpose and alerts the owner that a payment cleared without
        # being credited.
        if event_type not in _CREDIT_EVENTS:
            return None
        # Reached only for the no-``event_type`` callback: ``status`` is
        # all it says about itself, and refusing those would drop real
        # payments.
        if not event_type and status != "paid":
            return None

        payment_id = str(data.get("payment_id") or "")
        if not payment_id:
            log.warning("rollypay: paid event without payment_id")
            return None

        # Sandbox guard. The callback is correctly signed — the sandbox
        # shares the terminal's signing secret — so nothing upstream of
        # this line distinguishes a test payment from a real one.
        # Without the check, "create a test payment" is a coin faucet.
        if bool(data.get("test")):
            log.warning(
                "rollypay: refusing to credit TEST payment {pid} — sandbox "
                "callbacks are signed but no money moved",
                pid=payment_id,
            )
            return None

        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            log.warning(
                "rollypay: payment {pid} carries no metadata object — refusing to credit",
                pid=payment_id,
            )
            return None
        try:
            user_id = int(metadata.get("user_id") or 0)
        except (TypeError, ValueError):
            log.warning(
                "rollypay: invalid metadata.user_id for {pid}: {meta!r}",
                pid=payment_id,
                meta=metadata.get("user_id"),
            )
            return None
        if user_id <= 0:
            log.warning(
                "rollypay: payment {pid} carries no user_id — refusing to credit",
                pid=payment_id,
            )
            return None

        # #299: the two keys are read as alternatives, not as a
        # preference order. The module docstring used to promise
        # ``payment_currency`` while this line read ``currency`` first —
        # a contradiction that cannot be resolved from inside the repo,
        # because the callback schema belongs to RollyPay. Refusing a
        # disagreement is the only answer that is right regardless of
        # which key wins: everything below prices the amount as roubles,
        # so guessing wrong is a mis-credit by the whole FX rate, and a
        # refused payment the owner settles by hand is the cheaper half
        # of that trade.
        declared = str(data.get("currency") or "").upper()
        settled = str(data.get("payment_currency") or "").upper()
        if declared and settled and declared != settled:
            log.warning(
                "rollypay: payment {pid} declares currency={cur} but "
                "payment_currency={pcur} — refusing to credit",
                pid=payment_id,
                cur=declared,
                pcur=settled,
            )
            return None
        currency = declared or settled
        if currency != "RUB":
            # ``coins_for_rub`` would price 100 EUR as 100 RUB — roughly
            # a ninefold under-credit, or the reverse for a crypto leg.
            log.warning(
                "rollypay: payment {pid} settled in {cur}, not RUB — refusing to credit",
                pid=payment_id,
                cur=currency or "?",
            )
            return None

        # Decimal, not float: RollyPay quotes "1500.00" strings and the
        # float path rounds half-to-even at the IEEE-754 boundary, which
        # can silently lose a coin on small amounts. ``coins_for_rub``
        # stays in Decimal end to end.
        try:
            amount = Decimal(str(data.get("amount") or ""))
        except InvalidOperation:
            log.warning(
                "rollypay: missing/invalid amount for {pid}: {amt!r}",
                pid=payment_id,
                amt=data.get("amount"),
            )
            return None
        # ``is_priceable`` first and separately: it carries the
        # finiteness half — ``Decimal("NaN")`` parses cleanly and then
        # *raises* on comparison, while ``Decimal("Infinity")``
        # compares fine and blows up at int() — and, since #1698, the
        # magnitude half too, so a finite but absurd amount is refused
        # here under an accurate log line rather than one branch later
        # under "prices below one coin".
        if not is_priceable(amount) or amount <= 0:
            log.warning(
                "rollypay: unusable amount for {pid}: {amt!r}",
                pid=payment_id,
                amt=str(amount),
            )
            return None

        coins = coins_for_rub(amount, self._usd_to_rub)
        if coins <= 0:
            log.warning(
                "rollypay: {amt} RUB prices below one coin for {pid}",
                amt=str(amount),
                pid=payment_id,
            )
            return None

        return ParsedEvent(
            provider=Provider.ROLLYPAY,
            external_id=payment_id,
            user_id=user_id,
            coins=coins,
            reason=_REASON,
            # #239: the audit trail. This is the only place in the
            # process that still knows the rouble figure the customer
            # was charged and the rate it was priced at; two lines
            # further down both are gone and the credit is a bare coin
            # count. RollyPay settles in roubles, so without these
            # their report and our books have no common column.
            fiat_amount=amount,
            fiat_currency=currency,
            fx_rate=Decimal(str(self._usd_to_rub)),
        )
