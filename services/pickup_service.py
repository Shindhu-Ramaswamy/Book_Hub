"""
PickupService
=============
All return-pickup business logic lives here. Routes call these methods
— no SQLAlchemy queries on ReturnPickupOrder outside this file. Agent
roster CRUD is NOT duplicated here — pickups reuse DeliveryService's
DeliveryAgent roster directly (same table, same agents).

Lifecycle (see models/pickup.py for the full picture)
---------------------------------------------------------
  requested → accepted → out_for_pickup → picked_up → returned (auto)
  requested → rejected                        (librarian declines)
  requested/accepted → cancelled               (user or librarian cancels)

Unlike delivery, there is no "mark returned" action here — 'returned'
is set automatically once every linked BorrowRecord reaches a terminal
status via the ordinary BookService.return_book() inspection flow
(see _maybe_close_order(), called from book_service.py).

Round-trip fee waiver: if the book being picked up was originally
home-delivered, the delivery fee already paid covers the return leg —
request_pickup() waives the pickup fee to 0/paid in that case.
"""

import logging
from datetime import date

from extensions import db
from models.pickup import ReturnPickupOrder, PICKUP_PIPELINE
from models.delivery import DeliveryAgent
from models.transaction import BorrowRecord
from config import Config

log = logging.getLogger('lms.pickup')

# Statuses from which a cancellation is still allowed — once the agent
# is en route, the pickup is physically in progress and out of scope
# to reverse.
_CANCELLABLE_STATUSES = ('requested', 'accepted')


