"""Inject request-scoped repos backed by a shared ``economy.db`` session.

It has grown well past its original four keys — roughly thirty repos
and services are injected below, gated by feature flags and chat type.
Read the ``data[...]`` assignments in :meth:`__call__` for the current
list rather than trusting a summary here; the ones that set the shape
are:

* ``economy_repo`` — Stage 7 (``/balance``).
* ``shop_items_repo`` / ``inventory_repo`` — Stage 16 (``/shop`` and
  ``/inventory``, read-only).
* ``purchase_service`` — Stage 17 (``/buy``). Cross-table write
  (``users`` + ``shop_items`` + ``inventory`` + ``transactions``)
  that commits atomically against this same session, or rolls back
  via the base-class ``except`` branch.

They all share ONE :class:`AsyncSession` — that's the point. Without
it, ``/buy`` would have to coordinate four sessions and four commits,
losing the atomicity guarantee that makes the rowcount-based race
guards in :class:`PurchaseService` correct.

Attached to per-domain routers (not the dispatcher) so ``/start``,
``/weather`` and ``/profile`` don't pay for an ``economy.db`` session
they don't use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.repositories.achievements_repo import AchievementsRepo
from telegram_invite_bot.repositories.checks_repo import ChecksRepo
from telegram_invite_bot.repositories.donations_rating_repo import DonationsRatingRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.emoji_badge_repo import EmojiBadgeRepo
from telegram_invite_bot.repositories.game_limits_repo import GameLimitsRepo
from telegram_invite_bot.repositories.game_stats_repo import GameStatsRepo
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.repositories.p2p_repo import P2pRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.promo_repo import PromoRepo
from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.repositories.withdrawals_repo import WithdrawalsRepo
from telegram_invite_bot.services.check_service import CheckService
from telegram_invite_bot.services.daily_service import DailyConfig, DailyService
from telegram_invite_bot.services.duel_service import DuelService
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.effects_service import EffectsService
from telegram_invite_bot.services.emoji_badge_service import EmojiBadgeService
from telegram_invite_bot.services.game_limit_service import GameLimitService
from telegram_invite_bot.services.group_donation_service import GroupDonationService
from telegram_invite_bot.services.inventory_use_service import InventoryUseService
from telegram_invite_bot.services.p2p_service import P2pService
from telegram_invite_bot.services.promo_service import PromoService
from telegram_invite_bot.services.purchase_service import PurchaseService
from telegram_invite_bot.services.pvp_service import PvpService
from telegram_invite_bot.services.rps_service import RpsService
from telegram_invite_bot.services.transfer_service import (
    TransferConfig,
    TransferService,
)
from telegram_invite_bot.services.withdraw_service import WithdrawService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import WithdrawConfig
    from telegram_invite_bot.db import EngineRegistry


class EconomyMiddleware(BaseSessionMiddleware):
    """Open one ``economy`` session per update; expose every economy-DB repo.

    ``withdraw_config`` is opt-in: only the ``/withdraw`` router passes
    it, so the (cheap) ``withdraw_service`` + ``withdrawals_repo`` keys
    are bound only where a handler actually consumes them. Every other
    economy router constructs the middleware with the default ``None``
    and never sees those keys.
    """

    def __init__(
        self,
        registry: EngineRegistry,
        *,
        withdraw_config: WithdrawConfig | None = None,
        developer_commission_percent: int = 0,
        developer_id: int = 0,
        purchase_donation_to_group_percent: int = 0,
        p2p_pending_ttl_minutes: int = 30,
        transfer_tax_rate: float = 0.0,
        transfer_admin_user_id: int = 0,
        daily_new_user_lockout_hours: int = 0,
        daily_lockout_exempt_ids: frozenset[int] = frozenset(),
    ) -> None:
        super().__init__(registry, DBName.ECONOMY)
        self._withdraw_config = withdraw_config
        # Developer cut + recipient wallet for the RR-2 #14 group-donation
        # split (the shop's *referral* percent left with T-020/R10 — a
        # coin-paid buy mints nothing any more). Defaults 0 = disabled,
        # so the many EconomyMiddleware(registry) call sites that don't
        # sell items stay behaviour-identical.
        self._developer_percent = developer_commission_percent
        self._developer_id = developer_id
        # RR-2 #14: the group's cut of a group-targeted shop purchase.
        # Same default-0 = disabled posture as the two above.
        self._group_donation_percent = purchase_donation_to_group_percent
        # P2P D2: pending-trade TTL threaded to P2pService for lazy expiry.
        self._p2p_pending_ttl_minutes = p2p_pending_ttl_minutes
        # #193: /send tax rate + treasury destination. Same
        # default-disabled posture as the three above — only the
        # /send router passes the Settings-derived values, so the
        # many EconomyMiddleware(registry) call sites that never
        # see a transfer stay behaviour-identical.
        self._transfer_tax_rate = transfer_tax_rate
        self._transfer_admin_user_id = transfer_admin_user_id
        # #1946: the /daily anti-abuse lockout window and its developer
        # whitelist. Same default-disabled posture as everything above —
        # only the /daily router passes the Settings-derived values, so
        # the many EconomyMiddleware(registry) call sites that never see
        # a bonus claim stay behaviour-identical.
        self._daily_new_user_lockout_hours = daily_new_user_lockout_hours
        self._daily_lockout_exempt_ids = daily_lockout_exempt_ids

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        economy_repo = EconomyRepo(session)
        transactions_repo = TransactionsRepo(session)
        # AchievementsRepo (A-07) — read-only viewer for /achievements.
        # Shares this economy session; no write path, no service layer.
        data["achievements_repo"] = AchievementsRepo(session)
        # GameStatsRepo (RR-2 #25) — per-game profit breakdown for the
        # /balance dashboard. Read-only over the same economy session.
        data["game_stats_repo"] = GameStatsRepo(session)
        # EconomyService composes economy + ledger writes (Stage 9);
        # DailyService composes EconomyService + the cooldown / streak
        # SQL guard for /daily (Stage 10). Both share the same session
        # so a /daily ledger row and a /buy wallet write coalesce into
        # one commit if the handler ever needs to do both — and a
        # failed flush rolls back the whole bundle cleanly.
        economy_service = EconomyService(economy_repo, transactions_repo)
        privileges_repo = PrivilegesRepo(session)
        vip_repo = VipRepo(session)
        data["economy_repo"] = economy_repo
        data["transactions_repo"] = transactions_repo
        data["economy_service"] = economy_service
        data["daily_service"] = DailyService(
            economy_repo,
            economy_service,
            config=DailyConfig(
                new_user_lockout_hours=self._daily_new_user_lockout_hours,
                lockout_exempt_ids=self._daily_lockout_exempt_ids,
            ),
            session=session,
        )
        # EffectsService resolves per-user shop/VIP grants into typed
        # bundles for downstream services (DailyEffects today;
        # TransferEffects / XpEffects later). Sharing the session
        # means a future /daily handler that consumes a double_daily
        # buster lands the DELETE in the same commit as the wallet
        # credit — atomicity by composition.
        data["privileges_repo"] = privileges_repo
        data["vip_repo"] = vip_repo
        data["effects_service"] = EffectsService(privileges_repo, vip_repo)
        shop_items_repo = ShopItemsRepo(session)
        inventory_repo = InventoryRepo(session)
        data["shop_items_repo"] = shop_items_repo
        data["inventory_repo"] = inventory_repo
        # T-020/R10: no commission service is threaded in. #75 used to
        # build one here so a coin-paid /buy paid the inviter and the
        # developer their cuts (legacy bot.py:13243) — which minted 15%
        # of every shop burn back into circulation. Those commissions
        # now run only where real money enters (PaymentsService).
        data["purchase_service"] = PurchaseService(session)
        # GroupDonationService (RR-2 #14) — routes a slice of a *group*
        # purchase to that group's rating xp and pays the group's creator
        # (legacy bot.py:13240 → donation_from_purchase). Same session as
        # the purchase itself, so the donation rows, the xp bump and the
        # owner payout commit with the buy or roll back with it.
        # The repo itself is NOT published into ``data``: nothing injects
        # it by name today, and an unused key is a trap — a future
        # handler parameter that happens to share the name would start
        # resolving to *this* session silently.
        data["group_donation_service"] = GroupDonationService(
            DonationsRatingRepo(session),
            economy_repo,
            transactions_repo,
            percent=self._group_donation_percent,
            developer_percent=self._developer_percent,
            developer_id=self._developer_id,
        )
        # InventoryUseService (Stage 28) composes inventory + vip +
        # privileges + shop_items + economy repos over the SAME session
        # so the race-safe ``inventory.consume`` UPDATE and the matching
        # grant write (VIP upsert OR privilege upsert OR — for luck
        # items — the ``economy.credit`` coin payout) commit
        # together. The Stage 29 callback handler will read this key
        # off the middleware data; exposing it here ahead of the
        # handler keeps the wiring change reviewable in isolation.
        data["inventory_use_service"] = InventoryUseService(
            inventory_repo,
            vip_repo,
            privileges_repo,
            shop_items_repo,
            economy_repo,
            transactions_repo,
            # #1930: the same percent the service above routes to the
            # group is what a group-scoped purchase hands back to the
            # buyer when he owns that group, so the planner has to net
            # it off the price before it calls a luck row a coin sink.
            group_rebate_percent=self._group_donation_percent,
        )
        # TransferService composes EconomyRepo + TransactionsRepo
        # directly (not EconomyService.transfer, which is the
        # tax-naive primitive). Sharing the session means the gross
        # debit, net credit, admin credit and ledger rows commit
        # together — the outer transaction boundary the service's
        # docstring promises.
        #
        # #193: the config is threaded from ``Settings`` by the /send
        # router (handlers/send.py). It used to be omitted entirely,
        # under a comment calling the wiring "upcoming ... once the
        # /send handler lands" — but that handler had long since
        # landed, so ``TransferConfig()`` defaults applied forever:
        # a hard-coded 5% rate legacy never charged (bot.py:2550 is
        # 0), and ``admin_user_id=None``, which makes
        # transfer_service.py skip the treasury credit and write the
        # cut as ``transfer_tax_burned`` — destroying every taxed
        # coin. Legacy credits ADMIN_CHAT_ID (bot.py:10270-10273).
        #
        # The 0 default is normalised to ``None`` rather than passed
        # through: 0 is not a real wallet, and the service would
        # ``get_or_create(0)`` a phantom one on the first taxed /send.
        data["transfer_service"] = TransferService(
            economy_repo,
            transactions_repo,
            config=TransferConfig(
                base_tax_rate=self._transfer_tax_rate,
                admin_user_id=self._transfer_admin_user_id or None,
            ),
        )
        # RpsService (Stage 33) composes EconomyRepo + TransactionsRepo
        # over the SAME session so the atomic escrow → resolve → payout
        # → ledger flow lands as one commit. The headline value over
        # legacy is the compensating credit on a failed second escrow
        # (rock_paper_scissors.py:539-541 does NOT do this and can mint
        # coins). The Stage 34 /cpc handler will read this key off the
        # middleware data; exposing it here ahead of the handler keeps
        # the wiring change reviewable in isolation.
        data["rps_service"] = RpsService(economy_repo, transactions_repo)
        # DuelService (T-018) — twin of RpsService for the /duel
        # PvP dice game. Same atomic-escrow / compensating-credit
        # contract, same shared session so escrow + payout + ledger
        # land as one commit.
        data["duel_service"] = DuelService(economy_repo, transactions_repo)
        # PvpService (AUD-2) — /pvp_coin + /pvp_dice escrow stake games.
        # Same atomic escrow-on-create / status-claim / payout contract on
        # the shared session; needs the session for the PvpRepo too.
        data["pvp_service"] = PvpService(economy_repo, transactions_repo, session)
        # CheckService (#26) composes ChecksRepo + EconomyRepo +
        # TransactionsRepo over the SAME session so a check create
        # (debit + insert + ledger) or a claim (decrement + claim-row +
        # credit + ledger) lands as one atomic commit. The shared
        # session is also what lets the service ``rollback()`` a
        # double-claim's IntegrityError cleanly (undoing the decrement)
        # before the middleware would otherwise commit. The /check
        # handler reads ``check_service`` off this data; ``checks_repo``
        # is exposed too for repo-level callers / tests.
        checks_repo = ChecksRepo(session)
        data["checks_repo"] = checks_repo
        data["check_service"] = CheckService(checks_repo, economy_repo, transactions_repo, session)
        # PromoService (L-96) composes PromoRepo + EconomyRepo +
        # TransactionsRepo over the SAME session so a redeem (reserve-use +
        # redemption-row + credit + ledger) lands as one atomic commit and
        # the service can rollback() a per-user-once race cleanly. The /promo
        # handler reads ``promo_service`` off this data.
        promo_repo = PromoRepo(session)
        data["promo_repo"] = promo_repo
        data["promo_service"] = PromoService(promo_repo, economy_repo, transactions_repo, session)
        # P2pService (#64) composes P2pRepo + EconomyRepo + TransactionsRepo
        # over THIS session so escrow debit + order insert + ledger (and
        # every other P2P flow) commit atomically; the service rollback()s
        # on a failed checked credit, which needs this shared session.
        p2p_repo = P2pRepo(session)
        data["p2p_repo"] = p2p_repo
        data["p2p_service"] = P2pService(
            p2p_repo,
            economy_repo,
            transactions_repo,
            session,
            pending_ttl_minutes=self._p2p_pending_ttl_minutes,
        )
        # GameLimitService (L-25) — persistent anti-abuse caps (cooldown
        # 180s / 8 per hour / 25 per day) over the economy ``game_plays``
        # table. Shares THIS session so the play-stamp commits atomically
        # with the wallet settlement; replaces the restart-losing in-memory
        # RouletteLimiter. Exposed to the game handlers (roulette/duel/rps).
        game_limits_repo = GameLimitsRepo(session)
        data["game_limits_repo"] = game_limits_repo
        data["game_limit_service"] = GameLimitService(game_limits_repo)
        # EmojiBadgeService (#25) — VIP cosmetic emoji badge. Reads the
        # same economy session: ``EmojiBadgeRepo`` for the equipped badge
        # (``user_emoji_badge``) and the existing ``vip_repo`` for the
        # VIP gate (``EconomyUser.vip_till`` also lives in economy.db, so
        # there is no cross-file read here). No money path — equipping is
        # free for VIP — so unlike the other services it composes no
        # ledger write. ``emoji_badge_repo`` is exposed too for the
        # display resolver / tests.
        emoji_badge_repo = EmojiBadgeRepo(session)
        data["emoji_badge_repo"] = emoji_badge_repo
        data["emoji_badge_service"] = EmojiBadgeService(emoji_badge_repo, vip_repo)
        # WithdrawService (#28, T-027) — escrow-on-create withdrawal
        # lifecycle. Composes the same ``economy_service`` + a
        # ``WithdrawalsRepo`` over THIS session so the create-time escrow
        # debit + pending-row insert (and the reject-time refund) commit
        # as one transaction. Bound only when the router opted in with a
        # ``withdraw_config`` — keeps the limits/rate injected from
        # Settings rather than hard-coded here.
        if self._withdraw_config is not None:
            withdrawals_repo = WithdrawalsRepo(session)
            data["withdrawals_repo"] = withdrawals_repo
            data["withdraw_service"] = WithdrawService(
                session=session,
                economy=economy_service,
                withdrawals=withdrawals_repo,
                coins_per_usdt=self._withdraw_config.coins_per_usdt,
                min_coins=self._withdraw_config.min_coins,
                max_coins=self._withdraw_config.max_coins,
                asset=self._withdraw_config.asset,
                daily_limit_coins=self._withdraw_config.daily_limit_coins,
                monthly_limit_coins=self._withdraw_config.monthly_limit_coins,
                # T-019 (R2): the deposit gate reads the SAME session's
                # ledger, so "has this account ever bought coins?" is
                # answered inside the transaction that would escrow them.
                # #776: sharing the session is what makes that *possible*;
                # what makes it *true* is the writer lock ``create`` takes
                # before the gate reads (``WithdrawalsRepo.lock_writer``).
                # Without it a SELECT-only prologue runs outside any
                # SQLite transaction at all — see ``db/engines.py:207-208``.
                transactions=transactions_repo,
                require_deposit=self._withdraw_config.require_deposit,
                # T-020 (R6): the lifetime payout cap. Same shape as the
                # rolling quotas above — derived live from the ledger and
                # the requests table inside the escrowing transaction
                # (again, under the #776 writer lock), never from a stored
                # counter that could drift.
                payout_ratio=self._withdraw_config.payout_ratio,
            )
