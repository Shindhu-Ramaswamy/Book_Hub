"""
Notification model
==================
One row per notification per user. All notifications are stored
in-app — no email or SMS.

Notification types
------------------
  borrow_requested    — user submitted a borrow request
  borrow_approved     — librarian approved the request
  borrow_rejected     — librarian rejected the request
  book_returned       — book successfully returned
  return_requested    — user flagged they're bringing a book back in person (self + librarians)
  fine_created        — overdue fine raised on return
  fine_paid           — fine marked as paid
  damage_charge       — book returned damaged, charge raised
  lost_charge         — book returned lost, charge raised
  due_reminder_3      — book due in 3 days (scheduler)
  due_reminder_1      — book due tomorrow (scheduler)
  overdue_alert       — book is now overdue (scheduler, daily)
  reservation_queued  — user joined the reservation queue
  reservation_ready   — user's turn: book is ready to collect
  reservation_expired — hold window expired without collection
  reservation_cancelled — user cancelled their reservation
  reservation_fulfilled — reservation converted to a borrow
  payment_received     — librarian alert: a fine was paid online (no cash to collect)
  membership_due       — registration/renewal fee raised, payment needed to borrow
  membership_paid      — registration/renewal fee paid, account/tier active
  membership_upgraded  — basic → membership upgrade fee paid, new perks active
  membership_payment_received — librarian alert: a membership fee was paid online
  delivery_requested    — user submitted a home-delivery order
  delivery_accepted     — librarian accepted the order
  delivery_packed       — order packed and agent assigned
  delivery_out_for_delivery — agent is en route
  delivery_delivered    — order delivered
  delivery_rejected     — librarian declined the order
  delivery_cancelled    — order cancelled (by user or librarian)
  delivery_fee_paid     — delivery fee marked as paid
  delivery_payment_received — librarian alert: a delivery fee was paid online
  pickup_requested       — user submitted a return-pickup request
  pickup_accepted        — librarian accepted the pickup request
  pickup_out_for_pickup  — agent is en route to collect the book
  pickup_picked_up       — agent collected the book, in transit to library
  pickup_returned        — book received & inspected — pickup order closed
  pickup_rejected        — librarian declined the pickup request
  pickup_cancelled       — pickup request cancelled (by user or librarian)
  pickup_fee_paid        — pickup fee marked as paid
  pickup_payment_received — librarian alert: a pickup fee was paid online

Icon and colour are stored on the row so templates need no logic.
"""

from extensions import db
from datetime import datetime, timezone


# Map each type to (tabler icon name, CSS colour class)
NOTIFICATION_META = {
    'borrow_requested':       ('ti-shopping-cart',      'blue'),
    'borrow_approved':        ('ti-circle-check',       'green'),
    'borrow_rejected':        ('ti-circle-x',           'red'),
    'book_returned':          ('ti-corner-down-left',   'teal'),
    'return_requested':       ('ti-corner-down-left',   'blue'),
    'fine_created':           ('ti-receipt-tax',        'amber'),
    'fine_paid':              ('ti-receipt',            'green'),
    'damage_charge':          ('ti-alert-octagon',      'amber'),
    'lost_charge':            ('ti-file-x',             'red'),
    'due_reminder_3':         ('ti-clock',              'blue'),
    'due_reminder_1':         ('ti-alarm',              'amber'),
    'overdue_alert':          ('ti-alert-triangle',     'red'),
    'reservation_queued':     ('ti-list',               'blue'),
    'reservation_ready':      ('ti-bell-ringing',       'green'),
    'reservation_expired':    ('ti-clock-off',          'red'),
    'reservation_cancelled':  ('ti-x',                  'amber'),
    'reservation_fulfilled':  ('ti-circle-check',       'green'),
    'payment_received':       ('ti-credit-card',        'green'),
    'membership_due':         ('ti-id-badge-2',         'amber'),
    'membership_paid':        ('ti-rosette-discount-check', 'green'),
    'membership_upgraded':    ('ti-crown',               'green'),
    'membership_payment_received': ('ti-credit-card',    'green'),
    'delivery_requested':      ('ti-truck-delivery',     'blue'),
    'delivery_accepted':       ('ti-circle-check',       'green'),
    'delivery_packed':         ('ti-package',            'blue'),
    'delivery_out_for_delivery': ('ti-map-pin',          'amber'),
    'delivery_delivered':      ('ti-package-import',     'green'),
    'delivery_rejected':       ('ti-circle-x',           'red'),
    'delivery_cancelled':      ('ti-x',                  'amber'),
    'delivery_fee_paid':       ('ti-receipt',            'green'),
    'delivery_payment_received': ('ti-credit-card',      'green'),
    'pickup_requested':       ('ti-package-export',      'blue'),
    'pickup_accepted':        ('ti-circle-check',        'green'),
    'pickup_out_for_pickup':  ('ti-truck',                'blue'),
    'pickup_picked_up':       ('ti-package',              'blue'),
    'pickup_returned':        ('ti-corner-down-left',     'green'),
    'pickup_rejected':        ('ti-circle-x',             'red'),
    'pickup_cancelled':       ('ti-x',                    'amber'),
    'pickup_fee_paid':        ('ti-receipt',              'green'),
    'pickup_payment_received': ('ti-credit-card',         'green'),
}


