"""ORM mappings for ``economy.db``.

Stage 7 maps only the subset of ``economy.users`` that ``/balance``
needs — adding the remaining columns is mechanical and lands as the
later handlers (``/daily``, ``/gift``, ``/buy``, …) migrate. Mapping
unused columns now would freeze type signatures before we've agreed
on them.

Prod schema reference: ``docs/prod_schemas.sql``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import EconomyBase


class EconomyUser(EconomyBase):
    """A wallet row in ``economy.users``.

    Legacy default balance for fresh rows is 100 (welcome credit). New
    code MUST preserve that default — otherwise users seeded by the
    old code and users seeded by the new code would differ on day one.
    """

    __tablename__ = "users"

    # No ``__table_args__`` on purpose — but production is not
    # index-free (#1621): legacy created ``idx_users_balance`` and
    # ``idx_users_streak`` there, and neither is declared here, so a
    # fresh database gets neither. Nothing to reconcile today; the
    # point is that this class is not evidence about query plans. The
    # columns the sweeps actually filter on — ``vip_till`` and
    # ``vip_notified_till`` — are unindexed in BOTH places (#1617).
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    total_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_spent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    games_played: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    games_won: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_daily: Mapped[datetime | None] = mapped_column(nullable=True)
    last_daily_streak: Mapped[datetime | None] = mapped_column(nullable=True)
    registered: Mapped[datetime | None] = mapped_column(nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(nullable=True)
    language: Mapped[str] = mapped_column(String, nullable=False, default="ru")
    # ``referred_by`` — user_id of the inviter who shared the ``ref_<uid>``
    # deep-link the rower used at /start. Nullable: most wallets have no
    # referrer (organic joins). The column was added by legacy's lazy
    # ALTER TABLE at startup (bot.py:5367 ``_ensure_referred_by_column``)
    # and is present in the prod dump (``docs/prod_schemas.sql:320``);
    # ``create_all`` adds it in tests. Nothing in this repo adds it,
    # because nothing needed to — worth knowing before restoring a
    # backup older than that ALTER, which no migration here would heal.
    # Stage 29 (``/referrals``) reads it and
    # :meth:`EconomyRepo.set_referrer` writes it, called from the ported
    # /start (``handlers/start.py:264``) under a first-attribution-wins
    # WHERE clause.
    referred_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ``vip_till`` — unix timestamp (REAL) when the global VIP expires;
    # NULL means "never had VIP" or "VIP was cleared". Legacy stores
    # both REAL and (rarely, from older bug paths) ISO strings — we
    # map as Float and let SQLAlchemy coerce; a ValueError on read
    # surfaces as a typed exception rather than the legacy silent-None.
    # Group-scoped VIP lives in :class:`UserGroupVip` (composite PK)
    # so the two are independent — a user can be global-VIP but not
    # group-VIP in a specific chat.
    vip_till: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ``vip_notified_till`` — the ``vip_till`` value the L-95 expiry
    # sweep last DM'd this user about (NULL = never notified). Storing
    # the deadline itself (not a boolean) makes the notice once-per-grant:
    # extending VIP changes ``vip_till`` and automatically re-arms it.
    # Added by economy migration ``0011_vip_expiry_notice``; legacy never
    # touches the column.
    vip_notified_till: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ``display_currency`` — the ISO-ish code (``RUB``/``USD``/``TON``/…)
    # the user picked in ``/currency``; see
    # :func:`~telegram_invite_bot.services.currency_service.effective_currency`
    # for how it is read. The column was shared with the legacy process
    # until T-011, which both read it (balance/price rendering) and
    # wrote it (its own ``/currency``). Nothing else writes it now, but
    # every value legacy ever wrote is still in the column, so the
    # vocabulary of codes stays exactly as wide and a value this
    # pipeline doesn't recognise still must not be "normalised" —
    # rewriting it would destroy a user's actual choice. Legacy created
    # the column via a lazy startup ALTER (bot.py:5298
    # ``_ensure_display_currency_columns``) with a server
    # default of ``'RUB'``, which is why the economy ``0013`` migration
    # inspects before adding and why "stored RUB" cannot be told apart
    # from "never chose".
    display_currency: Mapped[str | None] = mapped_column(
        String, nullable=True, server_default=text("'RUB'")
    )


class UserGroupVip(EconomyBase):
    """Group-scoped VIP grant row in ``economy.user_group_vip``.

    Legacy reads this when a handler passes a ``group_id`` to
    ``get_vip_profile`` (e.g. group-feature checks that want
    "is this user VIP in *this* community?"). The default
    consumer (``/daily``, profile rendering) reads the global
    flag on :class:`EconomyUser` instead; both sources are
    independent by design — a per-chat VIP doesn't grant global
    perks and vice versa.

    PK is the composite ``(user_id, group_id)`` matching prod;
    one row per (user, chat). ``vip_till`` is REAL (unix
    timestamp) and is NOT NULL — the absence of VIP is the
    absence of the row, not a NULL deadline.

    Schema source: ``docs/prod_schemas.sql:589``.
    """

    __tablename__ = "user_group_vip"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vip_till: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (Index("idx_user_group_vip_group", "group_id"),)


class ShopItem(EconomyBase):
    """Catalog row in ``economy.shop_items``.

    Stage 16 mapped every column from the prod dump so
    ``Base.metadata.create_all`` (used in tests) produces a schema
    compatible with what the legacy admin handlers wrote, and consumed
    the catalog without touching it. The second half stopped being true
    when the shop ported:
    :class:`~telegram_invite_bot.services.purchase_service.PurchaseService`
    decrements ``stock`` inside the purchase savepoint under a
    ``stock > 0`` guard, so two buyers racing for the last copy cannot
    both win. ``stock`` defaults to -1 in legacy ("infinite"); we
    preserve that convention, and the decrement leaves those rows alone.
    """

    __tablename__ = "shop_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    stock: Mapped[int | None] = mapped_column(Integer, default=-1, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[str | None] = mapped_column(Text, nullable=True)
    added: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Transaction(EconomyBase):
    """A coin-movement ledger row in ``economy.transactions``.

    Stage 17 (/buy) writes here with ``type="shop"`` to mirror the
    legacy ``record_transaction(..., transaction_type="shop")`` call
    at bot.py:13233.

    ``amount`` is a POSITIVE magnitude; the direction of the move lives
    in ``from_id`` (payer) / ``to_id`` (payee), either of which is NULL
    when the counterparty is the system. Legacy wrote spends negative,
    and those rows are still in prod — every aggregate over ``amount``
    therefore takes ``ABS`` per row so old and new rows add up the same
    way (see ``TransactionsRepo.window_stats``).

    Indexed on ``from_id``, ``to_id``, ``date`` in prod; SQLAlchemy
    creates indexes lazily, so we accept the default for tests and
    rely on the existing prod indexes for live queries.
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False, default="transfer")


