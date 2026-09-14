# 📘 Complete Bot Commands Guide: From Beginner to Pro

*Updated: March 2026.*

Welcome! This article is your main reference for all bot commands. Here we explain in plain language what commands exist, where and how to use them, and what you get in return. Bookmark this page — you won’t get lost.

---

## 📑 Table of contents

1. [Start and basic navigation](#start)
2. [Main commands: economy and profile](#main)
3. [Games and fun](#games)
4. [Settings and personalization](#settings)
5. [Moderation (for group admins)](#moderation)
6. [Marriages, relationships, and RP — group only](#social)
7. [Useful tips and tricks](#tips)
8. [Adding the bot to a group](#bot_add_to_group)

---

## 🚀 Section 1. Start and basic navigation {#start}

### The `/start` command

**What it does:** Launches the bot and opens the main menu. On first run, the bot greets you and shows buttons: balance, top, commands, help, and other sections. To use the bot, you need to join the main group — after joining, press the “I joined” button in the menu.

*Example:* You’ve just opened the bot in private chat. Type **/start** — you’ll see a welcome message and a menu with buttons. If you open a referral link (e.g. `t.me/botname?start=ref_123456789`), the inviter gets a share of your purchases.

---

### The `/help` command

**What it does:** Shows a short help message (same wording as the “Verbatim…” block below) and a **📋 Full command list** button linking to the full on-site guide (`/commands/en`). Main-menu buttons are added below, like after **/start**. Command aliases: **/help**, **/h**, **/commands**, **/kom_help**.

**In a group, commands must not overlap with other bots.** So in groups the bot menu shows only **kom_**-prefixed commands. Use them to target this bot: **/kom_help**, **/kom_start**, **/kom_balance**, **/kom_games**, etc.

---

### Verbatim in-bot `/help` text (what Telegram shows)

This matches the strings the bot builds from localization (`help_title`, `help_intro`, …) for **/help** in private chat (bold as in Telegram Markdown):

🤖 **BOT COMMANDS**

Full list of all commands with descriptions is available on our page:

📌 **Quick access:**
• /start — main menu  
🎮 **Games:** /roulette, /dice, /duel, /duel_stats, /roll, /flip, /calc  
• /ai — ask Kom  
• /help — this help  

For all commands, click the button below.

**PvP-only mode:** if the bot process has the `PVP_GAMES_ONLY` environment variable set (on a VPS: in `.env` or the systemd unit), the bot replaces the “🎮 Games: …” line with two lines (offers created in DMs — see the PvP section below):

• /pvp_dice <amount> — PvP dice (in DM)  
• /pvp_coin <amount> heads/tails — PvP coin (in DM)  

Button under the message: **📋 Full command list**.

---

### Commands with `kom_` prefix (for groups)

In a **group**, the bot menu lists only these commands — so they don’t conflict with other bots. In private chat the menu uses plain names (**/help**, **/balance**, etc.); after you join the main group those commands work in DM (see “Private chat vs group” below).

**Basic:** **/kom_start**, **/kom_help**, **/kom_lang**, **/kom_time**, **/kom_faq**, **/kom_ping**, **/kom_botcheck**, **/kom_feedback**  
**City and weather:** **/kom_city**, **/kom_weather**, **/kom_forecast**, **/kom_timezone**  
**Economy:** **/kom_balance**, **/kom_daily**, **/kom_shop**, **/kom_currency**, **/kom_buy**, **/kom_top**  
**Games and fun:** **/kom_games**, **/kom_flip**, **/kom_calc**, **/kom_joke**, **/kom_joke18**, **/kom_quote**  
**Profile:** **/kom_profile**, **/kom_whoami**  
**AI and crypto:** **/kom_ai**, **/kom_crypto**  
**Group:** **/kom_rules**, **/kom_report**, **/kom_donate**, **/kom_donaters**, **/kom_groupstats**, **/kom_top** (groups ranking)  
**Moderation (for admins):** **/kom_warn**, **/kom_mute**, **/kom_unmute**, **/kom_ban**, **/kom_unban**, **/kom_pin**, **/kom_unpin**, **/kom_kick**

Group management (for admins): **/groupadmin** — no prefix, unique command.

---

### Private chat vs group (how it works) {#group-vs-dm}

1. **Main group subscription (private only):** until you join the bot’s main group, in DM essentially only **/start** works — other commands are blocked with a subscribe reminder (owner/dev IDs are exempt).

2. **Telegram command menu (the “/” suggestion list):** private chats show commands **without** `kom_`; groups show **kom_** commands plus **/groupadmin**. That avoids clashes with other bots **in the menu**, not by forbidding plain command names.

3. **Typing in a group:** if the handler registers both forms, **both** work (e.g. **/balance** and **/kom_balance**, **/help** and **/kom_help**).  
   **Exception — table games:** **/roulette**, **/dice**, **/duel**, **/roll**, **/cpc**, **/rps** have **no** `kom_…` aliases in code — use **/roulette**, **/dice**, etc. (full list under **/kom_games**). **/flip** and **/calc** also accept **/kom_flip** and **/kom_calc**.

4. **Group only:** moderation (**/warn**, …), coin games (**/roulette**, **/dice**, …), full **/games** output. In DM, **/games** says games run in a group.

5. **Private only (PvP creation):** **/pvp_dice** and **/pvp_coin** create offers in DM; if sent in a group, the bot replies with a hint instead of starting creation.

---

### The `/faq` and `/faq2` commands

**What they do:** Answers to frequently asked questions: how to earn coins, how to play, how the shop works. **/faq** — first part, **/faq2** (or **/faq_2**) — second part. Check here before asking in chat.

---

### The `/ping` and `/botcheck` commands

**What they do:** **/ping** — check the bot's response speed (delay in milliseconds). **/botcheck** (or **/alive**) — quick check: bot is online.

---


### The “🏠 Main menu” button

**Why it’s there:** Takes you back to the main screen with buttons (balance, top, commands, shop, etc.) so you don’t have to type commands. One tap — and you see all main actions in one place.

*Example:* You’ve opened the shop or profile and want to go back to the main choices. Press **“🏠 Main menu”** — the same menu as after **/start** will open.

---

## 💰 Section 2. Main commands: economy and profile {#main}

### The `/balance` command (or `/bal`)

**What it does:** Shows your balance — how many coins (COM) you have now, how much you’ve earned and spent. Coins are used for games, transfers, and shop purchases.

*Example:* You type **/balance** — the bot replies: “💰 Balance: 1,500 COM. Earned: 3,200. Spent: 1,700.” In a group you can write without a slash: **bot balance** or **.balance**.

---

### The `/daily` command

**What it does:** Claim the daily bonus — coins once per day. If you log in several days in a row, the bonus may increase. If you've already claimed — the bot will tell you when the next one is available.

---

### The `/profile` command

**What it does:** Shows your full profile: your card, rank, achievements, stats. In a group — also stats for that chat. In private — general data without a specific group.

* **/profile** — your own profile.
* **/profile** (reply to a message) — profile of the user you replied to.
* **/profile @username** — profile of a member by username.

**Text commands without slash (in a group):**
* **who are you?** (as a reply) — show the profile of the person you replied to.
* **who is this?** (as a reply) — same.

🔐 Viewing other profiles may be restricted by group settings (minimum rank required).

**Also:** **/whoami** (or **/me**) — short "who am I": name, balance, rank. **/achievements** (or **/ach**) — list of achievements: which you have, which you haven't unlocked yet.

*Example:* You want to see your rank and how many achievements you’ve unlocked. Type **/profile** — you’ll get a message with your card and buttons (e.g. “My relations”, “My family” in a group).

---

### How `/start` differs from the menu

**/start** — launches the bot and shows the main menu with buttons. The **menu** (the button or the same buttons after /start) is the main screen: balance, top, commands, help, shop. There is no separate “menu” command — the main menu is opened via **/start** or the “Main menu” button.

---

### No-slash mode: text as command

In a **group**, the bot understands commands without “/”: you can type **bot balance**, **.daily**, **?top** — the bot will react the same as to **/balance**, **/daily**, **/top**. To reliably target this bot when several bots are in the chat, use **kom_**-prefixed commands in the group: **/kom_balance**, **/kom_daily**, **/kom_help**, etc. (see the block above).

*Example:* In a group you type **daily** or **bot daily** — the bot will count it as the daily bonus, as if you had sent **/daily**. Or use **/kom_daily** to avoid overlapping with other bots.

---

## 🎮 Section 3. Games and fun {#games}

### Commands with coins: roulette, dice, duel

- **/games** — list of available games with descriptions. Shows what you can play in this group.
- **/roulette 100** — Russian roulette: bet 100 coins, one “shot” out of six. In 5 out of 6 cases win ×2.
- **/dice 100 5** — dice: bet 100, guess a number from 1 to 6 (e.g. 5). If correct — win ×6.
- **/roll 4** or **/roll 100 4** — dice: without bet you just guess the number; with bet — same as dice.
- **/flip heads** or **/flip 50 tails** — coin flip: guess the side — win ×2.
- **/duel 200 3** — duel for coins. Reply to the opponent’s message with this command (bet 200, 3 rounds). They accept — you take turns rolling; whoever wins more rounds takes the pot.
- **/cpc 100** (rock, paper, scissors for coins). Reply to the opponent’s message or mention @username. They accept or decline the challenge with the buttons under the bot’s message. Both choose a move in private chat with the bot (✊ rock, ✌️ scissors, ✋ paper). Winner takes double the bet; draw — refund. **/cpc_cancel** — cancel your current game.
- **/duel_stats** — your duel statistics: games played, wins, losses.

*Example:* You want to risk 50 coins on heads. Type **/flip 50 heads** — the bot “flips” the coin and says whether you won or lost.

---

### PvP commands (player vs player)

In PvP mode, the group offers commands for player vs player duels. These offers are created in private and published to the group, and the opponent accepts via a button.

- **/pvp_dice 100** — PvP dice. In private, create an offer for a 100 coin bet and choose a group to publish it to. They accept — and the bot resolves the duel based on the rolled values.
- **/pvp_coin 100 heads** — PvP coin. In private, create an offer for a 100 coin bet and specify the side (`heads`/`head` or `tails`/`tail`). They accept — and the bot compares choices to determine the winner.

---

### Commands without bets

- **/joke** — random joke from the bot.
- **/quote** — random quote or thought.
- **/calc 2+2** or **/calc 10*3-1** — calculator: evaluate an expression (+, −, *, /, parentheses).

*Example:* You need to quickly compute 15% of 800. Type **/calc 800*0.15** — the bot will return the result.

---

## ⚙️ Section 4. Settings and personalization {#settings}

### The `/settings` command

**What it does:** Opens settings: view and change profile options, notifications (if the bot supports them in this section), language, and other preferences.

*Example:* Type **/settings** — a menu or message with options appears. Language change is often available there too.

---

### The `/lang` command (or **/language**)

**What it does:** Changes the bot’s interface language (Russian or English). Hints and messages will be in the selected language.

*Example:* You’re in private chat with the bot, everything in Russian. Type **/lang** — the bot will offer a language; choose English — the interface will switch to English.

---

### The `/city` command

**What it does:** Sets or changes your city. Used for **/time** and **/weather** — the bot will show time and weather for your city. Without arguments — the bot shows which city is currently saved.

*Example:* Type **/city London** — the bot saves the city. Then **/weather** will show weather in London.

---

### The `/feedback` command

**What it does:** Sends feedback to the owner/admins: suggestions, remarks, ideas about the bot. Different from a support ticket — this is for general feedback, not urgent issues.

*Example:* You want to suggest a new command or say the “Shop” button is awkward. Type **/feedback** and then your message — it goes to the developer.

---

### The `/support` command (support ticket)

**What it does:** Contact support: create a ticket, describe the issue. Admins receive the message and will reply. Use this if something is broken or you need help.

---

### The `/ad` command (advertising)

**What it does:** Submit an ad request (placement in the chat). Works in private chat with the bot: message the bot and choose the request. Admins will review and get in touch. Aliases: **/ads**.

---

## 🛡️ Section 5. Moderation (for group admins) {#moderation}

These commands work **only in a group** and are available to the group owner, assigned admins/moderators (via the "Roles and permissions" panel), or those with the required Telegram moderation rights. Manage permissions in private chat with the bot: use **/groupadmin** in the group to open the group settings panel.

### Warnings

- **/warn** (or **/kom_warn**) — issue a warning. Reply to the violator's message and type **/warn** or **/warn reason**. You can set how long the warn lasts: **/warn 7d reason** (expires in 7 days), **/warn 30d** or **/warn 0 reason** (warn never expires). When the warning limit is reached, the user may be automatically banned for 7 days.
- **/unwarn** (or **/kom_unwarn**) — remove one warning. Reply to the user's message. You can specify the warning ID: **/unwarn 123**.
- **/warnings** — show your warnings in this chat: total count and list of recent ones.

### Mute and unmute

- **/mute** (or **/kom_mute**) — mute a member. Reply to their message and set duration: **/mute** (default from settings), **/mute 10m**, **/mute 1h**, **/mute 1d**, **/mute 7d**, etc. Units: **m** — minutes, **h** — hours, **d** — days, **w** — weeks. Then optional reason: **/mute 1h spam**.
- **/unmute** (or **/kom_unmute**) — remove mute. Reply to the user's message. Optional comment: **/unmute reason**.

### Ban and unban

- **/ban** (or **/kom_ban**) — ban a member. Reply to their message and set duration: **/ban** (default one week), **/ban 10m**, **/ban 1h**, **/ban 7d**, **/ban 1w**, or **/ban 0** / **/ban forever** — permanent. Units: **m**, **h**, **d**, **w**. Then optional reason: **/ban 1d violation**.
- **/unban** (or **/kom_unban**) — unban. Reply to the message or specify the user. Comment for the log: **/unban reason**.

### Kick and other

- **/kick** (or **/kom_kick**) — kick a member from the chat. Reply to their message. After a kick the user can rejoin via the link; a ban blocks re-entry.
- **/pin**, **/unpin** — pin or unpin a message (reply to the message).
- **/clear** (or **/purge**) — bulk delete messages in the group. Requires admin rights; see **/help** for moderation syntax.
- **/filter_add**, **/filter_list**, **/filter_remove** — banned-word filter (the same actions live in **/groupadmin** → “🚫 Filter”).
- Staff ranks (moderator/admin) are assigned in **/groupadmin** → “🛡 Staff”; there is no separate command for that.

### Reporting to the admins

The only command in this section available to **every member**, not just admins.

- **/report** (or **/kom_report**) — report a message to the group admins. Reply to the offending message and send **/report** or **/report reason**. The bot DMs the group's admins a notice plus a copy of the message itself, and tells you how many of them it reached.
- Send it **from inside the group** — then the bot never has to guess which chat you mean. To keep the command from turning into a broadcast tool there is a pause: one report per minute.
- **/report** is accepted in a DM too — as a reply to a forwarded message, but only when Telegram still shows the source chat (channel posts, messages sent on behalf of a chat). For an ordinary member's forwarded message Telegram hides the source, so that report cannot be sent from a DM — use **/report** inside the group instead.

---

## 🤖 Section 6. Marriages, relationships, and RP — group only {#social}

These commands and actions work **only in a group**, not in private chat with the bot.

### Marriages

- **/marry** — propose marriage. Reply to the person’s message and type **/marry** — they get an “Accept” or “Decline” button. If they accept — you’re married in that chat.
- **/marriage** (or **/my_marriage**) — show your marriage in this chat: with whom and from what date.
- **/divorce** — dissolve the marriage. After that you can marry again.
- **/marriages** (or **/couples**) — list of all married pairs in the chat.

*Example:* You’re in a group, replied to a friend’s message and typed **/marry**. They pressed “Accept” — the bot announced you as a couple in that chat.

---

### Relationships (not marriage)

- **/relationship** (or **/rel**) — reply to a message to propose “being in a relationship”; they get accept/decline buttons. Without a reply — the bot shows who you’re in a relationship with in this chat.
- **/breakup** — end the relationship in this chat.
- **/relations** — list of pairs “in a relationship” in the chat.
- **/rp_commands** (or **relationship commands**) — show your relationship level in the chat and the full list of RP commands by level. Group only.

---

### RP commands (hug, kiss, etc.)

**Work only in a group.** Reply to your partner’s message with an action word: **hug**, **kiss**, **handshake**, etc. The bot sends the action text and gives the couple experience (XP). Relationship level grows — new commands unlock. Your level and the list of commands by level — use **/rp_commands**.

*Example:* In a group, reply to your partner’s message with **hug** or **bot hug** — the bot will say something like “John hugged Mary” and add XP to the couple.

---

**Full list of RP commands by level** (reply to a message with one of these words, English only):

- **Level 1 — Acquaintances:** handshake, high five, hit, kick
- **Level 2 — Friendly:** hug, stroke, sorry
- **Level 3 — Warm:** bite, tickle, calm
- **Level 4 — Crush:** feed, drink, offend
- **Level 5 — Flirting:** kiss, lick, gift
- **Level 6 — Courtship:** romantic dinner, compliment (18+: sex)
- **Level 7 — Relationship:** flowers, confess
- **Level 8 — Serious:** ring, photo
- **Level 9 — Love:** propose, engagement
- **Level 10 — Great love:** wedding

Use **/rp_commands** to see your level in the chat and the commands available to you.

---

## 📦 Section 7. Useful tips and tricks {#tips}

### Shop and inventory

- **/shop** — open the shop: items (VIP, bonuses, unwarn, etc.), prices, “Buy” buttons. Payment from your balance.
- **/inventory** (or **/inv**) — your inventory: bought and not yet used items. Each has an “Use” button.

*Example:* You bought VIP in the shop. Go to **/inventory** and press “Use” on the purchased VIP — it will activate.

---

### Transfers and top

- **/send 100** — send coins. Reply to the recipient’s message and enter the amount, or type **/send @username 50**.
- **/top** — ranking by coins (and other tops). Who’s the richest in the chat. In a group: **/top** or **/top balance** — by coins; **/top messages** — top by messages (group only).

---

### Withdrawals and P2P (private chat with the bot only)

- **/withdraw** (or **/wd**) — withdrawal menu: direct payout (card, crypto step-by-step), P2P market (sell coins to other users), express flow. The bot guides you with buttons and asks for amounts and details in private chat.
- **/withdraw_status** — status of withdrawal requests: pending, completed, rejected.
- To cancel an unfinished flow: **/cancel** in private chat resets withdrawal and P2P steps.

---

### Groups ranking by donations and donations

- **/top**, **/top_groups**, **/rating_groups**, **/rating**, **/kom_top** — open the **groups ranking by donations** (ranking by “Thunder” points). Shows groups that have received donations; each group in the top has its name and a unique invite link. Available in private chat with the bot; interface language follows your setting (Russian or English).
- **/donate** — donate to a group: deduct coins from your balance and add points to the group’s ranking; the group owner receives part of the amount (after commission). In a group you can press the “Donate” button in stats or in the group card in the ranking.
- **/groupstats** (or **/group_treasury**, **/kom_groupstats**) — in a group: this group’s stats (Thunder ranking, top donators, “Donate” and “Groups ranking” buttons). In private: opens the groups ranking view.
- **/mydonates** — your donation history and total amount; private chat only.

*Example:* You want to see which groups lead by donations. In private chat type **/top** or **/rating** — the ranking page with groups and links opens. To donate to a group — go to that group and use **/donate** or the button in **/groupstats**.

---

### Currency and rates

- **/currency** — set display currency: which currency to show balance and prices in (rubles, dollars, euros, TON, etc.). No arguments — choose from the list.
- **/rate** — current rate: 1 coin (COM) = how much in your chosen currency. You can specify a currency code.
- **/convert** — convert a coin amount to your chosen currency. Enter the number of COM and optionally the currency code.

---

### Statistics

- **/stats** — your personal stats: messages, commands, games, transfers, daily streak.
- **/chatstats** (or **/cstats**) — chat statistics: who wrote how much in a period, top active. Group only.
- **/chatinfo** — chat info: name, type, member count.
- **/top_activity** (or **/topactive**) — top most active over recent days. Default — last week; you can set the period: **/top_activity 14** — for 14 days.

---

### AI assistant

- **/ai** (or **/ask**) — ask the AI assistant (Kom). Daily request limit; more with VIP. In a group Kom uses chat context.

---

### Time and weather

- **/time** — time (e.g. Moscow), date, day of week. Uses your city from **/city**.
- **/weather** — weather in your city or in a city you specify: **/weather Berlin**.

---

### Bot check

- **/ping** — check the bot’s response speed (delay in milliseconds).
- **/botcheck** (or **/alive**) — quick check: bot is online, all good.

---

### If you forget the commands

You can always type **/help** — the bot will show the list of commands by section. The full list with descriptions is right above on this page — it is generated from the bot itself, so it cannot go stale.

---

## 🤖 Section 8. Adding the bot to a group {#bot_add_to_group}

When you add the bot to a group, it will ask you to **grant it administrator rights**. For the bot to work fully (moderation, games, economy, invite links), enable in the group settings for the bot:

- **Delete messages**
- **Invite users via link** (add members)
- **Restrict members** (mute/ban)
- Optional: **Pin messages**

Group settings → Administrators → add the bot (or tap on it) → enable the permissions above. Then press the “I granted rights” button in the bot’s message — it will check and confirm.

---

## Conclusion

This article covered the main bot commands: start and menu, economy and profile, games, settings, moderation (for group admins), marriages and relationships (group only), groups ranking by donations, shop and inventory, and adding the bot to a group. Some commands work only in a group, some in both private and group. If you forget something — use **/help** or open the main menu with **/start**. Happy using!