class PickupService:

    # ── Fee ──────────────────────────────────────────────────────────
    @staticmethod
    def calc_fee(num_books: int) -> float:
        return Config.PICKUP_BASE_FEE + Config.PICKUP_FEE_PER_BOOK * num_books

    # ── Order creation (one borrowed book → ReturnPickupOrder) ───────
    @staticmethod
    def request_pickup(user_id, record_id, recipient_name, phone, address_line1,
                        address_line2, city, state, pincode, landmark):
        """
        Returns (ReturnPickupOrder, None) or (None, error_string).
        """
        record = BorrowRecord.query.filter_by(id=record_id, user_id=user_id).first()
        if not record:
            return None, 'Borrowed book not found.'
        if record.status != 'borrowed':
            return None, 'This book is not currently borrowed.'
        if record.user.membership_type != 'membership':
            return None, 'Return pickup is a Membership perk — upgrade your membership to use it.'
        if record.pickup_order_id and not record.pickup_order.is_terminal:
            return None, 'A return pickup is already in progress for this book.'

        if not all([recipient_name, phone, address_line1, city, state, pincode]):
            return None, 'Please fill in all required address fields.'

        # Round-trip waiver — a home-delivered book's original delivery
        # fee already covers the return leg, so this pickup is free.
        waived = record.delivery_order_id is not None
        fee    = 0.0 if waived else PickupService.calc_fee(1)

        order = ReturnPickupOrder(
            user_id        = user_id,
            status         = 'requested',
            recipient_name = recipient_name,
            phone          = phone,
            address_line1  = address_line1,
            address_line2  = address_line2 or None,
            city           = city,
            state          = state,
            pincode        = pincode,
            landmark       = landmark or None,
            pickup_fee     = fee,
            fee_status     = 'paid' if waived else 'unpaid',
            fee_paid_date  = date.today() if waived else None,
            requested_date = date.today(),
        )
        db.session.add(order)
        db.session.flush()   # get order.id before linking the record

        record.pickup_order_id = order.id
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.pickup_requested(order)

        return order, None

    # ── Librarian: accept / reject / cancel ──────────────────────────
    @staticmethod
    def accept_order(order_id, librarian_id):
        order = ReturnPickupOrder.query.get_or_404(order_id)
        if order.status != 'requested':
            return None, 'Order is not awaiting acceptance.'

        order.status        = 'accepted'
        order.accepted_by   = librarian_id
        order.accepted_date = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.pickup_accepted(order)
        return order, None

    @staticmethod
    def reject_order(order_id, reason=None):
        order = ReturnPickupOrder.query.get_or_404(order_id)
        if order.status != 'requested':
            return None, 'Only a pending request can be rejected.'

        order.status           = 'rejected'
        order.rejected_date    = date.today()
        order.rejection_reason = reason or None
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.pickup_rejected(order)
        return order, None

    @staticmethod
    def cancel_order(order_id, reason=None):
        order = ReturnPickupOrder.query.get_or_404(order_id)
        if order.status not in _CANCELLABLE_STATUSES:
            return None, f'Cannot cancel a pickup that is already "{order.status}".'

        order.status         = 'cancelled'
        order.cancelled_date = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.pickup_cancelled(order)
        return order, None

    # ── Librarian: pipeline advance ──────────────────────────────────
    @staticmethod
    def assign_agent(order_id, agent_id):
        order = ReturnPickupOrder.query.get_or_404(order_id)
        agent = DeliveryAgent.query.get_or_404(agent_id)
        order.agent_id = agent.id
        db.session.commit()
        return order, None

    @staticmethod
    def mark_out_for_pickup(order_id):
        order = ReturnPickupOrder.query.get_or_404(order_id)
        if order.status != 'accepted':
            return None, 'Order must be accepted before it can go out for pickup.'
        if not order.agent_id:
            return None, 'Assign a delivery agent before marking as out for pickup.'
        if order.fee_status != 'paid':
            return None, 'The pickup fee must be paid before this order can go out for pickup.'
        order.status = 'out_for_pickup'
        order.out_for_pickup_date = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.pickup_out_for_pickup(order)
        return order, None

    @staticmethod
    def mark_picked_up(order_id):
        order = ReturnPickupOrder.query.get_or_404(order_id)
        if order.status != 'out_for_pickup':
            return None, 'Order must be out for pickup before it can be marked picked up.'
        order.status = 'picked_up'
        order.picked_up_date = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.pickup_picked_up(order)
        return order, None

    # ── Auto-close once every linked book has been inspected/returned ─
    @staticmethod
    def _maybe_close_order(pickup_order_id):
        """
        Called by BookService.return_book() after a book linked to a
        pickup order is inspected. If every linked BorrowRecord has
        reached a terminal status, the pickup order is done.
        """
        order = ReturnPickupOrder.query.get(pickup_order_id)
        if not order or order.is_terminal:
            return
        if not order.records:
            return
        if all(r.status in ('returned', 'lost') for r in order.records):
            order.status       = 'returned'
            order.returned_date = date.today()
            db.session.commit()

            from services.notification_service import NotificationService
            NotificationService.pickup_returned(order)

    # ── Fee payment ───────────────────────────────────────────────────
    @staticmethod
    def mark_fee_paid(order_id, librarian_id, payment_method):
        """Cash/counter payment — a librarian manually confirms collection."""
        order = ReturnPickupOrder.query.get_or_404(order_id)
        if order.fee_status == 'paid':
            return order
        order.fee_status       = 'paid'
        order.fee_paid_date    = date.today()
        order.fee_collected_by = librarian_id
        order.payment_method   = payment_method
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.pickup_fee_paid(order)
        return order

    @staticmethod
    def record_online_payment(order_id, gateway, order_id_gw, payment_id):
        """Mark the pickup fee paid via an online gateway after the
        caller has ALREADY verified the payment signature. Idempotent."""
        order = ReturnPickupOrder.query.get_or_404(order_id)
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
        NotificationService.pickup_fee_paid(order)
        NotificationService.pickup_payment_received(order)
        return order

    # ── Query helpers ────────────────────────────────────────────────
    @staticmethod
    def get_or_404(order_id):
        return ReturnPickupOrder.query.get_or_404(order_id)

    @staticmethod
    def user_order_or_404(order_id, user_id):
        return ReturnPickupOrder.query.filter_by(id=order_id, user_id=user_id).first_or_404()

    @staticmethod
    def user_orders(user_id):
        return (ReturnPickupOrder.query.filter_by(user_id=user_id)
                .order_by(ReturnPickupOrder.requested_date.desc()).all())

    @staticmethod
    def pending_requests():
        return (ReturnPickupOrder.query.filter_by(status='requested')
                .order_by(ReturnPickupOrder.requested_date.asc()).all())

    @staticmethod
    def orders_by_status(status):
        if status == 'all':
            return ReturnPickupOrder.query.order_by(ReturnPickupOrder.requested_date.desc()).all()
        if status not in ('requested', 'accepted', 'out_for_pickup', 'picked_up',
                          'returned', 'rejected', 'cancelled'):
            status = 'requested'
        return (ReturnPickupOrder.query.filter_by(status=status)
                .order_by(ReturnPickupOrder.requested_date.desc()).all())

    @staticmethod
    def status_snapshot(order: ReturnPickupOrder) -> dict:
        """Lightweight JSON payload for the tracking-page polling endpoint."""
        date_field = {
            'requested':       order.requested_date,
            'accepted':        order.accepted_date,
            'out_for_pickup':  order.out_for_pickup_date,
            'picked_up':       order.picked_up_date,
            'returned':        order.returned_date,
        }
        pipeline_order = [key for key, _ in PICKUP_PIPELINE]
        current_index = (pipeline_order.index(order.status)
                         if order.status in pipeline_order else -1)
        timeline = [
            {
                'step':  key,
                'label': label,
                'done':  current_index >= 0 and i <= current_index,
                'date':  str(date_field[key]) if date_field.get(key) else None,
            }
            for i, (key, label) in enumerate(PICKUP_PIPELINE)
        ]
        return {
            'id':              order.id,
            'status':          order.status,
            'status_label':    dict(PICKUP_PIPELINE).get(order.status, order.status.replace('_', ' ').title()),
            'timeline':        timeline,
            'agent_name':      order.agent.name  if order.agent else None,
            'agent_phone':     order.agent.phone if order.agent else None,
            'is_terminal':     order.is_terminal,
            'fee_status':      order.fee_status,
            'rejection_reason': order.rejection_reason,
        }