class InventoryItem(EconomyBase):
    """A purchased-item row in ``economy.inventory``.

    Prod schema includes a quirky inline-altered column (``group_id``
    added after the fact, comma-only separator visible in the dump).
    All columns mapped so create_all + the legacy write path stay
    compatible. ``UNIQUE(user_id, item_id, purchase_date)`` prevents
    accidental double-insert from a retried /buy.
    """

    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=True)
    used_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "item_id", "purchase_date", name="uq_inventory_user_item_dt"),
    )


class Donation(EconomyBase):
    """One donation event — append-only ledger keyed by autoincrement id.

    Stage 24 reads from this for ``/mydonates``. Two writers append
    here, both through :meth:`DonationsRatingRepo.record_donation`: the
    slice of a shop purchase that goes to the group (RR-2 #14), and
    ``/donate`` itself, ported in #2007 — see
    :class:`~telegram_invite_bot.services.group_donation_service.GroupDonationService`.
    Between T-011 and #2007 the surface was read-only, so rows with a
    ``created_at`` in that gap simply do not exist.
    Prod schema includes a free-text ``message`` column we don't render
    yet (legacy /mydonates didn't either), but the field is mapped so
    a future "show donation comment" view doesn't need an ALTER.

    Source: ``docs/prod_schemas.sql:416``.
    """

    __tablename__ = "donations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GroupDonationsAggregate(EconomyBase):
    """Per-group aggregate row — name, link, lifetime total, rating slot.

    ``/mydonates`` joins on this for the human-readable group name in
    each ledger line. Most columns are live:
    :meth:`DonationsRatingRepo.save_group_identity` maintains the name
    and link, :meth:`~DonationsRatingRepo.record_donation` bumps
    ``group_xp`` and ``last_donation``, and
    :meth:`~DonationsRatingRepo.recalc_positions` rewrites
    ``rating_position``.

    ``total_donations`` is the exception and is frozen on purpose: it
    stopped at its cutover value, the board ranks on ``group_xp``, and
    resuming the counter now would put one number in the same column as
    a differently-scoped older one. Read it as an archive, never as a
    running total — legacy's own ``/donate`` receipt printed it and was
    wrong for exactly that reason (#2007).

    Source: ``docs/prod_schemas.sql:404``.
    """

    __tablename__ = "groups_donations"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_donations: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    members_count: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    last_donation: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rating_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    group_xp: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    # Opt-OUT flag added by ``0009_donations_rating_writeside`` for the
    # ``/rating_exclude`` toggle: 1 = ranked (the default), 0 = hidden.
    # ``nullable=False`` matches the migration exactly, so autogenerate
    # doesn't keep proposing a phantom diff. The read filters still handle
    # NULL explicitly — see ``handlers.rating._ranked`` — because rows
    # written by the legacy monolith BEFORE the cutover came from raw SQL
    # that never named the column, and a database restored from that era
    # can still hold them. The branch costs nothing and is the difference
    # between "included" and "silently dropped from the board".
    in_rating: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


