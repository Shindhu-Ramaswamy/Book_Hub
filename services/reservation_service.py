"""
ReservationService
==================
All reservation business logic lives here. Routes and API endpoints
call these methods — no SQLAlchemy queries outside this file.

Queue rules
-----------
  - A user can only reserve a book once at a time.
  - A user cannot reserve a book they currently have pending/borrowed.
  - A user can reserve even when a book is available (edge case: rare
    race condition; we still add them to queue at position 1 and the
    scheduler promotes them immediately on next run).
  - queue_position is 1-based. Gaps are closed after every cancellation
    or expiry by _resequence_queue().
  - On promotion the top queued reservation moves to 'ready' and
    expires_at is set to now + RESERVATION_HOLD_HOURS.
  - If a 'ready' reservation expires, it moves to 'expired', the next
    'queued' entry is promoted, and the book copy is re-released
    (available_quantity goes back up — handled via issued_count not
    being decremented here since we never increment it for reservations).

Integration with BorrowRecord
------------------------------
  When a librarian approves a 'ready' reservation (via
  approve_reservation), we call BookService.approve_request() on the
  linked BorrowRecord — that BorrowRecord was pre-created in status
  'reserved_ready' so the librarian sees it in the Requests panel.
"""

import logging
from datetime import datetime, timedelta, timezone

from extensions import db
from models.reservation import Reservation
from models.transaction import BorrowRecord
from config import Config

log = logging.getLogger('lms.reservation')


# ── helpers ──────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _resequence_queue(book_id: int):
    """
    Close gaps in queue positions after a cancellation or expiry.
    Only 'queued' entries are resequenced; 'ready' always stays at 1.
    """
    queued = (
        Reservation.query
        .filter_by(book_id=book_id, status='queued')
        .order_by(Reservation.created_at.asc())
        .all()
    )
    # 'ready' entry (if any) always occupies position 1
    has_ready = Reservation.query.filter_by(
        book_id=book_id, status='ready').first() is not None
    start = 2 if has_ready else 1
    for i, r in enumerate(queued, start=start):
        r.queue_position = i


def _promote_next(book_id: int):
    """
    Promote the first 'queued' reservation to 'ready' and create a
    BorrowRecord in status 'reserved_ready' so the librarian can
    approve it. Returns the promoted Reservation or None.
    """
    next_res = (
        Reservation.query
        .filter_by(book_id=book_id, status='queued')
        .order_by(Reservation.queue_position.asc())
        .first()
    )
    if not next_res:
        return None

    now = _now()
    next_res.status     = 'ready'
    next_res.ready_at   = now
    next_res.expires_at = now + timedelta(hours=Config.RESERVATION_HOLD_HOURS)
    next_res.queue_position = 1

    # Pre-create a BorrowRecord so librarian can see + approve it
    br = BorrowRecord(
        user_id      = next_res.user_id,
        book_id      = next_res.book_id,
        status       = 'reserved_ready',
        request_date = now.date(),
    )
    db.session.add(br)
    db.session.flush()          # get br.id before commit
    next_res.borrow_record_id = br.id

    # Shift remaining queued entries up
    _resequence_queue(book_id)
    log.info(
        'Promoted reservation #%d (user=%d book=%d) → ready, expires %s',
        next_res.id, next_res.user_id, next_res.book_id,
        next_res.expires_at.isoformat()
    )

    # Notify the user their turn has come
    from services.notification_service import NotificationService
    NotificationService.reservation_ready(next_res)

    return next_res


# ── Public API ────────────────────────────────────────────────────────

