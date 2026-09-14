"""#2006: the shop may only offer what the effect planner can activate.

``plan_effect_application`` classifies a catalog row into an effect
kind, and anything it has no branch for falls to ``UNKNOWN``. Its own
docstring records what that costs the buyer: the use service refuses
BEFORE consuming, so an UNKNOWN row someone already paid for can never
be used, and nothing in the bot can refund it. Production sells one
such SKU — ``legend``, seeded at 2000 coins by legacy
``init_default_items`` and dispatched only by the legacy process that
T-011 removed. ``/buy`` took the money and wrote the row.

#2005 made the inventory card admit this after the fact.
:data:`ACTIVATABLE_ITEM_TYPES` removes the "after": the catalog repo
hides those rows and ``PurchaseService`` refuses them before any write.

Which leaves one thing to guard — that the set and the planner stay
the same answer. They live apart on purpose (a repository may not
import a service), so nothing in the type system connects them: adding
a branch to the planner without adding its string here would leave a
working item unsellable, and the reverse would put the money back at
risk. The planner is the authority, so the set is checked against its
branches rather than the other way round.
"""

from __future__ import annotations

import ast
from pathlib import Path

from telegram_invite_bot.core.entities.shop import ACTIVATABLE_ITEM_TYPES, PurchaseStatus

SRC = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
PLANNER = SRC / "services" / "inventory_use_planner.py"
SHOP_HANDLERS = SRC / "handlers" / "shop.py"


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} no longer defines {name}() — this guard is out of date")


def _dispatched_types() -> set[str]:
    """Every ``item_type == "..."`` the planner branches on.

    The planner reads ``item.type`` into a local named ``item_type``
    once and compares that local; matching the name rather than the
    attribute keeps an unrelated ``x.type == "..."`` elsewhere in the
    module out of the answer.
    """
    dispatched: set[str] = set()
    for node in ast.walk(_function(PLANNER, "plan_effect_application")):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "item_type"):
            continue
        if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
            continue
        compared = node.comparators[0]
        if isinstance(compared, ast.Constant) and isinstance(compared.value, str):
            dispatched.add(compared.value)
    return dispatched


def test_the_sellable_types_are_exactly_the_ones_the_planner_dispatches() -> None:
    dispatched = _dispatched_types()
    assert dispatched, (
        "no ``item_type == '...'`` branches found in plan_effect_application."
        " Either the dispatch was rewritten (a match statement, a lookup"
        " table) or the extraction broke; until this reads the real"
        " branches again it is asserting nothing, which is worse than"
        " failing"
    )

    unsellable = dispatched - ACTIVATABLE_ITEM_TYPES
    assert not unsellable, (
        "the planner can activate these types but the shop will not sell"
        f" them: {sorted(unsellable)}. A working item is hidden from"
        " /shop and refused by /buy. Add each string to"
        " ACTIVATABLE_ITEM_TYPES in core/entities/shop.py"
    )

    phantom = ACTIVATABLE_ITEM_TYPES - dispatched
    assert not phantom, (
        f"the shop sells these types and the planner cannot activate any"
        f" of them: {sorted(phantom)}. This is the #2006 failure itself —"
        " the buyer pays, the use flow refuses before consuming, and the"
        " coins are gone with no refund path. Either add a planner"
        " branch or drop the string from ACTIVATABLE_ITEM_TYPES"
    )


def test_both_buy_surfaces_render_every_purchase_status() -> None:
    """The other half of #2006: a new status must not render silence.

    ``handle_buy`` is an ``if/elif`` chain with no trailing ``else``, so
    unlike ``handle_shop_buy_confirm`` it has no ``Never`` check to fail
    on — a status nobody wrote a branch for would simply reply nothing
    and log the purchase as processed. That is how a refusal turns into
    a user staring at a bot that ignored them, and it is a live hazard
    now that :class:`PurchaseStatus` has grown a member for the first
    time since the enum was written.
    """
    expected = {member.name for member in PurchaseStatus}
    for function_name in ("handle_buy", "handle_shop_buy_confirm"):
        handled = {
            node.attr
            for node in ast.walk(_function(SHOP_HANDLERS, function_name))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "PurchaseStatus"
        }
        missing = expected - handled
        assert not missing, (
            f"handlers/shop.py:{function_name}() has no branch for"
            f" PurchaseStatus.{', PurchaseStatus.'.join(sorted(missing))}."
            " Every status reaches a user, so every status needs copy —"
            " an unhandled one is a silent reply"
        )