class GameResult(EconomyBase):
    """One row in ``economy.games`` — append-only result-of-a-game ledger.

    Stage 26 (``/duel_stats``) reads aggregates from this table filtered
    by ``game = 'duel'``. ``profit`` is signed (positive on win, negative
    on loss) — mirrors legacy's convention so ``SUM(profit)`` is the
    user's net duel P&L directly, with no client-side sign juggling.

    ``win`` is a 0/1 column in the prod dump (``BOOLEAN DEFAULT 0``).
    SQLite stores booleans as integers natively; mapping as ``bool``
    keeps the new code readable while round-tripping the legacy writer's
    integer values without an ORM-level migration.

    Source: ``docs/prod_schemas.sql:336``.
    """

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    game: Mapped[str] = mapped_column(String, nullable=False)
    bet: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    win: Mapped[bool] = mapped_column(Boolean, default=False, nullable=True)
    profit: Mapped[int] = mapped_column(Integer, default=0, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class GroupTopDonator(EconomyBase):
    """A pre-aggregated "lifetime donations per (chat, user)" row.

    The counter is maintained on every donation write (the upsert in
    :meth:`DonationsRatingRepo.record_donation`) so ``/donaters`` can
    render a top-N leaderboard with one indexed SELECT instead of a
    GROUP BY over the full ``donations`` ledger. Same two writers as
    :class:`Donation`: a purchase's group slice, and ``/donate``.

    Composite PK ``(group_id, user_id)`` matches prod (the donation
    write upserts on it). Source: ``docs/prod_schemas.sql:427``.
    """

    __tablename__ = "group_top_donators"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    total_donated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_donate: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserPrivilege(EconomyBase):
    """A per-user permission/effect row in ``economy.user_privileges``.

    Legacy uses this table as a polymorphic key/value store:
    ``privilege_type`` discriminates ("double_daily", "color_nick",
    "custom_title", "legend", "mute_protection", …) and ``value`` is a
    JSON-encoded payload.

    NULL is a NEW-pipeline convention, not a legacy one. Legacy wrote a
    payload even for the boolean-style switches — ``apply_double_daily``
    stores ``{"active": True}`` (``bot.py:13662``) — and its reader
    ``has_double_daily`` requires that payload to be TRUTHY on top of
    the row existing (``:13673``). Our writer stores NULL
    (:meth:`repositories.privileges_repo.PrivilegesRepo.grant_buster`)
    because our own reader keys on row presence plus ``expires_at`` and
    never inspects ``value`` for these types. The two are
    self-consistent but NOT interchangeable: a legacy reader over a row
    this pipeline wrote sees ``bool(None)`` and concludes the privilege
    is absent. Prod holds
    zero ``user_privileges`` rows today, so nothing is broken — but any
    plan to run the two stacks over one economy DB has to reconcile
    this first.

    The composite PK ``(user_id, privilege_type, group_id)`` lets a
    user hold the same privilege type once per scope: ``group_id=0``
    means "global" (the default for /daily-relevant privileges like
    ``double_daily``); positive values scope to a single chat (e.g. a
    custom_title that only renders in one community). The new
    pipeline preserves that 0-vs-positive semantic — a future
    "global helpers don't accidentally read group-scoped rows" guard
    sits at the repo layer, not on the model.

    ``expires_at`` is a unix timestamp (REAL) where ``0`` means "never
    expires" (legend status, infinite buster). Storing as
    :class:`float` matches the ``time.time()`` values legacy wrote
    (``bot.py:13282`` declared the column REAL), and those rows are
    still on prod — so a migration to a proper ``DateTime`` column is
    a data conversion, not a negotiation with another writer. For now
    we mirror.

    Stage 11 mapped the columns and shipped the reads alone
    (``PrivilegesRepo.get_active`` / ``remove``) for /daily's
    double-buster check — /daily consumes privileges and never grants
    them, so there was nothing to insert yet. The grants arrived with
    the shop port and live here now: ``grant_buster`` and
    ``grant_with_value`` on the same repo are the insert call sites,
    both upserting on the composite PK above.

    Schema source: ``docs/prod_schemas.sql:577``.
    """

    __tablename__ = "user_privileges"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    privilege_type: Mapped[str] = mapped_column(Text, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    # JSON-encoded payload — see class docstring for the polymorphism.
    # Stored as TEXT in prod (legacy ``json.dumps`` writes) so we keep
    # it as a str column; callers that need a dict run their own
    # ``json.loads``. Centralising the decode would force every consumer
    # to share one schema, which defeats the point of a polymorphic
    # store.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_user_privileges_user", "user_id"),
        Index("idx_user_privileges_expires", "expires_at"),
    )


class ProcessedWebhook(EconomyBase):
    """Idempotency ledger for payment provider webhooks (T-025).

    One row per (provider, external_id) pair that we have already
    credited. The composite primary key is the idempotency contract:
    a second webhook delivery with the same identifier finds the row
    and returns 200 OK without crediting again. Providers (Stripe and
    YooKassa especially) retry deliveries on 5xx and on missed acks —
    making duplicate-after-success a non-event is mandatory, not a
    nice-to-have.

    Why a new table rather than reusing the legacy
    ``is_payment_transaction_processed`` lookup against
    ``economy.transactions``: legacy probes the ledger by
    ``transaction_id`` to derive idempotency, which conflates "did we
    pay them" with "should we pay them again". The ledger is the
    *consequence* of a credit, not the *gate* on whether to do one —
    keeping a dedicated table lets the gate sit at the top of the
    service before any wallet write and survives a future ledger
    schema change. A failed credit (DB hiccup mid-write) leaves no
    processed_webhooks row, so the provider's next retry will re-try
    the credit — fail-open on transient DB issues, fail-closed on
    duplicate after success.

    Provider is stored as a TEXT discriminator (``"crypto"`` /
    ``"yookassa"`` / ``"stripe"``) so two providers issuing the same
    invoice id (extremely unlikely, but legal in the spec) never
    collide on the PK. ``external_id`` matches the provider's own
    identifier: ``invoice_id`` for Crypto Pay, ``payment_id`` for
    YooKassa, ``session_id`` for Stripe (Stripe checkout-session ID).

    ``credited_amount`` records the wallet delta we applied (positive
    coins) so audit queries can SUM lifetime credited-via-webhook
    without joining the ledger. Storing the user_id makes per-user
    audit one indexed read.

    ``reversed_at`` / ``reversed_event`` (#174) mark a credit the
    provider later took back — a chargeback or a refund. The marker
    lives on the credit row rather than in a table of its own because
    this row already IS the payment↔user link: the reversal webhook
    carries a provider payment id and nothing else identifying, so
    without this join the owner's alert can only name an opaque id.
    Nothing about the wallet changes here — the debit stays a human
    decision (see ``webhook/payments._alert_reversal``) — but the fact
    that a user has a reversal on record must outlive the process, or
    the payout desk keeps deciding blind after every restart.

    The pair is nullable and set once, by the reversal path only. A
    provider redelivering the same reversal overwrites it with the
    same values, which is why the columns are a marker and not a
    counter: they answer "has this payment been taken back", not "how
    many times did the provider tell us so".

    ``fiat_amount`` / ``fiat_currency`` / ``fx_rate`` (#239) record
    what the customer was actually charged, in the provider's own
    currency, and the USD/RUB rate the coin conversion used. Every
    other money column in this schema is denominated in coins, which
    means that before these three existed there was no column
    anywhere that a provider's settlement report could be compared
    against: the roubles were known only inside the adapter, for the
    length of one function call, and the rate moves daily so they
    could not be reconstructed afterwards.

    All three are ``String``. ``fiat_amount`` and ``fx_rate`` are
    decimal money and a ``Float`` column would reintroduce exactly
    the drift ``WithdrawalRequest.amount_fiat`` was moved off floats
    to escape — the adapters already keep these values in
    ``Decimal`` end to end, and TEXT is the only SQLite affinity
    that preserves that. They are nullable because they are an
    audit trail bolted onto a table that has been credited against
    for months: rows written before this migration have nothing to
    say, and saying nothing is the honest answer.
    """

    __tablename__ = "processed_webhooks"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    external_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reversed_event: Mapped[str | None] = mapped_column(String, nullable=True)
    fiat_amount: Mapped[str | None] = mapped_column(String, nullable=True)
    fiat_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    fx_rate: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_processed_webhooks_user", "user_id"),
        Index("idx_processed_webhooks_processed_at", "processed_at"),
    )


