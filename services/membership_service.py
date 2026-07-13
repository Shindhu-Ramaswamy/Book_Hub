"""
MembershipService
=================
All membership tier / fee business logic lives here. Routes call these
methods — no SQLAlchemy queries on MembershipPayment outside this file.

Rules (from Config.MEMBERSHIP_RULES)
-------------------------------------
  basic:      free registration, ₹80/year renewal,  3 books, 30-day borrow
  membership: ₹100 upgrade (on top of basic),
              ₹200/year renewal, 5 books, 60-day borrow

Lifecycle
---------
  1. On registration, a 'registration' charge is raised. Basic
     registration is currently free (₹0), so this is auto-applied as
     'paid' immediately — see _raise_charge(). If registration_fee is
     ever changed to a non-zero amount, it goes back to sitting
     'unpaid' like any other charge, and BookService/ReservationService
     block borrowing (via User.membership_active) until it's paid.
  2. Paying registration sets membership_paid_until = today + 365 days.
  3. A basic member can request the paid upgrade to 'membership' at any
     time their membership is active — this raises an 'upgrade' charge.
     Paying it flips membership_type immediately; the renewal clock
     (membership_paid_until) is untouched by an upgrade.
  4. Once membership_paid_until passes, the nightly scheduler raises a
     'renewal' charge sized to whatever tier the user is currently in.
     Paying it extends membership_paid_until by another 365 days from
     the payment date and re-activates borrowing.

At most one unpaid charge is allowed to be outstanding per user at a
time — pending_payment() is checked before raising a new one, so a
renewal can't stack on top of an unpaid upgrade, etc.
"""

import logging
from datetime import date, timedelta

from extensions import db
from models.membership import MembershipPayment
from config import Config

log = logging.getLogger('lms.membership')


class MembershipService:

    # ── Queries ──────────────────────────────────────────────────────
    @staticmethod
    def pending_payment(user_id: int):
        """The one outstanding unpaid charge for this user, if any."""
        return (
            MembershipPayment.query
            .filter_by(user_id=user_id, status='unpaid')
            .order_by(MembershipPayment.issued_date.desc())
            .first()
        )

    @staticmethod
    def history(user_id: int):
        return (
            MembershipPayment.query
            .filter_by(user_id=user_id)
            .order_by(MembershipPayment.issued_date.desc())
            .all()
        )

    @staticmethod
    def all_pending():
        """Every outstanding unpaid charge — for the librarian queue."""
        return (
            MembershipPayment.query
            .filter_by(status='unpaid')
            .order_by(MembershipPayment.issued_date.asc())
            .all()
        )

    # ── Raise charges ────────────────────────────────────────────────
    @staticmethod
    def _raise_charge(user_id: int, payment_type: str, membership_type: str, amount: float):
        """
        Create a charge row. A ₹0 charge (e.g. the free registration
        fee) has nothing to actually collect, so it's recorded as
        already 'paid' and applied immediately instead of sitting
        'unpaid' waiting for someone to click "mark paid" on a zero
        amount — that would needlessly block borrowing on a free tier.
        """
        free = amount <= 0
        payment = MembershipPayment(
            user_id         = user_id,
            payment_type    = payment_type,
            membership_type = membership_type,
            amount          = amount,
            status          = 'paid' if free else 'unpaid',
            issued_date     = date.today(),
            paid_date       = date.today() if free else None,
            payment_method  = 'free' if free else None,
        )
        db.session.add(payment)
        db.session.commit()

        from services.notification_service import NotificationService
        if free:
            MembershipService._apply_payment(payment)
        else:
            NotificationService.membership_due(payment)
        return payment

    @staticmethod
    def create_registration_charge(user):
        """Called once, right after a 'user' account is created."""
        rules = Config.MEMBERSHIP_RULES['basic']
        return MembershipService._raise_charge(
            user.id, 'registration', 'basic', rules['registration_fee']
        )

    @staticmethod
    def request_upgrade(user_id: int):
        """
        Basic member requests the paid upgrade to Membership.
        Returns (MembershipPayment, None) or (None, error_string).
        """
        from models.user import User

        user = User.query.get(user_id)
        if not user:
            return None, 'User not found.'
        if user.membership_type == 'membership':
            return None, 'You are already a Membership member.'
        if not user.membership_active:
            return None, 'Please clear your current registration/renewal fee first.'
        if MembershipService.pending_payment(user_id):
            return None, 'You already have a pending membership payment.'

        rules = Config.MEMBERSHIP_RULES['membership']
        payment = MembershipService._raise_charge(
            user_id, 'upgrade', 'membership', rules['upgrade_fee']
        )
        return payment, None

    @staticmethod
    def create_renewal_charge(user):
        """
        Called by the scheduler when user.membership_paid_until has
        passed. No-op if an unpaid charge is already outstanding.
        """
        if MembershipService.pending_payment(user.id):
            return None
        rules = user.membership_rules
        return MembershipService._raise_charge(
            user.id, 'renewal', user.membership_type, rules['renewal_fee']
        )

    # ── Apply a paid charge (shared by cash + online paths) ────────────
    @staticmethod
    def _apply_payment(payment: MembershipPayment):
        """
        Flip the user's tier/renewal date once a charge is marked paid.
        Idempotent by construction — callers only invoke this once,
        guarded by a fine_status/status check before calling.
        """
        user = payment.user
        from services.notification_service import NotificationService

        if payment.payment_type == 'upgrade':
            user.membership_type = 'membership'
            db.session.commit()
            NotificationService.membership_upgraded(payment)
        else:  # registration | renewal
            user.membership_type       = payment.membership_type
            user.membership_paid_until = date.today() + timedelta(days=Config.MEMBERSHIP_VALID_DAYS)
            db.session.commit()
            NotificationService.membership_paid(payment)

    # ── Pay: cash / counter (librarian confirms) ────────────────────────
    @staticmethod
    def mark_paid(payment_id: int, librarian_id: int, payment_method: str):
        """Cash/counter payment — a librarian manually confirms collection."""
        payment = MembershipPayment.query.get_or_404(payment_id)
        if payment.status == 'paid':
            return payment
        payment.status         = 'paid'
        payment.paid_date      = date.today()
        payment.collected_by   = librarian_id
        payment.payment_method = payment_method
        db.session.commit()

        MembershipService._apply_payment(payment)
        return payment

    # ── Pay: online gateway (Razorpay) ──────────────────────────────────
    @staticmethod
    def record_online_payment(payment_id: int, gateway: str, order_id: str, gw_payment_id: str):
        """
        Mark a membership charge paid via an online gateway after the
        caller has ALREADY verified the payment signature. Idempotent —
        a second call (e.g. webhook arriving after the client-side
        verify already ran) is a no-op.
        """
        payment = MembershipPayment.query.get_or_404(payment_id)
        if payment.status == 'paid':
            return payment

        payment.status             = 'paid'
        payment.paid_date          = date.today()
        payment.collected_by       = None
        payment.payment_method     = 'online'
        payment.payment_gateway    = gateway
        payment.gateway_order_id   = order_id
        payment.gateway_payment_id = gw_payment_id
        db.session.commit()

        MembershipService._apply_payment(payment)

        from services.notification_service import NotificationService
        NotificationService.membership_payment_received(payment)
        return payment
