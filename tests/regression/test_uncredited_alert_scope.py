"""#1611/#1643: which routes raise a paid-but-uncredited alert, and on what proof.

Three adapters expose ``describes_paid_money`` and their routes turn it
into ``_alert_uncredited``. The YooKassa adapter deliberately does not,
because its callbacks are unsigned: the predicate would have to answer
from a body nobody proved, feeding a card whose first sentence tells the
owner the signature was good.

That asymmetry is still here and these tests still pin it. What #1643
changed is the money gap it used to leave open — a reverified YooKassa
payment refused a credit reached nobody but the journal. It is closed
now by the other mechanism the #1611 comment named: the parser returns
an :class:`UncreditedPayment` built entirely from the object
``Payment.find_one`` returned, so the route alerts off the merchant
API's word and never off the body. The predicate stays absent.

The line these tests defend is therefore precise, and it is not "the
fourth route must stay silent" — it is "the fourth route must never
raise a card from something a stranger typed".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_invite_bot.services.payments import (
    CryptoAdapter,
    RollyPayAdapter,
    StripeAdapter,
    YooKassaAdapter,
)
from telegram_invite_bot.services.payments.base import Provider, UncreditedCause
from telegram_invite_bot.webhook import payments as router_mod

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
ROUTER_SRC = (SRC_ROOT / "webhook" / "payments.py").read_text(encoding="utf-8")
ADAPTER_SRC = (SRC_ROOT / "services" / "payments" / "yookassa.py").read_text(encoding="utf-8")

SIGNED = frozenset({Provider.CRYPTO, Provider.ROLLYPAY, Provider.STRIPE})


@pytest.mark.parametrize(
    "adapter",
    [CryptoAdapter, RollyPayAdapter, StripeAdapter],
    ids=["crypto", "rollypay", "stripe"],
)
def test_signed_routes_carry_the_paid_money_predicate(adapter: type) -> None:
    """The three providers that sign their webhooks all answer the question."""
    assert callable(getattr(adapter, "describes_paid_money", None))


def test_yookassa_carries_no_paid_money_predicate() -> None:
    """Absent on purpose — the body it would read is unauthenticated."""
    assert not hasattr(YooKassaAdapter, "describes_paid_money")


def test_the_cause_shortlist_covers_exactly_the_signed_providers() -> None:
    """The "чаще всего" guesses are for the routes that cannot do better.

    A YooKassa entry here is a regression, not an improvement: that
    route knows which gate fired and prints it, so a shortlist beside
    it would be three guesses next to one fact.
    """
    assert set(router_mod._UNCREDITED_CAUSES) == SIGNED


def test_the_journal_prefix_covers_every_route_that_can_alert() -> None:
    """Asymmetric to the table above, and that is the whole point.

    The alert ends by naming the log line to grep. YooKassa can raise
    the card now (#1643), so it must appear here or the owner would be
    sent to a prefix that greps nothing.
    """
    assert set(router_mod._UNCREDITED_JOURNAL) == SIGNED | {Provider.YOOKASSA}


def test_every_cause_the_parser_can_produce_has_words() -> None:
    """A new ``UncreditedCause`` must arrive with its Russian line.

    Missing one is not fatal — the alert falls back to the shortlist —
    but on this route the shortlist is empty, so the owner would get a
    card that says a payment failed and nothing about why.
    """
    assert set(router_mod._UNCREDITED_VERDICT) == set(UncreditedCause)
    for text in router_mod._UNCREDITED_VERDICT.values():
        assert text and text == text.strip()


def test_the_yookassa_card_never_claims_a_signature_was_checked() -> None:
    """The objection that kept this alert off the route for two tickets.

    YooKassa signs nothing. The default sentence — the one the three
    HMAC routes earn — would be a false statement printed beside a real
    payment id, to an owner who acts on it with real money.
    """
    proof = router_mod._UNCREDITED_PROOF[Provider.YOOKASSA]
    assert "Подпись" not in proof
    assert "ЮKassa" in proof
    assert "Подпись верна" in router_mod._UNCREDITED_PROOF_DEFAULT
    for provider in SIGNED:
        assert provider not in router_mod._UNCREDITED_PROOF


def test_the_yookassa_route_alerts_only_off_the_reverified_payment() -> None:
    """Source-pinned: the card is built from the parser's value, not the body.

    ``_alert_uncredited`` may appear in this route exactly once, inside
    the ``UncreditedPayment`` branch. An arm keyed on the body — the
    ``describes_paid_money`` shape the other three routes use — would
    reintroduce the free "make the owner's phone ring" primitive.
    """
    start = ROUTER_SRC.index("    async def yookassa_webhook(request: Request)")
    end = ROUTER_SRC.index("    async def stripe_webhook(request: Request)")
    route = ROUTER_SRC[start:end]
    assert "_alert_reversal(" in route
    assert route.count("_alert_uncredited(") == 1
    assert "isinstance(event, UncreditedPayment)" in route
    assert "rationed=True" in route
    # The name survives in this route exactly once, in the comment that
    # explains why the arm is absent. Any occurrence on a line of code is
    # the regression.
    code = [ln for ln in route.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "describes_paid_money" in ln]


def test_the_two_alert_budgets_are_separate_lists() -> None:
    """One shared ration would let a flood of one kind silence the other.

    They are the owner's only signals for two different ways of losing
    money — a chargeback and a stuck top-up — and both routes that feed
    them are reachable by a stranger.
    """
    assert router_mod._unverified_alert_marks is not router_mod._uncredited_alert_marks
    router_mod.reset_unverified_alert_budget()
    router_mod.reset_uncredited_alert_budget()
    for _ in range(router_mod._UNCREDITED_ALERT_BUDGET):
        assert router_mod._uncredited_alert_allowed() is True
    assert router_mod._uncredited_alert_allowed() is False
    # The refund card is untouched by that exhaustion.
    assert router_mod._unverified_alert_allowed() is True
    router_mod.reset_uncredited_alert_budget()
    assert router_mod._uncredited_alert_allowed() is True


def test_both_sides_say_why_the_predicate_is_missing() -> None:
    """The explanation is load-bearing: without it the gap reads as an oversight.

    Pinned at both ends because they answer different readers — one is
    looking at four adapters, the other at four routes.
    """
    for src in (ADAPTER_SRC, ROUTER_SRC):
        assert "#1611" in src
        assert "#1643" in src