class ReservationService:

    # ── Place a reservation ───────────────────────────────────────────
    @staticmethod
    def reserve(user_id: int, book_id: int):
        """
        Add the user to the reservation queue for book_id.
        Returns (Reservation, None) or (None, error_string).

        Edge case: if the book already has a free copy sitting on the
        shelf (nobody happened to be queued for it yet), there is no
        return event coming to trigger a promotion — so this reservation
        would otherwise sit in 'queued' forever. We promote it to
        'ready' immediately in that case instead of waiting on a
        return that may never come.
        """
        from models.book import Book
        from models.user import User

        user = User.query.get(user_id)
        if not user:
            return None, 'User not found.'
        if not user.membership_active:
            return None, 'Please clear your membership payment before reserving books.'

        book = Book.query.filter_by(id=book_id, is_deleted=False).first()
        if not book:
            return None, 'Book not found.'

        # Already has an active borrow/request?
        active_borrow = BorrowRecord.query.filter(
            BorrowRecord.user_id == user_id,
            BorrowRecord.book_id == book_id,
            BorrowRecord.status.in_(['pending', 'borrowed', 'reserved_ready', 'in_delivery']),
        ).first()
        if active_borrow:
            return None, f'You already have "{book.title}" pending or borrowed.'

        # Already in queue?
        existing = Reservation.query.filter(
            Reservation.user_id == user_id,
            Reservation.book_id == book_id,
            Reservation.status.in_(['queued', 'ready']),
        ).first()
        if existing:
            return None, f'You are already in the queue for "{book.title}" (position {existing.queue_position}).'

        # Count current active reservations to determine position
        current_count = Reservation.query.filter(
            Reservation.book_id == book_id,
            Reservation.status.in_(['queued', 'ready']),
        ).count()
        position = current_count + 1

        res = Reservation(
            user_id        = user_id,
            book_id        = book_id,
            queue_position = position,
            status         = 'queued',
            created_at     = _now(),
        )
        db.session.add(res)
        db.session.commit()

        log.info(
            'Reservation #%d created user=%d book="%s" pos=%d',
            res.id, user_id, book.title, position
        )

        # Nobody ahead of us and a copy is already free — skip the
        # queue and promote straight to 'ready' instead of leaving this
        # stranded until an unrelated future return happens to fire.
        if position == 1 and book.available_quantity > 0:
            promoted = _promote_next(book_id)
            db.session.commit()
            if promoted:
                return promoted, None

        from services.notification_service import NotificationService
        NotificationService.reservation_queued(res)

        return res, None

    # ── Cancel a reservation ──────────────────────────────────────────
    @staticmethod
    def cancel(reservation_id: int, user_id: int):
        """
        Cancel a reservation. Only the owning user can cancel.
        Returns (Reservation, None) or (None, error_string).
        """
        res = Reservation.query.filter_by(id=reservation_id, user_id=user_id).first()
        if not res:
            return None, 'Reservation not found.'
        if res.status not in ('queued', 'ready'):
            return None, f'Cannot cancel a reservation with status "{res.status}".'

        book_id    = res.book_id
        was_ready  = res.status == 'ready'
        borrow_id  = res.borrow_record_id

        res.status       = 'cancelled'
        res.cancelled_at = _now()

        # If the user was 'ready', cancel the pre-created BorrowRecord too
        if was_ready and borrow_id:
            br = BorrowRecord.query.get(borrow_id)
            if br and br.status == 'reserved_ready':
                br.status = 'cancelled'

        _resequence_queue(book_id)

        # If the cancelled reservation was 'ready', promote the next person
        if was_ready:
            _promote_next(book_id)

        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.reservation_cancelled(res)

        log.info('Reservation #%d cancelled by user=%d', reservation_id, user_id)
        return res, None

    # ── Get user's reservations ───────────────────────────────────────
    @staticmethod
    def user_reservations(user_id: int):
        """All active reservations for a user, ordered by created_at."""
        return (
            Reservation.query
            .filter_by(user_id=user_id)
            .filter(Reservation.status.in_(['queued', 'ready']))
            .order_by(Reservation.created_at.asc())
            .all()
        )

    # ── Get queue for a book ──────────────────────────────────────────
    @staticmethod
    def book_queue(book_id: int):
        """
        Full reservation queue for a book — for librarian view.
        Returns active (queued + ready) reservations ordered by position.
        """
        return (
            Reservation.query
            .filter_by(book_id=book_id)
            .filter(Reservation.status.in_(['queued', 'ready']))
            .order_by(Reservation.queue_position.asc())
            .all()
        )

    # ── All pending ready reservations — for librarian ────────────────
    @staticmethod
    def all_ready():
        """
        All 'ready' reservations across all books — what the librarian
        needs to action (approve or let expire).
        """
        return (
            Reservation.query
            .filter_by(status='ready')
            .order_by(Reservation.expires_at.asc())
            .all()
        )

    # ── Fulfil a reservation (librarian approves) ─────────────────────
    @staticmethod
    def fulfil(reservation_id: int, librarian_id: int):
        """
        Convert a 'ready' reservation into a real borrow.
        Calls BookService.approve_request on the linked BorrowRecord.
        Returns (BorrowRecord, None) or (None, error_string).
        """
        from services.book_service import BookService

        res = Reservation.query.get(reservation_id)
        if not res:
            return None, 'Reservation not found.'
        if res.status != 'ready':
            return None, f'Reservation is not ready (status: {res.status}).'
        if not res.borrow_record_id:
            return None, 'No linked borrow record found.'

        # approve_request handles: available_quantity check, issue_date, due_date, issued_by
        record, err = BookService.approve_request(res.borrow_record_id, librarian_id)
        if err:
            return None, err

        res.status       = 'fulfilled'
        res.fulfilled_at = _now()
        db.session.commit()

        from services.notification_service import NotificationService
        NotificationService.reservation_fulfilled(res, record)

        return record, None

    # ── Promote on return (called by BookService.return_book) ─────────
    @staticmethod
    def on_book_returned(book_id: int):
        """
        Called every time a copy of book_id is returned.
        If there is a queued reservation, promote it to 'ready'.
        Safe to call even when queue is empty.
        """
        promoted = _promote_next(book_id)
        if promoted:
            db.session.commit()
        return promoted

    # ── Expire stale holds (called by scheduler) ──────────────────────
    @staticmethod
    def expire_stale_holds():
        """
        Expire 'ready' reservations whose expires_at has passed.
        Cancel the linked BorrowRecord and promote the next in queue.
        Returns count of expired reservations.
        """
        now     = _now()
        expired = (
            Reservation.query
            .filter(
                Reservation.status == 'ready',
                Reservation.expires_at <= now,
            )
            .all()
        )
        count = 0
        for res in expired:
            # Cancel the pre-created BorrowRecord
            if res.borrow_record_id:
                br = BorrowRecord.query.get(res.borrow_record_id)
                if br and br.status == 'reserved_ready':
                    br.status = 'cancelled'

            res.status = 'expired'
            _resequence_queue(res.book_id)
            _promote_next(res.book_id)

            from services.notification_service import NotificationService
            NotificationService.reservation_expired(res)

            count += 1
            log.info(
                'Reservation #%d expired (user=%d book=%d)',
                res.id, res.user_id, res.book_id
            )

        if count:
            db.session.commit()
            log.info('[expire_stale_holds] expired %d reservation(s)', count)
        return count

    # ── History (for user profile / admin) ───────────────────────────
    @staticmethod
    def user_history(user_id: int):
        return (
            Reservation.query
            .filter_by(user_id=user_id)
            .order_by(Reservation.created_at.desc())
            .all()
        )
