"""Test surface for the safe arithmetic evaluator.

The contract is "number-or-None, never raises", so the test matrix is
two-axis: each happy-path case asserts an exact numeric value (no
floating-point fuzz tolerance — these all have exact float reps); each
rejection case asserts ``is None``. We deliberately avoid asserting the
specific error reason that triggered None — callers don't see it, so
locking the test to a particular branch would just create churn when
the internal control flow changes.

The security cases (attribute access, name lookup, function call,
comprehension) are the load-bearing ones: if one of these starts
returning a number, the calculator has become an arbitrary-code-exec
vector. Keep them.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.utils.calc import (
    MAX_CALC_EXPR_LENGTH,
    MAX_CALC_RESULT_ABS,
    safe_calc,
)


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("2+2", 4.0),
        ("2 + 2", 4.0),
        ("10*3", 30.0),
        ("10/4", 2.5),
        ("10//3", 3.0),
        ("10%3", 1.0),
        ("2**8", 256.0),
        ("-5", -5.0),
        ("+5", 5.0),
        ("(1+2)*3", 9.0),
        ("2,5+2,5", 5.0),  # Russian decimal separator — load-bearing.
        ("0.1+0.2", pytest.approx(0.3)),
    ],
)
def test_happy_path(expr: str, expected: float | object) -> None:
    result = safe_calc(expr)
    assert result is not None
    # ``pytest.approx`` is for the 0.1+0.2 case only; the rest are
    # exact-float-representable.
    if isinstance(expected, float):
        assert result == expected
    else:
        assert result == expected


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "   ",
        "1/0",  # ZeroDivisionError
        "1%0",  # ZeroDivisionError (modulo)
        "1//0",  # ZeroDivisionError (floor div)
        "abc",  # SyntaxError
        "1+",  # SyntaxError
        "(1+2",  # SyntaxError
        "1**1000",  # Pow exp cap
        "1e10**3",  # Pow base cap
        "2" * (MAX_CALC_EXPR_LENGTH + 1),  # Length cap
    ],
)
def test_rejects_invalid_or_unsafe(expr: str) -> None:
    assert safe_calc(expr) is None


@pytest.mark.parametrize(
    "expr",
    [
        # ----- the security-critical set: arbitrary code exec vectors -----
        "__import__('os')",  # function call
        "open('/etc/passwd')",  # function call
        "().__class__",  # attribute access
        "x",  # name lookup
        "[1,2,3]",  # list literal
        "{1:2}",  # dict literal
        "[i for i in range(3)]",  # comprehension
        "lambda: 1",  # lambda
        "1 if True else 0",  # ternary
        "1 < 2",  # comparison
        "True and False",  # bool op
        "1 & 2",  # bitwise — explicitly NOT in the whitelist
        "1 | 2",
        "1 ^ 2",
        "~1",  # bitwise not
        "True + True",  # bool literal — must be rejected even though int-compatible
    ],
)
def test_rejects_dangerous_constructs(expr: str) -> None:
    assert safe_calc(expr) is None


def test_result_cap_returns_none() -> None:
    # ``9 ** 9`` = 387 420 489 — under the cap, fine.
    assert safe_calc("9**9") == 387_420_489.0
    # ``1e10 * 1e6`` = 1e16 — over the 1e15 cap.
    assert safe_calc("1000000*10000000000") is None


def test_max_result_constant_matches_doc() -> None:
    # Guard against future tuning silently breaking the documented
    # contract in the module docstring.
    assert MAX_CALC_RESULT_ABS == 1e15


def test_never_raises_for_any_string() -> None:
    """Defensive sweep — try a grab-bag of malformed inputs to confirm
    the contract holds end-to-end. If any of these raises, a caller
    that wraps ``safe_calc`` in ``try: ... except None`` would crash
    in production."""
    for expr in [
        "\x00",
        "\n\n",
        "🧮",
        "1 + " * 50,
        "((((((((",
        "1" * 1000,
        "1.7976931348623157e308 * 2",  # would overflow to inf
    ]:
        # The contract is "returns or returns None"; the actual value
        # doesn't matter for this test, only that no exception escapes.
        safe_calc(expr)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        # The headline case: 123456789012345678 does not fit a double, so
        # an early ``float()`` on the literal rounds it to
        # 1.2345678901234568e17 and the remainder comes out 0.0 instead
        # of 8. Legacy returned the constant unconverted (bot.py:17576)
        # and floated once at the end (bot.py:17597).
        ("123456789012345678 % 10", 8.0),
        ("123456789012345678 % 1000", 678.0),
        ("9007199254740993 - 9007199254740992", 1.0),
    ],
)
def test_integer_literals_keep_full_precision(expr: str, expected: float) -> None:
    """Operands wider than 2**53 must be evaluated as ints (#503)."""
    assert safe_calc(expr) == expected


def test_result_is_always_a_float() -> None:
    """The single conversion happens on the way out, not on the way in.

    Callers render the answer into a Telegram message and legacy always
    handed them a float (bot.py:17597); keeping ints internal must not
    change the type of the returned value.
    """
    for expr in ["2+2", "5//2", "7 % 3", "-4"]:
        result = safe_calc(expr)
        assert isinstance(result, float)


def test_a_long_flat_sum_is_refused_by_length_not_by_depth() -> None:
    """#1076: the two caps are sized to bind at the same place.

    A flat sum parses left-deep — one nesting level per term — so before
    the fix ``_MAX_DEPTH`` refused at 33 terms (65 characters) while the
    module advertised a 256-character ceiling that nothing could reach.
    The ceiling is now real: 128 single-digit terms is 255 characters and
    evaluates; 129 is 257 and is refused by the length cap.
    """
    ok = "+".join(["1"] * 128)
    assert len(ok) == 255
    assert len(ok) <= MAX_CALC_EXPR_LENGTH
    assert safe_calc(ok) == 128.0

    too_long = "+".join(["1"] * 129)
    assert len(too_long) > MAX_CALC_EXPR_LENGTH
    assert safe_calc(too_long) is None


def test_a_realistic_shopping_list_evaluates() -> None:
    """The regression the raise was actually for (#1076).

    Twelve prices in parentheses is 50 characters, not the 100 the old
    comment claimed — but at 12 terms it cleared the old depth cap too,
    so this is a guard against a future tightening, not a re-fix.
    """
    assert safe_calc("(100+250+399+1250+75+3400+220+180+990+45+1600+730)") == 9239.0


def test_parentheses_cost_no_depth() -> None:
    """Parentheses group; they are not AST nodes (#1076).

    The module docstring used to justify the depth cap with a
    ``((((...))))`` ladder. ``ast.parse("((((1))))")`` is a single
    ``Constant``, so that ladder was never what the cap defended against
    and a deep one evaluates fine — bounded only by the length cap.
    """
    assert safe_calc("(" * 120 + "1" + ")" * 120) == 1.0


def test_a_unary_chain_is_refused_by_depth() -> None:
    """The shape that genuinely out-nests its own length (#1076).

    ``"-" * 200 + "1"`` parses 201 levels deep from 201 characters, so
    it is inside the length cap and outside the depth cap. This is what
    ``_MAX_DEPTH`` is for, and why raising it did not remove it.
    """
    assert safe_calc("-" * 200 + "1") is None
    assert safe_calc("-" * 4 + "1") == 1.0