class WithdrawalRequest(EconomyBase):
    """A pending / completed / rejected withdrawal request.

    Stage 39 reads the ``status='pending'`` slice for
    ``/admin_withdrawals`` (operator's daily ops surface). Writes still
    live in legacy until the user-side ``/withdraw`` flow ports — that
    handler is FSM-heavy and depends on currency conversion rates we
    haven't migrated yet. Read-side mapping in isolation lets the
    operator surface land first; the action callbacks (confirm/reject)
    come with the write-side port.

    ``created_at`` is ``TEXT`` in prod (legacy writes ISO strings via
    ``CURRENT_TIMESTAMP`` and ``datetime('now')``) — we expose it as
    ``str`` rather than ``datetime`` so the legacy values pass through
    untouched. Mapping it as DateTime would let SQLAlchemy coerce
    legacy-written ISO strings inconsistently across drivers, and the
    operator card displays the value verbatim anyway.

    Schema source: ``docs/prod_schemas.sql:503``.
    """

    __tablename__ = "withdrawal_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_com: Mapped[int] = mapped_column(Integer, nullable=False)
    # M-E-6: stored in minor units (cents for USD/EUR, kopecks for
    # RUB). Legacy stored the major-unit value as REAL (Float), which
    # bit the codebase with the classic 0.1 + 0.2 != 0.3 drift on
    # admin sum aggregates. The column is still nullable so legacy
    # rows that never had a fiat amount stay nullable through the
    # migration. Display conversion lives in
    # ``utils/economy.format_fiat_amount`` — every UI that renders
    # this field must route through that helper, not divide by 100
    # inline.
    amount_fiat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Crypto amounts stay float for now — fractional asset units
    # (0.0001 BTC) don't have a clean integer minor unit at the
    # schema level and the write-side port can revisit. Audit
    # explicitly scopes M-E-6 to ``amount_fiat`` only.
    amount_crypto: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True, default="pending")
    # Kept as TEXT to match legacy ISO-string writes (see class
    # docstring) — DateTime coercion would alter values on read.
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # #1518: when the #169 stale-payout alert last named this
    # request, in the same naive-UTC TEXT frame ``created_at`` uses.
    # NULL means "never reported". This replaced an in-process set
    # that every deploy emptied — the alert rides the 60s money tick
    # since #281, so a restart-scoped ledger re-sent the whole
    # backlog often enough to train the owner to mute it.
    # ``WithdrawalsRepo.release_processing`` clears it: a provider
    # refusal puts the row back in the queue, and a request that is
    # actionable again has to be able to earn a fresh alert.
    alerted_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_withdrawal_requests_user", "user_id"),
        Index("idx_withdrawal_requests_status", "status"),
    )


