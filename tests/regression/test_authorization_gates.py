"""Regression guards for the two load-bearing authorization invariants.

Both are the same species of bug as the ones ``test_money_call_sites``
covers: code that *reads* as a check, passes review, type-checks, and
enforces nothing.

1. :func:`test_every_admin_handler_gates_on_the_developer_set` — every
   ``/admin_*`` handler actually calls ``is_developer`` and branches on
   the answer.
2. :func:`test_admin_status_tri_state_is_never_leaked` — every caller of
   the ``bool | None`` live-admin probe handles ``None`` explicitly
   instead of letting it decay into a truthy/falsy branch (the
   R-FIX-007 fail-open class), over a probe inventory
   :func:`test_the_probe_inventory_is_discovered_not_named` keeps
   honest.

Part 1 — the developer gate
===========================
``src/telegram_invite_bot/handlers/admin/`` is owner-only *by
construction* — it exposes host telemetry (``/admin_envscan``,
``/admin_netconns``, ``/admin_fds``), database internals
(``/admin_tables``, ``/admin_pragmas``, ``/admin_dbprobe``), payment
configuration (``/admin_payment_keys``) and money-moving actions
(``/give``, the withdrawal approve/reject callbacks). None of that has a
router-level filter protecting it: aiogram routers here filter on chat
type at most, so the ONLY thing standing between a stranger and
``/admin_envscan`` is an in-handler ``settings.bot.is_developer(...)``
check.

That makes the gate load-bearing and, worse, easy to forget — the
surface is ~111 modules of near-identical boilerplate, and a new one is
copied from a neighbour. A copy that drops the gate looks completely
normal in review and ships an owner-only command to every user of the
bot. This test is the backstop: it fails with a precise ``file:line``
list rather than letting that reach prod.

What is checked
---------------
Every function passed as the first positional argument to a
``router.message.register(...)`` / ``router.callback_query.register(...)``
call inside ``handlers/admin/``:

1. must resolve to a ``def`` in the same module (a lambda or an imported
   name cannot be proved gated — that is a failure, not a pass), and
2. that ``def`` — or, within 3 hops, a same-module function it calls —
   must invoke :meth:`BotSettings.is_developer` (or ``is_admin``) **in a
   guard context**: inside an ``if`` / ``while`` / ``assert`` test, a
   comparison / boolean op / ``not``, or a ``return`` of the verdict.

The hop-following matters because the house pattern is a thin
``_entry`` closure registered on the router that delegates to a
module-level ``handle_admin_x(message, settings)`` holding the real
gate. The guard-context requirement matters because a bare
``settings.bot.is_developer(uid)`` whose result is discarded reads like
a check and enforces nothing.

Waiver
------
An admin registration that genuinely must be open to everyone carries
``# admin-gate: allow (<reason>)`` on the ``register(...)`` call line(s).
There are none today, and the count should stay at zero: a public
command belongs outside ``handlers/admin/``.

Part 2 — the tri-state live-admin probe
=======================================
:func:`~telegram_invite_bot.utils.telegram_admin.is_user_admin` (and its
twin ``handlers/moderation._is_user_admin``) return **three** values:
``True`` / ``False`` for a confirmed status and ``None`` when the
``get_chat_member`` API call failed. The ``None`` exists so each caller
picks its own fail direction — R-FIX-007 — because the safe direction is
not the same one twice:

* **actor** side ("may this user ban?") must fail *closed*: unknown
  status → refuse the action.
* **target** side ("is this user an admin I must not ban?") must not
  invent a verdict at all: unknown status → refuse the command with an
  explicit "retry in a moment" (#249). Replying "that user is an
  administrator" after a failed probe states a fact the bot never
  established and leaves the issuer unable to tell a real admin from
  a 429.

Writing ``if await is_user_admin(bot, chat, uid):`` silently picks
falsy-on-error for both, which is correct for the actor and exactly
backwards for the target — an API blip becomes "ban the chat owner".
Nothing in the type system objects: ``bool | None`` is perfectly
truth-testable, and so is ``ChatMember | None``.

So this guard requires every call site to consume the result through an
**identity comparison** (``is True`` / ``is not True`` / ``is None`` /
``is False``), either on the call expression itself or on the name it is
bound to, somewhere in the same function. Truth-testing it directly
fails. Every call site in the tree passes today — the point is that the
next one cannot be written carelessly.

*Which* functions count as probes is **discovered**, not listed
(:func:`_discovered_probes`): any ``def`` whose ``try`` makes a live
``get_chat_member`` round-trip and whose ``except`` returns ``None`` is
one, by shape. The four names this test originally carried were simply
the four that existed when it was written; ``rating``'s
``_caller_is_rating_admin`` and ``group_pay``'s ``_caller_is_owner``
were written later to exactly the same contract and were outside the
guard the whole time. A regression test that only knows the names it
was born with cannot catch the next copy, which is the only thing it is
for.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
ADMIN_ROOT = SRC_ROOT / "handlers" / "admin"

# Predicates that constitute an authorization check. ``is_developer`` is
# the real one (``settings.bot.is_developer``); ``is_admin`` is accepted
# so a future per-chat admin surface is not forced to widen this test.
GATE_METHODS = frozenset({"is_developer", "is_admin"})

# Audited intentionally-ungated registrations carry this marker on the
# ``register(...)`` call line(s), with a reason. Expected count: zero.
ALLOW_MARKER = "admin-gate: allow"

# How far to follow same-module delegation (``_entry`` → ``handle_x`` is
# one hop; the limit keeps a cyclic call graph from looping).
_MAX_HOPS = 3


def _admin_modules() -> list[Path]:
    # ``rglob``, not ``glob``: a future ``handlers/admin/<subpackage>/``
    # would otherwise be invisible to this invariant — the test would
    # still pass while the new modules went unchecked.
    return sorted(p for p in ADMIN_ROOT.rglob("*.py") if p.name != "__init__.py")


def _defs_by_name(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Every ``def`` in the module (including nested ones), by name.

    A name can map to several defs — the ``build_router`` closures are
    all called ``_entry``. All of them must be gated, so we keep the
    list rather than picking one.
    """
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def _guard_expressions(func: ast.AST) -> list[ast.AST]:
    """Sub-expressions of ``func`` that actually decide control flow.

    Mirrors ``test_money_call_sites._guard_names``: an ``if`` / ``while``
    / ``assert`` / ternary test, any comparison / boolean op / ``not``,
    and a returned value (a helper propagating the verdict to a caller
    that branches on it).
    """
    out: list[ast.AST] = []
    for node in ast.walk(func):
        if isinstance(node, (ast.If, ast.While, ast.Assert, ast.IfExp)):
            out.append(node.test)
        elif isinstance(node, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            out.append(node)
        elif isinstance(node, ast.Return) and node.value is not None:
            out.append(node.value)
    return out


def _is_gate_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in GATE_METHODS
    return isinstance(func, ast.Name) and func.id in GATE_METHODS


def _gate_result_names(func: ast.AST) -> set[str]:
    """Names bound to a gate call: ``allowed = settings.bot.is_developer(x)``."""
    names: set[str] = set()
    for node in ast.walk(func):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            value, targets = node.value, [node.target]
        if isinstance(value, ast.Await):
            value = value.value
        if not _is_gate_call(value):
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _guards_directly(func: ast.AST) -> bool:
    """True iff ``func`` itself branches on an authorization check."""
    guards = _guard_expressions(func)
    for expr in guards:
        for sub in ast.walk(expr):
            if _is_gate_call(sub):
                return True
    bound = _gate_result_names(func)
    if not bound:
        return False
    for expr in guards:
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Name) and sub.id in bound:
                return True
    return False


