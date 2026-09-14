"""``/send`` handler — Stage 17 of the strangler migration.

The legacy ``cmd_send`` (``bot.py:18948``) is a ~150-line tangle
that handles three input forms (reply / @username / chat-member-
search), per-user anti-flood, group-feature gating, auto-delete
scheduling and the actual transfer. Stages 14-16 carved out the
typed pipeline behind this handler (TransferEffects bundle →
pure tax math → TransferService.send with full 8-step flow); this
handler ships the *narrowest* slice that consumes the new
pipeline end-to-end without forcing the still-unported edges.

Scope (Stages 17 → 18 → T-015)
------------------------------
* **Private + group chats.** T-015 extends ownership to groups —
  ``ensure_user_access`` / ``require_group_feature`` are still NOT
  ported (per-chat admin opt-in is a separate workstream), but the
  rate-limit middleware (Stage 19) and the transfer service itself
  are chat-agnostic, so the strangler can safely claim the group
  form. The group features legacy gated behind per-chat opt-in
  (auto-delete, ad-hoc spam shields) were left with it, and T-011
  removed legacy — so they are not deferred to another layer any
  more, they are simply absent: a chat admin who once switched
  ``/send`` off for their group has no switch here. Restoring or
  dropping that opt-in is an owner decision.
* **Three recipient forms:**
  1. Numeric user_id: ``/send <user_id> <amount> [comment]`` (Stage 17).
  2. ``@username``: ``/send @alice <amount> [comment]`` (Stage 18) —
     resolved via :class:`UsersRepo.get_by_username`.
  3. Reply form (T-015): a ``/send <amount> [comment]`` whose
     ``reply_to_message`` carries a non-bot ``from_user`` → recipient
     is that author. Works in both private (rare but legal) and
     groups (the legacy primary use site). Chat-member-search via
     ``bot.get_chat_member`` is NOT ported — Telegram only resolves
     a numeric id from a username after a prior interaction with
     the bot, and the UsersRepo path already covers everyone who
     ever hit /start; falling back to a live API call would add a
     network round-trip per /send for a vanishingly small "user
     in this group but never started a DM with the bot" cohort.
* **Sender_chat rejected** in-handler. Anonymous channel transfers
  are a money-laundering vector legacy refuses (``transfer_anon_disabled``)
  — we mirror.
* **Rate limit lives in a middleware, not inline.** Legacy's
  ``_check_transfer_rate_limit`` was an in-memory token bucket inside
  the handler. Here it is :class:`TransferRateLimitMiddleware`,
  mounted on this router by ``build_router`` below, so /send, /gift
  and any future /tip draw on one bucket table instead of three
  private ones.
* **No auto-delete.** Same posture as /daily: cosmetic concern,
  separate cross-cutting middleware later.

What we DO own end-to-end
-------------------------
1. Parse + validate ``<user_id> <amount> [comment]``. Surfaces
   typed errors before any DB I/O.
2. Resolve TransferEffects via :class:`EffectsService` (VIP-aware
   tax discount).
3. Call :meth:`TransferService.send` with the parsed args + effects.
4. Render one of N cards based on :class:`TransferOutcome` —
   success / self-transfer / invalid amount / sender wallet missing
   / recipient wallet missing / insufficient funds. The taxonomy
   is exhaustive at the service layer; we map each member to its
   own message so a future "merge two outcomes" refactor surfaces
   here as a missing branch (type checker won't help — StrEnum —
   so the dictionary lookup is the guard).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Literal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.middlewares.transfer_rate_limit import (
    TransferRateLimitMiddleware,
)
from telegram_invite_bot.services.transfer_service import TransferOutcome
from telegram_invite_bot.utils.aiogram import (
    command_args,
    mention_html,
    require_from_user,
)
from telegram_invite_bot.utils.numbers import format_number, parse_int_token

log = logger.bind(component="handlers.send")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.effects_service import EffectsService
    from telegram_invite_bot.services.transfer_service import TransferService


# Stage 20: copy lives in ``i18n/data/{ru,en}.yaml`` under the ``h_send_*``
# namespace — see ``handlers/daily.py`` for the namespacing rationale
# (handler-side HTML kept disjoint from the legacy ``send_*`` / ``transfer_*``
# Markdown keys imported from translations.py).


ParsedRecipient = (
    tuple[Literal["id"], int, int]
    | tuple[Literal["username"], str, int]
    | tuple[Literal["reply"], None, int]
)
"""Tagged-union result of :func:`_parse_args`.

