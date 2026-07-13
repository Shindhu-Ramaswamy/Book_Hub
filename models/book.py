from extensions import db

GENRES = [
    'Fiction', 'Non-Fiction', 'Mystery', 'Thriller', 'Science Fiction',
    'Fantasy', 'Romance', 'Horror', 'Historical Fiction', 'Biography',
    'Autobiography', 'Self-Help', 'Adventure', 'Drama', 'Poetry',
    "Children's Literature", 'Young Adult', 'Educational', 'Technology',
    'Health & Fitness',
]


class Book(db.Model):
    __tablename__ = 'books'

    id              = db.Column(db.Integer, primary_key=True)
    isbn            = db.Column(db.String(20),  unique=True, nullable=False)
    title           = db.Column(db.String(200), nullable=False)
    author          = db.Column(db.String(100), nullable=False)
    genre           = db.Column(db.String(50),  nullable=False)
    total_quantity  = db.Column(db.Integer, nullable=False, default=1)
    issued_count    = db.Column(db.Integer, default=0)
    lifetime_issued = db.Column(db.Integer, default=0)
    is_deleted      = db.Column(db.Boolean, default=False, nullable=False)  # soft delete

    transactions = db.relationship('BorrowRecord', backref='book', lazy=True)
    damaged_logs = db.relationship('DamagedBook',  backref='book', lazy=True)
    lost_logs    = db.relationship('LostBook',     backref='book', lazy=True)

    @property
    def available_quantity(self):
        return self.total_quantity - self.issued_count

    @property
    def active_reservation_count(self):
        """How many users are currently queued or holding for this book."""
        from models.reservation import Reservation
        return Reservation.query.filter(
            Reservation.book_id == self.id,
            Reservation.status.in_(['queued', 'ready']),
        ).count()

    def to_dict(self):
        return {
            'id':                       self.id,
            'isbn':                     self.isbn,
            'title':                    self.title,
            'author':                   self.author,
            'genre':                    self.genre,
            'total_quantity':           self.total_quantity,
            'issued_count':             self.issued_count,
            'available_quantity':       self.available_quantity,
            'lifetime_issued':          self.lifetime_issued,
            'active_reservation_count': self.active_reservation_count,
        }

    def __repr__(self):
        return f'<Book {self.title}>'
