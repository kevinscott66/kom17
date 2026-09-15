# COM↔fiat rate audit — «чтоб не опустошить карман владельца»

Status: **audit complete; R1–R11 shipped — see §8. R12 open (§8.5) — it
is the only recommendation left, and it is a product decision, not a
protection gap.**
Written 2026-08-09 against production (read-only `SELECT`s) and
the strangler tree.

The question this answers: *is the coin rate, across exchanges, markets
and games, set so that the bot owner cannot be drained?* Short answer as
found: **no.** The rate itself was defensible; the **emission side had no
daily ceiling**, and that — not the rate — is what made the wallet
drainable. One user chatting through a script could mint enough COM to
clear the withdrawal floor in about a day, with zero money ever having
entered the system.

That specific path is now closed three times over: emission is capped
(R1), withdrawals require a real deposit (R2), and lifetime payout is
bounded by lifetime deposits (R6, §8.1). The zero house edge on `/roll`
and `/flip` — the multiplication step in the middle of that path — is
closed too (R7, §8.2), and so are the last free multipliers in the
ecosystem — the rake-free PvP pots on `/duel`, `/cpc` and `/pvp_coin` /
`/pvp_dice` (R8, §8.3). No game in the bot is a zero-edge game any
more, and no game is an outlier either: R9 (§8.4) collapsed six
separate bet ceilings onto one, retiring `/cpc`'s legacy 100 000 — ten
times every sibling's. The emission side is closed on the *spending*
side too: R10 (§8.5) stopped a coin-paid shop buy from minting 15 % of
its own price back to the buyer's inviter and the developer, which had
been running the bot's largest sink at 85 % strength — and, at a
1-coin item price, running it in reverse. R11 (§8.6) then closed the
last structural hole — the rouble top-up leg, which priced coins in
roubles while `/withdraw` paid in dollars and so ran an unbounded,
unchosen FX position against the owner — and, in the process, turned
up something larger sitting one line above it: the YooKassa adapter
authenticated the payment *id* and then read the amount and the
recipient out of the unsigned request body anyway, so anyone holding a
real payment id could rewrite both (R11-b, §8.6). What remains open is
R12, raised by R10's sweep (§8.5): a product decision, not a leak.

---

## 1. The two pegs, and the fact that nothing links them

*As found. See §8 for what each of these is today.*

| Peg | Value | Where |
| --- | --- | --- |
| Display / RUB | `_RUB_PER_COIN = 0.1` | `handlers/profile.py:95`, used at `:542` |
| Display / RUB (again) | `"RUB": 0.1`, **pinned, never from the FX API** | `services/currency_service.py:274`, `:399` |
| Top-up / USD | `_COINS_PER_USD = 900.0` | `services/payments/crypto.py:45` |
| Top-up / USD (again) | `COINS_PER_USD: int = 900` | `services/payments/crypto_invoices.py:63` |
| Top-up / USD (again) | `_USD_TO_COINS = 900` | `services/payments/stripe.py:56` |
| Top-up / RUB | `_RUB_TO_COINS = 10` | `services/payments/yookassa.py:63` |
| Withdrawal | `coins_per_usdt = 900.0` | `WithdrawConfig`, `config/settings.py:912` |

Five independent literals for what is economically **one** number, plus
two RUB-denominated ones that only agree with it at a particular USD/RUB
fix — and one of those (the display peg) is *pinned by hand and
deliberately excluded from the live FX feed*: `currency_service` fetches
every other currency from the API and hard-codes RUB.

They happen to agree today: 900 COM = 1 USDT ≈ 90 ₽ ⇒ 0.1 ₽/COM, and
`_DEFAULT_USD_TO_RUB = 90` is the fallback. That agreement is a
coincidence of the USD/RUB rate at the time the constants were written,
and nothing in the code preserves it. When USD/RUB moves, the profile
card keeps promising `balance × 0.1 ₽` while the withdrawal desk pays
`balance / 900` USDT — the two diverge silently, in whichever direction
the market picks. Divergence *up* is a false promise to users;
divergence *down* is quiet extra margin the owner never chose. Both are
bugs.

### 1.1 The RUB top-up leg is an open FX position

`_RUB_TO_COINS = 10` prices coins in **roubles** while `/withdraw` pays
in **USDT**. Nothing reconciles them, so the ecosystem is short USD/RUB
whether the owner wants the position or not:

| USD/RUB | 900 COM costs (YooKassa) | 900 COM pays out | Owner's P&L per 900 COM |
| --- | --- | --- | --- |
| 90 (the fix the constants were written at) | 90 ₽ = 1.00 USD | 1 USDT | break-even |
| 100 | 90 ₽ = 0.90 USD | 1 USDT | **−0.10 USD (−11 %)** |
| 80 | 90 ₽ = 1.13 USD | 1 USDT | +0.13 USD (the user overpays) |

Both directions are bugs. A weakening rouble turns the RUB top-up into a
standing discount on USDT that anyone can arbitrage at will — buy coins
with roubles, cash out in USDT, repeat — and a strengthening one
silently overcharges rouble payers. The exposure is *unbounded in size*:
it scales with top-up volume, not with any configured limit.

This one is **not** in the safe-to-apply set. Re-pricing the RUB leg
changes what a rouble buyer pays, which is a published real-money value
— see **R11** in §7.2. **Shipped in T-020; see §8.6** for what tipped
the decision (the strangler tree turned out to issue no rouble invoice
at all, so there was no in-bot quote to break) and for the one
operational consequence the owner has to act on.

**Buy/sell spread is zero.** `WithdrawConfig`'s own docstring states
the intent plainly: 900 "mirrors the top-up side … so a coins→USDT→coins
round trip is value-neutral." That is correct as parity with legacy and
wrong as economics — it means the ecosystem has no margin anywhere, and
every free coin it mints is a 1:1 liability against the owner's Crypto
Pay wallet.

## 2. Emission — where free COM comes from

Everything below mints coins **out of nothing**. None of it is funded by
money entering the system.

### 2.1 Passive message earning — the hole that was

> **Fixed.** `message_reward_daily_cap` shipped as **R1** (§7.1) and is
> the reason this section is written in the past tense. It is kept
> because the *shape* of the defect is the interesting part; the
> reproduction details are not published while the service is live.

[`middlewares/message_activity.py`](../src/telegram_invite_bot/middlewares/message_activity.py)
credits `coins_message_reward = 1` COM per qualifying group message,
throttled by `_RewardTracker`:

- `message_reward_min_chars = 8`
- `message_reward_cooldown_sec = 20`
- `message_reward_max_per_minute = 3`
- `message_reward_duplicate_window_sec = 120`

**There was no per-day cap.** Every constraint above is a *rate* limit,
and a rate limit with no daily ceiling bounds how fast a balance grows,
not how large it gets: left running, the account earns all day. The
duplicate filter compares normalised text, so it stops repetition and
not persistence. Two multipliers stacked on top:

