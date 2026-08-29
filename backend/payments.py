"""Real money in, real Mana out - PayPal Orders v2.

Server-authoritative on both ends: the checkout amount is read from
config.MANA_PACKS by pack id (never trusted from the client), and Mana is
granted only after PayPal's own capture response confirms COMPLETED status
and the captured amount matches what was ordered. A player approves the
payment in PayPal's own popup; this module never sees card details, a
password, or anything else that would make it a credential handler.

Flow: create_order() -> the browser opens PayPal's approval popup (the
PayPal JS SDK drives that) -> the player approves -> capture_order() closes
the sale and grants Mana. If PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not
set, every call here raises NotConfigured and the caller falls back to the
dev-only instant-grant stub, exactly like the voice and Redis fallbacks
elsewhere in this codebase.
"""
from __future__ import annotations

import json
import os
import threading
import time

import httpx

from . import config, db

CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
ENV = os.getenv("PAYPAL_ENV", "sandbox").strip().lower()
# The PayPal *business* account payouts land in. This is a public identifier
# (comparable to a Stripe account id or a PayPal.me handle), never a secret -
# it is shown to players so they know who they are paying, and it plays no
# part in authentication. Which account actually receives funds is
# determined by CLIENT_ID/CLIENT_SECRET (the app you register against that
# account in the PayPal Developer Dashboard), not by this string.
BUSINESS_EMAIL = os.getenv("PAYPAL_BUSINESS_EMAIL", "").strip()

BASE_URL = os.getenv("PAYPAL_BASE_URL", "").strip().rstrip("/") or (
    "https://api-m.paypal.com" if ENV == "live" else "https://api-m.sandbox.paypal.com")
TIMEOUT = 20.0


class NotConfigured(RuntimeError):
    pass


class PaymentError(RuntimeError):
    pass


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def status() -> dict:
    return {
        "configured": configured(), "env": ENV, "business_email": BUSINESS_EMAIL or None,
        # The client id is the public half of a PayPal app credential - it is
        # meant to sit in frontend JS (it's what the PayPal SDK script tag
        # takes). The secret never leaves this module.
        "client_id": CLIENT_ID or None,
    }


# ---------------------------------------------------------------------------
# OAuth2 client-credentials token, cached in-process with its expiry
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
_token_cache = {"value": None, "expires": 0.0}


def _access_token() -> str:
    if not configured():
        raise NotConfigured("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set")
    with _token_lock:
        if _token_cache["value"] and time.time() < _token_cache["expires"]:
            return _token_cache["value"]
        r = httpx.post(
            f"{BASE_URL}/v1/oauth2/token",
            auth=(CLIENT_ID, CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            raise PaymentError(f"PayPal auth failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        _token_cache["value"] = data["access_token"]
        # Refresh a little early so a request never races an expiring token.
        _token_cache["expires"] = time.time() + max(60, int(data.get("expires_in", 300)) - 60)
        return _token_cache["value"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}


def _pack(pack_id: str) -> dict:
    pack = next((p for p in config.MANA_PACKS if p["id"] == pack_id), None)
    if not pack:
        raise PaymentError(f"unknown pack {pack_id!r}")
    return pack


# ---------------------------------------------------------------------------
# Orders v2
# ---------------------------------------------------------------------------

def create_order(*, user_id: str, playthrough_id: str, pack_id: str) -> dict:
    """Opens an order for the pack's price, read from our own price table -
    the amount charged is never taken from the client."""
    pack = _pack(pack_id)
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "amount": {"currency_code": "USD", "value": f"{pack['usd']:.2f}"},
            "description": f"StoryLiver — {pack['name']} ({pack['mana']:,} Mana)",
            "custom_id": f"{user_id}:{playthrough_id}:{pack_id}",
        }],
        "application_context": {
            "brand_name": "StoryLiver",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
        },
    }
    r = httpx.post(f"{BASE_URL}/v2/checkout/orders", headers=_headers(),
                   json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise PaymentError(f"PayPal order create failed: {r.status_code} {r.text[:300]}")
    order = r.json()

    db.run(
        "INSERT INTO payments (order_id,provider,user_id,playthrough_id,pack_id,mana,usd,"
        "status,raw,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (order["id"], "paypal", user_id, playthrough_id, pack_id, pack["mana"], pack["usd"],
         "created", json.dumps(order), db.now()),
    )
    return {"order_id": order["id"], "pack": pack}


def capture_order(order_id: str, *, expected_user_id: str) -> dict:
    """Closes the sale. Grants Mana ONLY if PayPal itself reports COMPLETED
    and the captured amount matches what we opened the order for - a second,
    independent check even though the amount was server-set at creation."""
    row = db.row("SELECT * FROM payments WHERE order_id=?", (order_id,))
    if not row:
        raise PaymentError("no such order")
    if row["user_id"] != expected_user_id:
        raise PaymentError("this order belongs to a different account")
    if row["status"] == "captured":
        return {"already_captured": True, "mana": row["mana"], "pack_id": row["pack_id"],
                "playthrough_id": row["playthrough_id"]}

    r = httpx.post(f"{BASE_URL}/v2/checkout/orders/{order_id}/capture",
                   headers=_headers(), json={}, timeout=TIMEOUT)
    if r.status_code >= 400:
        db.run("UPDATE payments SET status='failed', raw=? WHERE order_id=?",
               (json.dumps({"error": r.text[:500]}), order_id))
        raise PaymentError(f"PayPal capture failed: {r.status_code} {r.text[:300]}")
    result = r.json()

    if result.get("status") != "COMPLETED":
        db.run("UPDATE payments SET status='failed', raw=? WHERE order_id=?",
               (json.dumps(result), order_id))
        raise PaymentError(f"payment not completed (status={result.get('status')})")

    captured = result["purchase_units"][0]["payments"]["captures"][0]
    paid = float(captured["amount"]["value"])
    if abs(paid - row["usd"]) > 0.01:
        db.run("UPDATE payments SET status='failed', raw=? WHERE order_id=?",
               (json.dumps(result), order_id))
        raise PaymentError(f"captured amount {paid} does not match order {row['usd']}")

    db.run("UPDATE payments SET status='captured', raw=?, captured_at=? WHERE order_id=?",
           (json.dumps(result), db.now(), order_id))
    return {"already_captured": False, "mana": row["mana"], "pack_id": row["pack_id"],
            "playthrough_id": row["playthrough_id"], "usd": row["usd"]}


def history(user_id: str, limit: int = 20):
    return db.rows(
        "SELECT order_id,pack_id,mana,usd,status,created_at,captured_at FROM payments"
        " WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
