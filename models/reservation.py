"""
Reservation — holds a user's place in a queue for an unavailable book.

Lifecycle
---------
  queued          → user reserved an unavailable book, waiting their turn
  ready           → a copy became free; this user is next; they have
                    RESERVATION_HOLD_HOURS hours to collect
  expired         → hold window passed without collection; skipped in queue
  fulfilled       → librarian approved the BorrowRecord; done
  cancelled       → user cancelled before becoming ready

Queue position
--------------
  queue_position is 1-based within a book's active reservations.
  It is recalculated on every promotion so gaps are never exposed.
"""

from extensions import db
from datetime import datetime, timezone


class Reservation(db.Model):
    __tablename__ = 'reservations'

    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    book_id        = db.Column(db.Integer, db.ForeignKey('books.id'), nullable=False)
    queue_position = db.Column(db.Integer, nullable=False)
    status         = db.Column(db.String(20), default='queued', nullable=False)
    # queued | ready | expired | fulfilled | cancelled

    created_at     = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ready_at       = db.Column(db.DateTime, nullable=True)   # when status → ready
    expires_at     = db.Column(db.DateTime, nullable=True)   # deadline to collect
    fulfilled_at   = db.Column(db.DateTime, nullable=True)   # when BorrowRecord created
    cancelled_at   = db.Column(db.DateTime, nullable=True)

    # borrow_record_id — set when this reservation produces a BorrowRecord
    borrow_record_id = db.Column(db.Integer, db.ForeignKey('borrow_records.id'), nullable=True)

    # relationships
    user         = db.relationship('User',         foreign_keys=[user_id],   backref='reservations')
    book         = db.relationship('Book',         foreign_keys=[book_id],   backref='reservations')
    borrow_record = db.relationship('BorrowRecord', foreign_keys=[borrow_record_id])

    # ── helpers ──────────────────────────────────────────────────
    @property
    def is_active(self):
        return self.status in ('queued', 'ready')

    @property
    def hours_left(self):
        """Hours remaining in the hold window (only meaningful when ready)."""
        if self.status != 'ready' or not self.expires_at:
            return None
        now = datetime.now(timezone.utc)
        expires = self.expires_at
        # SQLite drops tzinfo on read regardless of how the value was
        # stored, so a naive expires_at here doesn't mean it was ever
        # meant to be interpreted as local time — treat it as UTC to
        # match how it was written.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        delta = expires - now
        return max(0, int(delta.total_seconds() // 3600))

    def to_dict(self):
        return {
            'id':              self.id,
            'book_id':         self.book_id,
            'book_title':      self.book.title      if self.book else None,
            'book_isbn':       self.book.isbn       if self.book else None,
            'user_name':       self.user.name       if self.user else None,
            'user_id_fmt':     self.user.formatted_id if self.user else None,
            'queue_position':  self.queue_position,
            'status':          self.status,
            'created_at':      self.created_at.isoformat()  if self.created_at  else None,
            'ready_at':        self.ready_at.isoformat()    if self.ready_at    else None,
            'expires_at':      self.expires_at.isoformat()  if self.expires_at  else None,
            'fulfilled_at':    self.fulfilled_at.isoformat() if self.fulfilled_at else None,
            'cancelled_at':    self.cancelled_at.isoformat() if self.cancelled_at else None,
            'hours_left':      self.hours_left,
            'borrow_record_id': self.borrow_record_id,
        }

    def __repr__(self):
        return (f'<Reservation #{self.id} book={self.book_id} '
                f'user={self.user_id} pos={self.queue_position} [{self.status}]>')
