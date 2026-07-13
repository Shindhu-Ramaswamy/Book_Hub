from extensions import db
from datetime import date


class DamagedBook(db.Model):
    __tablename__ = 'damaged_books'

    id            = db.Column(db.Integer, primary_key=True)
    book_id       = db.Column(db.Integer, db.ForeignKey('books.id'),  nullable=False)
    borrow_id     = db.Column(db.Integer, db.ForeignKey('borrow_records.id'), nullable=True)
    reported_by   = db.Column(db.Integer, db.ForeignKey('users.id'),  nullable=False)
    reported_date = db.Column(db.Date, default=date.today)
    quantity      = db.Column(db.Integer, default=1)
    notes         = db.Column(db.Text, nullable=True)

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
        }
