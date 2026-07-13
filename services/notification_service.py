"""
NotificationService
===================
Single responsibility: create Notification rows.
Nothing here touches Flask request/response.

Every public method maps 1-to-1 with a business event.
Called from BookService, ReservationService, and the scheduler.

Design rules
------------
1. Never raise — notification failure must never break the main action.
   Every method wraps its DB work in try/except and logs on failure.
2. Never import from routes or templates.
3. All methods are @staticmethod — no instance needed.
4. Bulk notifications (scheduler reminders) use bulk_insert for
   performance instead of one session.add() per row.
"""

import logging
from datetime import datetime, timezone

from extensions import db
from models.notification import Notification

log = logging.getLogger('lms.notifications')


def _push(user_id: int, notif_type: str, title: str, body: str,
          borrow_id=None, reservation_id=None, overdue_id=None,
          membership_payment_id=None, delivery_order_id=None, pickup_order_id=None):
    """
    Internal helper — create one Notification row and flush.
    Returns the Notification or None on failure.
    """
    try:
        n = Notification(
            user_id        = user_id,
            notif_type     = notif_type,
            title          = title,
            body           = body,
            is_read        = False,
            created_at     = datetime.now(timezone.utc),
            borrow_id      = borrow_id,
            reservation_id = reservation_id,
            overdue_id     = overdue_id,
            membership_payment_id = membership_payment_id,
            delivery_order_id     = delivery_order_id,
            pickup_order_id       = pickup_order_id,
        )
        db.session.add(n)
        db.session.commit()
        return n
    except Exception as exc:
        db.session.rollback()
        log.error('Failed to create notification type=%s user=%d: %s',
                  notif_type, user_id, exc)
        return None


