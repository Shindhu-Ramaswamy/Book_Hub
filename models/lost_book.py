"""
LostBook — registry of books marked lost during return inspection.

Mirrors DamagedBook's shape (book, borrow record, who reported it, when,
notes) but is its own table so lost and damaged copies are tracked
separately, as they have different real-world outcomes (damaged copies
may come back into circulation after repair; lost copies never do).

The actual fine/charge for a lost book still flows through the shared
OverdueRecord ledger (charge_type='lost') — same paid/unpaid + collector
+ payment-method workflow as overdue fines and damage charges. This
table is the physical-loss log, not the payment ledger.
"""
from extensions import db
from datetime import date


class LostBook(db.Model):
    __tablename__ = 'lost_books'

    id            = db.Column(db.Integer, primary_key=True)
    book_id       = db.Column(db.Integer, db.ForeignKey('books.id'),  nullable=False)
    borrow_id     = db.Column(db.Integer, db.ForeignKey('borrow_records.id'), nullable=True)
    reported_by   = db.Column(db.Integer, db.ForeignKey('users.id'),  nullable=False)
    reported_date = db.Column(db.Date, default=date.today)
    quantity      = db.Column(db.Integer, default=1)
    notes         = db.Column(db.Text, nullable=True)
    charge_amount = db.Column(db.Float, nullable=True)   # amount charged for this loss

    librarian     = db.relationship('User', foreign_keys=[reported_by])
    borrow_record = db.relationship('BorrowRecord', foreign_keys=[borrow_id])

    def to_dict(self):
        return {
            'id':            self.id,
            'book_title':    self.book.title       if self.book      else None,
            'borrow_id':     self.borrow_id,
            'reported_by':   self.librarian.name   if self.librarian else None,
            'reported_date': str(self.reported_date),
            'quantity':      self.quantity,
            'notes':         self.notes,
            'charge_amount': self.charge_amount,
        }
