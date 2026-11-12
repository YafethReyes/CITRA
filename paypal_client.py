"""Thin PayPal REST client (Orders v2 to collect money, Payouts to pay creators).
Uses the Sandbox by default. Credentials come from .env, never from code."""
import os
import time
import uuid

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Retry only when the connection could not even be opened (DNS or network hiccups).
# Nothing was sent in that case, so a retry can never duplicate a payment.
_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, connect=3, read=0, status=0, other=0, backoff_factor=1)))


class PayPalError(Exception):
    """A problem with PayPal that is safe to show to the user."""


_token = {"value": None, "expires": 0.0}


def _base_url():
    return os.environ.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")


def _access_token():
    if _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    secret = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not client_id or not secret:
        raise PayPalError("PayPal credentials are missing. Set PAYPAL_CLIENT_ID and "
                          "PAYPAL_CLIENT_SECRET in your .env file.")
    try:
        resp = _session.post(f"{_base_url()}/v1/oauth2/token", auth=(client_id, secret),
                             data={"grant_type": "client_credentials"},
                             headers={"Accept": "application/json"}, timeout=20)
    except requests.RequestException as exc:
        raise PayPalError("Could not reach PayPal. Check your internet connection, VPN or DNS "
                          f"and try again ({exc.__class__.__name__}).") from exc
    if resp.status_code != 200:
        raise PayPalError(f"PayPal login failed ({resp.status_code}). "
                          "Check the client ID and secret in .env.")
    data = resp.json()
    _token["value"] = data["access_token"]
    _token["expires"] = time.time() + int(data.get("expires_in", 300))
    return _token["value"]


def _request(method, path, body=None, request_id=None):
    headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    try:
        resp = _session.request(method, f"{_base_url()}{path}", json=body,
                                headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise PayPalError(f"Could not reach PayPal: {exc.__class__.__name__}.") from exc
    if resp.status_code >= 400:
        try:
            payload = resp.json()
            detail = payload.get("message") or payload.get("name") or "unknown error"
        except ValueError:
            detail = "unknown error"
        raise PayPalError(f"PayPal error {resp.status_code}: {detail}")
    return resp.json() if resp.content else {}


def create_order(amount, currency, description, reference, return_url, cancel_url):
    """Create an order and return (order_id, url where the buyer approves the payment)."""
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": reference,
            "description": description[:120],
            "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "brand_name": "Citra",
            "user_action": "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "payment_method_preference": "IMMEDIATE_PAYMENT_REQUIRED",
            "return_url": return_url,
            "cancel_url": cancel_url,
        }}},
    }
    data = _request("POST", "/v2/checkout/orders", body, request_id=str(uuid.uuid4()))
    link = next((l["href"] for l in data.get("links", [])
                 if l.get("rel") in ("payer-action", "approve")), None)
    if not data.get("id") or not link:
        raise PayPalError("PayPal did not return an approval link for this order.")
    return data["id"], link


def capture_order(order_id):
    """Capture an approved order. Returns (order status, capture id)."""
    data = _request("POST", f"/v2/checkout/orders/{order_id}/capture",
                    request_id=f"capture-{order_id}")
    units = data.get("purchase_units") or [{}]
    captures = (units[0].get("payments") or {}).get("captures") or [{}]
    return data.get("status"), captures[0].get("id")


def send_payout(email, amount, currency, note, batch_key):
    """Pay a creator by PayPal email. batch_key must be unique per payout
    (PayPal ignores duplicates, which protects against double payments)."""
    body = {
        "sender_batch_header": {
            "sender_batch_id": batch_key,
            "email_subject": "You have a payment from Citra",
            "email_message": "Your campaign payment is on its way.",
        },
        "items": [{
            "recipient_type": "EMAIL",
            "amount": {"value": f"{amount:.2f}", "currency": currency},
            "receiver": email,
            "note": note[:100],
            "sender_item_id": batch_key,
        }],
    }
    data = _request("POST", "/v1/payments/payouts", body)
    header = data.get("batch_header") or {}
    if not header.get("payout_batch_id"):
        raise PayPalError("PayPal did not confirm the payout.")
    return header["payout_batch_id"], header.get("batch_status")
