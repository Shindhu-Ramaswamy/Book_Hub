"""
Home delivery — DeliveryAgent + DeliveryOrder.

DeliveryAgent
-------------
A librarian-managed roster (name + phone) — no login of their own.
Librarians assign one to a DeliveryOrder and update its status on the
agent's behalf. Soft-disable only (is_active) — never hard-delete,
since past orders keep a foreign key to whichever agent carried them.

DeliveryOrder
-------------
One row per checkout — groups every book requested for delivery in a
single cart submission into one parcel, the same way an Amazon "order"
groups multiple items. Each book gets its own BorrowRecord (via
BorrowRecord.delivery_order_id) so all the existing borrow/fine/return
machinery keeps working unmodified once the parcel is delivered.

Lifecycle
---------
  requested         → user submitted cart for delivery, awaiting librarian
  accepted           → librarian accepted; books leave the shelf now
  packed             → librarian packed the parcel
  shipped            → handed to the assigned delivery agent
  out_for_delivery   → agent is en route
  delivered          → agent handed it over; BorrowRecords become 'borrowed'
  rejected           → librarian declined (only reachable from 'requested')
  cancelled          → user or librarian cancelled (only reachable before 'packed')

See BorrowRecord.status='in_delivery' for how each book's status maps
to this pipeline while the parcel is in flight.
"""
from extensions import db
from datetime import date

DELIVERY_STATUSES = [
    'requested', 'accepted', 'packed', 'shipped',
    'out_for_delivery', 'delivered', 'rejected', 'cancelled',
]

# Ordered forward pipeline used to build the tracking-page stepper.
DELIVERY_PIPELINE = [
    ('requested',         'Requested'),
    ('accepted',          'Accepted'),
    ('packed',            'Packed'),
    ('shipped',           'Shipped'),
    ('out_for_delivery',  'Out for Delivery'),
    ('delivered',         'Delivered'),
]


class DeliveryAgent(db.Model):
    __tablename__ = 'delivery_agents'

    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(100), nullable=False)
    phone        = db.Column(db.String(15),  nullable=False)
    is_active    = db.Column(db.Boolean, default=True, nullable=False)
    created_date = db.Column(db.Date, default=date.today)

    orders = db.relationship('DeliveryOrder', backref='agent', lazy=True)

    def to_dict(self):
        return {
            'id':        self.id,
            'name':      self.name,
            'phone':     self.phone,
            'is_active': self.is_active,
        }

    def __repr__(self):
        return f'<DeliveryAgent {self.name} ({self.phone})>'


class DeliveryOrder(db.Model):
    __tablename__ = 'delivery_orders'

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status  = db.Column(db.String(20), default='requested', nullable=False)

    # ── Delivery address (entered per-order) ────────────────────────
    recipient_name = db.Column(db.String(100), nullable=False)
    phone          = db.Column(db.String(15),  nullable=False)
    address_line1  = db.Column(db.String(200), nullable=False)
    address_line2  = db.Column(db.String(200), nullable=True)
    city           = db.Column(db.String(80),  nullable=False)
    state          = db.Column(db.String(80),  nullable=False)
    pincode        = db.Column(db.String(10),  nullable=False)
    landmark       = db.Column(db.String(120), nullable=True)

    # ── Fee (frozen at request time) ─────────────────────────────────
    delivery_fee   = db.Column(db.Float, nullable=False)
    fee_status     = db.Column(db.String(20), default='unpaid', nullable=False)  # unpaid | paid
    payment_method = db.Column(db.String(20), nullable=True)   # cash | upi | card | online
    fee_paid_date  = db.Column(db.Date, nullable=True)
    fee_collected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Online payment gateway trail — same shape as OverdueRecord/MembershipPayment
    payment_gateway    = db.Column(db.String(30),  nullable=True)
    gateway_order_id   = db.Column(db.String(100), nullable=True)
    gateway_payment_id = db.Column(db.String(100), nullable=True)

    # ── Pipeline ──────────────────────────────────────────────────────
    agent_id          = db.Column(db.Integer, db.ForeignKey('delivery_agents.id'), nullable=True)
    requested_date    = db.Column(db.Date, default=date.today)
    accepted_date     = db.Column(db.Date, nullable=True)
    accepted_by       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    packed_date       = db.Column(db.Date, nullable=True)
    shipped_date      = db.Column(db.Date, nullable=True)
    out_for_delivery_date = db.Column(db.Date, nullable=True)
    delivered_date    = db.Column(db.Date, nullable=True)
    rejected_date     = db.Column(db.Date, nullable=True)
    cancelled_date    = db.Column(db.Date, nullable=True)
    rejection_reason  = db.Column(db.String(300), nullable=True)

    # ── Relationships ────────────────────────────────────────────────
    user      = db.relationship('User', foreign_keys=[user_id],           backref='delivery_orders')
    librarian = db.relationship('User', foreign_keys=[accepted_by])
    collector = db.relationship('User', foreign_keys=[fee_collected_by])
    records   = db.relationship('BorrowRecord', backref='delivery_order', lazy=True)

    # ── Helpers ───────────────────────────────────────────────────────
    @property
    def book_count(self):
        return len(self.records)

    @property
    def is_terminal(self):
        return self.status in ('delivered', 'rejected', 'cancelled')

    def to_dict(self):
        return {
            'id':                self.id,
            'status':            self.status,
            'recipient_name':    self.recipient_name,
            'phone':             self.phone,
            'address_line1':     self.address_line1,
            'address_line2':     self.address_line2,
            'city':              self.city,
            'state':             self.state,
            'pincode':           self.pincode,
            'landmark':          self.landmark,
            'book_count':        self.book_count,
            'books':             [r.book.title for r in self.records if r.book],
            'delivery_fee':      self.delivery_fee,
            'fee_status':        self.fee_status,
            'agent_name':        self.agent.name  if self.agent else None,
            'agent_phone':       self.agent.phone if self.agent else None,
            'requested_date':    str(self.requested_date)    if self.requested_date    else None,
            'accepted_date':     str(self.accepted_date)     if self.accepted_date     else None,
            'packed_date':       str(self.packed_date)       if self.packed_date       else None,
            'shipped_date':      str(self.shipped_date)      if self.shipped_date      else None,
            'out_for_delivery_date': str(self.out_for_delivery_date) if self.out_for_delivery_date else None,
            'delivered_date':    str(self.delivered_date)    if self.delivered_date    else None,
            'rejected_date':     str(self.rejected_date)     if self.rejected_date     else None,
            'cancelled_date':    str(self.cancelled_date)    if self.cancelled_date    else None,
            'rejection_reason':  self.rejection_reason,
            'is_terminal':       self.is_terminal,
        }

    def __repr__(self):
        return f'<DeliveryOrder #{self.id} user={self.user_id} [{self.status}]>'
