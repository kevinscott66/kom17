"""M-I-4: HANDLER_ERRORS{exc_type} label cardinality is bounded.

A flaky upstream that raises dozens of distinct exception class names
would otherwise grow the Prometheus label set past the recommended
ceiling (~10⁴ series). The mapping in ``_bounded_exc_label`` collapses
unknown class names to ``"Other"`` so the series count is fixed by
the allowlist size.
"""

from __future__ import annotations

from telegram_invite_bot.handlers.errors import (
    _ALLOWED_EXC_TYPE_LABELS,
    _bounded_exc_label,
)


class _UnusualExc(Exception):
    """Synthetic exception with a class name that is NOT in the allowlist."""


def test_unknown_exception_maps_to_other() -> None:
    """Synthesise an exception whose class name isn't in the allowlist;
    the label must collapse to ``"Other"``.
    """
    exc = _UnusualExc("boom")
    assert _bounded_exc_label(exc) == "Other"


def test_known_exception_passes_through() -> None:
    """Allowlisted exception class names keep their original name."""
    assert _bounded_exc_label(ValueError("x")) == "ValueError"
    assert _bounded_exc_label(KeyError("x")) == "KeyError"
    assert _bounded_exc_label(TimeoutError()) == "TimeoutError"
    # #1692: kept distinct from "Other" on purpose — see the set.
    assert _bounded_exc_label(OverflowError()) == "OverflowError"


def test_allowlist_contains_other_sentinel() -> None:
    """``"Other"`` is the collapse target — it must be a member of the
    allowed-labels set so the metric's series count is well-defined
    even when every raised exception is unknown.
    """
    assert "Other" in _ALLOWED_EXC_TYPE_LABELS


def test_allowlist_size_is_bounded() -> None:
    """Sanity: keep the allowlist small. If somebody adds 100 entries,
    the bound is gone — fail loudly so the addition is reviewed.
    """
    assert len(_ALLOWED_EXC_TYPE_LABELS) <= 32
