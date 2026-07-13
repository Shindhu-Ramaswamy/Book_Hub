"""
MembershipPayment — charge ledger for registration, renewal, and tier
upgrade fees. Mirrors OverdueRecord's paid/unpaid + collector +
payment-method shape (including the online gateway trail), but is its
own table since these charges aren't tied to a borrow_id the way fines
and damage/loss charges are.

payment_type: 'registration' | 'renewal' | 'upgrade'
  registration — created once, unpaid, when a user account is created.
                 Blocks borrowing until paid.
  renewal      — created by the scheduler when membership_paid_until
                 has passed and no unpaid renewal already exists.
  upgrade      — created when a basic member requests the paid upgrade
                 to 'membership'; on payment, User.membership_type
                 flips to 'membership' (renewal date is untouched).

membership_type: which tier this payment is for/grants ('basic' |
'membership') — for 'renewal' this is the user's tier at the time the
renewal was raised, so the amount matches what was actually charged.
"""
from extensions import db
from datetime import date

PAYMENT_TYPES = ['registration', 'renewal', 'upgrade']


class MembershipPayment(db.Model):
    __tablename__ = 'membership_payments'

    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    payment_type    = db.Column(db.String(20), nullable=False)
    membership_type = db.Column(db.String(20), nullable=False)  # basic | membership
    amount          = db.Column(db.Float, nullable=False)
    status          = db.Column(db.String(20), default='unpaid', nullable=False)  # unpaid | paid
    notes           = db.Column(db.Text, nullable=True)
    issued_date     = db.Column(db.Date, default=date.today)
    paid_date       = db.Column(db.Date, nullable=True)
    collected_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    payment_method  = db.Column(db.String(20), nullable=True)  # cash | upi | card | online

    # ── Online payment gateway trail (same shape as OverdueRecord) ──
    payment_gateway    = db.Column(db.String(30),  nullable=True)
    gateway_order_id   = db.Column(db.String(100), nullable=True)
    gateway_payment_id = db.Column(db.String(100), nullable=True)

    user      = db.relationship('User', foreign_keys=[user_id], backref='membership_payments')
    collector = db.relationship('User', foreign_keys=[collected_by])

    def to_dict(self):
        return {
            'id':                  self.id,
            'user_name':           self.user.name         if self.user else None,
            'user_id_fmt':         self.user.formatted_id if self.user else None,
            'payment_type':        self.payment_type,
            'membership_type':     self.membership_type,
            'amount':              self.amount,
            'status':              self.status,
            'notes':               self.notes,
            'issued_date':         str(self.issued_date),
            'paid_date':           str(self.paid_date) if self.paid_date else None,
            'payment_method':      self.payment_method,
            'collector':           self.collector.name if self.collector else None,
            'payment_gateway':     self.payment_gateway,
            'gateway_payment_id':  self.gateway_payment_id,
        }

    def __repr__(self):
        return (f'<MembershipPayment #{self.id} user={self.user_id} '
                f'[{self.payment_type}/{self.membership_type}] {self.status} ₹{self.amount}>')
