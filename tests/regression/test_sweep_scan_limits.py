"""#1617/#1619: every background scan that fans out must stay bounded.

Three sweeps used to read their whole candidate set: the VIP
expiry-notice scan (``VipRepo.list_expiring_global``) and the two money
scans behind the 60-second tick (``PvpRepo.list_pending_older_than``,
``P2pRepo.list_pending_older_than``). Each now takes a cap.

What this file guards is the SHAPE of that fix rather than its numbers,
because the numbers are pinned by behavioural tests elsewhere
(``tests/integration/scheduler/test_economy_cleanup_expiry.py`` and the
two service suites). Specifically:

* ``limit`` is keyword-only and has NO default on all three repo
  methods — a default is exactly how an unbounded scan comes back, one
  forgotten call site at a time;
* both money services carry an ``_EXPIRY_SCAN_LIMIT`` and pass it;
* the sweeper passes its ``vip_notice_budget`` to the VIP scan instead
  of a literal, so the ctor kwarg is not decorative.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from telegram_invite_bot.repositories.p2p_repo import P2pRepo
from telegram_invite_bot.repositories.pvp_repo import PvpRepo
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.scheduler import economy_cleanup as ec_mod
from telegram_invite_bot.services import p2p_service, pvp_service


@pytest.mark.parametrize(
    "method",
    [
        VipRepo.list_expiring_global,
        PvpRepo.list_pending_older_than,
        P2pRepo.list_pending_older_than,
    ],
    ids=["vip", "pvp", "p2p"],
)
def test_scan_limit_is_mandatory_and_keyword_only(method: object) -> None:
    param = inspect.signature(method).parameters["limit"]  # type: ignore[arg-type]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    ("module", "call"),
    [
        (pvp_service, "list_pending_older_than(cutoff, limit=_EXPIRY_SCAN_LIMIT)"),
        (p2p_service, "limit=_EXPIRY_SCAN_LIMIT, order_id=order_id"),
    ],
    ids=["pvp", "p2p"],
)
def test_money_sweeps_pass_their_cap(module: object, call: str) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "_EXPIRY_SCAN_LIMIT = " in source
    assert call in source


def test_vip_notice_budget_reaches_the_query() -> None:
    source = Path(ec_mod.__file__).read_text(encoding="utf-8")
    assert "limit=self._vip_notice_budget," in source
    param = inspect.signature(ec_mod.EconomyCleanupSweeper).parameters["vip_notice_budget"]
    assert param.default == ec_mod._VIP_NOTICE_BUDGET_PER_PASS
