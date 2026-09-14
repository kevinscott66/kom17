"""Safe arithmetic evaluator — pure-functional, no dependencies.

Extracted verbatim (in behavior) from legacy ``_fallback_safe_calc``
(bot.py:17560-17604). The legacy code had a
``simpleeval``-based fast path that this module deliberately omits — the
new pipeline does not take that dependency, and the AST walker matches
``simpleeval``'s behavior on the operator set we expose. If a /calc port
later needs the exact ``simpleeval`` semantics (e.g. for some edge case
operator precedence we missed), a thin wrapper can be added; for now the
single, dependency-free path is the simpler contract.

Why extract this now, ahead of the ``/calc`` handler port: the function
is pure (no I/O, no globals, no module-level state), so it can land and
be tested standalone. When the handler port follows, it imports
``safe_calc`` and focuses purely on the message-flow / AI-runner wiring
without re-deriving the security model around ``eval``-style input.

Security stance — what's safe and why
-------------------------------------
The whole reason this helper exists rather than ``eval(expr)``:

* **AST whitelist** — only ``BinOp``, ``UnaryOp``, ``Constant`` of
  ``int|float``, and ``Expression`` nodes parse. Attribute access, name
  lookups, calls, comprehensions, subscripts, and lambdas all raise
  ``ValueError`` before any evaluation. A user can't reach
  ``os.system`` because ``os`` cannot be named.
* **Operator whitelist** — ``+ - * / // % **`` plus unary ``+ -``. No
  bitwise, no boolean, no comparison. ``Pow`` is further constrained
  (see below).
* **Pow caps** — ``base`` capped at ``1e6`` and ``exp`` at ``10`` in
  absolute value. Without this, ``9**9**9`` would burn CPU and memory
  for seconds and DoS the worker thread.
* **Depth cap** — ``_MAX_DEPTH`` bounds the recursion. Defends against
  a ladder that would otherwise blow Python's default recursion limit
  and raise an unrelated ``RecursionError`` that callers would have to
  special-case. Note that ``((((...))))`` is NOT that ladder and never
  was (#1076): parentheses only group, they produce no AST node at all,
  so ``ast.parse("((((1))))")`` is one ``Constant`` at depth 1. The
  shape that actually nests is a chain of operators — ``-`` repeated,
  or a long flat sum, which parses left-deep and so costs one level
  per term.
* **Result cap** — ``MAX_CALC_RESULT_ABS`` (1e15) clamps the output;
  larger values return ``None``. This matters for downstream — we
  ship the result into a Telegram message, and 1e308 has no display
  value.
* **Input length cap** — ``MAX_CALC_EXPR_LENGTH`` (256) on the raw
  string. ``ast.parse`` on a 1MB input is itself a CPU sink.
* **NaN guard** — explicit ``result != result`` check. ``float("nan")``
  cannot reach us (no name lookups) but ``0/0`` via FloorDiv on floats
  in some Python versions can; cheaper to guard than to audit.

What ``None`` means
-------------------
Every error path collapses to ``None``. Callers want a single signal
("did this produce a usable number?") not a typed exception hierarchy —
the legacy code already treated all failures as "show the user the
calc-hint message", and this module preserves that contract.

Comma handling
--------------
Legacy accepts ``2,5`` as ``2.5`` (Russian decimal separator). Kept,
because the RU users who originally added it are the majority and
``ast.parse`` doesn't accept ``2,5`` natively (it parses as a tuple).
"""

from __future__ import annotations

import ast
import operator
from typing import Final

# Legacy's ceiling was 100 (bot.py:716, verified). Raised deliberately:
# the length cap is not what makes this module safe — the node
# whitelist, ``_MAX_DEPTH`` and ``_MAX_POW_EXP``/``_MAX_POW_BASE`` bound
# the work per expression regardless of length, and ``ast.parse`` on 256
# characters is microseconds. ``MAX_CALC_RESULT_ABS`` below IS legacy's
# number verbatim (bot.py:717) — that one is about the answer, not the
# input, so there is no reason to move it.
#
# #1076: the raise was previously inert, and the reasoning given for it
# was wrong twice over. "A parenthesised sum of a dozen prices reaches
# 100 characters" is false — twelve four-digit prices in parentheses is
# 50 characters. And a flat sum parses left-deep, one nesting level per
# term, so ``_MAX_DEPTH`` (then 32) refused at 33 terms — 65 characters.
# The advertised 256-character ceiling could not be reached by the only
# expression shape long enough to approach it; the real ceiling was a
# quarter of it, and neither number was written down anywhere.
#
# The two are now sized to bind together: 128 single-digit terms joined
# by ``+`` is 255 characters, so for a flat sum — the realistic long
# expression — the length cap is what refuses, exactly as this comment
# claims. ``_MAX_DEPTH`` stays a real guard for the one shape that
# out-nests its own length, a unary chain (``"-" * 200 + "1"`` parses
# 201 levels deep), and 128 frames is comfortably inside CPython's
# default 1000-frame recursion limit even under a deep aiogram stack.
MAX_CALC_EXPR_LENGTH: Final[int] = 256
MAX_CALC_RESULT_ABS: Final[float] = 1e15
_MAX_POW_EXP: Final[int] = 10
_MAX_POW_BASE: Final[float] = 1e6
_MAX_DEPTH: Final[int] = 128

