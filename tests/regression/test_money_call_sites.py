"""Regression guard: every wallet-mutating call inspects its result.

Locks in the SEC-1/SEC-2 posture: repo/service money mutators return
``None`` (instead of raising) when the wallet row is missing or the
balance is insufficient, so EVERY mutator call site must either inspect
the result or carry an audited ``# money-guard: allow`` waiver — an
unchecked call silently mints or burns coins.

What counts as a money mutator
------------------------------
* ``*.credit(...)`` / ``*.debit(...)`` — the repo/service primitives.
* ``*.set_balance(...)`` and ``*.transfer(...)`` **when the receiver is an
  economy service** (``self._economy``, ``economy``, ``economy_service`` …).
  These are real wallet mutators (``EconomyService.set_balance`` /
  ``EconomyService.transfer``) the old proximity test could not see.
  The receiver scope matters: ``client.transfer(...)`` in
  ``withdraw_service`` is the *CryptoPay payout* client, not a wallet
  mutator, and must NOT be swept in.
* ``*.hold(...)`` / ``*.release(...)`` on an economy receiver — the
  escrow pair added by #238. They move ``balance`` exactly like
  ``debit``/``credit`` and share their ``None``-on-shortfall contract;
  the only difference is that the lifetime counters stay put, which is
  invisible to a caller and therefore no reason to trust the site less.
  Receiver-scoped for the same reason ``transfer`` is: ``lock.release()``
  and ``session.hold()`` are not wallet writes.
* ``*.consume(...)`` on an inventory receiver — not a coin move, but the
  same rowcount-is-the-authority contract (#1133). It is the single-shot
  guard that turns a paid inventory entry into a one-time grant, and it
  returns ``False`` when a concurrent click already spent the entry. A
  discarded result therefore hands out the entitlement twice for one
  purchase, which is money by another name: ``_apply_unwarn`` lifted two
  warnings for one 800-coin item until #1133 for exactly this reason.
  Receiver-scoped because a rate limiter's ``bucket.consume()`` is not an
  entitlement.

What counts as "checked" (AUD-6 hardening)
-----------------------------------------
The previous guard accepted ANY ``is None`` / ``if not`` / ``assert``
within ±5 source lines. That is too loose: an unrelated nearby ``if not
ok:`` would satisfy a ``credit()`` whose result is discarded
(false-negative pass). The strengthened criterion is structural, not
proximity-based — a mutator call passes iff one of:

1. **Inline waiver** — ``# money-guard: allow (<reason>)`` on the call's
   own source line(s). Immune to line drift; the audited-exception
   convention the tree already uses (~6 markers).
2. **Bound-and-tested** — the call's result is assigned to a name (incl.
   tuple-unpacking and walrus) AND that name is later used in a guard
   context (``if`` / ``while`` / ``IfExp`` test, a comparison, a boolean
   op, a ``not``, or a ``return`` of the value) within the same function.
   A bare expression-statement call (result thrown away) can therefore
   only pass via rule 1.

   **``assert`` does not count** (#286). It used to, and that is exactly
   how seven bare ``assert credited is not None`` on the /duel, /cpc and
   /pvp payout paths sat green under this very test until #263. Two
   reasons it is not a guard here. Under ``-O`` the statement vanishes
   and the money path reads ``None.balance``; without ``-O`` — which is
   how the systemd unit actually runs in production — it crashes mute,
   logging
   neither the player ids nor the amounts that were in flight, and the
   rollback then erases the session that could have told you. A money
   guard has to say what broke. ``assert`` cannot.

A new unchecked mutator call — or an existing one whose guard is deleted
— fails here with a precise ``file:line`` list.

Second guard: raw wallet writes are paired with a ledger row (#225)
------------------------------------------------------------------
Checking the return value only proves the coins moved — not that the move
was *booked*. ``/roulette``, ``/roll``, ``/flip``, the passive message
reward and the shop gift payout all moved real coins with no
``transactions`` row, so ``/balance``'s weekly cashflow under-reported
them and nothing could reconcile the coin supply. The second test
therefore asserts the structural pairing:

* A **raw** wallet write is a ``credit``/``debit``/``hold``/``release``
  call with no ``type=`` keyword — i.e. straight at ``EconomyRepo``
  (the escrow pair receiver-scoped, as above).
  ``EconomyService.credit`` / ``.debit`` / ``.hold`` / ``.release``
  *require* ``type=`` and write the companion row themselves,
  so a call that passes ``type=`` is already booked and is skipped. That
  signature difference is the whole basis of the heuristic, so the test
  asserts it against the live classes before using it — rename a
  parameter and this guard fails loudly instead of silently passing
  everything.
* A module holding a raw write must also hold a ledger ``record(...)``
  call on a transactions receiver, or carry an audited
  ``# ledger-guard: allow (<reason>)`` marker.

Module scope, not function scope, on purpose: ``/duel`` and ``/rps``
book their rows in a ``_write_ledger`` helper next to ``play()``, which a
per-function rule would flag as a false positive. The looser scope still
catches the whole #225 class — every one of the five sites was in a
module with no ledger write at all.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TypeGuard

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"

# Always-flagged primitives: any receiver.
_BROAD_MONEY_METHODS = frozenset({"credit", "debit"})

# Flagged only on an economy-service receiver (see module docstring): these
# names also exist on unrelated objects (``client.transfer`` = CryptoPay
# payout), so we scope by receiver to stay precise.
_SCOPED_MONEY_METHODS = frozenset({"set_balance", "transfer"})

# Escrow primitives (#238): they move ``balance`` and leave ``total_spent``
# / ``total_earned`` alone. Same ``None``-on-shortfall contract as
# ``debit``/``credit``, so the same result-checking rule applies. Scoped by
# receiver like the pair above — ``lock.release()`` is not a wallet write.
_ESCROW_MONEY_METHODS = frozenset({"hold", "release"})

# Receiver expressions that denote an economy service / wallet repo. Matched
# against the unparsed receiver, case-insensitively, as a substring.
_ECONOMY_RECEIVER_HINTS = ("economy", "wallet", "_econ")

# Single-shot entitlement primitives (#1133): they do not move coins, but
# their boolean result is the only thing standing between one purchase and
# two grants. Scoped by receiver — see the module docstring.
_ENTITLEMENT_METHODS = frozenset({"consume"})

# Receiver expressions that denote the inventory repository. Covers both
# ``inventory_repo`` in the handlers and ``self._inventory`` in the service.
_INVENTORY_RECEIVER_HINTS = ("inventory",)

# Audited intentionally-unchecked sites carry an inline marker on the call
# line(s). Each marker must state the audit reason.
# TODO(SEC-1/SEC-2): drive the marker count to zero before real-money.
ALLOW_MARKER = "money-guard: allow"

# #225: a module that moves coins straight at the repo must also book the
# companion ledger row. Audited exceptions carry this marker.
LEDGER_ALLOW_MARKER = "ledger-guard: allow"

# Receivers that denote the transactions ledger. ``game_limit_service.record``
# is a play-rate stamp, not a money row, so scoping by receiver matters.
_LEDGER_RECEIVER_HINTS = ("ledger", "transaction")


def _is_money_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """True for a wallet-mutating call (see module docstring for the rules).

    A ``TypeGuard`` rather than a plain ``bool`` so the sweep below can read
    ``lineno``/``end_lineno`` off an accepted node without an ``AST has no
    attribute`` complaint. Note it narrows to ``ast.Call``, not to "a Call
    whose func is an Attribute" — the type system cannot say that — so the
    one site that needs ``func.attr`` re-asserts it.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    attr = node.func.attr
    if attr in _BROAD_MONEY_METHODS:
        return True
    if attr in _SCOPED_MONEY_METHODS | _ESCROW_MONEY_METHODS:
        return _has_economy_receiver(node)
    if attr in _ENTITLEMENT_METHODS:
        return _has_receiver_hint(node, _INVENTORY_RECEIVER_HINTS)
    return False