class Check(EconomyBase):
    """A coin-code voucher row in ``economy.checks`` (#26).

    A "check" (чек) is a code a creator funds out of their wallet; one
    or more claimers redeem the code for coins. Legacy owned the create
    path at ``bot.py:25301`` (DEV-only ``/create_check``) and the claim
    path at ``bot.py:10081`` (``_activate_check``); this package ports
    both, with an ATOMIC claim that fixes the legacy TOCTOU drain race
    (see :meth:`ChecksRepo.claim_decrement`).

    Every column from the prod dump is mapped, so ``create_all``
    (tests) builds the same table the prod rows were written into.

    Amount semantics:

    * ``total_amount`` — coins debited from the creator at create time.
    * ``remaining_amount`` — coins left to pay out; the claim guard
      decrements it atomically and flips ``is_active`` to 0 when it
      hits zero.
    * ``min_amount`` / ``max_amount`` — bounds for ``type='random'``
      (each claim pays a uniform ``randint(min, max)``).
    * ``fixed_amount`` — per-claim payout for ``type='fixed'`` and the
      single-claim payout for ``type='individual'``.

    ``is_active`` is a 0/1 flag in the prod dump (``INTEGER DEFAULT 1``).
    SQLite stores booleans as integers; mapping as ``int`` (not ``bool``)
    keeps the value round-tripping exactly as the legacy writer/CASE
    expression produces it (the claim guard sets it via a SQL ``CASE``
    that emits literal 0/1).

    ``min_age`` / ``min_activity`` / ``allowed_countries`` /
    ``blocked_users`` are enforced at claim time by
    :meth:`CheckService._filter_gate` (Gate 8b of ``claim_check``);
    ``required_subscription`` is enforced one level up, by the
    claim-side handler gate in ``handlers/checks.py``, because it needs
    a Telegram membership call. See ``claim_check``'s gate list for the
    order and for the best-effort limits on ``min_activity`` and
    ``allowed_countries``.

    Schema source: ``docs/prod_schemas.sql:469``.
    """

    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    min_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fixed_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_claims: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    claims_count: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    required_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_premium: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    required_subscription: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    min_age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_activity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allowed_countries: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_users: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # is_active maps the prod ``INTEGER DEFAULT 1``; kept int (not bool)
    # so the claim guard's SQL CASE (emits 0/1) round-trips exactly.
    is_active: Mapped[int] = mapped_column(Integer, nullable=True, default=1)

    __table_args__ = (Index("idx_checks_code", "code"),)


