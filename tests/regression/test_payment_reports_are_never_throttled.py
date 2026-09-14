"""#1996: every payment REPORT the package listens for is exempt from
the throttle.

The throttle stands in front of the whole ``message`` observer as an
OUTER middleware (``di/providers.py``), so anything it drops never
reaches a handler at all. For a request that is a nuisance; for a
report it is a silent loss, because Telegram delivers the update once
and nothing redelivers it, and because everything the bot does about
the event — the credit, the reversal stamp, the owner alert — happens
inside the handler that was skipped.

#1814 saw this for ``successful_payment`` and exempted it. #1996 found
that ``refunded_payment``, registered eleven lines below it in
``handlers/topup.py``, had been left out although the #1814 comment's
own justification covered it word for word — and that the refund half
matters more, since ``processed_webhooks.reversed_at`` is the only
thing that stops refunded money counting toward the withdrawal gate.

So the rule is not "these two attributes". The rule is: **if the
package registers a message handler for a completed-money-movement
report, the throttle must exempt that report.** Which is checkable.

The discriminator is aiogram's own vocabulary rather than a list
maintained here: a ``Message`` field whose annotation names a type
containing ``Payment`` or ``Refunded`` is a report of money that has
already moved (``SuccessfulPayment``, ``RefundedPayment``,
``SuggestedPostRefunded``). ``Invoice`` is deliberately not in that
set — an invoice message is a request the bot itself sent, not a
report of a completed charge, and throttling one costs nobody money.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

from aiogram.types import Message

from telegram_invite_bot.middlewares.throttling import _PAYMENT_REPORTS

SRC = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"


def _completion_report_fields() -> frozenset[str]:
    """``Message`` fields that report money having already moved."""
    names: set[str] = set()
    for field, info in Message.model_fields.items():
        for arg in get_args(info.annotation) or (info.annotation,):
            name = getattr(arg, "__name__", "")
            if "Payment" in name or "Refunded" in name:
                names.add(field)
    return frozenset(names)


def _registered_message_filters() -> dict[str, list[str]]:
    """``F.<attr>`` filters passed to ``router.message.register(...)``.

    Returns attribute name → the call sites that register it, so a
    failure can name the line to look at rather than only the field.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # ``<something>.message.register``
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "register"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "message"
            ):
                continue
            for arg in node.args:
                # ``F.successful_payment`` — a MagicFilter attribute
                # access, which parses as Attribute(value=Name("F")).
                if (
                    isinstance(arg, ast.Attribute)
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "F"
                ):
                    site = f"{path.relative_to(SRC)}:{node.lineno}"
                    found.setdefault(arg.attr, []).append(site)
    return found


def test_the_discriminator_still_finds_the_reports_we_know_about() -> None:
    """Guard the guard.

    Everything below is vacuously true if aiogram renames its payment
    types or drops the fields, so pin the two the bot actually handles
    plus the exclusion that makes the rule non-trivial.
    """
    reports = _completion_report_fields()
    assert "successful_payment" in reports
    assert "refunded_payment" in reports
    assert "invoice" not in reports, (
        "an invoice is a request the bot sent, not a report of money that"
        " already moved — including it would force a pointless exemption"
    )


def test_every_registered_payment_report_is_exempt_from_the_throttle() -> None:
    """The finding itself, as a standing check."""
    reports = _completion_report_fields()
    registered = _registered_message_filters()
    unguarded = {
        attr: sites
        for attr, sites in registered.items()
        if attr in reports and attr not in _PAYMENT_REPORTS
    }
    assert not unguarded, (
        "a completed-payment report is handled but not exempt from the"
        " throttle — the outer middleware can drop it and Telegram will"
        " not send it again. Add the attribute to ``_PAYMENT_REPORTS``"
        " in middlewares/throttling.py:\n"
        + "\n".join(f"  {attr} registered at {sites}" for attr, sites in unguarded.items())
    )


def test_the_exemption_list_names_nothing_imaginary() -> None:
    """The other direction: an exemption for an attribute no real
    ``Message`` carries would be dead code that reads as protection.
    """
    stray = [attr for attr in _PAYMENT_REPORTS if attr not in Message.model_fields]
    assert not stray, f"_PAYMENT_REPORTS names non-Message attributes: {stray}"