class Notification(db.Model):
    __tablename__ = 'notifications'

    id         = db.Column(db.Integer,  primary_key=True)
    user_id    = db.Column(db.Integer,  db.ForeignKey('users.id'), nullable=False)
    notif_type = db.Column(db.String(40),  nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    body       = db.Column(db.String(400), nullable=False)
    is_read    = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime,
                           default=lambda: datetime.now(timezone.utc),
                           nullable=False)

    # Optional FK references — stored for deep-linking, not enforced strictly
    borrow_id      = db.Column(db.Integer, db.ForeignKey('borrow_records.id'), nullable=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservations.id'),   nullable=True)
    overdue_id     = db.Column(db.Integer, db.ForeignKey('overdue_records.id'), nullable=True)
    membership_payment_id = db.Column(db.Integer, db.ForeignKey('membership_payments.id'), nullable=True)
    delivery_order_id = db.Column(db.Integer, db.ForeignKey('delivery_orders.id'), nullable=True)
    pickup_order_id = db.Column(db.Integer, db.ForeignKey('pickup_orders.id'), nullable=True)

    user = db.relationship('User', foreign_keys=[user_id], backref='notifications')

    # ── Helpers ───────────────────────────────────────────────────
    @property
    def icon(self):
        return NOTIFICATION_META.get(self.notif_type, ('ti-bell', 'blue'))[0]

    @property
    def colour(self):
        return NOTIFICATION_META.get(self.notif_type, ('ti-bell', 'blue'))[1]

    @property
    def time_ago(self):
        """Human-readable relative time — e.g. '2 hours ago'."""
        now   = datetime.now(timezone.utc)
        delta = now - self.created_at.replace(tzinfo=timezone.utc) \
                if self.created_at.tzinfo is None \
                else now - self.created_at
        secs  = int(delta.total_seconds())
        if secs < 60:
            return 'Just now'
        if secs < 3600:
            m = secs // 60
            return f'{m} minute{"s" if m != 1 else ""} ago'
        if secs < 86400:
            h = secs // 3600
            return f'{h} hour{"s" if h != 1 else ""} ago'
        d = secs // 86400
        return f'{d} day{"s" if d != 1 else ""} ago'

    def to_dict(self):
        return {
            'id':         self.id,
            'type':       self.notif_type,
            'title':      self.title,
            'body':       self.body,
            'is_read':    self.is_read,
            'icon':       self.icon,
            'colour':     self.colour,
            'time_ago':   self.time_ago,
            'created_at': self.created_at.isoformat(),
            'borrow_id':  self.borrow_id,
            'reservation_id': self.reservation_id,
            'overdue_id': self.overdue_id,
            'membership_payment_id': self.membership_payment_id,
            'delivery_order_id': self.delivery_order_id,
            'pickup_order_id': self.pickup_order_id,
        }

    def __repr__(self):
        return f'<Notification #{self.id} [{self.notif_type}] user={self.user_id} read={self.is_read}>'
