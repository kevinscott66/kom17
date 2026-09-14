"""``/admin_audit`` — Python sys.audit subsystem liveness probe.

Python 3.8+ ships a runtime audit framework (PEP 578): a callback
chain that fires on hundreds of security-relevant events
(``open``, ``exec``, ``socket.connect``, ``compile``, …). Hooks
register via :func:`sys.addaudithook`. The framework is widely
under-used because there's no easy way to verify "is anything
actually listening?" — :mod:`sys` deliberately does NOT expose
the installed-hook list (the hook chain is append-only by design
to prevent malicious code from removing audit hooks).

What this card does, and why:

* **Liveness probe** — installs ONE hook the first time the
  card is opened, fires a one-shot synthetic event, and marks
  the hook inert again (PEP 578 doesn't permit removal, so the
  hook is reused on every later invocation rather than being
  replaced). Reports whether the hook ran. If it didn't, the
  auditing pipeline is broken — almost always a regression in
  PYTHONNODEBUGRANGES or a faulthandler conflict.
* **Dispatch count** — how many times OUR hook was called for
  the one synthetic event. It is not a census of the installed
  hooks and cannot be: PEP 578 keeps that list private and a
  hook only ever observes its own invocations (#1459). The
  healthy value is exactly 1; anything else means the framework
  is replaying events, which would be a CPython bug.
* **Recent audit events sample** — captures the last few real
  events fired by the runtime in the probe window. Operator gets
  a feel for which events fire during normal bot operation,
  which is otherwise invisible.

Why "audit-and-not-introspect": we deliberately do not crawl
:mod:`sys` for ``audit_hooks``-like attributes. PEP 578 keeps the
list private and any reflection-based probe would be unreliable
across CPython versions. The probe-and-measure approach works on
every supported Python.

Cry-wolf posture: ⚠ only when the probe fires AND our hook does
not run — that's a real broken-pipeline signal. Otherwise the
card is informational. Audit being "silently active with no
hooks" is normal for most bots and not a problem.
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.audit")


# Unique event name for our liveness probe. Prefixing with the
# package name keeps it distinguishable from any legitimate audit
# event the runtime emits — no collisions, easy to grep for in
# logs if the operator wires their own hook on the side.
_PROBE_EVENT = "telegram_invite_bot.admin.audit.probe"


# Cap on captured real events in the probe window. The framework
# can fire thousands of events per second in a busy process; we
# only want a representative sample for the operator's "what
# normally fires?" understanding.
_EVENT_SAMPLE_CAP = 8


class _AuditSnapshot:
    """Captured probe result.

    * ``probe_fired`` — did our hook see the synthetic event?
      ``False`` means the audit pipeline is broken end-to-end.
    * ``probe_call_count`` — how many times our hook was called
      during the probe window. We fire ``_PROBE_EVENT`` exactly
      once, so 1 is the healthy value. Greater than 1 means
      something is replaying events (rare; would be a CPython
      bug); 0 means the pipeline didn't dispatch.
    * ``other_events`` — sample of real (non-probe) audit events
      that fired during the probe window. Operator sees what
      normal bot operation looks like to the audit framework.
    * ``python_version`` — recorded for the card so the operator
      doesn't have to cross-reference /admin_python.
    """

    __slots__ = ("other_events", "probe_call_count", "probe_fired", "python_version")

    def __init__(
        self,
        *,
        probe_fired: bool,
        probe_call_count: int,
        other_events: tuple[str, ...],
        python_version: str,
    ) -> None:
        self.probe_fired = probe_fired
        self.probe_call_count = probe_call_count
        self.other_events = other_events
        self.python_version = python_version


class _ProbeState:
    """Process-wide state of the one installed probe hook.

    A single mutable object rather than a closure per probe,
    because the hook it feeds is installed ONCE and reused
    (#1458). ``installed`` is what makes that true; the other
    three are reset at the top of every :func:`_capture`.
    """

    __slots__ = ("active", "calls", "events", "installed")

    def __init__(self) -> None:
        self.installed = False
        self.active = False
        self.calls = 0
        self.events: list[str] = []


_STATE = _ProbeState()

# Serialises the whole probe. Two developers running
# ``/admin_audit`` at the same moment would otherwise share the
# one state object and each report the other's counts. Held
# across ``sys.audit`` — safe, because the hook it dispatches to
# never takes this lock.
_STATE_LOCK = threading.Lock()


def _probe_hook(event: str, _args: tuple[Any, ...]) -> None:
    """The one audit hook this module ever installs.

    Hot path — keep this branch-light. The framework calls it for
    every audit event in the process, forever, so the inert
    early-return is what keeps it free between probes.
    """
    if not _STATE.active:
        return
    if event == _PROBE_EVENT:
        _STATE.calls += 1
        return
    if len(_STATE.events) < _EVENT_SAMPLE_CAP:
        _STATE.events.append(event)


def _capture() -> _AuditSnapshot:
    """Fire the synthetic event through the probe hook and return
    the observation, installing that hook on first use.

    PEP 578 forbids hook removal — once installed, a hook stays
    for the life of the interpreter. That is exactly why only one
    is ever installed (#1458): the first version of this card
    added a fresh hook on every ``/admin_audit``, so N openings
    of the card left N permanent callbacks firing on every
    ``open`` / ``exec`` / ``socket.connect`` / ``import`` the
    process makes. The card charged the operator for having
    looked at it. One hook is a rounding error and honestly
    described as such; N was not.
    """
    with _STATE_LOCK:
        if not _STATE.installed:
            sys.addaudithook(_probe_hook)
            _STATE.installed = True
        _STATE.calls = 0
        _STATE.events = []
        _STATE.active = True
        try:
            # Fire the synthetic event. The framework dispatches to
            # every installed hook synchronously, so by the time
            # sys.audit returns we know whether our hook saw it.
            sys.audit(_PROBE_EVENT)
        finally:
            # Render the hook inert again until the next probe. We
            # can't unregister; we can neutralize.
            _STATE.active = False

        return _AuditSnapshot(
            probe_fired=_STATE.calls > 0,
            probe_call_count=_STATE.calls,
            other_events=tuple(_STATE.events),
            python_version=sys.version.split()[0],
        )


def _pipeline_broken(snap: _AuditSnapshot) -> bool:
    """⚠ predicate. The hook MUST fire when we trigger
    ``sys.audit(_PROBE_EVENT)`` — failure means the audit
    pipeline is broken end-to-end, almost certainly a CPython
    build regression or an environment misconfiguration."""
    return not snap.probe_fired


def _render(snap: _AuditSnapshot) -> str:
    lines = ["🛡 <b>sys.audit subsystem</b>", ""]
    lines.append(f"  <b>Python:</b> <code>{snap.python_version}</code>")
    lines.append("")

    lines.append("  <b>liveness probe:</b>")
    if snap.probe_fired:
        lines.append(
            "    • <code>sys.audit(probe_event)</code> dispatched "
            f"to our hook <b>{snap.probe_call_count}</b> time(s) — "
            "audit pipeline is live."
        )
    else:
        # The unambiguous failure mode. ⚠ here is justified —
        # this card exists to detect it.
        lines.append(
            "    • <code>sys.audit(probe_event)</code> fired but "
            "our hook did NOT run ⚠ — audit pipeline appears "
            "broken end-to-end (CPython build issue or "
            "faulthandler conflict)."
        )

    lines.append("")
    if snap.other_events:
        # Deduplicate while preserving first-seen order — same
        # event firing repeatedly during the tiny probe window
        # is noisy and uninformative.
        seen: set[str] = set()
        unique: list[str] = []
        for ev in snap.other_events:
            if ev in seen:
                continue
            seen.add(ev)
            unique.append(ev)
        lines.append("  <b>events observed during probe window:</b>")
        for ev in unique:
            lines.append(f"    • <code>{ev}</code>")
    else:
        lines.append(
            "  <i>no other audit events fired during the probe "
            "window. Normal for an idle bot; the framework only "
            "fires on security-relevant operations (open, exec, "
            "socket.connect, compile, etc.).</i>"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: only emitted when the synthetic probe "
        "event does NOT reach our hook — that's the unambiguous "
        "&quot;audit pipeline broken&quot; signal. PEP 578 keeps "
        "the installed-hook list private; this card observes "
        "the framework via a one-shot probe rather than "
        "reflecting on it.</i>"
    )
    return "\n".join(lines)


async def handle_admin_audit(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_audit; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        probe_fired=snap.probe_fired,
        probe_call_count=snap.probe_call_count,
        other_event_count=len(snap.other_events),
        broken=_pipeline_broken(snap),
    ).info("/admin_audit rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.audit")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_audit(message, settings)

    router.message.register(_entry, Command("admin_audit", ignore_case=True))
    return router
