"""``/check`` claim-side subscription-gate CallbackData factory (#26).

A single wire format — the "✅ Я подписался" verify button shown when a
claimer hits a check whose ``required_subscription`` flag is set but they
are not (yet) a member of the configured subscription channel. Rendered
by ``handlers/checks`` and consumed there.

Prefix selection
----------------
``check_sub`` — distinct from every other prefix in this codebase and
from legacy's literal ``check_*`` start-payload deep links (those ride
``/start``, not a callback_query). The claim/create flow uses no callback
prefixes of its own, so there is no collision with the existing ``/check``
handlers.

Field budget
------------
Telegram caps callback_data at 64 bytes. ``check_sub:<code>`` with a
short alphanumeric code (the service generates 8-char codes) is ~18
bytes — comfortable. The ``code`` is re-normalised (strip + upper) and
re-validated by ``CheckService.claim_check`` when the handler runs the
claim, so a hand-crafted payload just routes through the same gate set
as a fresh ``/check <code>`` — no privileged path.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class CheckCreateType(CallbackData, prefix="chk_new_type"):
    """Type-picker button on the ``/check_create`` FSM intro card (L-30).

    ``ctype`` is one of ``random`` / ``fixed`` / ``individual`` and
    selects which amount-entry branch the flow advances into. Carries
    ``owner_id`` as an auth tag so a foreign click on someone else's
    open card is rejected without mutating their FSM (private chat makes
    this near-impossible, but the guard mirrors the /withdraw posture).
    """

    owner_id: DbInt
    ctype: str


class CheckCreateConfirm(CallbackData, prefix="chk_new_ok"):
    """✅ Confirm button on the ``/check_create`` summary card (L-30).

    Carries only ``owner_id`` (auth tag). Every amount/count/gate value
    is read from FSM data on click — never trusted off the callback wire
    (a money path must not trust a client value), exactly like
    :class:`telegram_invite_bot.keyboards.builders.WithdrawConfirm`.
    """

    owner_id: DbInt


class CheckCreateCancel(CallbackData, prefix="chk_new_no"):
    """❌ Cancel button on the ``/check_create`` summary card (L-30).

    Nothing is debited until confirm, so cancel just clears the FSM.
    ``owner_id`` is the auth tag.
    """

    owner_id: DbInt


class CheckSubVerify(CallbackData, prefix="check_sub"):
    """ "✅ Я подписался" button on the subscription-gate prompt.

    Carries the check ``code`` so the verify handler can re-check
    membership and, if the claimer is now subscribed, run the exact same
    claim path as ``/check <code>``. No authorization rides on the
    payload: the claim service re-runs every gate (expiry, max-claims,
    already-claimed, language, premium) so a forged code can't skip them.
    """

    code: str
