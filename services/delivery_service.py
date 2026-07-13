"""
DeliveryService
===============
All home-delivery business logic lives here. Routes call these methods
— no SQLAlchemy queries on DeliveryOrder/DeliveryAgent outside this file.

Lifecycle (see models/delivery.py for the full picture)
---------------------------------------------------------
  requested → accepted → packed → out_for_delivery → delivered
  requested → rejected             (librarian declines)
  requested/accepted → cancelled   (user or librarian cancels)

Accepting an order is all-or-nothing: every book in the order must be
available, or nothing is changed and the order stays 'requested' so the
librarian can wait or reject it — no partial shipments.

The delivery fee must be paid right after acceptance, before the order
can be packed — not just before it goes out for delivery. Once it's
paid and packing has started, the order can no longer be cancelled
(see _CANCELLABLE_STATUSES) — processing is already underway.
"""

import logging
from datetime import date, timedelta

from extensions import db
from models.delivery import DeliveryOrder, DeliveryAgent, DELIVERY_PIPELINE
from models.transaction import BorrowRecord
from models.cart import Cart
from models.user import User
from config import Config

log = logging.getLogger('lms.delivery')

# Statuses from which a cancellation is still allowed — once the fee is
# paid and the order is packed, processing is already underway and out
# of scope to reverse.
_CANCELLABLE_STATUSES = ('requested', 'accepted')


