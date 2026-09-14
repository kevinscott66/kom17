"""Regression guard: the payout DM may only promise a destination we hold.

#1674. The ported ``/withdraw`` flow has exactly two FSM steps — amount,
then confirm (``WithdrawStates``) — and ``WithdrawalsRepo.create`` takes
no ``payment_details`` argument, so nothing in the live path ever writes
that column. It stays NULL, and the operator's card renders it as a dash.

The copy did not agree with that. ``h_withdraw_submitted`` told the user
the payout arrives "in Crypto Bot", while the approval DM
``h_withdraw_dm_completed_manual`` promised money sent "по указанным
реквизитам" / "to your payment details" — details the bot never asked
for and could not have. One of the two messages was always going to be
wrong, and the wrong one is the last thing the user reads before they
start waiting.

The destination that actually exists is the user's Telegram account:
``approve_manual`` is a manual CryptoPay transfer by the operator, and
the admin card carries the ``uid`` precisely so they can make it. So
both messages now name that one destination.

Both halves are pinned here, because the fix is only stable while the
premise holds. The day someone adds a destination step to the FSM,
``create`` grows the argument, this file fails, and whoever did it is
told to revisit the copy rather than leaving a second contradiction
behind.
"""

from __future__ import annotations

import inspect

from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.withdrawals_repo import WithdrawalsRepo

_KEY = "h_withdraw_dm_completed_manual"


def test_create_still_collects_no_payout_destination() -> None:
    """The premise behind the copy: no destination is ever stored."""
    params = inspect.signature(WithdrawalsRepo.create).parameters

    assert "payment_details" not in params, (
        "the withdraw port now stores a payout destination — "
        f"{_KEY} may (and should) describe it again"
    )


def test_approval_dm_names_the_destination_the_submit_message_promised() -> None:
    """Submit and approval must not name two different destinations."""
    for lang in ("ru", "en"):
        submitted = t("h_withdraw_submitted", lang, request_id=1, amount=1, crypto=1, asset="USDT")
        approved = t(_KEY, lang, request_id=1)

        # ``t`` returns the key itself when the key is missing, so every
        # assertion below would pass against an empty catalog.
        assert _KEY not in approved
        assert "Crypto Bot" in submitted
        assert "Crypto Bot" in approved, f"{lang}: approval DM names a different destination"


def test_approval_dm_promises_no_details_the_bot_never_asked_for() -> None:
    """The phrasing that outlived the collection step it referred to."""
    assert "реквизит" not in t(_KEY, "ru").lower()
    assert "payment details" not in t(_KEY, "en").lower()
