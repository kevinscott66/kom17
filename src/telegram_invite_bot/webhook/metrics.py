"""Prometheus counters for the webhook pipeline.

One ``updates_total`` counter labeled by routing outcome:

* ``new``             — dispatched into the aiogram pipeline
* ``duplicate``       — the ``update_id`` was already claimed by an
  in-flight or recently-finished delivery; acked as 200 *without*
  dispatching, because a second run of the same update could bill,
  credit or refund twice. The claim is released again only on the
  ``dispatch_error`` path, so a Telegram retry after a genuine failure
  lands as ``new``, not here.
* ``forbidden``       — the ``X-Telegram-Bot-Api-Secret-Token`` header
  did not match; answered 403 without reading the body. Counted
  separately from ``error`` because the two want different alerts: a
  sustained rate here is either someone who learned the webhook URL
  and is forging updates, or a token rotation that has left Telegram
  403-ing every real update — a failure whose only other symptom is
  "the bot went quiet".
* ``error``           — HTTP-layer rejection (oversize body, bad
  Content-Length, invalid JSON, non-object payload); always answered
  with the appropriate 4xx so the caller knows the request itself was
  wrong.
* ``parse_error``     — aiogram ``Update.model_validate`` failed
  (unknown update type / schema drift); acked as 200 because Telegram
  retrying the same poison payload would loop forever (R-FIX-005,
  at-most-once for permanent failures).
* ``dispatch_error``  — exception escaped ``dispatcher.feed_update``
  after the per-handler error router had its chance; answered with
  500 so Telegram retries with exponential backoff (R-FIX-005,
  at-least-once for transient failures).

The ``legacy`` outcome label is retired with T-011 — the strangler
bridge was removed once every legacy command had a native port.

Counters are process-global (``prometheus_client`` keeps them in a module
registry). Tests don't reset them; assertions read the current value and
diff against a snapshot taken before the act.
"""

from __future__ import annotations

from prometheus_client import Counter

UPDATES_TOTAL = Counter(
    "tib_updates_total",
    "Webhook updates by routing outcome.",
    ["outcome"],
)

# Handler-level exceptions. Distinct from ``UPDATES_TOTAL{outcome=error}``
# (which counts pre-routing errors at the HTTP layer): this counts
# exceptions raised AFTER aiogram matched a handler — i.e. real bugs
# in business logic, transient DB errors, external-API timeouts. The
# ``exc_type`` label scopes alerts to a class of failure (``TimeoutError``
# vs ``OperationalError`` vs ``ValueError``) rather than firing on the
# union of every error in every handler.
HANDLER_ERRORS = Counter(
    "tib_handler_errors_total",
    "Exceptions raised inside an aiogram handler.",
    ["exc_type"],
)

# Updates dropped by the per-user token-bucket throttling middleware,
# scoped by ``event_type`` (``message`` / ``callback_query``). Sustained
# nonzero rate against a real user_id means the bucket is too tight;
# a constant zero means the bucket is wider than reality and isn't
# protecting anything — both signals are actionable.
THROTTLED_TOTAL = Counter(
    "tib_throttled_total",
    "Updates dropped by the throttling middleware.",
    ["event_type"],
)

# M-E-5: per-provider counter for failed post-credit DMs. The credit
# itself has already committed to ``economy.db`` by the time the DM
# fires — a DM failure (user blocked the bot, network blip,
# users.db connection error) must not roll back the wallet write, but
# it MUST be observable so an operator can tell "credit landed, user
# didn't get the receipt" apart from "credit silently never happened".
# The ``provider`` label scopes alerts to the four webhook providers
# — Crypto Pay, YooKassa, Stripe, RollyPay — so a flaky single
# integration doesn't drown out the others. ``stars`` never appears
# here: the Stars leg confirms in place on the buyer's own message
# (handlers/topup.py:842-846) and logs ``stars receipt undeliverable``
# rather than bumping a counter, because there is no DM to fail.
PAYMENT_DM_FAILURES = Counter(
    "tib_payment_dm_failures_total",
    "Post-credit confirmation-DM failures, scoped by payment provider.",
    ["provider"],
)

# Credit-side failures — the money DIDN'T land. Distinct from
# ``PAYMENT_DM_FAILURES`` (credit landed, receipt didn't): a nonzero
# rate here means a verified provider event was accepted but the
# wallet was never credited. The ``reason`` label discriminates the
# four emitted causes, and they do NOT all mean the same thing:
#
#   ``invalid_amount`` — our coin-amount validator rejected the event,
#       i.e. a config or pack-table bug on our side. Acked 200.
#   ``credit_refused`` — the economy layer would not take the write.
#       Acked 200. Since #770 the only surviving cause is the balance
#       ceiling (economy_repo.py:305); the payer having no wallet row
#       is not one, because the credit seeds it.
#   ``pipeline_crash`` — an exception escaped the credit transaction
#       and the route 200-acked anyway, to stop the provider retrying
#       a poison event.
#   ``db_unavailable`` — economy.db was unreachable; emitted by
#       ``_pipeline_failure_is_retryable`` in ``webhook/payments.py``.
#       This one answers 503 deliberately, so the provider retries and
#       the payment usually recovers without hand-crediting. A spike
#       here is an outage signal, not a list of debts.
#
# The three 200 arms are invisible outside the log without this
# counter. ``user_not_found`` was renamed to ``credit_refused`` in
# #770 and is no longer emitted by anything — a dashboard still
# querying that label reads a flat zero forever. The ``provider``
# label spans Crypto Pay, YooKassa, Stripe, RollyPay and ``stars``:
# handlers/topup.py:707 bumps this same metric with the same
# ``reason`` vocabulary on purpose, so one query covers every way a
# paid top-up can fail to land.
PAYMENT_CREDIT_FAILURES = Counter(
    "tib_payment_credit_failures_total",
    "Verified payment events that were acked but never credited.",
    ["provider", "reason"],
)

