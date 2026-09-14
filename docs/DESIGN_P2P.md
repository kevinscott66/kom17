# DESIGN: P2P COM marketplace epic (#64, L-77/78/79)

> Status: **SHIPPED.** Kept as an ADR — this is the design the
> implementation was built from, money-critical and signed off before build.
> Source: deep truth-read of legacy (anchors: tables bot.py:5175-5234,
> sell FSM 19278-19516, escrow-on-create 19478, buy 19742-19931, express
> 19583-19726, lifecycle 19982/20034-20039, disputes 20179-20300,
> guards table in the research log).

## 1. Legacy facts (the model we port)

- **Pure COM escrow, fiat moves OUTSIDE the bot.** No payment providers,
  no verification, **0% commission** (verified — no fee code exists).
- **Escrow-on-create:** seller's balance is debited the moment the sell
  order is created; the escrow IS `p2p_sell_orders.remaining_com`.
- **Order:** amount → currency (RUB/USD/UAH/EUR/USDT/TON) → price
  (market-only from a fixed rate table) → optional payment-methods +
  min/max limits. Statuses: active/completed/cancelled. Cancel returns
  `remaining_com` to balance. **No expiry** (field dead).
- **Buy:** order book sorted price-ASC, currency filter, paging;
  partial fills supported; express-buy auto-fills cheapest orders for a
  fiat budget; self-trade rejected everywhere.
- **Trade lifecycle:** pending → (buyer "Я оплатил") paid → (seller
  "COM отправлены") confirmed = COM credited to buyer + seller stats
  (successful_trades, total_withdrawn_com). Status-check guards on
  every transition.
- **Disputes:** either party opens; ONLY developers resolve; both legacy
  outcomes ("refund buyer" / "confirm seller") credit the COM **to the
  buyer** — they differ only in seller stats/penalty. **There is NO path
  returning COM to the seller** (e.g. buyer never paid fiat and
  disputes) — a real legacy gap.
- **Reputation:** successful_trades / total_withdrawn_com /
  dispute_count; `rating` is a dead always-5.0 field.
- **Known fragilities:** read-then-write race lets two express buyers
  oversell one order; escrow has no ledger trail; withdrawal limits
  defined but never enforced for P2P.

## 2. New-pipeline design

### 2.1 Schema — economy.db migration `0010_p2p`

`p2p_sell_orders(id PK, user_id, amount_com, remaining_com,
price_per_com REAL, fiat_currency TEXT, payment_methods TEXT NULL,
min_amount INT NULL, max_amount INT NULL, status TEXT default 'active',
created_at)` + idx(user_id), idx(status, fiat_currency, price_per_com).

`p2p_trades(id PK, order_id, seller_id, buyer_id, amount_com,
price_per_com, total_fiat REAL, fiat_currency, status TEXT default
'pending', created_at, paid_at NULL, confirmed_at NULL,
resolved_by NULL, resolved_at NULL)` + idx(order_id), idx(buyer_id),
idx(seller_id), idx(status).

Dead legacy fields (expires_at, completed_count, total_sold,
payment_method/details, dispute_reason, escrow_release_tx) — NOT ported.
Reputation reuses the EXISTING withdrawals/limits surface? No — legacy
kept it in user_withdrawal_limits; we add the 3 live counters
(successful_trades, total_sold_com, dispute_count) to a tiny
`p2p_seller_stats(user_id PK, ...)` table in the same migration
(the new pipeline's withdrawals_repo schema differs; keeping P2P stats
self-contained avoids touching the withdraw domain).

### 2.2 Money invariants (hardening over legacy — display-neutral)

1. **Escrow-on-create kept** (legacy semantic), but every movement gets
   a **ledger row**: debit type `p2p_escrow` on order create; credit
   type `p2p_refund` on cancel/seller-return; credit type `p2p_release`
   to the buyer on confirm/dispute-release. /balance "tracked
   operations" then reconciles.
2. **Race-safe fills:** the project-standard atomic guard —
   `UPDATE p2p_sell_orders SET remaining_com = remaining_com - :take
   WHERE id = :id AND status='active' AND remaining_com >= :take`
   with rowcount check; a failed guard aborts the trade (no oversell —
   fixes legacy fragility #6).
3. **Checked credits** everywhere (credit() None → rollback the
   transition, loud log) — same posture as withdraw/transfer.
4. All transitions status-guarded in SQL (`... WHERE status='pending'`),
   not read-then-write.

### 2.3 Deliberate deviations (NEED SIGN-OFF)

- **D1 — third dispute outcome "вернуть продавцу":** admin button that
  returns the trade's COM to the seller's **balance** (ledger
  `p2p_refund`), for the "buyer never paid fiat" case legacy could not
  resolve. Buyer gets dispute_count? No — seller stats untouched, buyer
  is NOT penalized in v1 (no buyer-stats table) — just the money path.
- **D2 — pending-trade auto-expiry 30 min:** legacy trades could hang
  in `pending` forever, locking the order's COM slice. A lazy expiry
  (checked on access + hourly sweep via the existing
  EconomyCleanupSweeper) cancels `pending` trades older than 30 min and
  returns the slice to `remaining_com` (order back to active if it was
  completed by that fill). `paid` trades never auto-expire (dispute
  only). Env `P2P_PENDING_TTL_MINUTES=30`.
- **D3 — rating dead field not ported**; cards show
  "✅ сделок: N | ⚠️ споров: M" instead of the fake 5.0.
- Everything else legacy-exact: 0% fee, market-price-only, 6 currencies,
  partial fills, express algorithm (cheapest-first), menu tree,
  developer-only dispute resolution, no withdrawal-limit enforcement.

### 2.4 Surface (private-only, like legacy)

`/p2p` command (new canonical entry; legacy reached it via the
withdraw menu — we ALSO add the button there) → menu:
Продать COM | Купить COM | Экспресс-покупка | Мои сделки | Все ордера.
All legacy callbacks become CallbackData factories; the sell FSM gets
sweeper timeout rules (10 min) like /support. i18n: the p2p_* legacy
keys already in yaml are reused verbatim where the screen survives;
new copy = `h_p2p_*`.

### 2.5 Implementation (one batch, three parallel workstreams)

- **P1 core:** migration 0010, models, P2pRepo (orders/trades/stats,
  atomic-guard fills), P2pService (create/cancel/buy/express/mark-paid/
  confirm/dispute/resolve×3/expiry) with ALL money invariants +
  exhaustive integration tests (escrow ledger, oversell race, double
  release, expiry). Returns consumption_api.
- **P2 sell-side UI:** /p2p menu, sell FSM (4 steps + sweeper rules),
  my-orders + cancel, my-trades list.
- **P3 buy-side UI:** order book + filters/paging, order detail + buy
  amount + buy-all, express flow, trade cards + buyer "Я оплатил" +
  seller "COM отправлены" + dispute open + admin resolve buttons (3
  outcomes) + all notifications + seller-stats popup.
- Integration pass, once the three land: i18n, router include,
  withdraw-menu button, sweeper rules in app.py, env, command_surface
  (p2p token), full suite, deploy (migration economy 0010), userbot e2e:
  a test account creates a sell
  order → cancels → balance restored; order book renders.

## 3. Risks

Money-critical: P1's tests are the gate — oversell race, double-release,
escrow-vs-ledger reconciliation, expiry-returns-slice. The scam-risk
inherent to out-of-band fiat (legacy reality) is documented in the trade
card copy ("проверьте получение оплаты ДО подтверждения"); real payment
verification stays out of scope (= legacy parity).
