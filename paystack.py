"""Paystack checkout and verification adapter."""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional

API_BASE = "https://api.paystack.co"


def _request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError("PAYSTACK_SECRET_KEY is not configured")
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{API_BASE}{path}", data=body, method=method,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode())
    if not result.get("status"):
        raise RuntimeError(result.get("message", "Paystack request failed"))
    return result.get("data") or {}


class PaystackPayment:
    """Initialize and verify payments; fulfillment is performed by webhook."""
    def initialize_payment(self, email: str, amount_pesewas: int, user_id: int, bot_name: str,
                           payment_type: str = "bot_clone", extra_metadata: Optional[Dict] = None):
        reference = f"animebot-{user_id}-{uuid.uuid4().hex}"
        metadata = {"user_id": user_id, "bot_name": bot_name, "payment_type": payment_type, **(extra_metadata or {})}
        try:
            data = _request("POST", "/transaction/initialize", {
                "email": email,
                "amount": int(amount_pesewas),
                "currency": "GHS",
                "reference": reference,
                "metadata": metadata,
                "callback_url": os.getenv("PAYSTACK_CALLBACK_URL", ""),
            })
            return {"status": "success", "reference": data.get("reference", reference), "authorization_url": data["authorization_url"]}
        except (KeyError, ValueError, TypeError, RuntimeError, urllib.error.URLError) as exc:
            return {"status": "error", "message": str(exc)}

    def verify_payment(self, reference: str):
        try:
            data = _request("GET", f"/transaction/verify/{reference}")
            return {"status": "success" if data.get("status") == "success" else data.get("status", "pending"), **data}
        except (RuntimeError, urllib.error.URLError) as exc:
            return {"status": "error", "message": str(exc)}


paystack = PaystackPayment()


def valid_signature(raw_body: bytes, signature: str) -> bool:
    secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip().encode()
    supplied = (signature or "").strip().lower()
    expected = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    return bool(secret and supplied and hmac.compare_digest(expected, supplied))


def normalize_transaction(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    metadata = data.get("metadata") or {}
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except json.JSONDecodeError: metadata = {}
    return {"reference": str(data.get("reference") or "").strip(), "status": data.get("status"), "amount": data.get("amount"), "currency": data.get("currency"), "metadata": metadata, "raw": data}


__all__ = ["paystack", "valid_signature", "normalize_transaction"]

# Backwards-compatible internal name for callers migrated in stages.
selar = paystack

def valid_secret(headers: Dict[str, str], query_secret: str = "") -> bool:
    return valid_signature(b"", headers.get("x-paystack-signature") or query_secret)

def normalize_sale(payload):
    return normalize_transaction(payload)
