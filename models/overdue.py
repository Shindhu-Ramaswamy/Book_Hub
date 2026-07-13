"""
OverdueRecord doubles as a general "charge" ledger row.

Originally this only ever represented an overdue fine, raised either
live by the return flow or nightly by the scheduler. It now also
represents damage and lost-book charges raised during return
inspection — same paid/unpaid + mark-paid workflow, just a different
charge_type. Existing rows have no charge_type set, so the column
defaults to 'overdue' to stay backward compatible.
"""
from extensions import db
from datetime import date

PAYMENT_METHODS = ['cash', 'upi', 'card', 'online']
CHARGE_TYPES     = ['overdue', 'damaged', 'lost']


class OverdueRecord(db.Model):
    __tablename__ = 'overdue_records'

    id             = db.Column(db.Integer, primary_key=True)
    borrow_id      = db.Column(db.Integer, db.ForeignKey('borrow_records.id'), nullable=False)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount         = db.Column(db.Float, nullable=False)
    fine_status    = db.Column(db.String(20), default='unpaid')   # unpaid | paid
    charge_type    = db.Column(db.String(20), default='overdue', nullable=False)  # overdue | damaged | lost
    notes          = db.Column(db.Text, nullable=True)
    issued_date    = db.Column(db.Date, default=date.today)
    paid_date      = db.Column(db.Date, nullable=True)
    collected_by   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    payment_method = db.Column(db.String(20), nullable=True)

    # ── Online payment gateway trail ────────────────────────────
    # Populated only when paid_method == 'online' via Razorpay (or
    # similar). collected_by stays NULL for these — no librarian was
    # involved. Kept separately from payment_method so we can add other
    # gateways later without a schema change.
    payment_gateway    = db.Column(db.String(30),  nullable=True)   # e.g. 'razorpay'
    gateway_order_id   = db.Column(db.String(100), nullable=True)
    gateway_payment_id = db.Column(db.String(100), nullable=True)

    user      = db.relationship('User', foreign_keys=[user_id],    backref='overdue_records')
    collector = db.relationship('User', foreign_keys=[collected_by])

    def to_dict(self):
        br = self.borrow_record
        return {
            'id':             self.id,
            'borrow_id':      self.borrow_id,
            'user_name':      self.user.name         if self.user      else None,
            'user_id_fmt':    self.user.formatted_id if self.user      else None,
            'book_title':     br.book.title          if (br and br.book) else None,
            'amount':         self.amount,
            'fine_status':    self.fine_status,
            'charge_type':    self.charge_type,
            'notes':          self.notes,
            'issued_date':    str(self.issued_date),
            'paid_date':      str(self.paid_date)    if self.paid_date else None,
            'payment_method': self.payment_method,
            'collector':      self.collector.name    if self.collector else None,
            'payment_gateway':    self.payment_gateway,
            'gateway_payment_id': self.gateway_payment_id,
        }
