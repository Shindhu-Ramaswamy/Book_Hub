"""
BookService — complete borrow lifecycle using BorrowRecord only.
No IssuedBook model. Every stage (pending→borrowed→returned) lives on BorrowRecord.
"""
from extensions import db
from models.book        import Book
from models.cart        import Cart
from models.transaction import BorrowRecord, _calc_fine
from models.overdue     import OverdueRecord
from models.user        import User
from config import Config
from datetime import date, timedelta


def _is_valid_isbn(isbn: str) -> bool:
    """
    True if isbn is a well-formed ISBN-10 or ISBN-13 (correct length,
    digits in range, and a valid check digit) — catches typos like
    transposed or mistyped digits, not just "is it the right length".
    """
    clean = isbn.replace('-', '').replace(' ', '').upper()

    if len(clean) == 10:
        if not clean[:9].isdigit() or not (clean[9].isdigit() or clean[9] == 'X'):
            return False
        total = sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(clean))
        return total % 11 == 0

    if len(clean) == 13:
        if not clean.isdigit():
            return False
        total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(clean))
        return total % 10 == 0

    return False


class BookService:

    # ── Catalog ──────────────────────────────────────────────────────
    @staticmethod
    def _catalog_query(genres=None, query=None, sort=None, availability=None):
        q = Book.query.filter_by(is_deleted=False)
        if genres:
            q = q.filter(Book.genre.in_(genres))
        if query:
            q = q.filter(
                db.or_(
                    Book.title.ilike(f'%{query}%'),
                    Book.author.ilike(f'%{query}%'),
                    Book.isbn.ilike(f'%{query}%'),
                )
            )
        if availability == 'available':
            q = q.filter((Book.total_quantity - Book.issued_count) > 0)
        elif availability == 'unavailable':
            q = q.filter((Book.total_quantity - Book.issued_count) <= 0)
        return q.order_by(Book.title.desc() if sort == 'title_desc' else Book.title.asc())

    @staticmethod
    def get_all(genres=None, query=None, sort=None, availability=None):
        return BookService._catalog_query(genres, query, sort, availability).all()

    @staticmethod
    def get_all_paginated(genres=None, query=None, sort=None, availability=None,
                          page=1, per_page=25):
        return BookService._catalog_query(genres, query, sort, availability) \
            .paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def get_or_404(book_id):
        return Book.query.filter_by(id=book_id, is_deleted=False).first_or_404()

    @staticmethod
    def create(isbn, title, author, genre, total_quantity):
        if not _is_valid_isbn(isbn):
            return None, 'Invalid ISBN — enter a valid ISBN-10 or ISBN-13.'
        if Book.query.filter_by(isbn=isbn, is_deleted=False).first():
            return None, 'A book with this ISBN already exists.'
        book = Book(isbn=isbn, title=title, author=author,
                    genre=genre, total_quantity=int(total_quantity))
        db.session.add(book)
        db.session.commit()
        return book, None

    @staticmethod
    def update(book, title, author, genre, total_quantity):
        book.title          = title
        book.author         = author
        book.genre          = genre
        book.total_quantity = int(total_quantity)
        db.session.commit()
        return book

    @staticmethod
    def delete(book):
        """Soft delete — marks is_deleted=True, never removes the row."""
        if book.issued_count > 0:
            return 'Cannot delete a book that is currently issued.'
        if BorrowRecord.query.filter(
            BorrowRecord.book_id == book.id,
            BorrowRecord.status.in_(['pending', 'reserved_ready']),
        ).first():
            return 'Cannot delete a book with a pending borrow request.'
        if book.active_reservation_count > 0:
            return 'Cannot delete a book with an active reservation queue.'
        book.is_deleted = True
        db.session.commit()
        return None

    # ── Cart ─────────────────────────────────────────────────────────
    @staticmethod
    def cart_items(user_id):
        return Cart.query.filter_by(user_id=user_id).all()

    @staticmethod
    def add_to_cart(user_id, book_id):
        user = User.query.get_or_404(user_id)
        if not user.membership_active:
            return None, 'Please clear your membership payment before borrowing books.'
        book = Book.query.get_or_404(book_id)
        if Cart.query.filter_by(user_id=user_id, book_id=book_id).first():
            return None, f'"{book.title}" is already in your cart.'
        active = BorrowRecord.query.filter(
            BorrowRecord.user_id == user_id,
            BorrowRecord.book_id == book_id,
            BorrowRecord.status.in_(['pending', 'borrowed', 'reserved_ready', 'in_delivery']),
        ).first()
        if active:
            return None, f'You already have "{book.title}" requested or borrowed.'
        if Cart.query.filter_by(user_id=user_id).count() >= user.max_books:
            return None, f'Cart is full (max {user.max_books} books).'
        item = Cart(user_id=user_id, book_id=book_id)
        db.session.add(item)
        db.session.commit()
        return item, None

    @staticmethod
    def remove_from_cart(user_id, book_id):
        item = Cart.query.filter_by(user_id=user_id, book_id=book_id).first()
        if item:
            db.session.delete(item)
            db.session.commit()
            return True
        return False

    # ── Borrow request ────────────────────────────────────────────────
    @staticmethod
    def request_books(user_id):
        user = User.query.get_or_404(user_id)
        if not user.membership_active:
            return [], 'Please clear your membership payment before borrowing books.'
        items = Cart.query.filter_by(user_id=user_id).all()
        if not items:
            return [], 'Cart is empty.'
        active_count = BorrowRecord.query.filter(
            BorrowRecord.user_id == user_id,
            BorrowRecord.status.in_(['pending', 'borrowed', 'reserved_ready', 'in_delivery']),
        ).count()
        if active_count + len(items) > user.max_books:
            return [], (f'You can only have {user.max_books} books at a time. '
                        f'Currently active: {active_count}.')
        records = []
        for item in items:
            r = BorrowRecord(
                user_id=user_id,
                book_id=item.book_id,
                status='pending',
                request_date=date.today(),
            )
            db.session.add(r)
            db.session.delete(item)
            records.append(r)
        db.session.commit()

        # Notify user for each requested book
        from services.notification_service import NotificationService
        for r in records:
            NotificationService.borrow_requested(r)

        return records, None

    # ── Approve ───────────────────────────────────────────────────────
    @staticmethod
    def approve_request(record_id, librarian_id):
        record = BorrowRecord.query.get_or_404(record_id)
        if record.status not in ('pending', 'reserved_ready'):
            return None, 'Request is not pending or reserved.'
        if record.delivery_order_id:
            return None, 'This is a home-delivery request — manage it from Deliveries, not Requests.'
        if record.book.available_quantity <= 0:
            return None, 'No copies available.'
        if not record.user.membership_active:
            return None, f'{record.user.name}\'s membership payment is due — cannot issue until paid.'

        today              = date.today()
        record.status      = 'borrowed'
        record.issue_date  = today
        record.borrow_date = today
        record.due_date    = today + timedelta(days=record.user.borrow_days)
        record.issued_by   = librarian_id

        record.book.issued_count    += 1
        record.book.lifetime_issued += 1
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.borrow_approved(record)

        return record, None

    # ── Reject ────────────────────────────────────────────────────────
    @staticmethod
    def reject_request(record_id):
        record = BorrowRecord.query.get_or_404(record_id)
        if record.delivery_order_id:
            return None, 'This is a home-delivery request — manage it from Deliveries, not Requests.'
        record.status = 'rejected'
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.borrow_rejected(record)

        return record, None

    # ── Return request (advance heads-up, in-person return) ────────────
    @staticmethod
    def request_return(record_id, user_id):
        """
        User flags "I'm bringing this book back to the library" from My
        Books. Purely informational — notifies the librarian and shows a
        badge; the actual return still goes through the normal
        return_inspect/return_book flow when the book is physically
        handed over. Idempotent: re-flagging an already-flagged record
        is a no-op (no duplicate notification spam).
        """
        record = BorrowRecord.query.filter_by(id=record_id, user_id=user_id).first()
        if not record:
            return None, 'Borrowed book not found.'
        if record.status != 'borrowed':
            return None, 'This book is not currently borrowed.'
        if record.pickup_order_id and not record.pickup_order.is_terminal:
            return None, 'A return pickup is already in progress for this book.'
        if record.return_requested_at:
            return record, None

        record.return_requested_at = date.today()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.return_requested(record)

        return record, None

    # ── Return ────────────────────────────────────────────────────────
    @staticmethod
    def return_book(record_id, librarian_id=None, condition='good',
                     notes=None, charge_amount=None):
        """
        Return-inspection flow.

        `condition` is the physical state the librarian found the book
        in: 'good' | 'damaged' | 'lost'. It is independent of lateness —
        a book can be both overdue AND damaged, so the overdue fine
        (date-driven, unchanged from before) and the condition charge
        (damage/lost, librarian-driven) are calculated separately and
        both attach to the same return.

        A 'lost' condition means the book never actually came back, so
        the record's terminal status is 'lost', not 'returned' — and the
        loss is logged to LostBook rather than DamagedBook.

        Returns (record, overdue_charge, condition_charge) — either
        charge may be None.
        """
        record = BorrowRecord.query.get_or_404(record_id)
        today               = date.today()
        record.return_date  = today
        record.status       = 'lost' if condition == 'lost' else 'returned'
        record.return_requested_at = None
        record.book.issued_count = max(0, record.book.issued_count - 1)

        # ── Overdue fine (date-driven, same as before) ─────────────
        overdue_charge = None
        if record.overdue_days > 0:
            fine_amt = _calc_fine(record.overdue_days)
            record.fine_amount = fine_amt
            if fine_amt > 0:
                overdue_charge = OverdueRecord(
                    borrow_id   = record.id,
                    user_id     = record.user_id,
                    amount      = fine_amt,
                    fine_status = 'unpaid',
                    charge_type = 'overdue',
                    issued_date = today,
                )
                db.session.add(overdue_charge)

        # ── Condition charge (damaged / lost) ───────────────────────
        condition_charge = None
        if condition in ('damaged', 'lost'):
            default_amt = (Config.DAMAGE_CHARGE if condition == 'damaged'
                           else Config.LOST_BOOK_CHARGE)
            try:
                amt = float(charge_amount) if charge_amount not in (None, '') else default_amt
            except (TypeError, ValueError):
                amt = default_amt

            # Copy leaves circulation either way — damaged or lost.
            record.book.total_quantity = max(0, record.book.total_quantity - 1)

            if condition == 'lost':
                from models.lost_book import LostBook
                db.session.add(LostBook(
                    book_id       = record.book_id,
                    borrow_id     = record.id,
                    reported_by   = librarian_id,
                    quantity      = 1,
                    notes         = notes or 'Reported lost at return.',
                    reported_date = today,
                    charge_amount = amt,
                ))
            else:
                from models.damaged import DamagedBook
                db.session.add(DamagedBook(
                    book_id       = record.book_id,
                    borrow_id     = record.id,
                    reported_by   = librarian_id,
                    quantity      = 1,
                    notes         = notes or 'Reported damaged at return.',
                    reported_date = today,
                ))

            condition_charge = OverdueRecord(
                borrow_id   = record.id,
                user_id     = record.user_id,
                amount      = amt,
                fine_status = 'unpaid',
                charge_type = condition,
                notes       = notes,
                issued_date = today,
            )
            db.session.add(condition_charge)

        db.session.commit()

        from services.notification_service import NotificationService
        if condition_charge:
            NotificationService.condition_charge_created(record, condition_charge)
        if overdue_charge:
            NotificationService.fine_created(record, overdue_charge)
        if not overdue_charge and not condition_charge:
            NotificationService.book_returned(record)

        # Trigger reservation queue promotion only when a copy actually
        # became free. A 'good' return frees a shelf copy (issued_count
        # drops, total_quantity doesn't) — but for damaged/lost returns,
        # total_quantity was also decremented above, so available_quantity
        # is unchanged. Promoting the queue in that case would falsely
        # tell the next person their book is ready when no copy exists,
        # a mistake _promote_next() has no way to catch on its own.
        if condition == 'good':
            from services.reservation_service import ReservationService
            ReservationService.on_book_returned(record.book_id)

        # If this book came back via a return-pickup request, check whether
        # every book in that pickup order is now inspected — if so, the
        # pickup order itself is done (see PickupService docstring).
        if record.pickup_order_id:
            from services.pickup_service import PickupService
            PickupService._maybe_close_order(record.pickup_order_id)

        return record, overdue_charge, condition_charge

    # ── Damaged ───────────────────────────────────────────────────────
    @staticmethod
    def log_damaged(book_id, librarian_id, quantity, notes):
        from models.damaged import DamagedBook
        book     = Book.query.get_or_404(book_id)
        quantity = int(quantity)
        # This logs shelf copies found damaged, not ones out on loan —
        # currently-issued copies go through the return_book() condition
        # path instead. Validate against available_quantity (not
        # total_quantity), otherwise a damage log could exceed the number
        # of copies actually on the shelf and drive available_quantity
        # negative.
        if quantity > book.available_quantity:
            return None, 'Damaged quantity exceeds available (non-issued) quantity.'
        book.total_quantity -= quantity
        log = DamagedBook(
            book_id=book_id, reported_by=librarian_id,
            quantity=quantity, notes=notes, reported_date=date.today(),
        )
        db.session.add(log)
        db.session.commit()
        return log, None

    # ── Fine ──────────────────────────────────────────────────────────
    @staticmethod
    def mark_fine_paid(overdue_id, librarian_id, payment_method):
        """Cash/counter payment — a librarian manually confirms collection."""
        record = OverdueRecord.query.get_or_404(overdue_id)
        if record.fine_status == 'paid':
            return record
        record.fine_status    = 'paid'
        record.paid_date      = date.today()
        record.collected_by   = librarian_id
        record.payment_method = payment_method
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.fine_paid(record)

        return record

    @staticmethod
    def record_online_payment(overdue_id, gateway, order_id, payment_id):
        """
        Mark a fine paid via an online gateway (Razorpay etc.) after the
        caller has ALREADY verified the payment signature — this method
        does not verify anything itself, it only records the outcome.
        No librarian is involved in this path at all (collected_by stays
        NULL) — it's fully automatic.

        Idempotent: if the record is already 'paid' (e.g. both the
        client-side verify call and a webhook fired for the same
        payment), this is a no-op on the second call instead of
        re-sending notifications twice.
        """
        record = OverdueRecord.query.get_or_404(overdue_id)
        if record.fine_status == 'paid':
            return record

        record.fine_status         = 'paid'
        record.paid_date           = date.today()
        record.collected_by        = None          # no librarian involved
        record.payment_method      = 'online'
        record.payment_gateway     = gateway
        record.gateway_order_id    = order_id
        record.gateway_payment_id  = payment_id
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.fine_paid(record)                 # tells the user their fine is clear
        NotificationService.online_payment_received(record)   # tells librarians no cash is owed on this one

        return record

    # ── Active issued records (replaces IssuedBook queries) ───────────
    @staticmethod
    def active_borrows(user_id=None):
        q = BorrowRecord.query.filter_by(status='borrowed')
        if user_id:
            q = q.filter_by(user_id=user_id)
        return q.order_by(BorrowRecord.due_date.asc()).all()

    @staticmethod
    def all_transactions(filter_by=None):
        """filter_by: 'borrowed' | 'returned' | 'lost' | 'overdue' | None (all)"""
        if filter_by == 'borrowed':
            records = BorrowRecord.query.filter_by(status='borrowed').all()
        elif filter_by == 'returned':
            records = BorrowRecord.query.filter_by(status='returned').all()
        elif filter_by == 'lost':
            records = BorrowRecord.query.filter_by(status='lost').all()
        elif filter_by == 'overdue':
            records = [r for r in BorrowRecord.query.filter_by(status='borrowed').all()
                       if r.is_overdue]
        else:
            records = BorrowRecord.query.order_by(
                BorrowRecord.request_date.desc()).all()
        return records
