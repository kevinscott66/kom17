#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Устарел: используйте main.py с WEBHOOK_URL в .env (единая точка входа).
Этот файл оставлен для обратной совместимости: при наличии WEBHOOK_DOMAIN
запускает тот же webhook-сервер; иначе перенаправляет на main.
"""

import os
import sys
import logging
import threading
import importlib
import hashlib
import hmac
from pathlib import Path

# Загрузка .env из папки со скриптом (чтобы лаунчер без export подхватил WEBHOOK_DOMAIN)
_script_dir = Path(__file__).resolve().parent
_env_file = _script_dir / ".env"
if _env_file.exists():
    try:
        with open(_env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
    except Exception:
        pass

# Порт 8443 разрешён Telegram для webhook (443, 80, 88, 8443)
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8443"))
WEBHOOK_DOMAIN = os.getenv("WEBHOOK_DOMAIN", "").strip()

# Пути к SSL (по умолчанию — типичные для Let's Encrypt)
DEFAULT_CERT = f"/etc/letsencrypt/live/{WEBHOOK_DOMAIN}/fullchain.pem" if WEBHOOK_DOMAIN else ""
DEFAULT_KEY = f"/etc/letsencrypt/live/{WEBHOOK_DOMAIN}/privkey.pem" if WEBHOOK_DOMAIN else ""
SSL_CERT = os.getenv("SSL_CERT", DEFAULT_CERT).strip() or None
SSL_KEY = os.getenv("SSL_KEY", DEFAULT_KEY).strip() or None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("webhook")


def main():
    logger.info("webhook_server: старт main(), cwd=%s", os.getcwd())
    if not WEBHOOK_DOMAIN:
        logger.critical("Задай WEBHOOK_DOMAIN в .env или export (например: WEBHOOK_DOMAIN=bot.example.com)")
        sys.exit(1)

    if not SSL_CERT or not SSL_KEY or not Path(SSL_CERT).exists() or not Path(SSL_KEY).exists():
        logger.critical(
            "Нужны SSL-сертификат и ключ. Пример:\n"
            "  sudo certbot certonly --standalone -d %s\n"
            "  export SSL_CERT=/etc/letsencrypt/live/%s/fullchain.pem\n"
            "  export SSL_KEY=/etc/letsencrypt/live/%s/privkey.pem",
            WEBHOOK_DOMAIN, WEBHOOK_DOMAIN, WEBHOOK_DOMAIN,
        )
        sys.exit(1)

    logger.info("webhook_server: загрузка bot...")
    import bot
    from flask import Flask, request

    # Событие для горячей перезагрузки кода по /reload или кнопке «Перезапуск»
    webhook_reload_event = threading.Event()
    bot.set_webhook_reload_event(webhook_reload_event)

    def reload_worker():
        while True:
            webhook_reload_event.wait()
            webhook_reload_event.clear()
            logger.info("Запрос перезагрузки кода (webhook)...")
            try:
                snapshot = bot.take_reload_snapshot()
                importlib.reload(bot)
                bot.restore_reload_snapshot(snapshot)
                bot.set_webhook_reload_event(webhook_reload_event)
                logger.info("Перезагрузка кода завершена.")
            except Exception as e:
                logger.exception("Ошибка перезагрузки кода: %s", e)

    t = threading.Thread(target=reload_worker, daemon=True)
    t.start()

    app = Flask(__name__)
    webhook_url = f"https://{WEBHOOK_DOMAIN}:{WEBHOOK_PORT}/webhook"

    @app.route("/webhook", methods=["POST"])
    def webhook():
        if request.is_json:
            update = request.get_json()
            try:
                bot.bot.process_new_updates([bot.telebot.types.Update.de_json(update)])
            except Exception as e:
                logger.exception("Ошибка обработки update: %s", e)
        return ""

    def _verify_crypto_pay_signature(body: bytes, signature: str) -> bool:
        """Проверка подписи Crypto Pay: HMAC-SHA256(sha256(token), body)."""
        token = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
        if not token or not signature:
            return False
        secret = hashlib.sha256(token.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    @app.route("/crypto-webhook", methods=["POST"])
    def crypto_webhook():
        """Вебхук Crypto Pay: уведомления об оплате счёта."""
        signature = (request.headers.get("crypto-pay-api-signature") or request.headers.get("Crypto-Pay-API-Signature") or "").strip()
        body = request.get_data()
        if not _verify_crypto_pay_signature(body, signature):
            logger.warning("crypto-webhook: неверная подпись")
            return {"ok": False, "error": "invalid_signature"}, 403
        try:
            data = request.get_json(force=True, silent=True) or {}
        except Exception:
            return {"ok": False, "error": "invalid_json"}, 400
        if data.get("update_type") != "invoice_paid":
            return {"ok": True}
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            return {"ok": True}
        try:
            user_id = int(payload.get("payload") or 0)
            invoice_id = str(payload.get("invoice_id") or "")
            amount_str = str(payload.get("amount") or "0")
            paid_usd_rate = float(payload.get("paid_usd_rate") or 1)
            amount_usd = float(amount_str) * paid_usd_rate
            asset = (payload.get("asset") or payload.get("paid_asset") or "USDT") if isinstance(payload.get("asset"), str) else "USDT"
        except (TypeError, ValueError) as e:
            logger.warning("crypto-webhook: неверный payload %s", e)
            return {"ok": True}
        if user_id and invoice_id:
            try:
                bot.on_crypto_payment_webhook(invoice_id, user_id, amount_usd, asset)
            except Exception as e:
                logger.exception("crypto-webhook обработка: %s", e)
        return {"ok": True}

    @app.route("/yookassa-webhook", methods=["POST"])
    def yookassa_webhook():
        """Вебхук ЮKassa: уведомления о платеже (payment.succeeded). Верификация через API."""
        try:
            data = request.get_json(force=True, silent=True) or {}
        except Exception:
            return "", 400
        event = (data.get("event") or data.get("type") or "").lower()
        obj = data.get("object") or data
        if event != "payment.succeeded" and not (obj.get("status") == "succeeded"):
            return "", 200
        payment_id = str(obj.get("id") or "")
        metadata = obj.get("metadata") or {}
        user_id = int(metadata.get("user_id") or 0)
        coins = int(metadata.get("coins") or 0)
        amount = (obj.get("amount") or {})
        amount_rub = float(amount.get("value") or 0)
        if not (user_id and payment_id and coins):
            return "", 200
        # Верификация: запрашиваем платёж через API ЮKassa
        shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
        secret = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
        if shop_id and secret:
            try:
                from yookassa import Payment
                import yookassa
                yookassa.Configuration.account_id = shop_id
                yookassa.Configuration.secret_key = secret
                payment = Payment.find_one(payment_id)
                if not payment or getattr(payment, "status", None) != "succeeded":
                    logger.warning("yookassa-webhook: платёж %s не найден или не succeeded", payment_id)
                    return "", 200
            except Exception as e:
                logger.warning("yookassa-webhook верификация: %s", e)
                return "", 200
        try:
            bot.on_yookassa_payment_webhook(payment_id, user_id, amount_rub, coins, "succeeded")
        except Exception as e:
            logger.exception("yookassa-webhook: %s", e)
        return "", 200

    @app.route("/stripe-webhook", methods=["POST"])
    def stripe_webhook():
        """Вебхук Stripe: checkout.session.completed."""
        body = request.get_data()
        sig = request.headers.get("Stripe-Signature", "")
        secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
        if secret:
            try:
                import stripe
                data = stripe.Webhook.construct_event(body, sig, secret)
            except Exception as e:
                logger.warning("stripe-webhook signature: %s", e)
                return "", 400
        else:
            try:
                data = request.get_json(force=True, silent=True) or {}
            except Exception:
                return "", 400
        if data.get("type") != "checkout.session.completed":
            return "", 200
        sess = data.get("data", {}).get("object") or {}
        session_id = sess.get("id") or ""
        metadata = sess.get("metadata") or {}
        user_id = int(metadata.get("user_id") or 0)
        coins = int(metadata.get("coins") or 0)
        amount_total = int(sess.get("amount_total") or 0)
        amount_usd = amount_total / 100.0 if amount_total else 0
        if user_id and session_id and coins:
            try:
                bot.on_stripe_payment_webhook(session_id, user_id, amount_usd, coins)
            except Exception as e:
                logger.exception("stripe-webhook: %s", e)
        return "", 200

    try:
        bot.bot.remove_webhook()
        bot.bot.set_webhook(url=webhook_url)
        logger.info("Webhook установлен: %s", webhook_url)
    except Exception as e:
        logger.exception("Не удалось установить webhook: %s", e)
        sys.exit(1)

    # Уведомление владельцу (как при старте polling)
    try:
        bot.bot.send_message(
            bot.ADMIN_CHAT_ID,
            f"✅ **Бот запущен (webhook)**\n{bot.BOT_NAME} v{bot.BOT_VERSION}\n{webhook_url}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.debug("Уведомление владельцу: %s", e)

    app.run(
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        ssl_context=(SSL_CERT, SSL_KEY),
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("webhook_server завершился с ошибкой: %s", e)
        sys.exit(1)
