"""Shop inline-keyboard CallbackData factories (Stage 24).

Three wire formats live here, all rendered by ``handlers/shop`` and
all consumed by the same router. Splitting prompt/confirm/cancel into
three classes (rather than one ``ShopAction`` discriminated by an
``action`` field) is deliberate: each step has a different field
contract — the cancel step needs no item_id (the inline keyboard's
context message already says which item was being considered), and
folding all three into one class would force a synthetic ``item_id=0``
sentinel on the cancel button that obscures the wire intent. Three
classes, three prefixes, one ``CallbackData.filter()`` registration
per handler — and a prefix-conflict audit reduces to one grep over
this package.

Prefix selection
---------------
* ``shop_buy``   — the "buy" inline button under each /shop row.
* ``shop_conf``  — the "Confirm" button on the confirmation card.
* ``shop_cxl``   — the "Cancel" button on the confirmation card.

None of the three collide with each other under aiogram's
:meth:`CallbackData.filter` (which matches the first ``:``-delimited
segment exactly, not as a prefix), and none collide with the legacy
literal ``"shop_buy_<id>"`` either: legacy emits no colon, so an
aiogram filter for prefix ``shop_buy`` sees first-segment
``shop_buy_42`` ≠ ``shop_buy`` and falls through. That fall-through
is what the Stage 23 strangler-bridge regression pin in
``tests/e2e/handlers/test_support.py::test_unrelated_callback_prefix_falls_through``
locks in — keep ``shop_buy`` distinct from any literal the legacy
ladder emits.

Field shape
-----------
``ShopBuyPrompt`` and ``ShopBuyConfirm`` both carry ``item_id: int``.
Telegram's 64-byte cap on callback_data leaves ``shop_buy:<int>`` and
``shop_conf:<int>`` comfortable headroom (10 + 1 + 19 = 30 bytes for
a max int64). ``ShopBuyCancel`` carries no fields — the cancel toast
is identical regardless of what was being bought, and stamping the
``item_id`` into it would only matter for analytics, which lives at
the log line, not the wire.

RR-2 #14 added a trailing ``group_id: int = 0`` to ``ShopBuyPrompt`` /
``ShopBuyConfirm`` / ``ShopPage`` so a group-scoped purchase survives
every hop without server-side state (legacy kept the selection in a
process-global ``_shop_selected_group`` dict, bot.py:13094 — lost on
restart and shared across concurrent flows). ``shop_buy:12:-100…`` is
still only ~27 bytes.

Note the deploy-window consequence of adding a field: aiogram's
``unpack`` is strict about the segment count, so a ``/shop`` card
rendered by the *previous* build packs one segment too few and its
buttons stop matching the filter (``CallbackData.filter`` swallows the
``ValueError`` and falls through). The click becomes a silent no-op on
a stale card until the user re-runs ``/shop``. Accepted: these cards
are short-lived private-chat messages, and the alternative — a parallel
legacy-shaped factory kept alive forever — is a permanent tax for a
one-deploy annoyance.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class ShopBuyPrompt(CallbackData, prefix="shop_buy"):
    """The "🛒 Купить" button rendered next to each /shop row.

    Click → handler edits the message in place into a confirmation
    card with two further buttons (:class:`ShopBuyConfirm` /
    :class:`ShopBuyCancel`). No mutation at this step — purely a
    confirmation prompt, which mirrors the legacy two-step flow
    (``bot.py``'s ``shop_buy_<id>`` ladder branch renders an inline
    keyboard with ``shop_confirm_buy_<id>`` and ``shop_cancel_buy``).
    """

    item_id: DbInt
    group_id: DbInt = 0


class ShopBuyConfirm(CallbackData, prefix="shop_conf"):
    """The "Подтвердить" button on the confirmation card.

    Click → handler invokes :class:`PurchaseService` for an atomic
    debit + stock decrement + inventory insert, then edits the
    confirmation card into a success / failure card. ``item_id`` is
    re-asserted on the wire (rather than read from FSM state) because
    a stateless callback is one less moving part — clicking an old
    confirmation card the user scrolled back to still resolves the
    exact item that was being bought, without any storage layer.
    """

    item_id: DbInt
    group_id: DbInt = 0


class ShopBuyCancel(CallbackData, prefix="shop_cxl"):
    """The "Отмена" button on the confirmation card.

    No fields — see module docstring. The handler edits the card to a
    brief "Cancelled" line and removes the keyboard so a re-click
    can't fire the cancel toast twice.
    """


class ShopPage(CallbackData, prefix="shop_pg"):
    """The « prev / page N/M / next » buttons on the /shop nav row (Stage 25).

    Stage 24 capped the inline keyboard at 8 buttons and noted the
    page-flip callbacks would land in a separate stage; this is that
    stage. ``page`` is a 0-based index into the price-ASC catalog
    listing; the handler clamps it to ``[0, M-1]`` on every click so a
    stale callback against a shrunk catalog (admin pulled rows between
    renders) snaps to the new last page instead of rendering an empty
    body.

    Same-page clicks (the middle "page N/M" indicator) reuse this
    prefix with the current page number — the handler detects the
    no-op shape, answers the callback silently, and skips the edit so
    Telegram doesn't reject the "message is not modified" attempt and
    poison the dispatcher.

    Pagination state lives entirely on the wire (``shop_pg:7`` is 11
    bytes — same headroom posture as ShopBuyPrompt). No server-side
    per-user cursor: the same /shop message can be navigated by anyone
    in a multi-user chat, matching the /faq continue button's posture
    from Stage 23.

    Prefix ``shop_pg`` is distinct from ``shop_buy`` / ``shop_conf`` /
    ``shop_cxl`` under aiogram's exact-first-segment filter match —
    same conflict audit as the other three shop classes.
    """

    page: DbInt
    group_id: DbInt = 0


class ShopGroupPick(CallbackData, prefix="shop_grp"):
    """A row on the "buy for which group?" chooser (RR-2 #14).

    ``group_id`` is the chat the purchase should be credited to, or
    ``0`` for a plain global purchase ("🛒 Без группы"). Legacy used the
    same ``0``-means-global sentinel on the wire (``shop_group_0``,
    bot.py:24066-24078) — the difference is that legacy *trusted* the id
    it received and stashed it in a process-global dict, so anyone could
    hand-craft ``shop_group_<any chat>`` and route another group's
    rating points and its owner's payout wherever they liked. Here the
    id is re-verified against ``bot_groups.added_by`` on EVERY step that
    consumes it (chooser click, buy prompt, confirm) — the wire carries
    a *claim*, never an authorization.

    ``shop_grp:-1001234567890`` is 23 bytes, well inside the 64-byte cap.
    """

    group_id: DbInt


class ShopGroupMenu(CallbackData, prefix="shop_grpm"):
    """The "🔁 Сменить группу" button on a group-scoped catalog page.

    No fields — the chooser is always rebuilt from the caller's current
    group list, so there is nothing to carry. Distinct prefix from
    ``shop_grp`` (aiogram matches the first segment exactly, so
    ``shop_grpm`` and ``shop_grp`` never cross-fire).
    """


# ── Stage 26: /inventory pagination + inspect ──────────────────────────


class InventoryPage(CallbackData, prefix="inv_pg"):
    """The « prev / page N/M / next » buttons on /inventory's nav row.

    Mirrors :class:`ShopPage` in shape (single ``page`` field) but
    carries a separate prefix so a stray /shop nav click against an
    /inventory message — and vice-versa — fails the filter rather
    than rendering the wrong list. Same 64-byte budget; ``inv_pg:7``
    is 9 bytes.

    Authorization is enforced at handler time, not in the wire format:
    the page-flip handler re-reads ``inventory_repo.list_for_user(
    callback.from_user.id, ...)``, so a forwarded /inventory message
    clicked by user B renders B's inventory (which is probably empty),
    not A's. That posture is intentional — the alternative (binding
    the owning user_id into the wire payload) would let a future bug
    that drops the from_user check still leak A's list to B.
    """

    page: DbInt


class InventoryInspect(CallbackData, prefix="inv_ins"):
    """The "🔍 {item_name}" button on each /inventory row.

    Carries the inventory PK (``entry_id``) only; the wire payload is
    deliberately NOT scoped to a user_id. Authorization lives in
    :meth:`InventoryRepo.get_for_user`, which the handler calls with
    ``(callback.from_user.id, entry_id)``. A user B who hand-crafts
    ``inv_ins:<A's entry>`` reaches the repo with
    ``user_id == B.id`` and gets ``None`` back — the same toast a
    legitimate "entry was deleted" lookup would produce. Encoding the
    owning user_id into the payload would only let the handler short-
    circuit one millisecond earlier and would invite a future bug
    where the user_id field is trusted instead of verified.
    """

    entry_id: DbInt


class InventoryUse(CallbackData, prefix="inv_use"):
    """The "🎁 Use / Активировать" button on the inspect card (Stage 29).

    Click → handler invokes :class:`InventoryUseService.use` against the
    same single ``economy.db`` session the inspect render rode on, then
    edits the inspect card into a per-outcome result card (success line
    + Back button, or one of the rejection toasts inlined as the card
    body).

    Like :class:`InventoryInspect`, the wire payload carries only the
    inventory PK — NOT the owning user_id. Authorization lives in
    :meth:`InventoryUseService.use`'s composition of
    :meth:`InventoryRepo.get_for_user` (which constrains the SELECT to
    ``user_id == callback.from_user.id``). A hand-crafted
    ``inv_use:<A's entry>`` from user B reaches the service with
    ``user_id == B.id`` and gets back NOT_FOUND — the same outcome a
    legitimate "entry was deleted" attempt produces, so the toast
    leaks no information about whether the id belongs to someone else.

    Prefix ``inv_use`` is distinct from ``inv_ins`` / ``inv_bk`` /
    ``inv_pg`` under aiogram's exact-first-segment filter match — same
    audit posture as the other four inventory prefixes. The 64-byte
    payload budget is comfortable (``inv_use:<int64>`` ≤ 27 bytes).
    """

    entry_id: DbInt


class InventoryBack(CallbackData, prefix="inv_bk"):
    """The "🔙 Back" button under the inspect card → return to page 0.

    No fields. The inspect card is reached from somewhere in the
    paginated list, but "back to where you came from" requires either
    threading the source page through every callback or accepting
    that snapping to page 0 is a fine simplification. The latter is
    cheap and predictable; the user's recently-purchased items live
    at the top of the list anyway (newest-first ordering), so page 0
    is a sensible default re-entry point.
    """