- VIP `message_bonus: 1` ([vip_repo.py:24](../src/telegram_invite_bot/repositories/vip_repo.py#L24));
- the `xp_boost` shop item, applied at `message_activity.py:261`.

So the daily yield of one patient account was of the same order as the
daily withdrawal cap — unpaid activity alone very nearly funded the
maximum permitted payout, forever. That is the whole of the finding;
the per-account numbers stay out of the repository while the bot runs.
A daily cap (R1, §7.1, default **150 COM/day**) removes the property
outright: a normal chatter never reaches it, and persistence stops
paying.

### 2.2 Daily bonus

`DailyConfig` ([daily_service.py:139](../src/telegram_invite_bot/services/daily_service.py#L139)):
base 10 (or random 1–50), streak `+5`/day capped at 30 days (`+150`),
VIP `+15…25 %`, plus the `✨ Двойной daily` shop item (×2).

Ceiling ≈ `(50 + 150) × 1.25 × 2 =` **500 COM/day** (0.56 USDT).
Bounded and cheap — this one is fine.

### 2.3 Referral commission — minted, not split

`referral_commission_percent = 10`, and
[`referral_commission_service.py`](../src/telegram_invite_bot/services/referral_commission_service.py)
is explicit: *"There is no debit anywhere in this flow — the commission
is minted to the inviter."*

Two consequences:

1. On a **top-up**, the owner banks 100 % of the fiat but issues 110 %
   of the coins. The real margin on every referred sale is therefore
   `-10 %` of face value, redeemable at 900 COM/USDT.
2. It also fires on **shop purchases paid in coins** (legacy
   `buy_item`). Those purchases are the ecosystem's main sink, and 10 %
   of every one of them leaks straight back into supply. Net still
   deflationary (100 burned, 10 minted), so this is a weakened sink
   rather than a leak — but it weakens the only sink that works.

Also live and also minted: `developer_commission_percent = 5`,
`purchase_donation_to_group_percent = 15`.

### 2.4 Promo codes

*As found. Both holes below were closed by T-019/R3: `reward_coins` is
now capped at `MAX_PROMO_REWARD_COINS`, the code's whole mint budget at
`MAX_PROMO_MINT_TOTAL`, and `max_uses` defaults to 1 with `0` rejected
outright — see §8, row R3.*

`/promo_create <CODE> <reward_coins> [max_uses]` —
[promo.py:191](../src/telegram_invite_bot/handlers/promo.py#L191) parses
`reward_coins` as a bare `int(positional[1])` with **no upper bound**,
and `max_uses` **defaults to 0 = unlimited**. Developer-gated, so not an
attack surface, but a single fat-fingered command mints without limit
and cannot be recalled.

## 3. Games — «игры» specifically

*As found. `/roll` and `/flip` now pay ×5.7 and ×1.9 — see §8.2; every
PvP pot (`/duel`, `/cpc`, `/pvp_coin`, `/pvp_dice`) now pays ×1.9 of the
stake instead of the whole ×2 pot — see §8.3.*

| Game | Payout | Win chance | Expected return | Verdict |
| --- | --- | --- | --- | --- |
| `/roulette` | ×1.89 | 1/2 | 0.945 | **5.5 % house edge** — a real sink |
| `/roll` (dice) | ×6 | 1/6 | **1.000** | zero edge |
| `/flip` | ×2 | 1/2 | **1.000** | zero edge |
| `/duel` (PvP) | pot = 2×bet | — | 1.000 | zero-sum, **no rake** |

`stake_games_service.py` documented the zero edge as deliberate legacy
parity, and the live ledger confirmed it empirically: the net across
every game row played was a coin flip's worth of noise around exactly
break-even, which is what a fair game looks like at small n. (The same
constants now carry the R7 edge and say why.)

Zero edge means the games neither drain nor fund the owner — they are
economically inert, which is a wasted sink given they are the most-used
feature. It also means unbounded variance at `MAX_BET = 10 000`: a
single lucky `/roll` pays +50 000 COM (55 USDT) and the house has no
long-run edge to recover it from.

`/duel` taking no rake is the same story: every duel is a pure transfer
between two players, so a colluding pair can shuttle a balance around
for free (harmless in itself, but it means duels launder grinding into
a single wallet at no cost). R8 prices that shuttle — see §8.3.

## 4. Sinks — what does remove coins

- **Shop** — 500…10 000 COM per item (`👑 VIP статус` 5 000,
  `💎 Легендарный` 10 000). It is the single largest sink in the
  ledger by a wide margin; the balances themselves stay out of the
  repository (§5). *(2026-09: economy migration
  `0017_shop_price_rebalance` cuts the sellable prices to 90…990 COM (9…99 ₽) —
  buffs 90, VIP 990, gifts 190/990 with prizes 30–400 /
  350–2 000 at a ~77–79 % payout; see the revision docstring.)*
- **Transfer tax** — `base_tax_rate = 0.05`
  ([transfer_service.py:168](../src/telegram_invite_bot/services/transfer_service.py#L168)),
  halved for VIP. Small: `tax` rows are a rounding error next to `shop`.
- **`/roulette`** — 5.5 % of turnover.

Note the shop was priced **below the withdrawal floor**: before R1, a
single day of message grinding bought any item in it except the
legendary status.

## 5. Exposure — the shape of it

The numbers below are deliberately not in this repository: the live
balance sheet of a running bot is operational data, and the domain and
bot handle are one click away in `README.md`. What matters for the
argument is the *shape*, and the shape is public-safe:

- Every coin in circulation was **minted, never bought** — at the time
  of the audit the `transactions` table contained no `topup`, `stars`
  or `crypto` row at all. The float is therefore pure liability with no
  matching inflow.
- The overwhelming majority of that float sat in **test accounts**, so
  the realised exposure was a small fraction of the nominal one.
- The ledger is dominated by two rows, `admin_give` on the mint side and
  `shop` on the sink side; organic sinks (`tax`, `game`) are a rounding
  error beside them.

So today's *actual* exposure is small and mostly synthetic. The
structure, however, is already the drainable one — it simply has not
been discovered yet.

## 6. The arbitrage, in one sentence

Message rewards were uncapped per day, and `/withdraw` did not require
that anything had ever been paid in — so patient, unpaid activity
converted into a withdrawal request at a fixed rate, bounded only by an
admin's willingness to approve it. Manual approval was the only thing
between that and a real loss: a human, not a rule.

The reproduction steps and the per-account ceiling are deliberately not
published here — the service is live. What is published is the fix:
**R1** put a daily cap on message rewards and **R2** gated `/withdraw`
on lifetime deposits, which together make the ecosystem structurally
un-drainable. Both shipped; see §7 and §8.

## 7. Recommendations

Split by risk, because changing a published price retroactively affects
balances users already hold.

### 7.1 Safe to implement now (protects the owner, harms no existing holder)

| # | Change | Effect |
| --- | --- | --- |
| **R1** | Add `message_reward_daily_cap` (recommend **150 COM/day**) | Closes the main hole: the uncapped daily yield of §2.1 drops to a fixed 150 COM/day. A normal chatter never touches it; a grinder is stopped dead. |
| **R2** | Gate `/withdraw` on lifetime deposits > 0 | Makes the ecosystem **structurally un-drainable**: no account can ever take out more than the money that came in. This is the single highest-value change in the list. |
| **R3** | Bound `/promo_create`: cap `reward_coins`, make `max_uses` default finite | Removes the unrecallable-typo footgun. |
| **R4** | Derive the RUB display peg from `live USD/RUB ÷ coins_per_usdt` instead of the pinned `0.1` | The ₽ figure on the profile card becomes the *actual* cash-out value and can no longer drift into a false promise. |
| **R5** | Collapse the four `900` literals into one settings value the others read | Removes the possibility of a partial rate change. |

### 7.2 Owner's call — real-money / gameplay values

| # | Change | Rationale |
| --- | --- | --- |
| **R6** | ~~Introduce a **buy/sell spread**~~ → **shipped instead as a lifetime payout cap.** See §8.1. | The spread as written was aimed at the wrong target and is *not* being implemented. Once R2 gates withdrawals on having paid, a grinder cannot cash out at any rate, so the spread stops taxing grinders and starts taxing the only people still able to withdraw: paying customers. It would also have devalued every balance already held. What actually leaks after R2 is *winnings on top of a token deposit* — one 5 USDT top-up, then run the balance up in the near-zero-edge games and export all of it. A cap on lifetime payout closes exactly that, changes no published price, and leaves the honest buyer a full unpenalised exit. |
| **R7** | ~~Give `/roll` and `/flip` the edge `/roulette` already has~~ → **shipped.** See §8.2. | `DICE_MULTIPLIER` 6 → **5.7**, `FLIP_MULTIPLIER` 2 → **1.9**. Turns the two most-played games from inert into a ~5 % sink. The direct answer to "игры" in the brief. Breaks legacy parity deliberately. |
| **R8** | ~~Take a 5 % rake on `/duel` pots~~ → **shipped**, on all three PvP pots. See §8.3. | Same reasoning; also prices the collusion shuttle. Written for `/duel` alone, but `/cpc` and `/pvp_coin` / `/pvp_dice` are the same zero-edge two-seat pot with different skins, so raking only `/duel` would have moved the free multiplier one command sideways instead of closing it. |
| **R9** | ~~Lower `MAX_BET` from 10 000, or cap absolute payout~~ → **shipped.** See §8.4. | Bounds single-event variance while the house edge is thin. Shipped as the second half of that row: the 10 000 ceiling stays (R2 + R6 already bound the owner's cash exposure, and cutting it would tax paying customers), but `/cpc`'s legacy 100 000 outlier — ten times every sibling — is gone, and all six games now read one shared constant. Worst single play drops from 190 000 COM to 57 000. |
| **R10** | ~~Reconsider minting referral commission on **coin-paid** shop purchases~~ → **shipped**, both halves. See §8.5. | Restores the shop to a full-strength sink. Top-up commission is a genuine acquisition cost and stays, untouched. Written about the referral half; the developer half is minted by the same call on the same burn, so dropping one and keeping the other would have left the leak at a third of its size. The sweep also found the `max(1, …)` floor turning a 1-coin item into a net money printer, and raised **R12** (below). |
| **R12** | Decide whether a coin-paid *group* purchase should still mint the group creator's payout (`GroupDonationService`, default 15 %) | Same economic shape as R10, and larger. Left in place by R10 because unlike the referral kickback it is a **visible product feature** — the shop card quotes the group's cut before the buy — so removing it changes what a user was promised. Owner's call. |
| **R11** | ~~Close the RUB FX position (§1.1): price the YooKassa leg off the live USD/RUB instead of the frozen `_RUB_TO_COINS = 10`~~ → **shipped**. See §8.6. | Removes an unbounded, unchosen short USD/RUB position. The "needs an explicit decision" caveat turned out to be lighter than written: the strangler tree issues **no rouble invoice** — `/topup`'s YooKassa button only ever renders "pay externally" — so the bot never quoted a rouble price it could break. What it did do was *credit* at a frozen one. The offline path reproduces the old number exactly; only a fix away from 90 moves anything. |

R1–R5 are pure protection and shipped first. R6, R7, R8, R9, R10 and
R11 followed once the owner asked for a rate that does not empty their
pocket "везде: обмены, маркеты, игры" — see §8.1–§8.6 for what each
actually does and what it costs a user. R12 is the only one left, and
it is the only one on this list that would take something away from a
user who was told they would get it.

### 7.3 If only one thing is done

**R2** (withdrawals gated on lifetime deposits). It stops the pure
grinder dead: an account that never paid cannot export anything, no
matter how much COM the emission side mints for it.

Note the limit of that claim — an earlier draft of this document said R2
"caps total lifetime payout at total money in, by construction". **It
does not.** R2 is a *threshold*: it asks whether money ever came in, not
how much. One 5 USDT top-up clears it permanently, and everything won on
top of that deposit was still exportable. The bound R2 was credited with
is what R6 actually delivers (§8.1); the two are independent knobs and
both are on by default.

---

## 8. What shipped

R1–R5 landed in T-019, R6–R11 and R13–R15 in T-020 (§8.1–§8.9). R12 is
left as written above: it changes a published product promise, and is
the owner's decision. R13, R14 and R15 are the odd ones out — none is a
rate finding. R13 is a double-payout window found by carrying R11-b's
question across to the value-transfer surfaces; R14 is the guard that
keeps the owner-only side of those surfaces owner-only; R15 is a silent
money-loss bug found by asking what an `except Exception` around a DB
write actually restores.

| # | Where it lives now | Notes |
| --- | --- | --- |
| **R1** | `MESSAGE_REWARD_DAILY_CAP` (default **150**) in `EconomyConfig`; enforced by the reward tracker in `middlewares/message_activity.py` | The cap clamps rather than rejects, so an `xp_boost` multiplier can't make the effective ceiling vary. Set to `0` to disable. The tracker refreshes a user's LRU slot *before* the gates: a capped user who fell out of the map would have come back with a fresh allowance, which is exactly the user who must not. |
| **R2** | `WITHDRAW_REQUIRE_DEPOSIT` (default **on**); `WithdrawService` refuses with `CreateOutcome.NO_DEPOSITS` unless `TransactionsRepo.lifetime_deposits() > 0` | Deposits are ledger rows typed `purchase_*`. Minted coins keep full in-ecosystem utility (shop, `/send`, games, `/daily`) — they simply can't be exported as USDT. Turn the flag off to honour pre-gate balances. |
| **R3** | `MAX_PROMO_REWARD_COINS = 10_000`, `MAX_PROMO_MINT_TOTAL = 100_000` in `services/promo_service.py`; `/promo_create` default `max_uses` 0 → 1 | `max_uses == 0` (unlimited) is now rejected outright rather than budget-checked as zero — an unbounded total can't pass a budget check honestly. |
| **R4** | `services/currency_service.py` anchors the rate table on **USD** (`1 / coins_per_usdt`) and derives RUB from the live fix; `handlers/profile.py` asks the same shared service | The anchor was inverted, not merely un-pinned: USD is the only leg with a real price, because `WITHDRAW_COINS_PER_USDT` is what a coin actually leaves at. Numerically identical at the historic 900 / 90; correct away from it. The degraded (API-down) path re-anchors too, so a configured spread can't be silently ignored when the upstream is unreachable. |
| **R5** | `services/payments/rates.py` — `COINS_PER_USD` plus `coins_for_usd`, `coins_for_usd_cents`, `rub_per_coin` | All four USD-denominated sites and the `WITHDRAW_COINS_PER_USDT` default now read it. `tests/unit/services/payments/test_rates.py` pins that they agree, so a future price change can't land on three sites out of four. |

Deliberately unchanged **by this batch**: the published 900 COM/USDT buy
rate, every game multiplier, `MAX_BET`, the duel rake, and the RUB
top-up leg. Nothing in T-019 alters a price a user has already been
quoted or devalues a balance anyone already holds. The stake-game
multipliers moved later, in T-020 (§8.2), the PvP pots after them
(§8.3), `/cpc`'s outlying ceiling after that (§8.4), the shop's
commission kickback after that (§8.5), and the RUB leg last (§8.6).

### 8.1 R6 — lifetime payout cap (T-020)

Shipped, on by default, and deliberately **not** the spread §7.2
originally proposed.

| Where it lives | Notes |
| --- | --- |
| `WITHDRAW_PAYOUT_RATIO` (default **1.0**) in `WithdrawConfig` | `1.0` = a user may cash out exactly what they paid in, never more. Raise it to hand back winnings above deposits (`1.5` = up to 150 %); lower it to take a house cut on the way out; `0` disables the cap — the same "0 means off" convention as `MESSAGE_REWARD_DAILY_CAP`. |
| `WithdrawalsRepo.lifetime_usage()` | Sums every `pending` + `completed` request the user has ever made. Pending counts: an unapproved request is escrowed money on its way out. Rejected does not — the escrow was refunded, so the headroom comes back. Unlike the rolling `period_usage`, it counts rows with a NULL `created_at`: money that left is money that left. |
| `WithdrawService.check_lifetime_gate()` → `CreateOutcome.PAYOUT_CAP_EXCEEDED` | Runs both lifetime gates as one check. `create()` calls it inside the escrow transaction; `handlers/withdraw.py` calls the same method at the amount step so the refusal arrives before the confirm card is drawn. One implementation, so the number the card promises is the number the gate enforces. |
| `WithdrawService.payout_headroom()` | Read-only, `None` when the cap is disarmed. Feeds the lifetime line on the `/withdraw` intro — the ceiling does not reopen on a clock, so the user sees it before typing an amount rather than after. |

The invariant this buys, which R2 alone did not: **for every account,
total coins exported ≤ total coins paid for × ratio.** Summed over
accounts, total payout ≤ total money in — and that survives `/send`
transfers between users, because each account is bounded by its own
deposits regardless of where the coins came from.

Scope of the guarantee, stated honestly: the gate is a live read, not a
stored counter, so it cannot drift — but like the rolling quotas beside
it, two `create` calls that interleave between the read and the escrow
could both see the same headroom. The FSM narrows that to near-zero (a
confirm consumes its amount before `create` runs, and `/withdraw`
refuses re-entry mid-flow), and the wallet debit is a conditional UPDATE
so no overdraft is possible either way. A hard serialisation would need
row-level locking the SQLite deployment does not offer.

Consequence worth knowing before flipping flags: `WITHDRAW_REQUIRE_DEPOSIT=false`
on its own no longer restores pre-T-019 behaviour. Both knobs have to be
off. `tests/integration/services/test_withdraw_service.py` pins that,
along with each gate binding independently of the other.

### 8.2 R7 — house edge on `/roll` and `/flip` (T-020)

Both stake games paid exactly fair odds: `/roll` wins 1 time in 6 and
paid 6×, `/flip` wins 1 in 2 and paid 2×. Expected return per coin
staked was 1.0 — the house could not win, and a patient script farmed
them at no cost. `/roulette` has carried a ~5 % edge since A-10
(`MULTIPLIER = 1.89` on a 50 % chance); the other two now match it.

| Where it lives | Notes |
| --- | --- |
| `DICE_MULTIPLIER = 5.7` in `services/stake_games_service.py` | Expected return 5.7 / 6 = **0.95**. A 100-coin win pays 570 gross (net +470) instead of 600 (net +500). |
| `FLIP_MULTIPLIER = 1.9` | Expected return 1.9 / 2 = **0.95**. A 50-coin win pays 95 gross (net +45) instead of 100. |
| `StakeGamesService.settle(multiplier: float)` → `int(round(bet * multiplier))` | Same rounding `RouletteService` has always used, so the three casino services cannot drift on the half-coin case. Balances stay integers; `games.profit` stays a signed int. |
| `game_dice_desc`, `game_flip_desc`, `faq_part2`, `h_dice_hint_stake` | The four places the multiplier is quoted to users, in both languages. `h_dice_hint_stake` interpolates the constant, so it can never go stale; the other three are prose and are pinned by the parity test's divergence allowlist. |

This is the first change in the programme that **reduces a payout users
have already seen**, so it is worth being plain about the trade: a
player who wins is paid ~5 % less than before. Nobody's balance is
devalued, no published buy rate moves, and the games stay comfortably
positive-expectation for a lucky player — but the *long-run* drift now
points at the house instead of away from it, which is the entire point.
Without it, R6's cap is the only thing standing between an idle script
and the owner's float.

`tests/unit/services/test_stake_games_service.py` pins the inequality
itself (`DICE_MULTIPLIER < 6`, `FLIP_MULTIPLIER < 2`, both within a
0.9–1.0 expected-return band) rather than only the literals, so the edge
can be re-tuned without editing tests, while a silent return to legacy
parity fails loudly.

Legacy `bot.py` still computes 6× / 2× at `bot.py:2575/2578`. Its
`translations.py` copy is therefore correct *for legacy* and must not be
"fixed" to match ours — `tests/unit/i18n/test_legacy_parity.py` carries
an explicit `_INTENTIONAL_DIVERGENCE` allowlist naming those keys and
the reason, plus a companion test that fails if an allowlisted key stops
diverging (a stale exemption is a hole in the parity check).

### 8.3 R8 — rake on every PvP pot (T-020)

The last zero-edge games in the ecosystem. Three commands settle a
two-seat pot — `/duel`, `/cpc` (rock-paper-scissors) and `/pvp_coin` /
`/pvp_dice` — and the audit's §7.2 row named only `/duel`. Raking one of
three would have moved the free multiplier sideways rather than closing
it, so all three ship together. Both seats escrow `bet`, so the pot is
exactly `2 × bet`, and legacy handed the winner all of it.
That is a *pure transfer*: no coin is created and none is destroyed, the
house takes nothing, and the expected return per coin staked is 1.0 —
the same fair-odds problem R7 fixed for the single-player games, wearing
a PvP skin.

Two things follow from it, and only the second is obvious:

1. **A colluding pair shuttles for free.** §3 flagged this: two accounts
   duelling each other move a balance wherever they like at zero cost,
   which is how grinding across many accounts gets laundered into the
   one wallet that will call `/withdraw`.
2. **The pot itself is a redeemable liability.** Every coin in it is
   claimable at `/withdraw` out of the owner's pocket. A game that
   neither mints nor burns still does nothing to reduce that liability
   while being the most-played thing in the bot.

R8 makes all three take ~5 %, matching `/roulette` and the R7 stake
games. Nothing else in the ecosystem is now a free multiplier.

| Where it lives | Notes |
| --- | --- |
| `games/pot.py` — `PVP_PAYOUT_MULTIPLIER = 1.9` and `split_pot(bet, multiplier) -> (payout, rake)` | **One** implementation for all three games. Three private copies of the same arithmetic is exactly the shape that drifts, and a drift here is not a cosmetic bug: the three games are the same product from a player's seat, so a cheaper cut on one is an arbitrage — players simply move to whichever command pays best. `tests/unit/games/test_pot.py` pins that each game's configured multiplier *is* this one, and that all three resolvers agree coin-for-coin. |
| `DuelConfig.payout_multiplier`, `RpsConfig.payout_multiplier` | Both were the literal `2`; both now read `PVP_PAYOUT_MULTIPLIER`. The winner collects `1.9 × bet` of a `2 × bet` pot; the remaining `0.1 × bet` is burned. A 100-coin duel pays 190 gross (net +90) instead of 200 (net +100). |
| `split_pot`'s `int()` **floor** | Unlike R7's `int(round(...))`, and the direction is load-bearing rather than stylistic. The single-player casino services pay from the house and can round to nearest; a PvP pot is finite — exactly `2 × bet` was escrowed — so rounding up could pay the winner more than the two stakes hold and mint coins. The floor always errs toward the house and can never mint. |
| `rake: int` on `DuelRoundResult` / `RpsRoundResult`; `payout` + `rake` on `PvpAcceptResult` | Carried on the result rather than re-derived by each caller, so the ledger row, the result card and any future report all quote the same number by construction. The `/pvp_*` card in particular now reads the number the service actually credited instead of recomputing `bet * 2`. |
| `duel_rake` / `rps_rake` / `pvp_rake` ledger rows, written only when `rake > 0` | `from_id=None, to_id=None` on purpose: the burn belongs to neither wallet. Attributing it to the loser would double-count against them — their `duel_loss` row (or the escrow, in `/pvp_*`) already covers the whole stake — and skew any per-user aggregate that sums by `from_id` without filtering on type. `WHERE type='duel_rake'` totals exactly what the game earned; a per-user audit sees nothing extra. |
| `h_rps_result_win`, `h_duel_result_win`, `h_duel_match_result`, `h_pvp_result_coin`, `h_pvp_result_dice` (ru + en) | The winner's card names the cut — *«Комиссия банка: 10 🪙»* / *"House fee: 10 🪙"* — rather than quietly paying less than the ×2 players remember. All are `h_`-prefixed and therefore outside the legacy-parity check, so unlike R7 this needed no divergence allowlist entry. |
| `h_pvp_offer_coin`, `h_pvp_offer_dice` — `{pot}` → `{prize}` | The `/pvp_*` **offer** card is a contract an opponent taps Accept on, so it has to quote what the winner collects (`1.9 × bet`), not the pot. Leaving it at `2 × bet` would have advertised 10 coins the payout never pays. |

**No rake on a tie.** A tie refunds both stakes and writes `rake = 0`,
so no row is written at all. A tie is a non-event that neither player
chose; charging for it would be the one part of this change a player
could fairly call unfair. The gate is on `rake > 0` rather than on the
outcome, which also means the `house_edge_on_tie` knob in the RPS
resolver still records its burn if it is ever turned on — an early
`return` in the tie branch would have silently dropped it.

**Where the coins go: nowhere.** This is the first change in the
programme that *destroys* supply rather than redirecting it. The rake is
not credited to an owner wallet — there is no such wallet, and inventing
one would create a balance that is itself withdrawable. The integration
tests pin the consequence directly for `/duel`, `/cpc` and `/pvp_*`
alike: total money supply after a settled game is `supply_before − 10`
on a 100-coin bet. Legacy left it flat, which is exactly what made these
three free money for a patient (or colluding) player.

**What it costs a player.** 5 % of the pot on a win, capped in absolute
terms by `MAX_BET`: 1 coin at the 10-coin minimum, 500 at a 10 000-coin
duel. The floor bites hardest at the very bottom — a 10-coin win pays
19, a 9.1 % effective cut instead of 5 % — which is a rounding artefact
of integer coins, one coin in absolute terms, and always in the house's
direction. Swept across the whole legal bet range there is no bet where
`payout > bet * 2` or `rake < 0`.

Tests, including two coverage gaps this change closed:

* `tests/unit/games/test_pot.py` (new) — the no-mint property across
  fractional bets, that the flooring error is at most one coin and
  always in the house's direction, the inequality `< 2` rather than only
  the literal, and that all three games agree coin-for-coin.
* `tests/unit/games/test_duel.py` (new) — the duel resolver had **no**
  unit coverage before this; it was reached only through the service and
  e2e layers, which pin settled balances rather than the arithmetic that
  produces them.
* `tests/unit/handlers/test_pvp_result_card.py` (new) — the `/pvp_*`
  result card had no coverage either, which matters because the
  translator renders an unpassed placeholder *literally* rather than
  raising. A dropped `rake=` would have shipped a card reading
  "House fee: {rake}" to real users; these tests scan every branch for
  surviving braces.
* `tests/integration/services/test_{duel,rps,pvp}_service.py` — the
  money-supply burn, the rake row's NULL attribution, and that a tie
  writes no rake row at all.

### 8.4 R9 — one bet ceiling for the whole ecosystem (T-020)

Six commands take a stake: `/roll`, `/flip`, `/roulette`, `/duel`,
`/cpc` and `/pvp_coin` / `/pvp_dice`. Five capped it at 10 000. `/cpc`
capped it at **100 000** — legacy's `cpc_max_bet` default
(`rock_paper_scissors.py:664-665`), ten times every sibling.

Nothing justified the gap. From a player's seat `/cpc` is the same
two-seat pot as `/duel` with a different skin, so the outlier bought
exactly two things, both bad: a command where a single round could swing
190 000 COM, and — before R8 — a free shuttle ten times wider than
anywhere else for a colluding pair. R9 brings it in line and makes the
alignment structural rather than coincidental.

| Where it lives | Notes |
| --- | --- |
| `games/limits.py` — `MIN_BET = 10`, `MAX_BET = 10_000` | **One** pair of numbers behind all six games, for the same reason `games/pot.py` holds the pot split once. Six copies of a policy constant is the shape that drifts, and a drifting bet ceiling is not cosmetic: it is one command quietly paying better than the rest. |
| `DuelConfig`, `RpsConfig`, `pvp_service.MIN_BET/MAX_BET`, `roulette_service.MIN_BET/MAX_BET`, `stake_games_service.DICE_*`/`FLIP_*` | All six now read the shared constants. The per-game names stay, so handlers and tests keep their own vocabulary — but there is one number behind them, and diverging two games is now a decision made in one file rather than an edit to one literal that forgets the other five. |
| `h_pvp_invalid_bet` (ru + en) — `{min_bet}` / `{max_bet}` | The only user-facing string that still baked the bounds in as literals ("от 10 до 10000"). It happened to be right; it would have gone stale the first time the ceiling moved. A refusal that names a wrong number is worse than one that names none. |
| `tests/unit/games/test_limits.py` (new) | Sweeps all six ceilings and all six floors into a set and asserts it has one element, so a seventh game with its own literal fails at the moment it is added. Also multiplies each ceiling back out by its payout multiplier — bet ceilings only bound variance *together* with the multipliers they feed, and raising one alone is the easy mistake. |

**Why the ceiling stays at 10 000 rather than dropping further.** §7.2
offered "lower `MAX_BET`, or cap absolute payout". 10 000 COM is roughly
11 USDT — a modest maximum stake for someone who actually paid for their
balance — and the owner's cash exposure is already bounded from the far
end: R2 gates withdrawals on having deposited and R6 caps lifetime payout
at lifetime deposits, so no run of luck exports more than came in.
Cutting the ceiling below 10 000 would tax honest customers to buy
protection the ecosystem already has. What R9 buys is the *variance*
half of that row: after this, no single play in the bot can pay more
than **57 000 COM** (`/roll`'s 5.7× on the 1-in-6 guess, the longest odds
offered), down from `/cpc`'s 190 000.

**Safe to deploy mid-match.** Every game validates the bet *before* any
stake is escrowed — step 1 in `RpsService.play`, likewise in
`DuelService` and `PvpService` — so an in-flight offer above the new
bound fails clean with no coins moved. `/cpc` surfaces it as
"match not found" and logs the outcome; nothing is left half-settled.
Pinned by `test_bet_above_max_rejected`.

**What R9 deliberately does not do.** `/duel`, `/cpc` and `/pvp_*` carry
no per-hour or per-day play caps (`GameLimitService`'s 180 s / 8 / 25
windows cover only `/roll`, `/flip` and `/roulette`). That is left alone
on purpose: since R8 every PvP pot burns ~5 %, so volume on those three
*shrinks* the redeemable supply. Rate-limiting them would slow down the
one activity in the bot that works in the owner's favour.

### 8.5 R10 — a coin-paid shop buy mints nothing (T-020)

| Where | What changed |
| --- | --- |
| `services/purchase_service.py` | The `commissions=` constructor argument, the `last_commissions` attribute and the post-buy `apply_purchase_commissions` call are **removed** — not disabled. |
| `middlewares/economy.py` | Builds `PurchaseService(session)`. The `referral_commission_percent` knob is gone from the middleware; `developer_commission_percent` / `developer_id` stay, because the RR-2 #14 group-donation split still uses them. |
| `handlers/shop.py` | `_notify_referrer_kickback` deleted with both call sites. The identical DM still fires on the paths that do pay a commission (`handlers/topup.py`, `webhook/payments.py`). |
| `i18n` `h_referral_body`, `h_referral_commission_credited` | Re-worded from "покупок монет" / "coin purchases" to **пополнение / top-up**, matching what the programme now actually pays on — and matching `h_profile_soc_ref_rate`, which already said it. |
| `tests/integration/services/test_purchase_commissions_wiring.py` | The three shop-wiring tests are replaced by four R10 tests: mints nothing, is a pure sink (conservation check), the 1-coin case, and the absent seam. |

**What the leak was.** A shop buy is paid in coins the buyer already
holds. The price is debited and never re-credited to anyone — it is the
bot's single largest sink, and the only sink whose size the owner sets
directly. On top of that burn, #75 faithfully ported legacy's
`apply_purchase_commissions` (bot.py:13243): 10 % minted to the buyer's
inviter and 5 % to the developer wallet. Every 1 000 COM burned in the
shop therefore returned 150 COM of **fresh** supply. The sink ran at
85 % strength and no setting said so.

**The sharp edge.** `purchase_commission_amount` is legacy's
`max(1, int(amount * percent / 100))` — a one-coin **floor**, not a
plain percentage. At an item price of 1 COM that floor fired on both
halves: 1 coin burned, 2 minted. Shop items default to infinite stock
(`stock = -1`), so a single 1-coin item made the shop a net money
printer bounded only by how fast the buy could be repeated. Pinned by
`test_one_coin_item_no_longer_prints_money`.

**Why the top-up commission stays.** The four real-money paths — Stars,
Crypto Pay, YooKassa, Stripe — still pay both halves, and should. There
the mint is an *acquisition cost*: money entered the system, and the
inviter who brought that customer is paid out of a real inflow. That is
a referral programme. Paying a cut when an existing user spends coins
they already had is not — it is the same wallet handing itself supply.

**What this costs a referrer.** Nothing they were counting on. The
programme's headline is unchanged (10 % of every top-up, for life), and
that was already what `/profile`'s social panel and the referral card
promised — the shop cut was never advertised anywhere. The two strings
that said "покупок монет" were ambiguous between "bought coins" and
"bought in the shop"; they now say *пополнение* outright, so the promise
and the payout finally read the same.

**Still minting on a coin-paid buy: the group donation.** A purchase
made *for a group* routes `PURCHASE_DONATION_TO_GROUP_PERCENT`
(default 15) to that group's rating, and pays the group's creator that
slice minus the developer cut — as a **mint**, by
`GroupDonationService`'s own admission in its docstring. R10 leaves it
alone deliberately: unlike the referral kickback it is a *visible
product feature* (the shop card quotes the group's cut before the buy),
so removing it is a product decision rather than a rate correction.
It is logged here as **R12** so it is a decision on the record and not
an oversight — the economic shape is identical to what R10 just removed,
and it is larger.

### 8.6 R11 — the rouble leg, priced through the dollar anchor (T-020)

| Where | What changed |
| --- | --- |
| `services/payments/rates.py` | New `coins_for_rub(amount_rub, usd_to_rub)` — `amount_rub / usd_to_rub × COINS_PER_USD`, Decimal end to end, floored. New `sane_usd_to_rub` + the `USD_RUB_MIN/MAX` band. The module docstring no longer lists the RUB rate as "deliberately NOT centralised". |
| `services/payments/yookassa.py` | `_RUB_TO_COINS = 10` deleted. The adapter takes a keyword-only `usd_to_rub`, defaulted to `FALLBACK_USD_TO_RUB` and re-clamped in the constructor. |
| `services/currency_service.py` | New `usd_to_rub()` — recovers the raw fix from the already-cached COM table as `base["RUB"] / base["USD"]`. No second fetch, no second cache. |
| `webhook/payments.py` | `build_router(currency_service=None)`; `_resolve_usd_to_rub` awaits the fix under a 3 s ceiling and degrades to the anchor on any failure. |
| `webhook/server.py` | Builds the FX service and warms it during lifespan startup (real runs only), so no payment pays the cold-fetch latency. |
| tests | `test_rates.py` (+13), `test_adapters.py` (+5 cases across 4 tests), `test_currency.py` (+3), `test_payment_webhooks.py` (+3, plus an autouse offline-FX fixture). |

**What the position was.** `_RUB_TO_COINS = 10` sold coins in roubles;
`/withdraw` bought them back in USDT at `COINS_PER_USD = 900`. Ten coins
per rouble is exactly 900 per dollar *only* at USD/RUB = 90 — the fix
those two constants were written at. Anywhere else the two legs
disagree, and the disagreement is directional:

| USD/RUB | 1 USD of roubles credited | Redeemable at the desk for | Owner's loss per dollar |
| --- | --- | --- | --- |
| 75 | 750 COM | 0.83 USD | — (the *payer* is short 17 %) |
| 90 | 900 COM | 1.00 USD | break-even |
| 120 | 1 200 COM | 1.33 USD | **0.33 USD (33 %)** |
| 150 | 1 500 COM | 1.67 USD | **0.67 USD (67 %)** |

Nothing capped it. Deposit size is unlimited, the loop is
buy-in-roubles → withdraw-in-USDT → repeat, and every turn is
risk-free because both prices were fixed in advance. This was the
single largest remaining hole in the document, and unlike the emission
holes it did not require any grinding — only a bank card.

**Why it was safe to just fix.** §7.2 had this filed as "owner's call"
because re-pricing changes what a rouble buyer pays. Reading the
strangler tree closed that objection: `handlers/topup.py` has **no
in-bot YooKassa checkout** — the button renders `h_topup_method_external`
when the provider is configured and `h_topup_method_unavailable`
otherwise. The bot never issued a rouble invoice, so there was no quoted
price to break. The frozen constant lived *only* on the crediting side.

And the offline path is bit-identical: at `FALLBACK_USD_TO_RUB = 90` the
derivation returns exactly `amount × 10`, pinned by
`test_coins_for_rub_reproduces_the_legacy_rate_at_the_legacy_fix` across
six amounts. A dead FX upstream, a garbage quote, a slow one, or a
router built without a currency service all land there. **A top-up is
money the payer has already parted with; refusing to credit it because a
free FX endpoint had a bad minute would be the worst available trade**,
so every failure mode degrades to the old price rather than to an error.

**The sanity band.** A quote that prices a mint gets checked twice —
once in the router, once again inside the adapter — against
`30 ≤ USD/RUB ≤ 300`, with `math.isfinite` first so `NaN` (which fails
every comparison and would sail through a bare range check) cannot
reach the `Decimal` conversion. The band is deliberately wide: it
detects a broken upstream, it is not an opinion about where the rouble
should trade. Without it, an outage page parsed as `USD/RUB = 1.0`
would have credited **900 coins per rouble**.

**The two timeouts, and why their order matters.** The webhook cannot
reach the `CurrencyService` the bot router holds (it lives in a closure),
so it builds its own — same endpoint, same withdraw anchor, so the two
surfaces cannot quote different numbers. That service gets an httpx
timeout of 2.5 s, capped deliberately *below* the router's 3 s
`asyncio.wait_for`. The ordering is the whole point: when the service
times out itself it falls back to the offline table **and caches that
for the full TTL**, so a hanging upstream costs one 2.5 s wait an hour.
Had `wait_for` won the race it would have cancelled the fetch before the
cache was written, and every subsequent deposit would have paid the wait
again. `wait_for` stays as a backstop that should never fire.

**R11-b — the body was never evidence.** Hardening the rate exposed a
larger hole one line above it. YooKassa does not sign its webhooks, so
the adapter authenticates by calling `Payment.find_one(payment_id)`.
That call proves *a* payment with that id succeeded — and the code then
read the amount, the currency and `metadata.user_id` **out of the
request body anyway**. Anyone holding a real succeeded payment id (its
own payer, first of all) could POST the notification themselves with
`amount.value` rewritten: a 100 ₽ payment reported as 1 000 000 ₽
credited ten million coins, once per payment, with `metadata.user_id`
naming whichever account they liked. Every field the credit depends on
now comes off the reverified record; the body contributes the id and
nothing else. Three further guards landed with it — a payment settled
in a currency other than RUB is refused rather than priced as roubles
(a 90x discount), a reverified payment carrying no `user_id` is refused
rather than falling back to the body, and the amount is checked with
`Decimal.is_finite()` before anything else, because `Decimal("NaN")`
parses silently and then *raises* on `<= 0` while `Decimal("Infinity")`
compares fine and blows up later inside `int()`.

**The same sweep across the other two providers.** Crypto Pay and
Stripe both sign their bodies (HMAC-SHA256 and `construct_event`
respectively), so "the body is not evidence" does not apply to them —
but the currency question does, and it had the same answer. Stripe's
`amount_total` was priced through `coins_for_usd_cents`, which is USD
in its name and nowhere in its arithmetic: a session settled in roubles
would have sold 17 991 coins for about twenty dollars' worth of them.
Non-USD sessions are now refused. Crypto Pay was already
currency-correct — it multiplies by the signed `paid_usd_rate`, so any
asset converts properly — and only needed a `math.isfinite` guard so a
malformed `amount` returns 200-and-drop instead of a 500 the provider
would retry forever. One unrelated find went with them: Stripe's
verify→parse event cache was keyed on `id(body)`, and CPython reuses
those once the bytes are collected, so an entry stranded by an
exception could later be served to an unrelated request. It is keyed on
the body's bytes now.

**One thing the owner has to do.** Because the credit rate now floats
and the *storefront* is external, the price quoted on whatever page
issues the YooKassa payment must be generated from the same rate — or
it will drift from what the bot credits. The adapter already logs a
WARN on every such mismatch (`metadata.coins != server-derived`), which
before R11 meant "tampered checkout" and now also means "your storefront
is quoting a stale rate". That log line is the thing to watch after
deploy. Note the sign: USD/RUB above 90 means buyers now receive *fewer*
coins per rouble than the old constant gave them — which is the point,
and is exactly what "чтоб не опустошить карман владельца" asks for, but
it is a change a rouble buyer can notice.

**What did not change.** The dollar providers (Stars, Crypto Pay,
Stripe) were already priced off `COINS_PER_USD` and are untouched. No
existing balance is revalued — R11 prices new deposits only.

### 8.7 R13 — the escrow could be refunded while the payout was in flight (T-020)

R6–R11 all asked the same question about *pricing*: what is a coin
worth when it leaves. R13 came out of asking the R11-b question —
"what does this code treat as a fact, and who controls it?" — of the
value-transfer surfaces instead (`/withdraw`, `/send`, `/check`, P2P).
Nearly all of them answered well: `EconomyRepo.debit` is a single
conditional `UPDATE ... WHERE balance >= amount`, `/check` claims sit
behind an atomic decrement plus a `UNIQUE` row, P2P escrows on create
and every release is a status-guarded transition with a checked credit,
and `/send` brackets its debit/credit pair in a SAVEPOINT. One did not.

**The window.** `WithdrawService.approve` — the automatic Crypto Pay
payout — read the request, confirmed `status == 'pending'`, released
its read transaction (correctly: a DB transaction must not be held
across seconds of network I/O), paid, and *then* claimed the row. Two
guards covered two of the three races: `spend_id = wd_<id>` stops a
second transfer, and the conditional claim stops a second completion.
Neither covers the third:

```
approve   reads pending ─┐
                         ├─ reject() claims pending → rejected
                         │  and CREDITS THE ESCROW BACK
approve   pays USDT ─────┘
approve   claim_terminal fails → "already processed"
```

The user ends up with the coins *and* the crypto. The old code called
that outcome benign, and its comment explained why in terms of the
provider's dedup — "the user was paid exactly once regardless". True,
and the wrong invariant: the money that left twice left through two
different doors. The log line said the request was merely already
processed.

**The fix.** A non-terminal `processing` lease, claimed and *committed*
before the provider call, released on a provider refusal, and finalised
to `completed` after. `reject` and `approve_manual` both guard on
`pending`, so a leased row is untouchable by either — the refund and
the manual "paid out-of-band" flip both become `ALREADY_PROCESSED`
instead of a second payout. The commit matters as much as the claim:
the rejecting admin runs in a different session, and an uncommitted
lease is invisible to them.

`claim_processing` deliberately matches `pending` **or** `processing`.
A process that dies mid-transfer would otherwise strand the request in
a status no admin surface lists; re-driving it is safe because the row
stays un-refundable throughout and `spend_id` dedupes the transfer.
Terminal rows never match, so this cannot resurrect a finished payout.

`processing` also joins the quota statuses. Escrow held and money
leaving is exactly what the daily/monthly caps count, and omitting it
would have opened a second window the width of the provider round-trip
for a concurrent withdrawal to slip through.

**Scope, honestly stated.** `approve` is not wired to a handler today —
v1 keeps payouts manual (`approve_manual`), which is claim-first and
was never exposed. So this is a latent defect, not an active leak: the
fix is worth having because the method is live, tested, and one wiring
decision away from being the payout path, not because coins are
currently walking out the door.

Pinned by `test_reject_cannot_refund_a_payout_already_in_flight`
(a fake Crypto Pay client that runs `reject` from inside `transfer`,
putting it exactly in the window), plus tests for the manual-approve
block, the release-and-retry path, and the quota accounting. All four
fail against the pre-R13 code.

### 8.8 R14 — the authorization gates, made unforgettable (T-020)

R13 finished the "who controls this value?" sweep of the *user-facing*
money surfaces. The same question asked of the *owner-facing* ones has
a much shorter answer, and a much worse failure mode: `/give` mints
coins from nothing, and the withdrawal approve/reject callbacks decide
whether real USDT leaves the app wallet. Neither is rate-limited,
capped, or reversible. If either were reachable by a stranger, every
number in this document would be moot.

**What actually protects them.** Nothing structural. The routers under
`handlers/admin/` filter on chat type at most; there is no
authorization middleware. The only thing between a stranger and
`/admin_envscan` — or `/give 1000000` — is an in-handler
`settings.bot.is_developer(user.id)` call. Every one of the 117
registrations across those 111 modules was checked by hand and by
script: **all of them gate, and all of them gate before the first DB
read.** No live defect. That is the good news and also the whole
problem.

**Why a passing audit still needed a fix.** This surface is ~111
modules of near-identical boilerplate, and a new one gets written by
copying a neighbour. A copy that drops four lines reads completely
normally in review — there is no missing import, no type error, no
failing test, nothing to notice. The gate is load-bearing precisely
where it is least visible. An audit proves today; it does nothing about
the next module.

So R14 ships no behaviour change at all — it ships
`tests/regression/test_authorization_gates.py`, an AST guard in the
same shape as `test_money_call_sites.py`. For every function handed to a
`router.message.register(...)` / `router.callback_query.register(...)`
under `handlers/admin/`, it requires that the function — or, within
three hops of same-module delegation — calls `is_developer` **in a
guard context**: an `if`/`while`/`assert` test, a comparison, a `not`,
or a returned verdict.

Two details earn their keep:

* **Hop-following**, because the house pattern registers a thin
  `_entry` closure that delegates to `handle_admin_x(message,
  settings)` where the real gate lives. A guard that only inspected the
  registered function would fail all 117.
* **The guard-context requirement**, because a bare
  `settings.bot.is_developer(uid)` whose result is discarded *reads*
  like a check and enforces nothing. A test that merely greps for the
  name would wave that through — which is exactly the shape a careless
  refactor produces.

Both were verified by sabotage: deleting the gate from
`handlers/admin/uptime.py` fails the guard, and so does keeping the
call but discarding its result. A registration whose handler is a
lambda, or resolves outside the module, fails too — unprovable is
treated as ungated, not as fine.

The escape hatch (`# admin-gate: allow (<reason>)`) exists so the guard
can never become the reason a legitimate change is blocked, and a
second test asserts it is unused. A genuinely public command does not
belong in `handlers/admin/`; it belongs beside the other user-facing
handlers, where nobody reading it assumes an owner gate.

**The second gate, and the reason this file is plural.** The same sweep
asked the same question of the *group*-admin surfaces, and found a
second invariant with the same shape and a nastier failure mode.
`utils/telegram_admin.is_user_admin` returns `bool | None` — `None`
when the `get_chat_member` call itself failed — precisely so each
caller picks its own fail direction. That matters because the safe
direction is not the same one twice:

| Question | Safe answer on an API error |
| --- | --- |
| "may this user ban?" (actor) | no — refuse the action |
| "is this user an admin I must not ban?" (target) | yes — assume protected |

Write `if await is_user_admin(bot, chat, uid):` and you have silently
chosen falsy-on-error for both. That is right for the actor and exactly
backwards for the target: a transient Telegram blip turns into "ban the
chat owner". This repo has already had that bug once — it is what
R-FIX-007 closed — and nothing in the type system objects to its
return, because `bool | None` is perfectly truth-testable.

All 13 call sites in the tree handle the tri-state correctly today
(`is True`, `is not True`, an explicit `is None` branch, or antiflood's
deliberate `verdict is None or verdict` — an unknown status there means
"exempt from flood throttling", which is the benign direction). The
guard requires exactly that: the result must be consumed through an
identity comparison, on the call or on the name it is bound to, in the
same function. `==` is not accepted — `x == True` reads as a value test,
not the three-way discrimination the contract is about.

Sabotage-verified like the first: rewriting `_is_target_protected` back
into its pre-R-FIX-007 truth-testing form fails the guard, naming the
function.

Neither guard changes a single line of runtime behaviour. They exist
because both invariants are invisible — a missing gate and a leaked
`None` both look like ordinary code — and because the cost of learning
about either one in production is measured in the owner's wallet.

### 8.9 R15 — the swallowed exception that ate the purchase (T-020)

`PaymentsService.handle_event` paid the referral kickback inside the
same transaction as the buyer's top-up, wrapped in a blanket
`except Exception` whose comment promised *"a kickback bug must never
void a customer's paid top-up"*. For any DB-level failure that promise
was exactly backwards.

Catching an exception does not undo what it did to the transaction. A
failed flush deactivates the SQLAlchemy transaction; from that moment
the session is unusable, and swallowing the error changes nothing about
that. The damage lands at the caller's boundary:

```
async with economy_session.begin():        # webhook/payments.py
    ...credit + ledger row + idempotency row...
    try:
        await commission.apply_purchase_commissions(...)   # raises
    except Exception:
        log.error("... top-up credit unaffected")          # false
# ← exits here: rolls back EVERYTHING. Silently. No exception.
```

The failure mode is the worst-shaped one available:

| What happened | What each party saw |
| --- | --- |
| Whole transaction rolled back | — |
| `handle_event` returned `CREDITED` | operator log: `payments: credited crypto:EXT-1 uid=1 +50` |
| Router answered 200 | provider marks the delivery done and never retries |
| Wallet unchanged, no ledger row | **customer paid real money and received nothing** |

The `processed_webhooks` row is rolled back too, so a retry *would* have
recovered — but the 200 guarantees no retry is coming. And because the
rollback happens on the way out of the context manager rather than by
raising, nothing anywhere reports a problem.

Verified empirically rather than argued from SQLAlchemy semantics: the
probe reproduced the caller's exact shape and returned a final balance
of 100 instead of 150, with no exception raised anywhere.

**The fix** is the `begin_nested()` SAVEPOINT already used in
`transfer_service.send` (R-FIX-002-fp). The rollback then unwinds only
to the savepoint: the commission is dropped, the top-up survives, and
the blanket `except` becomes an honest statement of intent. Both live
top-up paths — the crypto/YooKassa/Stripe webhook and the Telegram
Stars handler — now pass their session in, and constructing a
`PaymentsService` with a commission but no session raises rather than
degrading quietly back to the unsafe shape. `last_commissions` is
cleared on failure so the post-commit DM cannot congratulate an inviter
on coins that were rolled back.

#### The same class, swept

The generalisable question is *"is there an `except Exception` around a
DB write that the caller's transaction then has to survive?"* An AST
scan over `src/` surfaced 31 candidate blocks; all but three were
either Telegram API calls (no session involved) or writes on a session
the block owns outright (`async with session_for(...)`), where a
poisoned session dies with its own scope. `handlers/rps.py:228` is
clean for the documented reason — the marriage-XP write goes to
users.db, a different session from the economy settlement.

Three were real, and all three had written a graceful recovery that
could not run:

* **`bonds_repo._create_marriage`** — on a failed insert it deletes the
  proposal and returns `marry_save_error`, a message translated in both
  `ru.yaml` and `en.yaml`. The `delete_proposal` call raises on the
  deactivated transaction, so the user got a crash where a "try again"
  had been written for them. Confirmed by probe: without the savepoint
  the recovery path never executes and an `InvalidRequestError` escapes
  instead.
* **`bonds_repo._create_relationship`** — same shape; its `False` is
  meant to send the caller down a cleanup path that cannot run.
* **`SupportTicketsRepo.create_open_ticket`** — all three `/feedback`
  and `/support` call sites catch, reply "couldn't save" and return,
  leaving `SessionMiddleware` to commit the rest of the update. That
  commit is a bare `await session.commit()`, so it raises
  `PendingRollbackError` — losing unrelated users-DB writes from the
  same update (the `last_seen` touch, a nickname, an AI-quota
  increment) *after* the user was reassured that only the ticket
  failed. Fixed in the repo rather than at the three call sites: the
  contract "raising is all it does" belongs to the writer.

Each fix is sabotage-verified — remove the `begin_nested()` and the
corresponding test fails with the exact `PendingRollbackError` the
savepoint exists to prevent.

The lesson worth keeping: **`except Exception` around a DB write is not
a recovery unless it is paired with a savepoint.** Without one it is
merely a way to keep going for a few more lines before the transaction
collapses somewhere the log will not connect to the cause.
