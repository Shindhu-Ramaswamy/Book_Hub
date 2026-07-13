"""
PaymentService — thin wrapper around the Razorpay Python SDK.

Two jobs only:
  1. create_order()      — ask Razorpay for an Order before showing checkout
  2. verify_signature()  — cryptographically confirm a completed payment
                           actually came from Razorpay, not a forged
                           client-side call claiming "it worked"

Nothing here touches Flask's request/response cycle or the database —
routes call this, then update OverdueRecord themselves. Keeping the
gateway SDK isolated to one file means swapping or adding a second
gateway later touches this file only, not every route that takes a
payment.

Razorpay amounts are always in paise (smallest currency unit), not
rupees — ₹150 must be sent as 15000. That conversion happens here,
once, so nobody calling this has to remember it.
"""
import hmac
import hashlib
import logging

import razorpay
from flask import current_app

log = logging.getLogger('lms.payments')


class PaymentConfigError(Exception):
    """Raised when Razorpay keys aren't configured — fail loudly rather
    than let the SDK raise a confusing auth error deep in a request."""
    pass


def _client() -> razorpay.Client:
    key_id     = current_app.config.get('RAZORPAY_KEY_ID')
    key_secret = current_app.config.get('RAZORPAY_KEY_SECRET')
    if not key_id or not key_secret:
        raise PaymentConfigError(
            'RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set. '
            'Add them to your .env (see .env.example).'
        )
    return razorpay.Client(auth=(key_id, key_secret))


def create_order(amount_rupees: float, receipt: str) -> dict:
    """
    Create a Razorpay Order for the given rupee amount.
    Returns the Razorpay order dict (contains 'id', 'amount', etc.)
    Raises PaymentConfigError or razorpay.errors.BadRequestError on failure.
    """
    client       = _client()
    amount_paise = int(round(amount_rupees * 100))
    order = client.order.create({
        'amount':          amount_paise,
        'currency':        'INR',
        'receipt':         receipt,
        'payment_capture': 1,   # auto-capture on success; no separate capture call needed
    })
    log.info('Razorpay order created id=%s amount_paise=%d receipt=%s',
              order.get('id'), amount_paise, receipt)
    return order


def verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Confirm the (order_id, payment_id, signature) triple was genuinely
    signed by Razorpay using our key_secret. This is the ONLY step that
    proves a payment actually happened — never trust a client-side
    "success" callback on its own.
    """
    client = _client()
    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id':   order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature':  signature,
        })
        return True
    except razorpay.errors.SignatureVerificationError as exc:
        log.warning('Signature verification failed order=%s payment=%s: %s',
                    order_id, payment_id, exc)
        return False


def verify_webhook_signature(raw_body: bytes, received_signature: str) -> bool:
    """
    Verify a Razorpay *webhook* payload (separate secret from the API
    keys — set under Dashboard → Settings → Webhooks). Webhooks are the
    reliability net: if a user pays but closes the tab before the
    browser-side verify call fires, the webhook still arrives and lets
    us reconcile the payment independently of the client.
    """
    webhook_secret = current_app.config.get('RAZORPAY_WEBHOOK_SECRET')
    if not webhook_secret:
        raise PaymentConfigError('RAZORPAY_WEBHOOK_SECRET is not set.')

    expected = hmac.new(
        key=webhook_secret.encode('utf-8'),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received_signature or '')
