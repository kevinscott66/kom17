"""``/calc`` — safe arithmetic evaluator.

Port of legacy ``cmd_calc`` (bot.py:17607). Behaviour delta:

* Legacy fed the result into an AI-narration pipeline (``_run_ai_request``)
  to wrap "2+2 = 4" in a short ChatGPT-style quip. The AI pipeline itself
  is not yet ported (T-021), and porting /calc behind that dependency
  would block this lift indefinitely. We ship the plain result now —
  "🧮 ``2+2`` = ``4``" — and re-introduce AI flavouring in T-021 when the
  shared narrator lands.
* The ``safe_calc`` core (utils/calc.py) is already extracted verbatim
  from legacy ``_fallback_safe_calc``; this handler is a thin shell on
  top.
* Legacy gates groups via ``require_group_feature(..., "ai", ...)``.
  The "ai" feature flag table doesn't exist in the new pipeline. Without
  the AI narrator the gate has no behavioural meaning, so we accept
  groups directly — the worst case is a calculator running where an
  admin disabled "ai", which is harmless arithmetic on a public chat.
* Legacy issues ``try_delete_message`` against the user's command.
  Skipped — requires bot-admin in groups (would 400) and is a clean-chat
  cosmetic that's invisible in private DMs.

Bare ``/calc`` with no argument renders the hint. The hint mentions both
example forms (``2+2``, ``10*3``) so a user who typed /calc by accident
gets actionable guidance, not a silent no-op.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import command_body, require_from_user
from telegram_invite_bot.utils.calc import safe_calc

log = logger.bind(component="handlers.calc")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService


def _format_result(value: float) -> str:
    """Render a numeric result the way users expect.

    ``safe_calc`` returns ``float`` for everything, including operations
    whose mathematical result is integral (``2+2`` → ``4.0``). Showing
    ``4.0`` for that is noisy; collapse to ``"4"`` when the float happens
    to be an exact integer. Non-integral results keep their fractional
    part as Python formats it (no fixed-precision rounding — that would
    discard legitimate digits like ``1/3``).
    """
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


async def handle_calc(
    message: Message,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)
    user = await user_service.touch(tg_user)
    # #1983: end the bookkeeping transaction here rather than hold
    # ``users.db``'s single writer slot across the reply below. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    parts = command_body(message).strip().split(maxsplit=1)
    expr = parts[1].strip() if len(parts) > 1 else ""

    result = safe_calc(expr) if expr else None
    if result is None:
        await message.reply(t("h_calc_hint", user.language))
        return

    # parse_mode is HTML at app level — escape the echoed expression in
    # case a user pastes something with literal ``<`` (e.g. nobody types
    # that in arithmetic, but the cheap guard documents the envelope).
    safe_expr = html.escape(expr)
    rendered = _format_result(result)
    await message.reply(
        t("h_calc_result", user.language, expr=safe_expr, result=rendered),
    )
    log.bind(uid=user.user_id, expr_len=len(expr)).info("/calc rendered")


def build_router() -> Router:
    """Aliases mirror legacy registration (bot.py:17607).

    No ``magic=~F.args`` escape valve here — unlike /time, the legacy
    /calc has no separate arg-vs-bare-form geocoder branch. Bare /calc
    renders the hint, /calc <expr> evaluates. Both routes here.
    """
    router = Router(name="calc")
    router.message.register(
        handle_calc,
        Command("calc", "калькулятор", "kom_calc", ignore_case=True),
        F.from_user,
    )
    return router
