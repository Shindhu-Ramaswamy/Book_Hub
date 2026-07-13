from extensions import db
from models.user        import User
from models.book        import Book
from models.transaction import BorrowRecord
from models.overdue     import OverdueRecord
from services.auth_service import AuthService


class AdminService:

    @staticmethod
    def dashboard_stats():
        active_borrowed = BorrowRecord.query.filter_by(status='borrowed').all()
        overdue_count   = sum(1 for r in active_borrowed if r.is_overdue)
        return {
            'total_members':    User.query.filter_by(role='user').count(),
            'total_librarians': User.query.filter_by(role='librarian').count(),
            'total_books':      Book.query.count(),
            'active_issues':    len(active_borrowed),
            'unpaid_fines':     OverdueRecord.query.filter_by(fine_status='unpaid').count(),
            'overdue_count':    overdue_count,
        }

    @staticmethod
    def all_users():
        return User.query.filter_by(role='user').order_by(User.id).all()

    @staticmethod
    def all_librarians():
        return User.query.filter_by(role='librarian').order_by(User.id).all()

    @staticmethod
    def all_books():
        return Book.query.filter_by(is_deleted=False).order_by(Book.title).all()

    @staticmethod
    def create_librarian(name, email, phone, password, library_code):
        return AuthService.register_user(
            name=name, email=email, phone=phone,
            password=password, role='librarian', library_code=library_code,
        )

    @staticmethod
    def toggle_librarian(librarian_id):
        lib = User.query.filter_by(id=librarian_id, role='librarian').first_or_404()
        lib.is_active = not lib.is_active
        db.session.commit()
        return lib
