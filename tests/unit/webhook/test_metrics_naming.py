"""Every webhook counter must carry the ``tib_`` namespace prefix.

Four payment counters shipped without it and were renamed once it was
verified that nothing consumed the old names (``/metrics`` is bound to
127.0.0.1 in nginx and no Prometheus scrapes prod).  That window is
closed the moment the first dashboard or alert rule exists, so this test
pins the invariant instead of trusting the next author to remember it.

Names are read through ``Counter.describe()`` — the public collector API
— rather than the private ``_name`` attribute, so the guard survives a
``prometheus_client`` upgrade.  ``describe()`` reports the metric
*family* name, which is the exposition name minus the ``_total`` suffix
the client appends for counters; the prefix is at the other end, so the
comparison is unaffected.
"""

from __future__ import annotations

import prometheus_client

from telegram_invite_bot.webhook import metrics

PREFIX = "tib_"


def _families() -> list[tuple[str, str]]:
    """``(python_symbol, exposed_family_name)`` for every counter."""
    return [
        (attr, family.name)
        for attr, value in vars(metrics).items()
        if isinstance(value, prometheus_client.Counter)
        for family in value.describe()
    ]


def test_module_defines_counters() -> None:
    """Guard the guard: an empty collection would pass vacuously."""
    assert _families(), "no counters found — has metrics.py been restructured?"


def test_every_counter_is_namespaced() -> None:
    offenders = [f"{attr} -> {name}" for attr, name in _families() if not name.startswith(PREFIX)]
    assert not offenders, (
        "webhook counters must be prefixed with "
        f"{PREFIX!r} to stay namespaced in a shared Prometheus: {offenders}"
    )


def test_exposed_names_are_unique() -> None:
    """A copy-pasted name would silently merge two unrelated series."""
    names = [name for _, name in _families()]
    assert len(names) == len(set(names)), f"duplicate counter names: {names}"