def _has_receiver_hint(node: ast.Call, hints: tuple[str, ...]) -> bool:
    """True when the call's receiver text contains one of ``hints``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    receiver = ast.unparse(func.value).lower()
    return any(hint in receiver for hint in hints)


def _has_economy_receiver(node: ast.Call) -> bool:
    """True when the call's receiver looks like an economy service/repo.

    Kept as its own name because the ledger guard below scopes by it too,
    and that guard must NOT widen to the entitlement receivers.
    """
    return _has_receiver_hint(node, _ECONOMY_RECEIVER_HINTS)


def _unwrap(value: ast.expr | None) -> ast.expr | None:
    """Strip a leading ``await`` so the underlying ``Call`` is visible."""
    if isinstance(value, ast.Await):
        return value.value
    return value


def _bound_targets(stmt: ast.stmt) -> tuple[ast.expr | None, set[str]]:
    """If ``stmt`` assigns a (possibly awaited) call to name(s), return the
    bound ``Call`` value and the set of target names.

    Handles ``x = await repo.credit(...)``, ``x: Wallet | None = ...`` and
    ``a, b = await econ.transfer(...)`` (tuple unpacking) and walrus
    ``(x := await ...)`` is handled separately at the expression level.
    """
    names: set[str] = set()
    if isinstance(stmt, ast.Assign):
        value = _unwrap(stmt.value)
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        value = _unwrap(stmt.value)
        targets = [stmt.target]
    else:
        return None, names
    for tgt in targets:
        if isinstance(tgt, ast.Name):
            names.add(tgt.id)
        elif isinstance(tgt, ast.Tuple):
            names.update(e.id for e in tgt.elts if isinstance(e, ast.Name))
    return value, names


def _guard_names(func: ast.AST) -> set[str]:
    """Names used in a guard context anywhere in ``func``.

    Guard contexts: the test of an ``if`` / ``while`` / ``IfExp``; any
    ``Compare`` / ``BoolOp`` / ``UnaryOp`` (covers ``x is None``,
    ``not x``, ``x and ...``); and a bare ``return <name>`` / ``return <expr
    with name>`` (the result is propagated to a caller that must check it).

    **Not** the test of an ``assert`` — see rule 2 in the module
    docstring (#286). Dropping ``ast.Assert`` from the branch list below
    would not be enough on its own: ``assert credited is not None`` also
    contains a ``Compare``, which the next branch harvests. So the whole
    subtree under every ``assert`` test is excluded up front.
    """
    names: set[str] = set()

    excluded: set[int] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            excluded.update(id(sub) for sub in ast.walk(node.test))

    def _harvest(expr: ast.AST) -> None:
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Name) and id(sub) not in excluded:
                names.add(sub.id)

    for node in ast.walk(func):
        if id(node) in excluded:
            continue
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            _harvest(node.test)
        elif isinstance(node, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            _harvest(node)
        elif isinstance(node, ast.Return) and node.value is not None:
            _harvest(node.value)
    return names


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_func(node: ast.AST, parents: dict[ast.AST, ast.AST], root: ast.AST) -> ast.AST:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return root


def test_every_money_mutator_call_site_is_result_checked() -> None:
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        parents = _build_parent_map(tree)

        # Map each money Call node -> the name(s) its result is bound to.
        bound_for_call: dict[int, set[str]] = {}
        for node in ast.walk(tree):
            # Walrus binding: ``(x := await repo.credit(...))``.
            if isinstance(node, ast.NamedExpr):
                inner = _unwrap(node.value)
                if (
                    isinstance(inner, ast.Call)
                    and _is_money_call(inner)
                    and isinstance(node.target, ast.Name)
                ):
                    bound_for_call.setdefault(id(inner), set()).add(node.target.id)
            # Assignment binding.
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value, names = _bound_targets(node)
                if isinstance(value, ast.Call) and _is_money_call(value) and names:
                    bound_for_call.setdefault(id(value), set()).update(names)

        for node in ast.walk(tree):
            if not _is_money_call(node):
                continue
            lineno = node.lineno
            end = node.end_lineno or lineno
            call_text = "\n".join(lines[lineno - 1 : end])

            # Rule 1: inline audited waiver on the call's own line(s).
            if ALLOW_MARKER in call_text:
                continue

            # Rule 2: result bound to a name that is later guarded.
            bound = bound_for_call.get(id(node), set())
            if bound:
                func = _enclosing_func(node, parents, tree)
                if bound & _guard_names(func):
                    continue

            # ``_is_money_call`` accepts only an ``Attribute`` func, so this
            # always holds. Kept as an assert rather than a silent fallback:
            # if the predicate is ever widened, this fails loudly instead of
            # mislabelling the offender line.
            func = node.func
            assert isinstance(func, ast.Attribute)
            method = func.attr
            offenders.append(f"{rel}:{lineno} (.{method})")

    assert not offenders, (
        "Money-mutator call sites whose result is neither checked nor "
        f"waived ({len(offenders)}):\n  " + "\n  ".join(offenders) + "\n"
        "Bind the result and test it (`wallet = await ...; if wallet is "
        "None: ...`) or, after an explicit audit, mark the call line with "
        f"`# {ALLOW_MARKER} (<reason>)`."
    )


def _raw_wallet_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, method)`` for every wallet write made straight at the repo.

    ``EconomyService.credit(..., type=...)`` books its own ledger row, so a
    call carrying ``type=`` is already paired and is not a raw write.

    Deliberately NOT ``_is_money_call``: ``EconomyService.set_balance``
    takes ``admin_id``, not ``type=``, so folding the scoped family in
    wholesale would read every admin balance edit as an unbooked raw
    write. Only the four primitives that genuinely carry ``type=`` on the
    service side belong here — ``credit``/``debit`` on any receiver, and
    the #238 escrow pair on an economy receiver.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        if attr in _ESCROW_MONEY_METHODS:
            if not _has_economy_receiver(node):
                continue
        elif attr not in _BROAD_MONEY_METHODS:
            continue
        if any(kw.arg == "type" for kw in node.keywords):
            continue
        found.append((node.lineno, attr))
    return found


def _is_ledger_write(node: ast.AST) -> TypeGuard[ast.Call]:
    """True for ``<ledger>.record(...)`` — the transactions-row write.

    ``TypeGuard`` for the same reason as :func:`_is_money_call`, and for
    symmetry with it: today the only caller wraps it in ``any(...)`` and
    needs no narrowing, but a future caller that reads ``lineno`` off an
    accepted node should not have to rediscover the fix.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "record":
        return False
    receiver = ast.unparse(node.func.value).lower()
    return any(hint in receiver for hint in _LEDGER_RECEIVER_HINTS)


def test_service_and_repo_wallet_writers_are_distinguishable_by_type_kwarg() -> None:
    """Precondition for the guard below: only the *service* takes ``type=``.

    The ledger-pairing test tells a booked write from a raw one by the
    presence of a ``type=`` keyword. If that stops being true — a renamed
    parameter, a default added to the repo — the guard would quietly wave
    everything through, so pin the signatures here instead.
    """
    import inspect

    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.economy_service import EconomyService

    for method in ("credit", "debit", "hold", "release"):
        service_params = inspect.signature(getattr(EconomyService, method)).parameters
        repo_params = inspect.signature(getattr(EconomyRepo, method)).parameters
        assert "type" in service_params, (
            f"EconomyService.{method} lost its `type` parameter — the ledger "
            "guard below can no longer tell a booked write from a raw one."
        )
        assert "type" not in repo_params, (
            f"EconomyRepo.{method} grew a `type` parameter — the ledger guard "
            "below would now read raw repo writes as already booked."
        )


def test_every_module_with_a_raw_wallet_write_books_a_ledger_row() -> None:
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if LEDGER_ALLOW_MARKER in source:
            continue
        tree = ast.parse(source)
        raw = _raw_wallet_writes(tree)
        if not raw:
            continue
        if any(_is_ledger_write(node) for node in ast.walk(tree)):
            continue
        rel = path.relative_to(SRC_ROOT).as_posix()
        offenders.extend(f"{rel}:{lineno} (.{method})" for lineno, method in raw)

    assert not offenders, (
        "Wallet writes that move coins straight at the repo, in a module "
        f"that books no ledger row ({len(offenders)}):\n  " + "\n  ".join(offenders) + "\n"
        "Write the companion `transactions` row (`await "
        "transactions_repo.record(from_id=..., to_id=..., amount=..., "
        "type=..., reason=...)`) next to the wallet write, or route the "
        "move through `EconomyService`, which books it for you. After an "
        f"explicit audit, mark the module with `# {LEDGER_ALLOW_MARKER} "
        "(<reason>)`."
    )
