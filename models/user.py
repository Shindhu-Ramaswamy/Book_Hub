from extensions import db, login_manager
from flask_login import UserMixin
from datetime import date


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(100), nullable=False)
    email        = db.Column(db.String(120), unique=True, nullable=False)
    phone        = db.Column(db.String(15),  nullable=False)
    password     = db.Column(db.String(200), nullable=False)
    role         = db.Column(db.String(20),  nullable=False)   # user | librarian | admin
    library_code = db.Column(db.String(50),  nullable=True)
    is_active    = db.Column(db.Boolean, default=True)
    is_deleted   = db.Column(db.Boolean, default=False, nullable=False)  # soft delete
    joined_date  = db.Column(db.Date, default=date.today)

    # ── Membership tier (role='user' only — librarian/admin ignore this) ──
    membership_type       = db.Column(db.String(20), default='basic', nullable=False)  # basic | membership
    membership_paid_until = db.Column(db.Date, nullable=True)  # None = never paid; blocks borrowing

    # ── Saved address (role='user' only) ──────────────────────────────
    # A single default address, reused to pre-fill delivery/pickup
    # request forms — not a full multi-address book. Each DeliveryOrder/
    # ReturnPickupOrder still keeps its own address snapshot; this is
    # just what pre-fills next time so the user isn't retyping it.
    address_line1 = db.Column(db.String(200), nullable=True)
    address_line2 = db.Column(db.String(200), nullable=True)
    address_city  = db.Column(db.String(80),  nullable=True)
    address_state = db.Column(db.String(80),  nullable=True)
    address_pincode = db.Column(db.String(10), nullable=True)
    address_landmark = db.Column(db.String(120), nullable=True)

    transactions  = db.relationship('BorrowRecord', backref='user', lazy=True,
                                     foreign_keys='BorrowRecord.user_id')

    @property
    def has_saved_address(self):
        return bool(self.address_line1 and self.address_city
                    and self.address_state and self.address_pincode)

    @property
    def formatted_id(self):
        year   = str(self.joined_date.year)[2:]
        prefix = {'user': 'USR', 'librarian': 'LIB', 'admin': 'ADM'}.get(self.role, 'USR')
        return f'{prefix}-{year}-{str(self.id).zfill(3)}'

    # ── Membership tier helpers ─────────────────────────────────────
    @property
    def membership_rules(self):
        from config import Config
        return Config.MEMBERSHIP_RULES.get(self.membership_type, Config.MEMBERSHIP_RULES['basic'])

    @property
    def membership_label(self):
        return self.membership_rules['label']

    @property
    def max_books(self):
        return self.membership_rules['max_books']

    @property
    def borrow_days(self):
        return self.membership_rules['borrow_days']

    @property
    def membership_active(self):
        """
        Whether this account may borrow right now. Only role='user'
        accounts are gated — librarians/admins never pay membership fees.
        None means never paid a cent (fresh registration, fee unpaid).
        """
        if self.role != 'user':
            return True
        return bool(self.membership_paid_until) and self.membership_paid_until >= date.today()

    @property
    def membership_days_left(self):
        """Days until membership_paid_until, or None if never paid. Negative = already expired."""
        if not self.membership_paid_until:
            return None
        return (self.membership_paid_until - date.today()).days

    def to_dict(self):
        return {
            'id':          self.id,
            'formatted_id': self.formatted_id,
            'name':        self.name,
            'email':       self.email,
            'phone':       self.phone,
            'role':        self.role,
            'is_active':   self.is_active,
            'joined_date': str(self.joined_date),
            'membership_type':       self.membership_type,
            'membership_active':     self.membership_active,
            'membership_paid_until': str(self.membership_paid_until) if self.membership_paid_until else None,
            'max_books':             self.max_books,
            'borrow_days':           self.borrow_days,
        }

    def __repr__(self):
        return f'<User {self.name} [{self.role}]>'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