class DeliveryService:

    # ── Fee ──────────────────────────────────────────────────────────
    @staticmethod
    def calc_fee(num_books: int) -> float:
        return Config.DELIVERY_BASE_FEE + Config.DELIVERY_FEE_PER_BOOK * num_books

    # ── Order creation (cart → DeliveryOrder) ───────────────────────
    @staticmethod
    def request_delivery(user_id, recipient_name, phone, address_line1,
                          address_line2, city, state, pincode, landmark):
        """
        Same validation as BookService.request_books, plus delivery
        address fields. Returns (DeliveryOrder, None) or (None, error).
        """
        user = User.query.get_or_404(user_id)
        if not user.membership_active:
            return None, 'Please clear your membership payment before requesting delivery.'
        if user.membership_type != 'membership':
            return None, 'Home delivery is a Membership perk — upgrade your membership to use it.'
        items = Cart.query.filter_by(user_id=user_id).all()
        if not items:
            return None, 'Cart is empty.'
        active_count = BorrowRecord.query.filter(
            BorrowRecord.user_id == user_id,
            BorrowRecord.status.in_(['pending', 'borrowed', 'reserved_ready', 'in_delivery']),
        ).count()
        if active_count + len(items) > user.max_books:
            return None, (f'You can only have {user.max_books} books at a time. '
                          f'Currently active: {active_count}.')

        if not all([recipient_name, phone, address_line1, city, state, pincode]):
            return None, 'Please fill in all required address fields.'

        order = DeliveryOrder(
            user_id         = user_id,
            status          = 'requested',
            recipient_name  = recipient_name,
            phone           = phone,
            address_line1   = address_line1,
            address_line2   = address_line2 or None,
            city            = city,
            state           = state,
            pincode         = pincode,
            landmark        = landmark or None,
            delivery_fee    = DeliveryService.calc_fee(len(items)),
            fee_status      = 'unpaid',
            requested_date  = date.today(),
        )
        db.session.add(order)
        db.session.flush()   # get order.id before creating linked records

        for item in items:
            db.session.add(BorrowRecord(
                user_id=user_id, book_id=item.book_id, status='pending',
                request_date=date.today(), delivery_order_id=order.id,
            ))
            db.session.delete(item)
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_requested(order)

        return order, None

    # ── Librarian: accept / reject / cancel ──────────────────────────
    @staticmethod
    def accept_order(order_id, librarian_id):
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.status != 'requested':
            return None, 'Order is not awaiting acceptance.'

        unavailable = [r.book.title for r in order.records
                       if not r.book or r.book.available_quantity <= 0]
        if unavailable:
            return None, f'"{unavailable[0]}" has no copies available — cannot accept this order yet.'
        if not order.user.membership_active:
            return None, f'{order.user.name}\'s membership payment is due — cannot accept until paid.'

        today = date.today()
        for r in order.records:
            r.status    = 'in_delivery'
            r.issued_by = librarian_id
            r.book.issued_count    += 1
            r.book.lifetime_issued += 1

        order.status       = 'accepted'
        order.accepted_by  = librarian_id
        order.accepted_date = today
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_accepted(order)
        return order, None

    @staticmethod
    def reject_order(order_id, reason=None):
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.status != 'requested':
            return None, 'Only a pending request can be rejected.'

        for r in order.records:
            r.status = 'rejected'
        order.status            = 'rejected'
        order.rejected_date     = date.today()
        order.rejection_reason  = reason or None
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_rejected(order)
        return order, None

    @staticmethod
    def cancel_order(order_id, reason=None):
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.status not in _CANCELLABLE_STATUSES:
            return None, f'Cannot cancel an order that is already "{order.status}".'

        released = order.status == 'accepted'
        for r in order.records:
            if released and r.book:
                r.book.issued_count = max(0, r.book.issued_count - 1)
            r.status = 'cancelled'

        order.status         = 'cancelled'
        order.cancelled_date = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_cancelled(order)
        return order, None

    # ── Librarian: pipeline advance ──────────────────────────────────
    @staticmethod
    def assign_agent(order_id, agent_id):
        order = DeliveryOrder.query.get_or_404(order_id)
        agent = DeliveryAgent.query.get_or_404(agent_id)
        order.agent_id = agent.id
        db.session.commit()
        return order, None

    @staticmethod
    def mark_packed(order_id):
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.status != 'accepted':
            return None, 'Order must be accepted before it can be packed.'
        if order.fee_status != 'paid':
            return None, 'The delivery fee must be paid before this order can be packed.'
        order.status     = 'packed'
        order.packed_date = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_packed(order)
        return order, None

    @staticmethod
    def mark_out_for_delivery(order_id):
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.status != 'packed':
            return None, 'Order must be packed before it can go out for delivery.'
        if not order.agent_id:
            return None, 'Assign a delivery agent before marking as out for delivery.'
        if order.fee_status != 'paid':
            return None, 'Delivery fee must be paid before this order can go out for delivery.'
        order.status = 'out_for_delivery'
        order.out_for_delivery_date = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_out_for_delivery(order)
        return order, None

    @staticmethod
    def mark_delivered(order_id):
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.status != 'out_for_delivery':
            return None, 'Order must be out for delivery before it can be marked delivered.'

        today = date.today()
        for r in order.records:
            r.status      = 'borrowed'
            r.issue_date  = today
            r.borrow_date = today
            r.due_date    = today + timedelta(days=r.user.borrow_days)

        order.status         = 'delivered'
        order.delivered_date = today
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_delivered(order)
        return order, None

    # ── Fee payment ───────────────────────────────────────────────────
    @staticmethod
    def mark_fee_paid(order_id, librarian_id, payment_method):
        """Cash/counter payment — a librarian manually confirms collection."""
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.fee_status == 'paid':
            return order
        order.fee_status       = 'paid'
        order.fee_paid_date    = date.today()
        order.fee_collected_by = librarian_id
        order.payment_method   = payment_method
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_fee_paid(order)
        return order

    @staticmethod
    def record_online_payment(order_id, gateway, order_id_gw, payment_id):
        """Mark the delivery fee paid via an online gateway after the
        caller has ALREADY verified the payment signature. Idempotent."""
        order = DeliveryOrder.query.get_or_404(order_id)
        if order.fee_status == 'paid':
            return order

        order.fee_status         = 'paid'
        order.fee_paid_date      = date.today()
        order.fee_collected_by   = None
        order.payment_method     = 'online'
        order.payment_gateway    = gateway
        order.gateway_order_id   = order_id_gw
        order.gateway_payment_id = payment_id
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.delivery_fee_paid(order)
        NotificationService.delivery_payment_received(order)
        return order

    # ── Delivery agent roster (librarian-managed, no login) ───────────
    @staticmethod
    def list_agents(active_only=False):
        q = DeliveryAgent.query
        if active_only:
            q = q.filter_by(is_active=True)
        return q.order_by(DeliveryAgent.name.asc()).all()

    @staticmethod
    def create_agent(name, phone):
        if not name or not phone:
            return None, 'Name and phone are required.'
        agent = DeliveryAgent(name=name, phone=phone)
        db.session.add(agent)
        db.session.commit()
        return agent, None

    @staticmethod
    def update_agent(agent_id, name, phone):
        agent = DeliveryAgent.query.get_or_404(agent_id)
        agent.name  = name
        agent.phone = phone
        db.session.commit()
        return agent

    @staticmethod
    def toggle_agent_active(agent_id):
        agent = DeliveryAgent.query.get_or_404(agent_id)
        agent.is_active = not agent.is_active
        db.session.commit()
        return agent

    # ── Query helpers ────────────────────────────────────────────────
    @staticmethod
    def get_or_404(order_id):
        return DeliveryOrder.query.get_or_404(order_id)

    @staticmethod
    def user_order_or_404(order_id, user_id):
        return DeliveryOrder.query.filter_by(id=order_id, user_id=user_id).first_or_404()

    @staticmethod
    def user_orders(user_id):
        return (DeliveryOrder.query.filter_by(user_id=user_id)
                .order_by(DeliveryOrder.requested_date.desc()).all())

    @staticmethod
    def pending_requests():
        return (DeliveryOrder.query.filter_by(status='requested')
                .order_by(DeliveryOrder.requested_date.asc()).all())

    @staticmethod
    def orders_by_status(status):
        if status == 'all':
            return DeliveryOrder.query.order_by(DeliveryOrder.requested_date.desc()).all()
        if status not in ('requested', 'accepted', 'packed',
                          'out_for_delivery', 'delivered', 'rejected', 'cancelled'):
            status = 'requested'
        return (DeliveryOrder.query.filter_by(status=status)
                .order_by(DeliveryOrder.requested_date.desc()).all())

    @staticmethod
    def status_snapshot(order: DeliveryOrder) -> dict:
        """Lightweight JSON payload for the tracking-page polling endpoint."""
        date_field = {
            'requested':        order.requested_date,
            'accepted':         order.accepted_date,
            'packed':           order.packed_date,
            'out_for_delivery': order.out_for_delivery_date,
            'delivered':        order.delivered_date,
        }
        pipeline_order = [key for key, _ in DELIVERY_PIPELINE]
        current_index = (pipeline_order.index(order.status)
                         if order.status in pipeline_order else -1)
        timeline = [
            {
                'step':  key,
                'label': label,
                'done':  current_index >= 0 and i <= current_index,
                'date':  str(date_field[key]) if date_field.get(key) else None,
            }
            for i, (key, label) in enumerate(DELIVERY_PIPELINE)
        ]
        return {
            'id':              order.id,
            'status':          order.status,
            'status_label':    dict(DELIVERY_PIPELINE).get(order.status, order.status.replace('_', ' ').title()),
            'timeline':        timeline,
            'agent_name':      order.agent.name  if order.agent else None,
            'agent_phone':     order.agent.phone if order.agent else None,
            'is_terminal':     order.is_terminal,
            'fee_status':      order.fee_status,
            'rejection_reason': order.rejection_reason,
        }
