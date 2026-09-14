"""YooKassa payment webhook adapter (T-025).

Provider spec: https://yookassa.ru/developers/using-api/webhooks

YooKassa does NOT sign its webhooks. The shop-id / secret pair is
used (post-hoc) to reverify the event by calling
``yookassa.Payment.find_one(payment_id)`` and confirming the
authoritative status is ``"succeeded"``. That reverify is what plays
the role of signature verification for this provider — the webhook
body alone is unauthenticated, so we trust it ONLY enough to pull
the payment_id, and then ask YooKassa's API directly whether the
payment really succeeded.

The reverify is a network call, which makes ``verify_signature`` an
async-style operation in spirit — but the adapter API is sync (the
:class:`stripe.Webhook.construct_event` pattern). To keep the
adapter interface uniform across providers, ``verify_signature``
returns True iff the shop credentials are present (gate check), and
the reverify happens inside ``parse_event`` where it can short-circuit
to None on a mismatch. The router doesn't need to know — it always
calls verify_signature first, then parse_event.

This is a parity port of the legacy ``main.py`` YooKassa handler:

* Empty body on response (provider only cares about 2xx vs not).
* 403 on missing credentials (the legacy choice; we elevate to 503
  at the router level for "missing credentials" because that's our
  config issue, not the caller's — see ADR 0014).
* Reverify *mismatch* → 200, NO credit. A spoofed webhook claiming
  "succeeded" gets caught here because the authoritative API call
  disagrees. A reverify that never *completed* is a different
  outcome and is no longer folded into this one — see
  :class:`ReverifyUnavailable`, which departs from legacy parity on
  purpose.

Currency: YooKassa carries the *intended* coin count in
``object.metadata.coins``, but that field is user-influenced (set
at session-creation time and never re-signed by the provider). We
DO NOT trust it for crediting. Instead, after the reverify
roundtrip, we compute coins from the authoritative
``payment.amount.value`` (RUB string). Metadata ``coins`` is still
parsed but only logged on mismatch — the credit is driven by the
server-derived value (R-FIX-003).

Rate: T-020 (R11) replaced the frozen legacy ``PAYMENT_RUB_TO_COINS
= 10`` (``bot.py:3179``) with :func:`~telegram_invite_bot.services.
payments.rates.coins_for_rub`, which prices roubles *through* the
dollar anchor the withdraw desk pays out at. Ten coins per rouble
was only ever the derived price at USD/RUB = 90; above that fix it
was cheaper than the dollar providers charged for the same coin, so
a buyer could top up in roubles and cash back out in USDT at a
profit — an unbounded drain, since nothing rate-limits how much a
payer may deposit. The live fix arrives via ``usd_to_rub``; the
adapter itself stays offline (see :meth:`YooKassaAdapter.__init__`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from loguru import logger

from telegram_invite_bot.services.payments.base import (
    ParsedEvent,
    Provider,
    UncreditedCause,
    UncreditedPayment,
)
from telegram_invite_bot.services.payments.rates import (
    FALLBACK_USD_TO_RUB,
    coins_for_rub,
    is_priceable,
    sane_usd_to_rub,
)

log = logger.bind(component="payments.yookassa")

#: Events that move money *away* from the merchant account after a
#: payment already succeeded — and, for a top-up, after coins were
#: already minted. Not credit-relevant, but the router alerts the owner
#: on them (see :meth:`YooKassaAdapter.classify`), exactly as the
#: RollyPay route does. ``payment.canceled`` is deliberately absent: a
#: cancelled payment was never captured, so nothing was ever credited
#: for it and there is nothing to tell the owner about.
REVERSAL_EVENTS: Final[frozenset[str]] = frozenset({"refund.succeeded"})


class ReverifyUnavailable(Exception):
    """The merchant-API reverify could not be *completed*.

    Not a verdict about the payment — which is the whole distinction.
    ``parse_event`` answering ``None`` means the notification was
    judged and found to describe no money we can confirm;
    :class:`~telegram_invite_bot.services.payments.base.UncreditedPayment`
    means the provider confirmed money we then refused to credit. This
    is neither: the call that would have decided never returned, so
    nothing about the payment is known.

    It exists because the router's answer differs. A verdict is
    acknowledged with 200; an unfinished reverify has to be answered
    503, because YooKassa's documented contract is that 200
    acknowledges a notification and stops its delivery. Acking here
    therefore does not defer the credit — it discards a real payment
    because *our* merchant-API call failed, with a WARNING as the only
    trace and no documented way to ask for the notification again.

    That call is already made one branch over. #1610 moved the
    reverify *timeout* from 200 to 503 on exactly this reasoning: we
    did not decide anything, nothing was written, so the honest answer
    is "ask me again". A reverify that raises is the same outcome
    reached by a different route — no network, a 5xx from the merchant
    API, rejected credentials, or ``ImportError``, since ``yookassa``
    is an optional dependency that is absent from ``pyproject.toml``
    and from prod's site-packages. Until this class existed all four
    were spelled ``None`` and acknowledged.

    Deliberately an exception rather than a third member of the union.
    The router already wraps the call in ``try`` for
    :class:`TimeoutError`, which is the same "did not finish", and a
    caller that forgets this one gets a 500 on a public route instead
    of a silently acknowledged payment — the safe direction, unlike
    ``UncreditedPayment``, where the union is what makes a forgetful
    caller fail under mypy instead of at runtime.
    """


class YooKassaAdapter:
    """YooKassa post-hoc reverifier + event parser."""

    def __init__(
        self, shop_id: str, secret: str, *, usd_to_rub: float = FALLBACK_USD_TO_RUB
    ) -> None:
        """``usd_to_rub`` is resolved by the caller, not fetched here.

        The adapter API is sync (see the module docstring), and an FX
        lookup is a network call — so the router awaits the rate and
        hands it in. That also keeps the resolution *one* decision made
        once per request instead of a hidden I/O hop buried inside
        parsing, and lets a test pin the rate with a plain float.

        Defaulted to :data:`FALLBACK_USD_TO_RUB` so an adapter built
        without a rate reproduces the legacy 10-coins-per-rouble price
        exactly, and clamped through :func:`sane_usd_to_rub` as a last
        line of defence — this number is about to mint coins, so the
        adapter re-checks it even though the router already did.
        """
        self._shop_id = shop_id
        self._secret = secret
        self._usd_to_rub = sane_usd_to_rub(usd_to_rub)

    @staticmethod
    def _remote_field(container: Any, key: str) -> Any:
        """Read ``key`` off a reverified SDK field, dict or object.

        ``Payment.find_one`` returns model objects (``payment.amount``
        is an ``Amount`` with ``.value`` / ``.currency``) but the SDK
        hands back plain dicts for some fields and versions. Reading
        both shapes keeps the authoritative path from silently falling
        back to the body just because the SDK changed representation.
        """
        if container is None:
            return None
        if isinstance(container, Mapping):
            return container.get(key)
        return getattr(container, key, None)

    @staticmethod
    def _remote_amount_text(payment: Any) -> str:
        """Render the reverified sum as ``"<value> <currency>"``, for prose.

        #1643. Two of the money-losing refusals fire before the amount
        is parsed, and the owner's card prints the sum first — that is
        how a payment is found again in the merchant dashboard. So the
        sum is read separately here, and defensively: this only ever
        runs on a failure path and must never itself be the thing that
        raises.

        Never fed to arithmetic. The parsed :class:`~decimal.Decimal`
        is the credit path's business (see :meth:`parse_event`); this
        is a caption, and a field the SDK will not give up is simply
        left out — the alert then prints ``?``.
        """
        amount_obj = YooKassaAdapter._remote_field(payment, "amount")
        value = str(YooKassaAdapter._remote_field(amount_obj, "value") or "").strip()
        currency = str(YooKassaAdapter._remote_field(amount_obj, "currency") or "").strip()
        return " ".join(part for part in (value, currency) if part)

    def verify_signature(
        self,
        headers: Mapping[str, str],
        body: bytes,  # noqa: ARG002
    ) -> bool:
        """Gate check: shop credentials must be set.

        Not a cryptographic verification — YooKassa doesn't sign
        webhooks (see module docstring). The real authentication is
        the reverify call inside ``parse_event``. Returning False
        here without a sig is a defense-in-depth check the router
        would have caught upstream via :attr:`PaymentsConfig.
        yookassa_configured`, but we don't depend on the caller
        having done it.
        """
        return bool(self._shop_id and self._secret)

    @staticmethod
    def classify(body: bytes) -> str:
        """``event`` off the notification body, or ``""``.

        Exists so the router can tell the two reasons ``parse_event``
        returns None apart: a benign non-credit notification (ack and
        forget) versus a refund — money that left the merchant account
        against coins that are already in circulation. The body is
        unauthenticated (YooKassa does not sign webhooks), so this is a
        *hint*, never evidence: it decides whether to raise an alert,
        never whether to move a balance and — since #188 — never
        whether to write anything down either. The worst a forged body
        can do here is make the owner look at a refund that did not
        happen, and the alert it produces says on its face that it is
        unverified.

        Never raises: a body that will not parse has no event.
        """
        try:
            data = json.loads(body) if body else {}
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("event") or data.get("type") or "").lower()

    # No ``describes_paid_money`` here, and that is the decision rather
    # than an omission (#1611). The other three adapters carry it right
    # at this spot; this one must not, for two reasons that both come
    # off the same peculiarity — YooKassa does not sign its callbacks.
    #
    # 1. On the signed routes the router reaches that predicate only
    #    after an HMAC passed, so "the body says money moved" is the
    #    provider talking. Here nothing was proven: :meth:`verify_signature`
    #    above reports only that the shop credentials are configured.
    #    The alert it feeds, ``_alert_uncredited``, opens with the words
    #    "Подпись верна, платёж настоящий" — a sentence that is false on
    #    this route, printed beside a payment id an anonymous POST got to
    #    choose, to an owner who acts on it with real money. #188 met the
    #    same problem on the refund path by labelling that card unverified
    #    and rationing it hourly; the uncredited card has no such half.
    #
    # 2. It would answer for the wrong bodies. Every refusal in
    #    :meth:`parse_event` that is decided from the body alone —
    #    unparseable, a reversal, a self-contradicting envelope, nothing
    #    naming success, no payment id, no ``metadata`` shape — describes
    #    no money at all, while every refusal that does leave a payer paid
    #    and empty-handed (no ``user_id`` on the reverified payment, a
    #    non-RUB settlement, an amount below one coin) is decided from the
    #    reverified payment, which the body cannot show. A body predicate
    #    is blind to all three of those and would instead answer True for
    #    the commonest case that is not money: a POST claiming
    #    ``"status": "succeeded"`` that the reverify below threw out.
    #
    # The gap it would have covered is now closed, and by that other
    # mechanism rather than by this predicate (#1643): the six refusals
    # named in reason 2 above return an :class:`UncreditedPayment`, so
    # the router learns the verdict from the reverify instead of
    # guessing it from the body. The distinction reason 1 rests on
    # survives intact — that value is only ever built after
    # ``Payment.find_one`` said "succeeded", so the card it raises is
    # backed by the merchant API and never by an anonymous POST.
    def parse_event(self, body: bytes) -> ParsedEvent | UncreditedPayment | None:
        """Parse the body, then reverify against YooKassa's API.

        Three outcomes, and the split between the last two is the whole
        point of the union (#1643): ``None`` means "this notification
        was not money", :class:`UncreditedPayment` means "this was
        money and we could not credit it". Both used to be ``None``,
        which is why a payer could be left paid and empty-handed with
        the sole trace a WARNING nobody reads.

        Raises :class:`ReverifyUnavailable` when the reverify itself
        could not run to an answer. That is a fourth outcome and not
        one of the three: it says nothing about the payment, and the
        router turns it into a 503 so the notification is redelivered
        rather than acknowledged.

        Returns ``None`` on:
        - Non-JSON body / non-dict payload.
        - A reversal (see :data:`REVERSAL_EVENTS`) — the router turns
          that ``None`` into the owner's alert.
        - Neither the event nor the object status naming a success —
          either alone is enough, since the envelope-less body shape
          has no event to read.
        - A ``payment.*`` event and an object status that contradict
          each other (#300). Both are attacker-supplied on an unsigned
          callback, and no real notification carries a disagreement.
        - Missing payment id.
        - A body carrying no ``metadata.user_id`` / ``metadata.coins``
          (#228) — the cheap shape check that keeps an anonymous POST
          from costing us a merchant-API round-trip. The values are
          discarded; only their presence is used.
        - Reverify miss (``Payment.find_one`` returns
          status != "succeeded"). This is the spoofed-webhook
          defense.
        Every one of those is decided from the body alone, and the
        body is unsigned — none of them describes money we know moved.

        Returns an :class:`UncreditedPayment` on the six refusals that
        are decided from the *reverified* payment: unreadable remote
        metadata, no ``metadata.user_id`` on it, a settlement that is
        not in RUB, a missing/unparsable amount, a non-finite or
        non-positive one, and an amount that converts to less than one
        coin. In each the merchant account has the money and the payer
        has nothing, so the router raises the owner's card.

        The body is treated as a *notification*, not as evidence:
        since T-020 R11-b the id is the only thing read from it, and
        the recipient, currency and amount all come off the object
        ``Payment.find_one`` returns.

        The reverify import is lazy (``yookassa`` is a third-party
        dependency we don't want to pull in at import time — only
        when an actual YooKassa webhook arrives).
        """
        try:
            data_any: Any = json.loads(body) if body else {}
        except Exception:
            log.warning("yookassa: invalid JSON body")
            return None
        if not isinstance(data_any, dict):
            return None
        data: dict[str, Any] = data_any
        event = (data.get("event") or data.get("type") or "").lower()
        obj_any = data.get("object") or data
        if not isinstance(obj_any, dict):
            return None
        obj: dict[str, Any] = obj_any
        # A refund notification carries a *refund* object, and a
        # completed refund is itself ``status: "succeeded"`` — so the
        # status arm below would read money leaving the account as a
        # payment arriving. The reverify would then be asked for a
        # payment under a refund id and (today) fail, which is luck
        # rather than a design: the exclusion is explicit so the
        # outcome does not depend on YooKassa keeping two id
        # namespaces apart. The same trap the RollyPay adapter
        # documents at ``REVERSAL_EVENTS``.
        if event in REVERSAL_EVENTS:
            return None
        # The event name and the object status are alternatives here,
        # not a preference order: a real notification carries both, but
        # the ``or data`` fallback above also accepts a bare payment
        # object, which has a status and no event at all. Either one
        # alone therefore has to be enough.
        #
        # #300 refuses the one shape that is neither: a body carrying
        # both and contradicting itself — a ``payment.succeeded``
        # envelope wrapped around an object whose status is ``canceled``,
        # or a ``payment.canceled`` envelope around a succeeded one. No
        # YooKassa notification looks like that, and both halves are
        # attacker-supplied, because YooKassa does not sign its callbacks
        # (:meth:`verify_signature` above only reports whether the
        # merchant credentials are configured at all).
        #
        # Only ``payment.*`` events are compared. ``event`` falls back to
        # ``data["type"]``, which on a real body reads ``"notification"``
        # — a value that says nothing about the outcome and must not be
        # read as disagreeing with a succeeded status.
        #
        # This is hardening, not a fix. Nothing reachable through the
        # loose arm can mis-credit: the reverify below re-reads the
        # status off YooKassa's own API and the recipient off the
        # reverified payment, so a lying body buys at most one outbound
        # round-trip — which is exactly what the #228 shape check
        # underneath is there to ration.
        status = str(obj.get("status") or "")
        event_says_paid = event == "payment.succeeded"
        status_says_paid = status == "succeeded"
        if event.startswith("payment.") and status and event_says_paid != status_says_paid:
            log.warning(
                "yookassa: body for {pid} contradicts itself — "
                "event={ev!r} but status={st!r}; refusing to act on it",
                pid=str(obj.get("id") or "?"),
                ev=event,
                st=status,
            )
            return None
        if not event_says_paid and not status_says_paid:
            return None
        # The ONLY thing taken from the body is the payment id, and it
        # is a lookup key, not a fact — everything the credit depends
        # on is re-read off the reverified payment below (T-020 R11-b).
        payment_id = str(obj.get("id") or "")
        if not payment_id:
            return None
        # #228, legacy parity (webhook_server.py:163): the body must
        # look like one of *our* checkouts before we spend an outbound
        # call on it. Every YooKassa payment this bot can receive a
        # notification for was created with
        # ``metadata={"user_id": ..., "coins": ...}`` — bot.py:18435
        # and bot.py:18580 are the only two creation sites and the port
        # has no YooKassa checkout of its own — so a body missing
        # either field cannot be a genuine notification.
        #
        # This does not walk back T-020 R11-b: both values are read
        # here as a *shape check* and then thrown away. The credit
        # still derives the recipient, currency and amount from the
        # reverified payment below, and the replay test at
        # ``test_yookassa_replayed_body_cannot_inflate_the_credit``
        # keeps that honest by posting a body that disagrees.
        #
        # What it buys: an anonymous POST can no longer make the
        # process issue ``Payment.find_one`` — a *blocking* HTTPS
        # round-trip to the merchant API. The caller does hand it to a
        # worker thread (``webhook/payments.py:801-804`` wraps
        # ``parse_event`` in ``asyncio.to_thread`` under an 8s
        # ``wait_for``), so the event loop itself is not blocked; what an
        # unauthenticated caller would otherwise get to spend is an
        # outbound merchant-API round trip and a thread-pool slot per
        # request. Legacy refused those bodies before reverifying for the
        # same reason; the port dropped the precondition silently.
        body_meta = obj.get("metadata")
        if not isinstance(body_meta, Mapping):
            return None
        try:
            claimed_user = int(body_meta.get("user_id") or 0)
            claimed_coins = int(body_meta.get("coins") or 0)
        except (TypeError, ValueError):
            log.warning(
                "yookassa: unparsable metadata in body for {pid} — not reverifying",
                pid=payment_id,
            )
            return None
        if claimed_user <= 0 or claimed_coins <= 0:
            log.warning(
                "yookassa: body for {pid} claims no user_id/coins — "
                "not one of our checkouts, not reverifying",
                pid=payment_id,
            )
            return None

        # Reverify with YooKassa's own API. This is the
        # authenticate-the-event step that the unsigned webhook
        # otherwise lacks.
        try:
            from yookassa import Configuration, Payment

            Configuration.account_id = self._shop_id
            Configuration.secret_key = self._secret
            payment = Payment.find_one(payment_id)
        except Exception as exc:
            # NOT "could not authenticate", which is what this used to
            # say — that is the ``status != "succeeded"`` arm below,
            # where the merchant API answered and disagreed. Here it
            # did not answer, so there is nothing to authenticate
            # against and no verdict to acknowledge. Legacy folded the
            # two together and answered 200 to both; the parity break
            # is deliberate and its reasoning lives on
            # :class:`ReverifyUnavailable`.
            #
            # The type, not just the message: ``str()`` on several of
            # the exceptions that land here (``TimeoutError`` among
            # them) is empty, and a log line reading "reverify failed
            # for PAY-1: " names neither the payment's fate nor the
            # cause.
            detail = str(exc).strip()
            log.warning(
                "yookassa: reverify could not complete for {pid} ({kind}{detail}) — "
                "asking for redelivery, no credit",
                pid=payment_id,
                kind=type(exc).__name__,
                detail=f": {detail}" if detail else "",
            )
            raise ReverifyUnavailable(detail or type(exc).__name__) from exc
        if not payment or getattr(payment, "status", None) != "succeeded":
            log.warning(
                "yookassa: reverify status mismatch for {pid}: {status!r}",
                pid=payment_id,
                status=getattr(payment, "status", None),
            )
            return None

        # T-020 R11-b: read the recipient off the reverified payment.
        # ``Payment.find_one`` only authenticates that *some* payment
        # with this id succeeded; taking user_id from the body would
        # let anyone who knows a real payment id redirect its credit.
        remote_meta = self._remote_field(payment, "metadata")
        try:
            user_id = int(self._remote_field(remote_meta, "user_id") or 0)
            # Metadata coins value is parsed for the post-credit
            # log-on-mismatch check below; it is NOT used as the
            # credit amount (R-FIX-003 — defense against user-
            # influenced metadata).
            metadata_coins = int(self._remote_field(remote_meta, "coins") or 0)
        except (TypeError, ValueError):
            log.warning(
                "yookassa: invalid reverified metadata for {pid}: {meta!r}",
                pid=payment_id,
                meta=remote_meta,
            )
            return UncreditedPayment(
                provider=Provider.YOOKASSA,
                external_id=payment_id,
                cause=UncreditedCause.BAD_METADATA,
                amount=self._remote_amount_text(payment),
            )
        if user_id <= 0:
            log.warning(
                "yookassa: reverified payment {pid} carries no user_id — refusing to credit",
                pid=payment_id,
            )
            # ``payer`` deliberately left empty: on this branch the
            # missing id *is* the finding, and the card prints "?".
            return UncreditedPayment(
                provider=Provider.YOOKASSA,
                external_id=payment_id,
                cause=UncreditedCause.NO_USER_ID,
                amount=self._remote_amount_text(payment),
            )

        # R-FIX-003: derive coins from the *reverified* amount (RUB),
        # not from the user-influenced metadata. A mismatch indicates
        # a tampered checkout, or — since T-020 R11 — simply that the
        # rouble fix moved between the moment the payment was created
        # and the moment it succeeded. Either way the server-derived
        # value wins and the mismatch is logged.
        #
        # R11-b: the amount comes off ``payment``, never off ``obj``.
        # The body reached us unsigned, so an attacker replaying a real
        # succeeded id with ``"amount": {"value": "1000000.00"}`` used
        # to mint ten million coins for a hundred-rouble payment.
        amount_obj = self._remote_field(payment, "amount")
        rub_amount_str = str(self._remote_field(amount_obj, "value") or "")
        currency = str(self._remote_field(amount_obj, "currency") or "RUB").upper()
        if currency != "RUB":
            # ``coins_for_rub`` would price e.g. 100 USD as 100 RUB.
            log.warning(
                "yookassa: payment {pid} settled in {cur}, not RUB — refusing to credit",
                pid=payment_id,
                cur=currency,
            )
            return UncreditedPayment(
                provider=Provider.YOOKASSA,
                external_id=payment_id,
                cause=UncreditedCause.NOT_RUB,
                amount=self._remote_amount_text(payment),
                payer=str(user_id),
            )
        # R-FIX-003-fp: parse the RUB amount as Decimal — YooKassa
        # emits "99.99" / "100.00" strings and ``float(...)`` rounds
        # half-to-even at the IEEE-754 boundary. ``Decimal("99.99")
        # * Decimal(900) / Decimal("90.0") == Decimal("999.9")``
        # (truncates cleanly to 999); the float path gives
        # 999.9000000000001 (also 999 by luck, but the closeness is
        # environment-dependent and a regression on any
        # small-magnitude amount in the future would silently lose a
        # coin). ``coins_for_rub`` stays in Decimal throughout.
        try:
            rub_amount = Decimal(rub_amount_str)
        except InvalidOperation:
            log.warning(
                "yookassa: missing/invalid amount.value for {pid}: {amt!r}",
                pid=payment_id,
                amt=rub_amount_str,
            )
            return UncreditedPayment(
                provider=Provider.YOOKASSA,
                external_id=payment_id,
                cause=UncreditedCause.BAD_AMOUNT,
                amount=self._remote_amount_text(payment),
                payer=str(user_id),
            )
        # ``is_priceable`` first, and not merged into the ``<= 0`` test:
        # ``Decimal("NaN")`` parses without raising and then *raises*
        # ``InvalidOperation`` on comparison, while ``Decimal("Infinity")``
        # compares fine and blows up later at ``int()``. Since #1698 the
        # same predicate also bounds the magnitude — this amount comes
        # off the reverified payment object rather than off the body, so
        # it is the provider that would have to be wrong, but the
        # refusal costs one call and the alternative is an exception
        # inside the credit transaction.
        if not is_priceable(rub_amount) or rub_amount <= 0:
            log.warning(
                "yookassa: unusable amount.value for {pid}: {amt!r}",
                pid=payment_id,
                amt=str(rub_amount),
            )
            return UncreditedPayment(
                provider=Provider.YOOKASSA,
                external_id=payment_id,
                cause=UncreditedCause.BAD_AMOUNT,
                amount=self._remote_amount_text(payment),
                payer=str(user_id),
            )
        coins = coins_for_rub(rub_amount, self._usd_to_rub)
        if coins <= 0:
            # This refusal used to log nothing at all — the one
            # money-losing branch in the method that was silent (#1643).
            # The card it now raises ends with "точная причина — в
            # журнале", so a silent branch would have sent the owner to
            # an empty journal.
            log.warning(
                "yookassa: {amt}RUB at {rate} converts to {coins} coins for {pid} — "
                "refusing to credit",
                amt=rub_amount,
                rate=self._usd_to_rub,
                coins=coins,
                pid=payment_id,
            )
            return UncreditedPayment(
                provider=Provider.YOOKASSA,
                external_id=payment_id,
                cause=UncreditedCause.BELOW_ONE_COIN,
                amount=self._remote_amount_text(payment),
                payer=str(user_id),
            )
        if metadata_coins and metadata_coins != coins:
            log.warning(
                "yookassa: metadata.coins={meta} != server-derived={srv} "
                "for pid={pid} amount={amt}RUB — crediting server value",
                meta=metadata_coins,
                srv=coins,
                pid=payment_id,
                amt=rub_amount,
            )

        return ParsedEvent(
            provider=Provider.YOOKASSA,
            external_id=payment_id,
            user_id=user_id,
            coins=coins,
            reason="Покупка (ЮKassa)",
            # #239: same audit trail as the RollyPay adapter. YooKassa
            # is disabled on prod today, but the field is populated
            # here and not left for whoever enables it — the rate is
            # only knowable at conversion time.
            fiat_amount=rub_amount,
            fiat_currency="RUB",
            fx_rate=Decimal(str(self._usd_to_rub)),
        )
