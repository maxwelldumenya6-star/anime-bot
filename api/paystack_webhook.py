"""Authenticated, idempotent Paystack/Zapier sale webhook."""
import asyncio
import hashlib
import json
import logging
from http.server import BaseHTTPRequestHandler

from database import db
from paystack import normalize_transaction, valid_signature

logger = logging.getLogger(__name__)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return self._respond(400, {"status": "error", "message": "Invalid JSON"})

        if not valid_signature(raw, self.headers.get("X-Paystack-Signature", "")):
            return self._respond(401, {"status": "error", "message": "Unauthorized"})

        sale = normalize_transaction(payload)
        if not sale["sale_id"]:
            sale["sale_id"] = "payload-" + hashlib.sha256(raw).hexdigest()
        mapping = {"type": sale.get("metadata", {}).get("payment_type", "bot_clone")}
        if mapping["type"] not in {"bot_clone", "ai_subscription", "utility_subscription", "image_search_unlock", "premium_group", "clone_monetization"}:
            return self._respond(422, {"status": "error", "message": "Unsupported Paystack payment type"})
        try:
            user_id = int(str(sale["telegram_user_id"]).strip())
            if user_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return self._respond(422, {"status": "error", "message": "Telegram User ID is required"})
        try:
            result = asyncio.run(db.claim_selar_sale(sale["sale_id"], user_id, sale, mapping["type"]))
            if result == "duplicate":
                return self._respond(200, {"status": "already_processed"})
            asyncio.run(self._grant(user_id, sale, mapping["type"]))
            return self._respond(200, {"status": "processed"})
        except Exception:
            logger.exception("[v0] Paystack sale processing failed: %s", sale["sale_id"])
            return self._respond(500, {"status": "error", "message": "Processing failed"})

    async def _grant(self, user_id: int, sale: dict, entitlement: str):
        clone_id = int(str(sale.get("clone_id") or "0"))
        if entitlement == "ai_subscription":
            from handlers.subscription import activate_subscription
            await activate_subscription(user_id, months=1, clone_id=clone_id)
        elif entitlement == "utility_subscription":
            from config import UTILITY_SUB_DAYS
            await db.activate_utility_subscription(user_id, days=UTILITY_SUB_DAYS, clone_id=clone_id)
        elif entitlement == "image_search_unlock":
            await db.mark_image_search_paid(user_id, clone_id=clone_id)
        elif entitlement == "premium_group":
            await db.set_premium_tier(user_id, clone_id=clone_id)
        elif entitlement == "clone_monetization" and clone_id:
            from config import CLONE_MONETIZATION_DAYS
            await db.activate_monetization_subscription(clone_id, days=CLONE_MONETIZATION_DAYS)
        elif entitlement == "bot_clone":
            await db.mark_clone_payment_paid(sale["sale_id"])

    def _respond(self, status: int, body: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        logger.debug("[v0] Paystack webhook: " + fmt, *args)