# Static dispatch table from AST node class to the Python operator that
# implements it. Members are restricted to the safe arithmetic subset —
# extending this set is the only place where a new attack surface can
# open, so keep it small and reviewable.
_BIN_OPS: Final[dict[type[ast.operator], object]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY_OPS: Final[dict[type[ast.unaryop], object]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval(node: ast.AST, depth: int) -> float | int:
    if depth > _MAX_DEPTH:
        raise ValueError("expression too deeply nested")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        # bool is a subclass of int in Python — exclude explicitly so
        # ``True + True`` doesn't quietly evaluate to 2. Bools shouldn't
        # be reachable from a parsed numeric literal anyway, but the
        # check is cheap and documents intent.
        if isinstance(node.value, bool):
            raise ValueError("bool literal not allowed")
        # Returned UNCONVERTED, exactly like legacy (bot.py:17576).
        # An early ``float()`` here silently loses precision above
        # 2**53, and the loss is visible in the answer, not just in
        # the last digit: ``123456789012345678 % 10`` is 8 on ints
        # and 0.0 once the operand has been rounded to a double.
        # Legacy converted once, at the point of return
        # (bot.py:17597), and :func:`safe_calc` does the same.
        return node.value
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type is ast.Pow:
            base = _eval(node.left, depth + 1)
            exp = _eval(node.right, depth + 1)
            if abs(exp) > _MAX_POW_EXP or abs(base) > _MAX_POW_BASE:
                raise ValueError("pow argument out of range")
            return base**exp
        fn = _BIN_OPS.get(op_type)
        if fn is None:
            raise ValueError(f"binop {op_type.__name__} not allowed")
        return fn(_eval(node.left, depth + 1), _eval(node.right, depth + 1))  # type: ignore[operator,no-any-return]
    if isinstance(node, ast.UnaryOp):
        fn = _UNARY_OPS.get(type(node.op))
        if fn is None:
            raise ValueError(f"unaryop {type(node.op).__name__} not allowed")
        return fn(_eval(node.operand, depth + 1))  # type: ignore[operator,no-any-return]
    raise ValueError(f"node {type(node).__name__} not allowed")


def safe_calc(expr: str) -> float | None:
    """Evaluate ``expr`` as a safe arithmetic expression.

    Returns the numeric result on success, or ``None`` on any error
    (invalid syntax, disallowed construct, division by zero, overflow,
    NaN, result outside the displayable range). Never raises.

    The deliberately-narrow surface — number in, number-or-None out —
    means callers don't have to decide what to do per-exception. The
    legacy handler already collapsed every error to "show the hint
    message"; preserving that semantics keeps the eventual port a
    one-line drop-in.

    See the module docstring for the security model.
    """
    cleaned = (expr or "").strip().replace(",", ".")
    if not cleaned or len(cleaned) > MAX_CALC_EXPR_LENGTH:
        return None
    try:
        tree = ast.parse(cleaned, mode="eval")
        # Single conversion at the point of return — see the
        # ``ast.Constant`` branch of :func:`_eval` and bot.py:17597.
        # Inside the ``try`` because ``float()`` on an int wider than
        # a double raises ``OverflowError``, which legacy also caught.
        result = float(_eval(tree, 0))
    except (ZeroDivisionError, OverflowError, ValueError, SyntaxError, TypeError):
        return None
    # NaN check — ``float("nan") != float("nan")`` is the canonical
    # detector and avoids importing ``math`` for one line.
    if result != result:  # noqa: PLR0124 — intentional NaN test
        return None
    if abs(result) > MAX_CALC_RESULT_ABS:
        return None
    return result