A two-arm union (not a NamedTuple / dataclass) because the only
consumer is the handler one branch below, and a tagged tuple keeps
the parse-site call shape (``kind, recipient, amount = parsed``)
identical to the previous 2-tuple — minimal diff against Stage 17.
A dataclass would also force a module-level type the handler has
to import; the inline ``Literal`` tag travels with the value.
"""


_COMMENT_MAX = 200


def _extract_comment(raw: str, kind: str) -> str | None:
    """Pull the optional trailing comment out of the raw args (#16).

    The parser deliberately ignores the comment (it only needs the
    recipient + amount); we recover it here so the receipt and the
    recipient DM can echo it. The split depth mirrors each recipient
    form: the reply form spends one leading token on the amount, the
    explicit forms spend two (recipient + amount). HTML-escaping and
    the length cap happen at the render edge, not here.
    """
    if kind == "reply":
        parts = raw.split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""
    else:
        parts = raw.split(maxsplit=2)
        rest = parts[2] if len(parts) > 2 else ""
    rest = rest.strip()
    return rest[:_COMMENT_MAX] if rest else None


def _parse_args(raw: str, *, has_reply: bool = False) -> ParsedRecipient | None:
    """Return a tagged recipient + amount on success, ``None`` on parse error.

    Two recipient forms are recognised:

    * Numeric user_id form (Stage 17): ``/send 12345 100`` →
      ``("id", 12345, 100)``.
    * @username form (Stage 18): ``/send @alice 100`` →
      ``("username", "alice", 100)``. The leading ``@`` is stripped
      here so :meth:`UsersRepo.get_by_username` doesn't have to
      think about it; the repo strips again defensively.

    Username detection is "first character is ``@``", not "first
    token isn't a clean int", because ``/send 12abc 100`` should
    be rejected as malformed (current behaviour) rather than
    treated as a username — Telegram usernames have a strict
    alphanumeric+underscore charset, and the legacy resolver path
    similarly bails out on non-canonical input. Anything failing
    the numeric parse without the ``@`` prefix is a parse error,
    not a username.

    ``comment`` is deliberately ignored *by this parser* — it only
    needs recipient + amount. The handler recovers it separately via
    :func:`_extract_comment` and renders it on both the receipt and
    the recipient DM (HTML-escaped at the render edge).

    Rejects: missing fields, non-numeric user_id (no ``@`` prefix),
    non-numeric amount, zero/negative amount, bare ``@`` token,
    empty username after strip.

    #1691: every numeric token goes through
    :func:`~telegram_invite_bot.utils.numbers.parse_int_token`, not a
    bare ``int()`` in a ``try``. ``except ValueError`` catches the parse
    failure and nothing else, so a twenty-digit ASCII run parsed
    cleanly, was declared a valid recipient here, and raised
    ``OverflowError`` one layer down where SQLite binds it — the user
    saw the generic error card for an input this parser should have
    named. The helper carries the ``2**63 - 1`` ceiling the ``int()``
    did not.
    """
    parts = raw.split(maxsplit=2)
    # Reply form (T-015): ``/send <amount> [comment]`` with a
    # reply_to_message carrying the recipient. Explicit ``<id> <amount>``
    # or ``<@user> <amount>`` wins over the reply target — the user
    # typing two unambiguous tokens has clearly overridden the implicit
    # reply recipient (rule mirrors legacy ``cmd_send`` precedence at
    # bot.py:18948 — explicit args first, reply as fallback).
    if has_reply and len(parts) >= 1:
        amount_first = parse_int_token(parts[0]) or 0
        first_is_int = amount_first > 0
        # A shape probe, not a value: "is the second token another
        # number?" An out-of-range one answers no and is read as the
        # start of a comment, which is the safe way to be wrong here.
        second_is_int = len(parts) >= 2 and parse_int_token(parts[1], signed=True) is not None
        # Reply arm wins when:
        #   * single token AND it's an amount (``/send 100``), OR
        #   * first token is an amount AND second is NOT another int
        #     (``/send 100 thanks`` → amount + comment).
        # Two-int form (``/send 777 100``) falls through to the
        # explicit-id branch below.
        explicit_id_form = len(parts) >= 2 and not parts[0].startswith("@") and second_is_int
        if first_is_int and not explicit_id_form:
            return ("reply", None, amount_first)
    if len(parts) < 2:
        return None
    recipient_raw, amount_raw = parts[0], parts[1]
    amount = parse_int_token(amount_raw)
    if amount is None or amount <= 0:
        return None
    if recipient_raw.startswith("@"):
        username = recipient_raw[1:]
        if not username:
            return None
        return ("username", username, amount)
    # ``signed=True`` keeps the negative ids the numeric form has
    # always accepted; the ceiling is what is new.
    to_id = parse_int_token(recipient_raw, signed=True)
    if to_id is None:
        return None
    return ("id", to_id, amount)


async def handle_send(
    message: Message,
    command: CommandObject,
    bot: Bot,
    economy_repo: EconomyRepo,
    users_repo: UsersRepo,
    transfer_service: TransferService,
    effects_service: EffectsService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)

    # Anonymous (channel/chat-on-behalf-of) — refused inline at the
    # handler edge because the service operates on user_id and
    # doesn't see message metadata. Same posture as /daily.
    if message.sender_chat is not None:
        await message.answer(t("h_send_anonymous", lang))
        return

    # Auto-create sender wallet up front — gives us ``language`` for
    # the response templates and ensures the service-layer
    # NO_SENDER_WALLET branch is truly defensive (not a first-time
    # user race).
    sender_wallet = await economy_repo.get_or_create(tg_user.id)
    lang = sender_wallet.language or "ru"

    # Reply-target metadata (T-015) — captured up front so the parser
    # can choose the reply arm when the user typed only ``/send N``.
    # Bots are excluded: a /send-to-bot via reply is almost always a
    # typo (the user replied to the bot's previous message), and
    # legacy refuses such transfers via its bot-id blocklist.
    reply_msg = message.reply_to_message
    reply_target = None
    if reply_msg is not None and reply_msg.from_user is not None and not reply_msg.from_user.is_bot:
        reply_target = reply_msg.from_user.id

    raw = command_args(command)
    parsed = _parse_args(raw, has_reply=reply_target is not None)
    if parsed is None:
        # Distinguish "no args at all" from "bad args" by inspecting
        # what split saw. Both render the usage card — the help
        # text already shows the expected form, no value in a
        # second-tier error message here.
        if not raw or len(raw.split()) < 2:
            await message.answer(t("h_send_usage", lang))
        else:
            await message.answer(t("h_send_invalid_amount", lang))
        return

    comment = _extract_comment(raw, parsed[0])
    # Recipient display name, captured opportunistically per resolution
    # path so the receipt + DM read as a mention, not a bare id (#16).
    recipient_name: str | None = None
    if parsed[0] == "username":
        _, username, amount = parsed
        recipient = await users_repo.get_by_username(username)
        if recipient is None:
            # Same user-facing meaning as a missing wallet: we can't
            # find the person. NO_RECIPIENT_WALLET copy already says
            # "ask them to /start" — exactly the actionable fix here
            # (Telegram won't expose username→id without a prior
            # interaction with the bot anyway). Inventing a separate
            # "username not in DB" outcome would split a UX-identical
            # state into two codepaths for no caller benefit.
            await message.answer(t("h_send_no_recipient", lang))
            log.bind(uid=tg_user.id, username=username, outcome="username_unknown").info(
                "/send rejected"
            )
            return
        to_id = recipient.user_id
        recipient_name = recipient.first_name
    elif parsed[0] == "reply":
        # ``reply_target`` is always non-None here — the parser only
        # returns the ``reply`` arm when ``has_reply=True`` was set.
        assert reply_target is not None
        _, _, amount = parsed
        to_id = reply_target
        if reply_msg is not None and reply_msg.from_user is not None:
            recipient_name = reply_msg.from_user.first_name
    else:
        _, to_id, amount = parsed

    # R-FIX-004 / R-FIX-004-fp: reject bot recipients on EVERY
    # resolution path (numeric-id, @username, reply). Previously only
    # the reply arm filtered out bots — a malicious group admin could
    # advertise a bot's numeric id as "the prize wallet" and the
    # credit would land in a wallet the user can never recover from.
    #
    # Iteration 1 used a ``username.lower().endswith("bot")`` heuristic
    # on top of ``bot.get_chat``. That misclassified ANY human user
    # whose handle ends in ``"bot"`` as a substring (``MyCoolRobot``,
    # ``Doctorbot``, ``hellochatbot``) because Python's ``str.endswith``
    # is a literal-suffix check and ``"robot".endswith("bot") is True``.
    # BotFather's policy (bots MUST end in ``"bot"``) does NOT have a
    # converse rule (humans MAY also end in ``"bot"``), so the
    # heuristic is a false-positive trap.
    #
    # Replacement policy: only reject when we can prove the recipient
    # is a bot — either:
    #
    # * ``to_id == bot.id`` (this bot itself, short-circuit), OR
    # * ``bot.get_chat_member(chat_id, to_id).user.is_bot`` is True
    #   (authoritative ``User.is_bot`` field, but only available when
    #   the target is in the same chat the /send was issued in).
    #
    # If ``get_chat_member`` fails with ``TelegramAPIError`` (target
    # not in this chat, transient outage, …) we DO NOT fall back to a
    # username heuristic: ``Chat`` from ``getChat`` doesn't carry
    # ``is_bot`` and there's no other authoritative signal. We
    # fail-open on the rationale that the legacy ``BOT_USER_IDS``
    # blocklist had the same posture (it could only ever block bots
    # an operator had pre-listed) and the wallet-existence check
    # inside ``TransferService.send`` still gates the transfer for
    # bots that never interacted with this bot. The known limitation
    # is documented here so a future caller doesn't re-introduce the
    # endswith trap.
    bot_me = await bot.me()
    if to_id == bot_me.id:
        await message.answer(t("h_send_bot_recipient", lang))
        log.bind(uid=tg_user.id, to=to_id, outcome="bot_self").info(
            "/send rejected — recipient is this bot"
        )
        return
    try:
        member = await bot.get_chat_member(message.chat.id, to_id)
    except TelegramAPIError as exc:
        # Target not in this chat / API outage / unknown user. The
        # wallet-existence check inside ``TransferService.send`` is
        # the floor; if the recipient is a bot that has never
        # interacted with this bot, no wallet row exists and the
        # service surfaces ``NO_RECIPIENT_WALLET``. Known limitation:
        # a third-party bot that DID interact with this bot (so its
        # wallet row exists) and is not in the originating chat
        # cannot be detected here. See R-FIX-004-fp docstring.
        log.bind(uid=tg_user.id, to=to_id, exc=str(exc)).warning(
            "/send: bot.get_chat_member failed — skipping is_bot gate"
        )
    else:
        target_user = getattr(member, "user", None)
        if target_user is not None and bool(getattr(target_user, "is_bot", False)):
            await message.answer(t("h_send_bot_recipient", lang))
            log.bind(uid=tg_user.id, to=to_id, outcome="bot_recipient").info(
                "/send rejected — recipient is a bot"
            )
            return
        # The probe already gave us the recipient's profile — reuse its
        # name for the mention rather than a separate lookup (#16).
        if recipient_name is None and target_user is not None:
            recipient_name = getattr(target_user, "first_name", None)

    # Self-transfer check happens AFTER username resolution: the
    # legacy contract is "you can't gift yourself" regardless of how
    # you addressed yourself. Resolving first means ``/send @my_handle``
    # surfaces SELF_TRANSFER (the service-layer floor), not
    # NO_RECIPIENT — same as the numeric self-id case.

    from datetime import UTC, datetime  # local — only path that needs it

    now = datetime.now(tz=UTC)
    effects = await effects_service.resolve_transfer_effects(tg_user.id, now=now)
    result = await transfer_service.send(
        from_id=tg_user.id, to_id=to_id, amount=amount, effects=effects
    )

    outcome = result.outcome
    if outcome is TransferOutcome.SUCCESS:
        # Last-resort name lookup for the bare numeric form when the
        # recipient isn't in this chat (get_chat_member failed) — one
        # indexed users-DB read, only on the success path (#16).
        if recipient_name is None:
            names = await users_repo.first_names_by_ids([to_id])
            recipient_name = names.get(to_id) or None
        comment_line = (
            "\n" + t("h_send_comment_line", lang, comment=html.escape(comment)) if comment else ""
        )
        # The recipient's courtesy DM is composed BEFORE the sender is
        # told the transfer went through. The old ``suppress(Exception)``
        # spanned this read too, and a failed statement leaves the
        # session unusable — so the middleware's commit blew up
        # afterwards and the transfer was rolled back under a sender who
        # had just been shown a success card. Anything that can fail on
        # our side now fails while a rollback is still the honest answer.
        r_wallet = await economy_repo.get(to_id)
        r_lang = (r_wallet.language if r_wallet is not None else None) or "ru"
        dm = t(
            "h_send_dm",
            r_lang,
            sender=mention_html(tg_user.id, tg_user.first_name),
            amount=format_number(result.received),
        )
        if comment:
            dm += "\n\n" + t("h_send_dm_comment", r_lang, comment=html.escape(comment))

        # Everything that can still fail on our side has run. Commit
        # here so the receipt below is a promise already kept: the DM
        # catches TelegramAPIError, but a transport failure underneath
        # that layer would otherwise reach the middleware and roll the
        # transfer back after the sender had been shown the success
        # card.
        #
        # #1656: "already kept" is a claim about the TRANSFER, and it
        # holds because the checkpoint commits economy first by
        # construction (``db.session._COMMIT_ORDER``, #1493). It is not
        # a claim about the whole update: this call commits several
        # SQLite files one at a time and cannot be atomic across them.
        # If a later one loses, what is missing is bookkeeping, and the
        # journal now says which file it was.
        if checkpoint is not None:
            await checkpoint()

        await message.answer(
            t(
                "h_send_success",
                lang,
                recipient=mention_html(to_id, recipient_name),
                amount=format_number(result.amount),
                received=format_number(result.received),
                tax=format_number(result.tax),
                balance=format_number(result.sender_balance),
                comment_line=comment_line,
            )
        )
        # The ping itself stays best-effort: the recipient may never
        # have opened a DM with the bot (Forbidden), and that must not
        # fail an already-committed transfer the sender has been shown.
        # Legacy sent this courtesy ping too (bot.py:18931-18935) — silently;
        # a log line is what tells us when it stops arriving.
        try:
            await bot.send_message(to_id, dm)
        except TelegramAPIError as exc:
            log.bind(uid=tg_user.id, to=to_id).warning(
                "/send recipient DM failed: {exc!r}", exc=exc
            )
        log.bind(
            uid=tg_user.id,
            to=to_id,
            amount=result.amount,
            tax=result.tax,
            vip_discount=effects.tax_discount_percent,
        ).info("/send success")
        return

    # #1967: two of the refusals below arrive with the write
    # transaction already open. ``INSUFFICIENT_FUNDS`` means ``debit``'s
    # ``WHERE balance >= amount`` matched zero rows, and
    # ``NO_RECIPIENT_WALLET`` means the debit landed and the SAVEPOINT
    # was rolled back — in both cases nothing survives to be written,
    # but ``db/engines.py`` promoted the connection to
    # ``BEGIN IMMEDIATE`` on the way (``savepoint`` is deliberately
    # absent from ``_NON_WRITE_HEADS``, so ``begin_nested`` takes the
    # writer lock too). Without this, ``economy.db`` stays locked over
    # nothing for the whole Telegram round trip and every other player's
    # write waits out the 5s ``busy_timeout``. The other three outcomes
    # return before any write and the call is a no-op there — the shape
    # ``/daily`` uses for RACE_LOST beside plain COOLDOWN (#1861).
    if checkpoint is not None:
        await checkpoint()

    if outcome is TransferOutcome.SELF_TRANSFER:
        await message.answer(t("h_send_self", lang))
    elif outcome is TransferOutcome.INVALID_AMOUNT:
        # Defensive — parser should have caught this. Surfacing the
        # service's outcome anyway pins the contract: if a future
        # parser change loosens validation, the service still acts
        # as the floor.
        await message.answer(t("h_send_invalid_amount", lang))
    elif outcome is TransferOutcome.NO_SENDER_WALLET:
        await message.answer(t("h_send_no_sender", lang))
    elif outcome is TransferOutcome.NO_RECIPIENT_WALLET:
        await message.answer(t("h_send_no_recipient", lang))
    elif outcome is TransferOutcome.INSUFFICIENT_FUNDS:
        await message.answer(t("h_send_insufficient", lang))
    else:
        # Unreachable today — every TransferOutcome member has a
        # branch above. The else exists so a future enum addition
        # without a corresponding branch produces a visible "..."
        # text in production, surfacing the gap immediately instead
        # of swallowing the message.
        log.bind(uid=tg_user.id, outcome=outcome.value).error(
            "/send: unhandled outcome — handler render branch missing"
        )
        await message.answer("…")

    log.bind(uid=tg_user.id, to=to_id, amount=amount, outcome=outcome.value).info("/send rejected")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh router + middleware per call so tests can re-wire.

    Both private and group chats are claimed at the router level
    (T-015). The per-chat feature-gate (``require_group_feature``) and
    auto-delete never ported; the bridge that used to carry them was
    removed in T-011, so nothing applies them today. See the module
    docstring — this is a gap, not a delegation.

    Three middlewares attached — registration order matters because
    aiogram applies router middlewares outer-to-inner in that order:

    * :class:`TransferRateLimitMiddleware` — Stage 19 per-user token
      bucket. Registered FIRST so a rejected request short-circuits
      with a cool-down reply before either DB session is opened. If
      this ran after EconomyMiddleware, a spam-bot's 1000 updates
      per second would still each cost a fresh ``economy.db`` session
      and a wallet ``get_or_create`` round-trip; placing it first
      makes the gate genuinely cheap on the reject path.
    * :class:`EconomyMiddleware` — opens an ``economy.db`` session
      and stamps the wallet repos / TransferService bundle. This is
      the only router that gives it the transfer knobs, so it is the
      only one whose TransferService is tax-aware: the rate comes
      from ``COINS_TRANSFER_TAX`` and the tax destination from
      ``ADMIN_CHAT_ID``, mirroring legacy bot.py:10244 / 10270-10273.
      Before #193 neither was passed, and the service silently burned
      a 5% cut legacy never took.
    * :class:`SessionMiddleware` — opens a ``users.db`` session and
      stamps :class:`UsersRepo` (Stage 18: needed for the
      ``@username`` resolution branch). The numeric form ignores it,
      but the cost is one extra session per /send update — negligible
      against the typed-pipeline DB work the handler already does,
      and the alternative (injecting ``users_repo`` into
      EconomyMiddleware) would bleed the users-domain into a strictly-
      economy middleware. Domain isolation matters here because
      EconomyMiddleware is also bound to /shop, /buy, /inventory,
      /daily — none of which should see a users-DB session at all.
    """
    router = Router(name="send")
    # Developers bypass the gate, as they did in legacy before the
    # tracker was consulted at all (bot.py:18832). Passing the bound
    # method keeps the ID list in one place — ``Settings`` — instead
    # of teaching the middleware about config.
    router.message.middleware(TransferRateLimitMiddleware(is_exempt=settings.bot.is_developer))
    router.message.middleware(
        EconomyMiddleware(
            registry,
            transfer_tax_rate=settings.economy.coins_transfer_tax,
            transfer_admin_user_id=settings.bot.admin_chat_id,
        )
    )
    router.message.middleware(SessionMiddleware(registry))
    router.message.register(
        handle_send,
        Command("send", ignore_case=True),
        F.from_user,
    )
    return router