def _called_names(func: ast.AST) -> list[str]:
    """Names of functions ``func`` calls, for same-module delegation."""
    out: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            out.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            out.append(node.func.attr)
    return out


def _is_gated(
    func: ast.AST,
    defs: dict[str, list[ast.AST]],
    *,
    seen: set[int] | None = None,
    hops: int = 0,
) -> bool:
    """``func`` gates directly, or delegates (≤ ``_MAX_HOPS``) to one that does."""
    if _guards_directly(func):
        return True
    if hops >= _MAX_HOPS:
        return False
    seen = seen or {id(func)}
    for name in _called_names(func):
        for target in defs.get(name, []):
            if id(target) in seen:
                continue
            seen.add(id(target))
            if _is_gated(target, defs, seen=seen, hops=hops + 1):
                return True
    return False


def _registrations(tree: ast.Module, lines: list[str]) -> list[tuple[str | None, int]]:
    """``(handler_name, lineno)`` for each router ``register(...)`` call.

    ``handler_name`` is ``None`` when the first argument is not a plain
    name (a lambda, a subscript, …) — unprovable, and reported as such.
    Waived call sites are skipped entirely.
    """
    out: list[tuple[str | None, int]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and node.args
        ):
            continue
        receiver = ast.unparse(node.func.value)
        if not receiver.startswith("router."):
            continue
        end = node.end_lineno or node.lineno
        if ALLOW_MARKER in "\n".join(lines[node.lineno - 1 : end]):
            continue
        first = node.args[0]
        name = first.id if isinstance(first, ast.Name) else None
        out.append((name, node.lineno))
    return out


