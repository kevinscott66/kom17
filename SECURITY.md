# Security Policy

## Threat model

KOM17 handles three things that attract abuse: moderator authority over live
groups, an in-chat currency convertible to USDT, and AI calls billed to the
operator's provider keys.

1. **Privilege abuse.** Admin actions are permission-checked per command and
   written to an audit log, including actions taken by anonymous group admins
   (recorded as `sender_chat.id` with `anonymous=True`).
2. **Economy abuse.** Coin minting, withdrawals and AI usage are each bounded
   by independent rate limits and caps — see the economy safety section of the
   README. A single bypassed gate does not open the payout path.
3. **Webhook forgery.** The Telegram webhook validates
   `X-Telegram-Bot-Api-Secret-Token`; the application refuses to start in
   production without `WEBHOOK_SECRET_TOKEN` set. Payment callbacks are
   verified per provider (HMAC signature for RollyPay, provider reverification
   for YooKassa); an endpoint whose secret is unset returns 503 rather than
   accepting unverified deliveries.
4. **Data safety.** In production, `db/safety.py` rejects `DELETE` and `UPDATE`
   statements without a `WHERE` clause.

Secrets are scrubbed from logs before they are written, and the scrubber is
covered by tests that assert on realistic token shapes.

## Reporting a vulnerability

Report privately to **hello@dobropalm.tech** or via Telegram
[@dobropalm](https://t.me/dobropalm). Do not open a public issue for an
unpatched vulnerability.

In scope: authentication or authorisation bypass, webhook forgery, economy
exploits that mint or extract value, payment-callback abuse, data leakage,
remote code execution.

Expect acknowledgement within 72 hours and a 90-day coordinated disclosure
window.

## Repository hygiene

No credentials are committed. Host names, IP addresses and deployment
identifiers are absent rather than masked. Token-shaped strings in tests are
fixtures asserting that the log scrubber redacts them.
