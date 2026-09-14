"""#1599: production sessionmakers must keep ``expire_on_commit=False``.

Three unrelated places rely on this and say so in prose —
``db/session.py:103-107``, ``services/withdraw_service.py`` in both
``approve_manual`` and ``reject`` — and one place used to rely on the
OPPOSITE, claiming a commit expires the row and a later attribute read
would fault. Only one of those readings can be right.

Nothing in the suite defended the invariant, because every test builds
its own ``async_sessionmaker(engine, expire_on_commit=False)`` by hand.
Flipping the production default would therefore break ``reject`` — it
reads ``row.user_id`` and ``row.amount_com`` AFTER its commit — while
the whole suite stayed green, and the failure in production is a
``MissingGreenlet`` from a lazy refresh under asyncio, not a slow query.

A source pin rather than a runtime one: importing the engine builder
opens real database files, and what needs defending is the single
keyword that call is written with.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
_ENGINES = (SRC_ROOT / "db" / "engines.py").read_text(encoding="utf-8")


def test_every_sessionmaker_disables_expire_on_commit() -> None:
    calls = re.findall(r"async_sessionmaker\((.*?)\)", _ENGINES, re.S)
    constructed = [c for c in calls if "expire_on_commit" in c or "engine" in c]
    assert constructed, "async_sessionmaker is no longer called here — rewrite this pin"
    for call in constructed:
        assert "expire_on_commit=False" in call, call


def test_the_invariant_is_documented_where_it_is_relied_on() -> None:
    """A reader who finds a post-commit attribute read has to be able to
    look the rule up; the citation is the only thing making that read
    reviewable rather than a bug that happens to work."""
    session_doc = (SRC_ROOT / "db" / "session.py").read_text(encoding="utf-8")
    assert "expire_on_commit=False" in session_doc
    withdraw = (SRC_ROOT / "services" / "withdraw_service.py").read_text(encoding="utf-8")
    assert withdraw.count("expire_on_commit=False") >= 2