def test_every_admin_handler_gates_on_the_developer_set() -> None:
    offenders: list[str] = []
    total = 0
    for path in _admin_modules():
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        defs = _defs_by_name(tree)
        for name, lineno in _registrations(tree, lines):
            total += 1
            if name is None:
                offenders.append(f"{path.name}:{lineno} — handler is not a plain name")
                continue
            targets = defs.get(name)
            if not targets:
                offenders.append(f"{path.name}:{lineno} — {name}() not defined in module")
                continue
            for target in targets:
                if not _is_gated(target, defs):
                    offenders.append(f"{path.name}:{lineno} — {name}() has no dev gate")

    assert not offenders, (
        "admin handler(s) registered without a developer gate — an owner-only "
        "surface is open to every user of the bot. Add "
        "`if not settings.bot.is_developer(user.id): return` before any work, "
        f"or an audited `# {ALLOW_MARKER} (<reason>)` on the register() line:\n  "
        + "\n  ".join(offenders)
    )
    # Sanity: if the scanner stops finding registrations (a refactor to
    # decorator-style routing, say) it would pass vacuously forever.
    assert total >= 100, f"expected ~117 admin registrations, scanned {total}"


def test_no_admin_gate_waivers_are_in_use() -> None:
    """The waiver escape hatch must stay unused.

    A genuinely public command does not belong in ``handlers/admin/`` —
    it belongs next to the other user-facing handlers, where nobody
    reading it assumes an owner gate. If this fails, move the handler
    instead of keeping the marker.
    """
    waived = [
        f"{path.name}:{lineno}"
        for path in _admin_modules()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if ALLOW_MARKER in line
    ]
    assert not waived, "ungated admin handlers waived instead of moved out: " + ", ".join(waived)


# --------------------------------------------------------------------------
# Part 2 — the tri-state live-admin probe (see module docstring)
# --------------------------------------------------------------------------

#: Probes the detector below cannot see, named by hand. Both are the
#: public wrappers in ``utils/telegram_admin.py``: they do not perform
#: the round-trip themselves — ``_fetch_member`` does — so no ``try``
#: of their own carries a ``get_chat_member``. Their ``bool | None``
#: contract is the original one this test was written for (#337 added
#: the wide sibling ``is_chat_admin_any``: different question,
#: identical three-valued answer, policed identically).
_DECLARED_PROBES = frozenset({"is_user_admin", "is_chat_admin_any"})

#: Anchors for :func:`test_the_probe_inventory_is_discovered_not_named`.
#: Not the inventory — the inventory is discovered. These four are the
#: ones a reader can check by eye, and their presence proves the
#: detector still recognises the shape after a refactor renames things
#: around it.
_ANCHOR_PROBES = frozenset(
    {"_fetch_member", "_is_user_admin", "_probe_chat_member", "_caller_is_owner"}
)


def _returns_none(node: ast.AST) -> bool:
    """Does anything under ``node`` ``return None`` (explicitly or bare)?"""
    return any(
        isinstance(sub, ast.Return)
        and (sub.value is None or (isinstance(sub.value, ast.Constant) and sub.value.value is None))
        for sub in ast.walk(node)
    )


def _calls_get_chat_member(body: list[ast.stmt]) -> bool:
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr == "get_chat_member"
        for stmt in body
        for sub in ast.walk(stmt)
    )


def _discovered_probes() -> frozenset[str]:
    """Every hand-rolled tri-state membership probe in the package.

    The shape, not the name: a ``def`` holding a ``try`` whose body
    makes a live ``get_chat_member`` round-trip and whose ``except``
    returns ``None``. That is exactly the R-FIX-007 contract — "I could
    not find out" as a third answer distinct from yes and no — and it
    is the thing that has to be discriminated at every call site.

    Discovered rather than listed because the four names this test
    originally carried were only the four that existed when it was
    written. ``handlers/rating.py`` ``_caller_is_rating_admin`` and
    ``handlers/group_pay.py`` ``_caller_is_owner`` were later written
    to the same contract, correctly, and sat outside the guard's blast
    radius the whole time: the next copy of the shape would not have
    been caught, which is the only case a regression test exists for.
    """
    names: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        for func in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for block in ast.walk(func):
                if not isinstance(block, ast.Try):
                    continue
                if not _calls_get_chat_member(block.body):
                    continue
                if any(_returns_none(handler) for handler in block.handlers):
                    names.add(func.name)
                    break
    return frozenset(names)