class NotificationService:

    # ────────────────────────────────────────────────────────────────
    # Borrow lifecycle
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def borrow_requested(record):
        """User submitted cart as borrow request — one notif per book."""
        _push(
            user_id    = record.user_id,
            notif_type = 'borrow_requested',
            title      = 'Borrow request submitted',
            body       = (f'Your request for "{record.book.title}" has been sent '
                          f'to the librarian for approval.'),
            borrow_id  = record.id,
        )

    @staticmethod
    def borrow_approved(record):
        """Librarian approved a borrow request."""
        _push(
            user_id    = record.user_id,
            notif_type = 'borrow_approved',
            title      = 'Borrow request approved!',
            body       = (f'"{record.book.title}" has been issued to you. '
                          f'Please collect it by {record.due_date} '
                          f'({record.days_remaining} days remaining).'),
            borrow_id  = record.id,
        )

    @staticmethod
    def borrow_rejected(record):
        """Librarian rejected a borrow request."""
        _push(
            user_id    = record.user_id,
            notif_type = 'borrow_rejected',
            title      = 'Borrow request rejected',
            body       = (f'Your request for "{record.book.title}" was rejected '
                          f'by the librarian. You may try requesting it again later.'),
            borrow_id  = record.id,
        )

    @staticmethod
    def book_returned(record):
        """Book was successfully returned (no fine)."""
        _push(
            user_id    = record.user_id,
            notif_type = 'book_returned',
            title      = 'Book returned',
            body       = (f'"{record.book.title}" has been returned successfully. '
                          f'Thank you!'),
            borrow_id  = record.id,
        )

    @staticmethod
    def return_requested(record):
        """
        User flagged they're bringing a book back in person. Confirms to
        the user and alerts every active librarian — advance notice
        only, no state change; the actual return still goes through the
        normal counter/return-inspection flow.
        """
        _push(
            user_id    = record.user_id,
            notif_type = 'return_requested',
            title      = 'Return noted',
            body       = f'We\'ve let the librarian know you\'re bringing back "{record.book.title}".',
            borrow_id  = record.id,
        )

        from models.user import User
        librarians = User.query.filter_by(role='librarian', is_active=True).all()
        for lib in librarians:
            _push(
                user_id    = lib.id,
                notif_type = 'return_requested',
                title      = 'Member returning a book',
                body       = f'{record.user.name} said they\'re bringing back "{record.book.title}".',
                borrow_id  = record.id,
            )

    @staticmethod
    def fine_created(record, overdue_record):
        """Book returned late — fine raised."""
        _push(
            user_id    = record.user_id,
            notif_type = 'fine_created',
            title      = f'Fine raised — ₹{overdue_record.amount:.0f}',
            body       = (f'"{record.book.title}" was returned {record.overdue_days} '
                          f'day(s) late. A fine of ₹{overdue_record.amount:.0f} '
                          f'has been added to your account. Please pay at the counter.'),
            borrow_id  = record.id,
            overdue_id = overdue_record.id,
        )

    @staticmethod
    def condition_charge_created(record, charge):
        """Book returned damaged or lost — condition charge raised."""
        if charge.charge_type == 'lost':
            title = f'Lost book charge — ₹{charge.amount:.0f}'
            body  = (f'"{record.book.title}" was marked lost on return. '
                     f'A charge of ₹{charge.amount:.0f} has been added to '
                     f'your account. Please pay at the counter.')
            notif_type = 'lost_charge'
        else:
            title = f'Damage charge — ₹{charge.amount:.0f}'
            body  = (f'"{record.book.title}" was returned damaged. '
                     f'A charge of ₹{charge.amount:.0f} has been added to '
                     f'your account. Please pay at the counter.')
            notif_type = 'damage_charge'
        _push(
            user_id    = record.user_id,
            notif_type = notif_type,
            title      = title,
            body       = body,
            borrow_id  = record.id,
            overdue_id = charge.id,
        )

    @staticmethod
    def fine_paid(overdue_record):
        """Fine or condition charge marked as paid, in person or online."""
        br    = overdue_record.borrow_record
        label = {'damaged': 'Damage charge', 'lost': 'Lost book charge'}.get(
            overdue_record.charge_type, 'Fine'
        )
        if overdue_record.payment_method == 'online' and overdue_record.payment_gateway:
            via = f'paid online via {overdue_record.payment_gateway.title()}'
        else:
            via = f'collected via {overdue_record.payment_method}'
        _push(
            user_id    = overdue_record.user_id,
            notif_type = 'fine_paid',
            title      = f'{label} paid — ₹{overdue_record.amount:.0f}',
            body       = (f'Your {label.lower()} of ₹{overdue_record.amount:.0f} for '
                          f'"{br.book.title if br and br.book else "a book"}" '
                          f'has been {via}. '
                          f'Your account is now clear.'),
            overdue_id = overdue_record.id,
        )

    @staticmethod
    def online_payment_received(overdue_record):
        """
        Alert every active account that a member just paid a fine/charge
        online (GPay via Razorpay, etc.) — nobody needs to collect cash
        for this one, and it's already marked paid automatically.

        Broadcasts to every active user rather than one specific person
        or just librarians.
        """
        from models.user import User

        br    = overdue_record.borrow_record
        label = {'damaged': 'Damage charge', 'lost': 'Lost book charge'}.get(
            overdue_record.charge_type, 'Fine'
        )
        gateway = (overdue_record.payment_gateway or 'the payment gateway').title()
        book_title = br.book.title if br and br.book else 'a book'

        recipients = User.query.filter_by(is_active=True).all()
        for recipient in recipients:
            _push(
                user_id    = recipient.id,
                notif_type = 'payment_received',
                title      = f'Online payment received — ₹{overdue_record.amount:.0f}',
                body       = (f'{overdue_record.user.name} paid their {label.lower()} of '
                              f'₹{overdue_record.amount:.0f} for "{book_title}" online via '
                              f'{gateway}. It\'s already marked as paid — no cash to collect.'),
                overdue_id = overdue_record.id,
            )

    # ────────────────────────────────────────────────────────────────
    # Membership lifecycle
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def membership_due(payment):
        """Registration/renewal/upgrade fee raised — payment needed to (keep) borrowing."""
        label = {
            'registration': 'Registration fee',
            'renewal':      'Membership renewal',
            'upgrade':      'Membership upgrade fee',
        }.get(payment.payment_type, 'Membership fee')
        if payment.payment_type == 'renewal':
            body = (f'Your {payment.membership_type.title()} membership has expired. '
                    f'Pay the ₹{payment.amount:.0f} renewal fee to keep borrowing books.')
        elif payment.payment_type == 'upgrade':
            body = (f'Pay ₹{payment.amount:.0f} to upgrade to Membership — '
                    f'5 books at once, 60-day borrow period.')
        else:
            body = (f'Pay the ₹{payment.amount:.0f} registration fee to activate '
                    f'borrowing on your account.')
        _push(
            user_id    = payment.user_id,
            notif_type = 'membership_due',
            title      = f'{label} due — ₹{payment.amount:.0f}',
            body       = body,
            membership_payment_id = payment.id,
        )

    @staticmethod
    def membership_paid(payment):
        """Registration or renewal fee paid (or free) — account/tier active."""
        label = 'Registration' if payment.payment_type == 'registration' else 'Renewal'
        title = (f'{label} complete — welcome!' if payment.amount <= 0
                 else f'{label} payment received — ₹{payment.amount:.0f}')
        _push(
            user_id    = payment.user_id,
            notif_type = 'membership_paid',
            title      = title,
            body       = (f'Your {payment.membership_type.title()} membership is now active. '
                          f'You can borrow up to {payment.user.max_books} book(s) at a time '
                          f'for {payment.user.borrow_days} days each.'),
            membership_payment_id = payment.id,
        )

    @staticmethod
    def membership_upgraded(payment):
        """Basic → Membership upgrade fee paid — new perks active immediately."""
        _push(
            user_id    = payment.user_id,
            notif_type = 'membership_upgraded',
            title      = 'Upgraded to Membership!',
            body       = (f'Your upgrade payment of ₹{payment.amount:.0f} was received. '
                          f'You can now borrow up to {payment.user.max_books} books at a time '
                          f'for {payment.user.borrow_days} days each.'),
            membership_payment_id = payment.id,
        )

    @staticmethod
    def membership_payment_received(payment):
        """
        Alert every active account that a member just paid a membership
        fee online — nobody needs to collect cash for this one. Same
        broadcast pattern as online_payment_received() for fines.
        """
        from models.user import User

        label = {
            'registration': 'Registration fee',
            'renewal':      'Membership renewal',
            'upgrade':      'Membership upgrade fee',
        }.get(payment.payment_type, 'Membership fee')
        gateway = (payment.payment_gateway or 'the payment gateway').title()

        recipients = User.query.filter_by(is_active=True).all()
        for recipient in recipients:
            _push(
                user_id    = recipient.id,
                notif_type = 'membership_payment_received',
                title      = f'Online membership payment — ₹{payment.amount:.0f}',
                body       = (f'{payment.user.name} paid their {label.lower()} of '
                              f'₹{payment.amount:.0f} online via {gateway}. '
                              f'It\'s already marked as paid — no cash to collect.'),
                membership_payment_id = payment.id,
            )

    # ────────────────────────────────────────────────────────────────
    # Reservation lifecycle
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def reservation_queued(reservation):
        """User joined the reservation queue."""
        _push(
            user_id        = reservation.user_id,
            notif_type     = 'reservation_queued',
            title          = 'Added to reservation queue',
            body           = (f'You are #{reservation.queue_position} in the queue for '
                              f'"{reservation.book.title}". '
                              f'We will notify you when a copy is available.'),
            reservation_id = reservation.id,
        )

    @staticmethod
    def reservation_ready(reservation):
        """User is now #1 — book is ready to collect."""
        from config import Config
        _push(
            user_id        = reservation.user_id,
            notif_type     = 'reservation_ready',
            title          = '📚 Your reserved book is ready!',
            body           = (f'"{reservation.book.title}" is now available for you. '
                              f'Please collect it from the library within '
                              f'{Config.RESERVATION_HOLD_HOURS} hours or your '
                              f'reservation will be given to the next person.'),
            reservation_id = reservation.id,
        )

    @staticmethod
    def reservation_expired(reservation):
        """Hold window expired — user did not collect in time."""
        _push(
            user_id        = reservation.user_id,
            notif_type     = 'reservation_expired',
            title          = 'Reservation expired',
            body           = (f'Your hold for "{reservation.book.title}" has expired '
                              f'because it was not collected in time. '
                              f'You can reserve the book again if you still need it.'),
            reservation_id = reservation.id,
        )

    @staticmethod
    def reservation_cancelled(reservation):
        """User cancelled their own reservation."""
        _push(
            user_id        = reservation.user_id,
            notif_type     = 'reservation_cancelled',
            title          = 'Reservation cancelled',
            body           = (f'Your reservation for "{reservation.book.title}" '
                              f'has been cancelled.'),
            reservation_id = reservation.id,
        )

    @staticmethod
    def reservation_fulfilled(reservation, record):
        """Reservation converted to a real borrow by librarian."""
        _push(
            user_id        = reservation.user_id,
            notif_type     = 'reservation_fulfilled',
            title          = 'Reserved book issued!',
            body           = (f'"{reservation.book.title}" has been issued to you. '
                              f'Return it by {record.due_date} '
                              f'({record.days_remaining} days).'),
            reservation_id = reservation.id,
            borrow_id      = record.id,
        )

    # ────────────────────────────────────────────────────────────────
    # Home delivery lifecycle
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def delivery_requested(order):
        """User submitted a cart for home delivery."""
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_requested',
            title      = 'Delivery request submitted',
            body       = (f'Your delivery request for {order.book_count} book(s) has been sent '
                          f'to the librarian for approval. Delivery fee: ₹{order.delivery_fee:.0f}.'),
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_accepted(order):
        """Librarian accepted the delivery order."""
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_accepted',
            title      = 'Delivery order accepted!',
            body       = (f'Your order of {order.book_count} book(s) has been accepted and '
                          f'is being prepared for delivery.'),
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_rejected(order):
        """Librarian declined the delivery order."""
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_rejected',
            title      = 'Delivery order declined',
            body       = (f'Your delivery order was declined by the librarian'
                          + (f': {order.rejection_reason}' if order.rejection_reason else '.')
                          + ' You may try again later.'),
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_cancelled(order):
        """Order cancelled — by the user themself or by a librarian."""
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_cancelled',
            title      = 'Delivery order cancelled',
            body       = f'Your delivery order of {order.book_count} book(s) has been cancelled.',
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_packed(order):
        """Librarian packed the parcel."""
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_packed',
            title      = 'Your order has been packed',
            body       = f'Your parcel of {order.book_count} book(s) has been packed and will ship soon.',
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_shipped(order):
        """Parcel handed to the assigned delivery agent."""
        agent_info = (f' It has been handed to {order.agent.name} ({order.agent.phone}).'
                      if order.agent else '')
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_shipped',
            title      = 'Your order has shipped!',
            body       = f'Your parcel is on its way.{agent_info}',
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_out_for_delivery(order):
        """Delivery agent is en route."""
        agent_info = (f' {order.agent.name} ({order.agent.phone}) is bringing it to you.'
                      if order.agent else '')
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_out_for_delivery',
            title      = 'Out for delivery!',
            body       = f'Your parcel is out for delivery and should arrive soon.{agent_info}',
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_delivered(order):
        """Parcel delivered — books are now on the user's borrowed list."""
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_delivered',
            title      = 'Delivered!',
            body       = (f'Your parcel of {order.book_count} book(s) has been delivered. '
                          f'Enjoy your reading!'),
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_fee_paid(order):
        """Delivery fee marked as paid, in person or online."""
        if order.payment_method == 'online' and order.payment_gateway:
            via = f'paid online via {order.payment_gateway.title()}'
        else:
            via = f'collected via {order.payment_method}'
        _push(
            user_id    = order.user_id,
            notif_type = 'delivery_fee_paid',
            title      = f'Delivery fee paid — ₹{order.delivery_fee:.0f}',
            body       = f'Your delivery fee of ₹{order.delivery_fee:.0f} has been {via}.',
            delivery_order_id = order.id,
        )

    @staticmethod
    def delivery_payment_received(order):
        """
        Alert every active account that a member just paid a delivery fee
        online — nobody needs to collect cash for this one. Same
        broadcast pattern as online_payment_received/membership_payment_received.
        """
        from models.user import User

        gateway = (order.payment_gateway or 'the payment gateway').title()
        recipients = User.query.filter_by(is_active=True).all()
        for recipient in recipients:
            _push(
                user_id    = recipient.id,
                notif_type = 'delivery_payment_received',
                title      = f'Online delivery payment — ₹{order.delivery_fee:.0f}',
                body       = (f'{order.user.name} paid their delivery fee of '
                              f'₹{order.delivery_fee:.0f} online via {gateway}. '
                              f'It\'s already marked as paid — no cash to collect.'),
                delivery_order_id = order.id,
            )

    # ────────────────────────────────────────────────────────────────
    # Return pickup lifecycle
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def pickup_requested(order):
        """User requested a return pickup for a borrowed book."""
        fee_note = (f' No pickup fee — already covered by your delivery.'
                    if order.pickup_fee <= 0
                    else f' Pickup fee: ₹{order.pickup_fee:.0f}.')
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_requested',
            title      = 'Return pickup requested',
            body       = (f'Your pickup request has been sent to the librarian for approval.'
                          f'{fee_note}'),
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_accepted(order):
        """Librarian accepted the pickup request."""
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_accepted',
            title      = 'Return pickup accepted!',
            body       = 'Your return pickup request has been accepted and will be scheduled soon.',
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_rejected(order):
        """Librarian declined the pickup request."""
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_rejected',
            title      = 'Return pickup declined',
            body       = (f'Your return pickup request was declined by the librarian'
                          + (f': {order.rejection_reason}' if order.rejection_reason else '.')
                          + ' Please return the book at the library counter, or try again later.'),
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_cancelled(order):
        """Pickup request cancelled — by the user themself or by a librarian."""
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_cancelled',
            title      = 'Return pickup cancelled',
            body       = 'Your return pickup request has been cancelled.',
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_out_for_pickup(order):
        """Delivery agent is en route to collect the book."""
        agent_info = (f' {order.agent.name} ({order.agent.phone}) is on the way.'
                      if order.agent else '')
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_out_for_pickup',
            title      = 'Agent on the way to collect your book',
            body       = f'Your return pickup is out for collection.{agent_info}',
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_picked_up(order):
        """Agent collected the book — it's in transit to the library."""
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_picked_up',
            title      = 'Book collected!',
            body       = 'Your book has been picked up and is on its way back to the library.',
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_returned(order):
        """Book received & inspected at the library — pickup order closed."""
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_returned',
            title      = 'Return complete',
            body       = 'Your book has been received and checked in at the library. Thank you!',
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_fee_paid(order):
        """Pickup fee marked as paid, in person or online."""
        if order.payment_method == 'online' and order.payment_gateway:
            via = f'paid online via {order.payment_gateway.title()}'
        else:
            via = f'collected via {order.payment_method}'
        _push(
            user_id    = order.user_id,
            notif_type = 'pickup_fee_paid',
            title      = f'Pickup fee paid — ₹{order.pickup_fee:.0f}',
            body       = f'Your return pickup fee of ₹{order.pickup_fee:.0f} has been {via}.',
            pickup_order_id = order.id,
        )

    @staticmethod
    def pickup_payment_received(order):
        """
        Alert every active account that a member just paid a pickup fee
        online — nobody needs to collect cash for this one. Same
        broadcast pattern as delivery_payment_received.
        """
        from models.user import User

        gateway = (order.payment_gateway or 'the payment gateway').title()
        recipients = User.query.filter_by(is_active=True).all()
        for recipient in recipients:
            _push(
                user_id    = recipient.id,
                notif_type = 'pickup_payment_received',
                title      = f'Online pickup payment — ₹{order.pickup_fee:.0f}',
                body       = (f'{order.user.name} paid their return pickup fee of '
                              f'₹{order.pickup_fee:.0f} online via {gateway}. '
                              f'It\'s already marked as paid — no cash to collect.'),
                pickup_order_id = order.id,
            )

    # ────────────────────────────────────────────────────────────────
    # Scheduler reminders (bulk — called by scheduler jobs)
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def due_reminder_3(record):
        """Book due in exactly 3 days."""
        _push(
            user_id    = record.user_id,
            notif_type = 'due_reminder_3',
            title      = 'Book due in 3 days',
            body       = (f'Reminder: "{record.book.title}" is due on '
                          f'{record.due_date} (3 days from today). '
                          f'Please return it on time to avoid a fine.'),
            borrow_id  = record.id,
        )

    @staticmethod
    def due_reminder_1(record):
        """Book due tomorrow."""
        _push(
            user_id    = record.user_id,
            notif_type = 'due_reminder_1',
            title      = 'Book due tomorrow!',
            body       = (f'"{record.book.title}" is due tomorrow ({record.due_date}). '
                          f'Please return it to avoid a ₹{record.fine_per_day}/day fine.'),
            borrow_id  = record.id,
        )

    @staticmethod
    def overdue_alert(record):
        """Book is overdue — sent daily by scheduler until returned."""
        _push(
            user_id    = record.user_id,
            notif_type = 'overdue_alert',
            title      = f'Book overdue — ₹{record.current_fine:.0f} fine so far',
            body       = (f'"{record.book.title}" is {record.overdue_days} day(s) '
                          f'overdue. Your current fine is ₹{record.current_fine:.0f} '
                          f'(₹{record.fine_per_day}/day, cap ₹{record.fine_cap}). '
                          f'Please return it as soon as possible.'),
            borrow_id  = record.id,
        )

    # ────────────────────────────────────────────────────────────────
    # Query helpers
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def unread_count(user_id: int) -> int:
        return Notification.query.filter_by(
            user_id=user_id, is_read=False).count()

    @staticmethod
    def get_for_user(user_id: int, limit: int = 50):
        return (
            Notification.query
            .filter_by(user_id=user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def mark_read(notification_id: int, user_id: int) -> bool:
        n = Notification.query.filter_by(
            id=notification_id, user_id=user_id).first()
        if not n:
            return False
        n.is_read = True
        db.session.commit()
        return True

    @staticmethod
    def mark_all_read(user_id: int) -> int:
        count = (
            Notification.query
            .filter_by(user_id=user_id, is_read=False)
            .update({'is_read': True})
        )
        db.session.commit()
        return count