# Money going back OUT after a successful top-up: a chargeback, or a
# refund issued from the provider's dashboard. Card and SBP payments can
# be pulled back long after the coins have been spent, which crypto and
# Stars cannot — so this is the counter that says "the owner just lost
# real money against coins that are already in circulation".
#
# Deliberately NOT wired to an automatic debit. By the time a chargeback
# lands the wallet may be empty, and choosing between a negative
# balance, a partial claw-back and a manual review is a policy decision
# the owner has to make. The counter plus the admin alert exist so the
# decision is at least *prompted*, rather than discovered in a month-end
# reconciliation.
PAYMENT_REVERSALS = Counter(
    "tib_payment_reversals_total",
    "Chargebacks and refunds reported by a payment provider.",
    ["provider", "event"],
)

# #144: the payer's money cleared and the wallet still did not move.
# Distinct from ``PAYMENT_CREDIT_FAILURES``, which counts events the
# credit pipeline *accepted* and then failed to finish: this one counts
# events the parser refused before the pipeline ever saw them — a
# non-RUB settlement (a crypto leg reported in its own currency), a
# callback with no ``metadata.user_id``, an amount below one coin. All
# three answer 200 and are correct refusals in the sense that crediting
# a number we do not trust would be worse; what is NOT correct is
# leaving the payer paid and empty-handed with only a log line to say
# so. Nonzero here means someone is owed coins by hand.
PAYMENT_UNCREDITED = Counter(
    "tib_payment_uncredited_total",
    "Settled payments the parser refused to credit.",
    ["provider"],
)


# M-I-7: per-probe failure counter. In practice exactly one label value
# is ever emitted — ``probe="readyz"`` (webhook/server.py). ``/healthz``
# cannot bump it: it is a pure liveness probe with no failure path, it
# answers 200 whenever the event loop answers at all. The label is kept
# anyway so a future probe can be told apart from readiness without a
# metric rename; an alert on this counter is therefore an alert on
# readiness, and a dashboard that expects a ``healthz`` series will show
# an empty panel rather than a zero.
HEALTH_CHECK_FAILURES = Counter(
    "tib_health_check_failures_total",
    "Health-probe failures by probe name.",
    ["probe"],
)


# #159: inline taps that reached the end of the router tree. Every one
# is a button rendered by a card the bot itself sent, so a nonzero rate
# means a keyboard outlived the handler behind it — a prefix retired in
# a deploy, an FSM state dropped on restart, a panel from last month.
# Labeled by the prefix the payload claims, and bounded to the set the
# assembled tree actually registers (plus "unknown" / "none") because
# ``callback_data`` arrives from outside and a verbatim label would be
# an unbounded series. See ``handlers.stale_callback.prefix_label``.
STALE_CALLBACKS = Counter(
    "tib_stale_callbacks_total",
    "Callback queries that matched no handler, by claimed prefix.",
    ["prefix"],
)


# #1989: checkpoints that committed one database and then failed
# another. The five DBs are five SQLite files with five engines and no
# two-phase commit between them, so a mid-update commit can land on
# ``economy.db`` and be refused on ``users.db`` — money taken for an
# effect that never applied. ``db/session.py`` chooses to have that
# failure in exactly that direction (``_COMMIT_ORDER``: the ledger
# survives, because "charged for nothing" is visible and refundable
# while "got it free" is neither), but choosing which failure to have
# is not the same as noticing it happened. Until now the only trace
# was one ERROR line nothing consumes.
#
# Labeled by the database that was LOST, not the one that survived:
# that is the half an operator has to go and repair by hand.
#
# Only a genuine tear counts. A checkpoint where nothing committed is
# an ordinary failed commit — the middleware rolls the update back
# whole — and counting those would put every transient "database is
# locked" on the same series and get the alert muted.
#
# A labeled Counter has no series until the first ``.labels(...)``
# call, so ``tib_checkpoint_tears_total`` is ABSENT from ``/metrics``
# on a healthy process rather than present at zero — the same shape
# ``HEALTH_CHECK_FAILURES`` documents above. An alert on it must be
# written to tolerate absence (``increase(...) > 0``); a rule that
# compares the series to zero will never fire.
#
# Layering note: this module is the process-wide Prometheus registry
# despite living under ``webhook/``; ``handlers/*`` already import it
# for the same reason. It pulls in nothing but ``prometheus_client``,
# so the import from ``db/session.py`` is acyclic.
CHECKPOINT_TEARS = Counter(
    "tib_checkpoint_tears_total",
    "Checkpoints that committed one database and failed another.",
    ["failed"],
)