#: The probes whose result must never decay into a two-way branch.
TRI_STATE_PROBES = _DECLARED_PROBES | _discovered_probes()


def test_the_probe_inventory_is_discovered_not_named() -> None:
    """The detector must keep finding probes, including unnamed ones.

    A discovery that quietly returns nothing turns
    :func:`test_admin_status_tri_state_is_never_leaked` into a test that
    scans the package and asserts about no call sites at all — green,
    and worth exactly as much as deleting it. This is the tripwire.
    """
    discovered = _discovered_probes()
    missing = sorted(_ANCHOR_PROBES - discovered)
    assert not missing, (
        "the tri-state probe detector stopped recognising known probes "
        f"{missing} — either they were rewritten (fix them or the anchors) "
        "or the shape it matches has drifted"
    )
    assert len(discovered) >= len(_ANCHOR_PROBES) + 2, (
        f"only {len(discovered)} probes discovered: {sorted(discovered)}"
    )


#: Identity operators. ``==``/``!=`` are NOT accepted: ``x == True`` is
#: true for ``1`` and, more to the point, reads as a value test rather
#: than the three-way discrimination the contract requires.
_IDENTITY_OPS = (ast.Is, ast.IsNot)


def _unwrap_await(node: ast.expr | None) -> ast.expr | None:
    return node.value if isinstance(node, ast.Await) else node


def _is_probe_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in TRI_STATE_PROBES
    return isinstance(func, ast.Attribute) and func.attr in TRI_STATE_PROBES


def _identity_compared_nodes(func: ast.AST) -> tuple[set[int], set[str]]:
    """What is discriminated with ``is`` / ``is not`` inside ``func``.

    Returns ``(call node ids, names)`` — the direct
    ``await probe(...) is True`` form and the
    ``status = await probe(...)`` … ``status is None`` form respectively.
    """
    call_ids: set[int] = set()
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, _IDENTITY_OPS) for op in node.ops):
            continue
        for operand in [node.left, *node.comparators]:
            inner = _unwrap_await(operand)
            if _is_probe_call(inner):
                call_ids.add(id(inner))
            elif isinstance(inner, ast.Name):
                names.add(inner.id)
    return call_ids, names


def _probe_bindings(func: ast.AST) -> dict[int, set[str]]:
    """Map each probe call inside ``func`` to the name(s) it is bound to.

    The call may sit anywhere inside the assigned expression, not just at
    its root: ``is_admin = await probe(...) if chat_id else False``
    binds through an ``IfExp``.
    """
    out: dict[int, set[str]] = {}
    for node in ast.walk(func):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            value, targets = node.value, [node.target]
        if value is None:
            continue
        bound = {t.id for t in targets if isinstance(t, ast.Name)}
        if not bound:
            continue
        for sub in ast.walk(value):
            if _is_probe_call(sub):
                out.setdefault(id(sub), set()).update(bound)
    return out


def _enclosing_def(
    node: ast.AST, parents: dict[int, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The innermost ``def`` containing ``node``, or ``None`` at module level."""
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(id(cur))
    return None


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def test_admin_status_tri_state_is_never_leaked() -> None:
    offenders: list[str] = []
    total = 0
    for path in sorted(SRC_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = _parent_map(tree)
        rel = path.relative_to(SRC_ROOT).as_posix()
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # The probes' own definitions return the tri-state; they do
            # not consume it.
            if func.name in TRI_STATE_PROBES:
                continue
            compared_ids, compared_names = _identity_compared_nodes(func)
            bindings = _probe_bindings(func)
            for node in ast.walk(func):
                if not _is_probe_call(node):
                    continue
                # Nested defs are walked by their own iteration too; only
                # score a call against its innermost enclosing function.
                if _enclosing_def(node, parents) is not func:
                    continue
                total += 1
                if id(node) in compared_ids:
                    continue
                if bindings.get(id(node), set()) & compared_names:
                    continue
                offenders.append(f"{rel}:{node.lineno} (in {func.name})")

    assert not offenders, (
        "live-admin probe result used without discriminating None — an API "
        "error silently becomes a permission decision (R-FIX-007). Compare "
        "with `is True` / `is None` and choose the fail direction "
        "explicitly:\n  " + "\n  ".join(offenders)
    )
    assert total >= 18, (
        f"only {total} tri-state call sites scanned across "
        f"{len(TRI_STATE_PROBES)} probes — the scan lost sight of the tree"
    )