class CheckClaim(EconomyBase):
    """One redemption event in ``economy.check_claims`` (#26).

    Append-only: one row per (check, claimer) pair. The
    ``UNIQUE(check_id, user_id)`` constraint is the RACE-PROOF
    double-claim guard — two concurrent claims that both pass the
    Python ``has_claimed`` pre-check (which can race) collide on the
    INSERT, and the loser's :class:`sqlalchemy.exc.IntegrityError`
    rolls its whole transaction back (including the wallet decrement),
    so a check can never pay the same user twice.

    The prod dump (``docs/prod_schemas.sql:497``) ships the table
    WITHOUT this UNIQUE constraint — migration
    ``0004_check_claims_unique`` adds it (after a human-confirmed
    dedup; see that file's docstring). ``create_all`` builds it for
    tests directly from this ``__table_args__``.

    Schema source: ``docs/prod_schemas.sql:497``.
    """

    __tablename__ = "check_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # #1949: prod declares ``FOREIGN KEY (check_id) REFERENCES checks(id)``
    # (read off the live economy.db, and documented at
    # ``docs/prod_schemas.sql:500``); the model did not, so every
    # ``create_all`` database accepted a claim against a check that does
    # not exist while prod rejected it. Laxer than production is the
    # wrong direction for a test schema to diverge in.
    check_id: Mapped[int] = mapped_column(Integer, ForeignKey("checks.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("check_id", "user_id", name="uq_check_claims_check_user"),
        Index("idx_check_claims_check", "check_id"),
    )


class UserEmojiBadge(EconomyBase):
    """A VIP's chosen cosmetic emoji badge in ``economy.user_emoji_badge`` (#25).

    One row per user = their currently-equipped badge. The badge is a
    purely cosmetic prefix the bot renders next to the user's display
    name on surfaces it fully controls (the ``/profile`` card and the
    tops/leaderboards) — it is NOT a Telegram premium ``custom_emoji``
    entity (those need Premium + a Fragment username; out of scope, see
    ``docs/CUSTOM_EMOJI_VIP_SPEC.md``).

    The feature is **free for VIP**: there is no money path. Equipping
    is gated on an active global VIP grant (``VipRepo.is_vip`` reading
    ``users.vip_till``), and the display resolver re-checks VIP at render
    time, so when VIP lapses the badge simply stops showing — the row
    stays so it returns automatically on renewal. ``emoji`` is always a
    member of the curated ``EmojiBadgeService.VIP_BADGE_SET`` (validated
    at the service layer), so it is trusted text (no HTML escape needed,
    unlike the user-controlled display name it prefixes).

    This is a NET-NEW table (not in the prod dump) — migration
    ``0005_user_emoji_badge`` creates it; ``create_all`` builds it for
    tests. Kept separate from the legacy-shared ``EconomyUser`` row so we
    never ALTER a table both pipelines write.
    """

    __tablename__ = "user_emoji_badge"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    emoji: Mapped[str] = mapped_column(String, nullable=False)
    set_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserAchievement(EconomyBase):
    """One earned-achievement row in ``economy.user_achievements`` (A-07).

    A row keyed by the composite ``(user_id, achievement_id)`` appears
    the first time a user qualifies:
    ``EconomyRepo.award_achievements`` (A-12) diffs what they are
    eligible for against what they already hold and upserts the
    difference. The ``/achievements`` card lists what is there and
    nothing more — ``AchievementsRepo`` has no write method at all — so
    anyone chasing the award path wants the economy repo, not the card.
    The 14 achievement *definitions* (id → name / icon) live in
    code (``core.achievements.DEFINITIONS``), not in this table —
    the legacy DB ``achievements`` table is usually empty and the code
    dict is the source of truth, so this model maps only the per-user
    earned ledger.

    ``earned_date`` is a TIMESTAMP in the prod dump; SQLAlchemy round-
    trips it as ``datetime``. ``notified`` is the legacy "have we already
    pinged the user about this unlock" flag — mapped for schema fidelity
    (so ``create_all`` produces a compatible table), written as ``False``
    by the award path and read by nothing.

    Schema: ``user_achievements(user_id INT, achievement_id TEXT,
    earned_date TIMESTAMP, notified BOOL, PK(user_id, achievement_id))``.
    """

    __tablename__ = "user_achievements"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    achievement_id: Mapped[str] = mapped_column(Text, primary_key=True)
    earned_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=True, default=False)


class RuntimeSecret(EconomyBase):
    """A runtime-settable secret/config value in ``economy.runtime_secrets``.

    One row per key (e.g. ``CRYPTO_PAY_TOKEN``). Lets a developer set a
    provider credential from the in-bot admin panel WITHOUT a restart —
    the DI container builds singletons at startup, so a value that only
    lived in ``.env`` could not change without redeploying. The payment
    code resolves the effective token at *call* time
    (``services/payments/secret_resolver``): this DB row wins when
    present, otherwise it falls back to the ``.env``-loaded
    ``PaymentsConfig`` value.

    Posture on storage: the value is stored in plaintext, exactly like
    the ``.env`` file it overrides — the threat model is unchanged
    (anyone with the DB file or the env file has the secret). DB
    snapshots stay on the operator's machine, never in git. The admin
    handler that writes this row deletes the Telegram message carrying
    the token immediately so the secret does not linger in chat history.

    NET-NEW table (not in the prod dump) — migration
    ``0006_runtime_secrets`` creates it; ``create_all`` builds it for
    tests.
    """

    __tablename__ = "runtime_secrets"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
