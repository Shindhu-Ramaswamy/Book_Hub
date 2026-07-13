"""
Return pickup — ReturnPickupOrder.

Reverse-logistics counterpart to home delivery (models/delivery.py):
instead of the user bringing a borrowed book back to the library, a
delivery agent collects it from their address.

One row per book (see routes/user.py pickup_request_form — one pickup
request per book, unlike DeliveryOrder which can bundle several).
Reuses the same DeliveryAgent roster as delivery — no separate agent
management.

Lifecycle
---------
  requested       → user requested pickup for a borrowed book
  accepted        → librarian accepted; a delivery agent gets assigned
  out_for_pickup  → agent is en route to collect (requires fee paid)
  picked_up       → agent collected the book; it's in transit to the library
  returned        → every linked BorrowRecord reached a terminal status
                    via the ordinary librarian return-inspection flow —
                    set automatically, never directly
  rejected        → librarian declined (only reachable from 'requested')
  cancelled       → user or librarian cancelled (only before 'out_for_pickup')

Fee
---
pickup_fee is normally base+per-book (Config.PICKUP_BASE_FEE /
PICKUP_FEE_PER_BOOK), same payment machinery as DeliveryOrder (cash or
Razorpay). But if the book being returned was originally home-delivered
(BorrowRecord.delivery_order_id is set), that original delivery fee
already covers the round trip — pickup_fee is waived to 0 and
fee_status starts 'paid', matching MembershipService's pattern for a
free charge (see services/pickup_service.py request_pickup()).

BorrowRecord.status is deliberately left untouched by this entire
pipeline — it stays 'borrowed' throughout (fines keep accruing exactly
as today) and only flips to 'returned'/'lost' via the existing
BookService.return_book() once the librarian inspects the book at the
library, exactly like a counter return.
"""
from extensions import db
from datetime import date

PICKUP_STATUSES = [
    'requested', 'accepted', 'out_for_pickup', 'picked_up',
    'returned', 'rejected', 'cancelled',
]

# Ordered forward pipeline used to build the tracking-page stepper.
PICKUP_PIPELINE = [
    ('requested',       'Requested'),
    ('accepted',        'Accepted'),
    ('out_for_pickup',  'Out for Pickup'),
    ('picked_up',       'Picked Up'),
    ('returned',        'Returned'),
]


class ReturnPickupOrder(db.Model):
    __tablename__ = 'pickup_orders'

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status  = db.Column(db.String(20), default='requested', nullable=False)

    # ── Pickup address (entered per-order) ───────────────────────────
    recipient_name = db.Column(db.String(100), nullable=False)
    phone          = db.Column(db.String(15),  nullable=False)
    address_line1  = db.Column(db.String(200), nullable=False)
    address_line2  = db.Column(db.String(200), nullable=True)
    city           = db.Column(db.String(80),  nullable=False)
    state          = db.Column(db.String(80),  nullable=False)
    pincode        = db.Column(db.String(10),  nullable=False)
    landmark       = db.Column(db.String(120), nullable=True)

    # ── Fee (frozen at request time; may be waived to 0 — see module docstring) ──
    pickup_fee     = db.Column(db.Float, nullable=False)
    fee_status     = db.Column(db.String(20), default='unpaid', nullable=False)  # unpaid | paid
    payment_method = db.Column(db.String(20), nullable=True)   # cash | upi | card | online
    fee_paid_date  = db.Column(db.Date, nullable=True)
    fee_collected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    payment_gateway    = db.Column(db.String(30),  nullable=True)
    gateway_order_id   = db.Column(db.String(100), nullable=True)
    gateway_payment_id = db.Column(db.String(100), nullable=True)

    # ── Pipeline ──────────────────────────────────────────────────────
    agent_id           = db.Column(db.Integer, db.ForeignKey('delivery_agents.id'), nullable=True)
    requested_date     = db.Column(db.Date, default=date.today)
    accepted_date      = db.Column(db.Date, nullable=True)
    accepted_by        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    out_for_pickup_date = db.Column(db.Date, nullable=True)
    picked_up_date      = db.Column(db.Date, nullable=True)
    returned_date        = db.Column(db.Date, nullable=True)
    rejected_date         = db.Column(db.Date, nullable=True)
    cancelled_date          = db.Column(db.Date, nullable=True)
    rejection_reason         = db.Column(db.String(300), nullable=True)

    # ── Relationships ────────────────────────────────────────────────
    user      = db.relationship('User', foreign_keys=[user_id],      backref='pickup_orders')
    librarian = db.relationship('User', foreign_keys=[accepted_by])
    collector = db.relationship('User', foreign_keys=[fee_collected_by])
    agent     = db.relationship('DeliveryAgent', foreign_keys=[agent_id], backref='pickup_orders')
    records   = db.relationship('BorrowRecord', backref='pickup_order', lazy=True)

    # ── Helpers ───────────────────────────────────────────────────────
    @property
    def book_count(self):
        return len(self.records)

    @property
    def is_terminal(self):
        return self.status in ('returned', 'rejected', 'cancelled')

    def to_dict(self):
        return {
            'id':               self.id,
            'status':           self.status,
            'recipient_name':   self.recipient_name,
            'phone':            self.phone,
            'address_line1':    self.address_line1,
            'address_line2':    self.address_line2,
            'city':             self.city,
            'state':            self.state,
            'pincode':          self.pincode,
            'landmark':         self.landmark,
            'book_count':       self.book_count,
            'books':            [r.book.title for r in self.records if r.book],
            'pickup_fee':       self.pickup_fee,
            'fee_status':       self.fee_status,
            'agent_name':       self.agent.name  if self.agent else None,
            'agent_phone':      self.agent.phone if self.agent else None,
            'requested_date':   str(self.requested_date)   if self.requested_date   else None,
            'accepted_date':    str(self.accepted_date)    if self.accepted_date    else None,
            'out_for_pickup_date': str(self.out_for_pickup_date) if self.out_for_pickup_date else None,
            'picked_up_date':   str(self.picked_up_date)   if self.picked_up_date   else None,
            'returned_date':    str(self.returned_date)    if self.returned_date    else None,
            'rejected_date':    str(self.rejected_date)    if self.rejected_date    else None,
            'cancelled_date':   str(self.cancelled_date)   if self.cancelled_date   else None,
            'rejection_reason': self.rejection_reason,
            'is_terminal':      self.is_terminal,
        }

    def __repr__(self):
        return f'<ReturnPickupOrder #{self.id} user={self.user_id} [{self.status}]>'
