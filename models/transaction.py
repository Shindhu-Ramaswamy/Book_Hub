"""
BorrowRecord — single model for the full borrow lifecycle.
  pending → borrowed → returned
  pending → borrowed → lost       (marked lost during return inspection)
  pending → rejected
  pending → in_delivery → borrowed   (home delivery — see DeliveryOrder)
  pending → cancelled

For a home-delivery request, delivery_order_id is set and the record
sits in 'in_delivery' from the moment the librarian accepts the parcel
(the book leaves the shelf) until the agent hands it over, at which
point it becomes 'borrowed' — same as any other issued book, so all
the fine/return logic below applies unchanged once delivered.

For a return-pickup request (see ReturnPickupOrder), pickup_order_id
is set but status is deliberately left untouched — it stays 'borrowed'
for the whole pickup pipeline (fines keep accruing normally) and only
becomes 'returned'/'lost' via the ordinary return_book() inspection
once the librarian receives the book back at the library.

Fine calculation rules (all driven by Config):
  - Grace period: FINE_GRACE_DAYS days after due_date before fine starts
  - Rate: FINE_PER_DAY (₹) per overdue day after grace period
  - Cap: FINE_MAX_AMOUNT (₹) — fine never exceeds this per book
  - On return or lost: fine is frozen into fine_amount; live fine stops accruing
"""
from extensions import db
from datetime import date
from config import Config


def _calc_fine(overdue_days: int) -> float:
    """Pure function — fine for N overdue days, applying grace and cap."""
    billable = max(0, overdue_days - Config.FINE_GRACE_DAYS)
    raw      = billable * Config.FINE_PER_DAY
    return min(raw, Config.FINE_MAX_AMOUNT)


class BorrowRecord(db.Model):
    __tablename__ = 'borrow_records'

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    book_id      = db.Column(db.Integer, db.ForeignKey('books.id'), nullable=False)
    request_date = db.Column(db.Date, default=date.today)
    issue_date   = db.Column(db.Date, nullable=True)
    issued_by    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    borrow_date  = db.Column(db.Date, nullable=True)
    due_date     = db.Column(db.Date, nullable=True)
    return_date  = db.Column(db.Date, nullable=True)
    status       = db.Column(db.String(20), default='pending')
    # status: pending | borrowed | returned | lost | rejected | in_delivery | cancelled
    fine_amount  = db.Column(db.Float, default=0.0)  # frozen on return/lost

    # Set only for home-delivery requests — NULL means the usual pickup flow.
    delivery_order_id = db.Column(db.Integer, db.ForeignKey('delivery_orders.id'), nullable=True)
    # Set only for return-pickup requests — NULL means the usual in-person return.
    pickup_order_id   = db.Column(db.Integer, db.ForeignKey('pickup_orders.id'),   nullable=True)
    # Set when the user flags "I'm bringing this back to the library" from
    # My Books — an advance heads-up for the librarian, not a state change.
    # The actual return still goes through the normal return_inspect flow.
    return_requested_at = db.Column(db.Date, nullable=True)

    librarian       = db.relationship('User', foreign_keys=[issued_by])
    overdue_records = db.relationship('OverdueRecord', backref='borrow_record', lazy=True)

    # ── Overdue days ─────────────────────────────────────────────
    @property
    def overdue_days(self) -> int:
        """How many calendar days past due_date."""
        if not self.due_date:
            return 0
        if self.status in ('returned', 'lost') and self.return_date:
            return max(0, (self.return_date - self.due_date).days)
        if self.status == 'borrowed':
            return max(0, (date.today() - self.due_date).days)
        return 0

    @property
    def billable_days(self) -> int:
        """Overdue days minus grace period (never negative)."""
        return max(0, self.overdue_days - Config.FINE_GRACE_DAYS)

    @property
    def is_overdue(self) -> bool:
        return (self.status == 'borrowed'
                and bool(self.due_date)
                and date.today() > self.due_date)

    @property
    def days_remaining(self) -> int:
        if self.status in ('returned', 'lost', 'pending', 'rejected') or not self.due_date:
            return 0
        return (self.due_date - date.today()).days

    # ── Fine ─────────────────────────────────────────────────────
    @property
    def current_fine(self) -> float:
        """
        Live fine: accrues each day while status='borrowed'.
        Frozen fine: returned from fine_amount once status is 'returned' or 'lost'.
        """
        if self.status in ('returned', 'lost'):
            return self.fine_amount
        return _calc_fine(self.overdue_days)

    @property
    def fine_per_day(self) -> int:
        return Config.FINE_PER_DAY

    @property
    def grace_days(self) -> int:
        return Config.FINE_GRACE_DAYS

    @property
    def fine_cap(self) -> int:
        return Config.FINE_MAX_AMOUNT

    # ── Condition charge (damaged/lost) ───────────────────────────
    @property
    def condition_type(self):
        """
        'damaged' | 'lost' | None — which condition charge (if any) was
        raised when this book was returned. status alone can't answer
        this: a damaged-but-returned book still has status='returned',
        identical to a book returned in good condition.
        """
        for o in self.overdue_records:
            if o.charge_type in ('damaged', 'lost'):
                return o.charge_type
        return None

    @property
    def total_charges(self) -> float:
        """
        Everything owed for this transaction — overdue fine plus any
        damage/lost charge. current_fine only ever reflects the overdue
        fine (frozen into fine_amount on return); a damage/lost charge
        is raised as its own OverdueRecord row and current_fine never
        includes it. Nothing is chargeable yet while still 'borrowed'
        (condition is only assessed on return), so current_fine — the
        live overdue estimate — is the right value for that case.
        """
        if self.status in ('returned', 'lost'):
            return sum(o.amount for o in self.overdue_records)
        return self.current_fine

    # ── Serialise ────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            'id':             self.id,
            'book_title':     self.book.title         if self.book      else None,
            'book_isbn':      self.book.isbn           if self.book      else None,
            'user_name':      self.user.name           if self.user      else None,
            'user_id_fmt':    self.user.formatted_id  if self.user      else None,
            'librarian':      self.librarian.name      if self.librarian else None,
            'status':         self.status,
            'request_date':   str(self.request_date)  if self.request_date else None,
            'issue_date':     str(self.issue_date)    if self.issue_date   else None,
            'borrow_date':    str(self.borrow_date)   if self.borrow_date  else None,
            'due_date':       str(self.due_date)      if self.due_date     else None,
            'return_date':    str(self.return_date)   if self.return_date  else None,
            'days_remaining': self.days_remaining,
            'overdue_days':   self.overdue_days,
            'billable_days':  self.billable_days,
            'fine':           self.current_fine,
            'fine_per_day':   self.fine_per_day,
            'grace_days':     self.grace_days,
            'is_overdue':     self.is_overdue,
        }

    def __repr__(self):
        return f'<BorrowRecord {self.id} [{self.status}] fine=₹{self.current_fine}>'
