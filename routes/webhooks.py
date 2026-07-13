"""
routes/webhooks.py
===================
Server-to-server callbacks from payment gateways. These are called
directly by Razorpay's servers, not by a user's browser — so there's
no session, no login, and no CSRF token (this blueprint is CSRF-exempt
in app.py, same reasoning as the JWT API blueprints).

Why this exists alongside the client-side verify flow in routes/user.py
------------------------------------------------------------------------
The client-side flow (create-order -> checkout -> verify) covers the
normal case. But if a user's payment succeeds and they close the tab,
lose signal, or the browser crashes before the verify call fires, our
database would never learn the payment happened — the money moved but
our fine stays 'unpaid'. The webhook is Razorpay proactively telling us
"this payment succeeded", independent of whether the client-side call
ever completes. Configure the webhook URL under Razorpay Dashboard →
Settings → Webhooks, subscribed to the 'payment.captured' event.

Both paths call the same idempotent record_online_payment methods, so
whichever one arrives first "wins" and the second is a no-op.

A single Razorpay account raises orders for three different kinds of
charge in this app — fines/damage/lost (OverdueRecord), membership
registration/renewal/upgrade (MembershipPayment), and home-delivery
fees (DeliveryOrder). All three share the same webhook endpoint, so an
incoming order_id is looked up in whichever table actually issued it.
"""
import logging
from flask import Blueprint, request, jsonify

from extensions import db
from models.overdue import OverdueRecord
from models.membership import MembershipPayment
from models.delivery import DeliveryOrder
from services.book_service import BookService
from services.membership_service import MembershipService
from services.delivery_service import DeliveryService
from services.payment_service import verify_webhook_signature, PaymentConfigError

webhooks = Blueprint('webhooks', __name__)
log = logging.getLogger('lms.webhooks')


@webhooks.route('/razorpay', methods=['POST'])
def razorpay_webhook():
    raw_body  = request.get_data()
    signature = request.headers.get('X-Razorpay-Signature', '')

    try:
        valid = verify_webhook_signature(raw_body, signature)
    except PaymentConfigError as e:
        log.error('Webhook received but not configured: %s', e)
        return jsonify({'error': 'webhook not configured'}), 503

    if not valid:
        log.warning('Rejected webhook call with invalid signature.')
        return jsonify({'error': 'invalid signature'}), 400

    payload = request.get_json(silent=True) or {}
    event   = payload.get('event')

    if event == 'payment.captured':
        entity     = payload.get('payload', {}).get('payment', {}).get('entity', {})
        order_id   = entity.get('order_id')
        payment_id = entity.get('id')

        record = OverdueRecord.query.filter_by(gateway_order_id=order_id).first()
        if record is not None:
            if record.fine_status != 'paid':
                BookService.record_online_payment(
                    record.id, gateway='razorpay', order_id=order_id, payment_id=payment_id
                )
                log.info('Fine #%d marked paid via webhook (order=%s payment=%s)',
                         record.id, order_id, payment_id)
        else:
            membership_record = MembershipPayment.query.filter_by(gateway_order_id=order_id).first()
            if membership_record is not None:
                if membership_record.status != 'paid':
                    MembershipService.record_online_payment(
                        membership_record.id, gateway='razorpay',
                        order_id=order_id, gw_payment_id=payment_id
                    )
                    log.info('Membership payment #%d marked paid via webhook (order=%s payment=%s)',
                             membership_record.id, order_id, payment_id)
            else:
                delivery_order = DeliveryOrder.query.filter_by(gateway_order_id=order_id).first()
                if delivery_order is None:
                    log.warning('Webhook payment.captured for unknown order_id=%s', order_id)
                elif delivery_order.fee_status != 'paid':
                    DeliveryService.record_online_payment(
                        delivery_order.id, gateway='razorpay',
                        order_id_gw=order_id, payment_id=payment_id
                    )
                    log.info('Delivery order #%d fee marked paid via webhook (order=%s payment=%s)',
                             delivery_order.id, order_id, payment_id)

    # Always 200 on anything we understood, even if there was nothing
    # to do — Razorpay retries on non-2xx, and we don't want retries
    # for events we don't care about.
    return jsonify({'status': 'ok'}), 200
