from extensions import db
from datetime import date


class Cart(db.Model):
    __tablename__ = 'cart'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    book_id    = db.Column(db.Integer, db.ForeignKey('books.id'), nullable=False)
    added_date = db.Column(db.Date, default=date.today)

    book = db.relationship('Book', backref='cart_items', lazy=True)
    user = db.relationship('User', backref='cart_items', lazy=True)

    def to_dict(self):
        return {
            'id':         self.id,
            'book_id':    self.book_id,
            'book_title': self.book.title  if self.book else None,
            'author':     self.book.author if self.book else None,
            'genre':      self.book.genre  if self.book else None,
            'added_date': str(self.added_date),
        }
