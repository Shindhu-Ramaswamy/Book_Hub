"""
JWT-protected librarian REST API. No IssuedBook — all via BorrowRecord.
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity
from services.book_service import BookService
from models.transaction import BorrowRecord
from models.overdue     import OverdueRecord, PAYMENT_METHODS
from models.user        import User

librarian_api = Blueprint('librarian_api', __name__)


def _guard():
    if get_jwt().get('role') not in ('librarian', 'admin'):
        return jsonify({'success': False, 'error': 'Forbidden.'}), 403
    return None

def _ok(data, status=200):  return jsonify({'success': True,  **data}), status
def _err(msg, status=400):  return jsonify({'success': False, 'error': msg}), status


@librarian_api.route('/stats', methods=['GET'])
@jwt_required()
def stats():
    g = _guard();
    if g: return g
    from models.book    import Book
    from models.damaged import DamagedBook
    from extensions     import db
    active = BorrowRecord.query.filter_by(status='borrowed').all()
    return _ok({
        'total_books':      Book.query.count(),
        'total_members':    User.query.filter_by(role='user').count(),
        'active_issues':    len(active),
        'overdue_count':    sum(1 for r in active if r.is_overdue),
        'pending_requests': BorrowRecord.query.filter_by(status='pending').count(),
        'unpaid_fines':     OverdueRecord.query.filter_by(fine_status='unpaid').count(),
        'damaged_count':    db.session.query(db.func.sum(DamagedBook.quantity)).scalar() or 0,
    })


@librarian_api.route('/requests', methods=['GET'])
@jwt_required()
def pending_requests():
    g = _guard();
    if g: return g
    pending = BorrowRecord.query.filter_by(status='pending')\
                .order_by(BorrowRecord.request_date.asc()).all()
    return _ok({'requests': [r.to_dict() for r in pending]})


@librarian_api.route('/requests/<int:record_id>/approve', methods=['POST'])
@jwt_required()
def approve(record_id):
    g = _guard();
    if g: return g
    record, err = BookService.approve_request(record_id, int(get_jwt_identity()))
    return _err(err, 409) if err else _ok({'message': 'Approved.', 'record': record.to_dict()}, 201)


@librarian_api.route('/requests/<int:record_id>/reject', methods=['POST'])
@jwt_required()
def reject(record_id):
    g = _guard();
    if g: return g
    record, err = BookService.reject_request(record_id)
    return _err(err, 409) if err else _ok({'message': 'Rejected.', 'record': record.to_dict()})


@librarian_api.route('/issued', methods=['GET'])
@jwt_required()
def issued():
    g = _guard();
    if g: return g
    f       = request.args.get('filter')
    records = BookService.all_transactions(filter_by=f)
    return _ok({'records': [r.to_dict() for r in records]})


@librarian_api.route('/issued/<int:record_id>/return', methods=['POST'])
@jwt_required()
def return_book(record_id):
    g = _guard();
    if g: return g
    body          = request.get_json(silent=True) or {}
    condition     = body.get('condition', 'good')
    notes         = body.get('notes')
    charge_amount = body.get('charge_amount')

    record, overdue_charge, condition_charge = BookService.return_book(
        record_id, librarian_id=int(get_jwt_identity()), condition=condition,
        notes=notes, charge_amount=charge_amount,
    )
    result = {'message': 'Returned.', 'record': record.to_dict()}
    if overdue_charge:
        result['overdue_charge']  = overdue_charge.to_dict()
        result['fine_created']    = True
    if condition_charge:
        result['condition_charge'] = condition_charge.to_dict()
    return _ok(result)


@librarian_api.route('/overdue', methods=['GET'])
@jwt_required()
def overdue():
    g = _guard();
    if g: return g
    records = OverdueRecord.query\
        .order_by(OverdueRecord.fine_status.asc(),
                  OverdueRecord.issued_date.desc()).all()
    return _ok({'fines': [r.to_dict() for r in records]})


@librarian_api.route('/overdue/<int:overdue_id>/pay', methods=['POST'])
@jwt_required()
def mark_paid(overdue_id):
    g = _guard();
    if g: return g
    body   = request.get_json(silent=True) or {}
    method = body.get('payment_method', 'cash')
    if method not in PAYMENT_METHODS:
        return _err(f'payment_method must be one of: {", ".join(PAYMENT_METHODS)}')
    record = BookService.mark_fine_paid(overdue_id, int(get_jwt_identity()), method)
    return _ok({'message': 'Fine marked paid.', 'record': record.to_dict()})


@librarian_api.route('/users', methods=['GET'])
@jwt_required()
def users():
    g = _guard();
    if g: return g
    return _ok({'users': [u.to_dict() for u in
                User.query.filter_by(role='user').order_by(User.id).all()]})


@librarian_api.route('/history', methods=['GET'])
@jwt_required()
def history():
    g = _guard();
    if g: return g
    records = BorrowRecord.query.order_by(BorrowRecord.request_date.desc()).all()
    return _ok({'history': [r.to_dict() for r in records]})


# ── Reservations ──────────────────────────────────────────────────────

@librarian_api.route('/reservations', methods=['GET'])
@jwt_required()
def reservations():
    g = _guard();
    if g: return g
    from models.reservation import Reservation
    from services.reservation_service import ReservationService
    ready  = ReservationService.all_ready()
    queued = Reservation.query.filter_by(status='queued')\
               .order_by(Reservation.queue_position.asc()).all()
    return _ok({
        'ready':  [r.to_dict() for r in ready],
        'queued': [r.to_dict() for r in queued],
    })


@librarian_api.route('/reservations/<int:reservation_id>/fulfil', methods=['POST'])
@jwt_required()
def fulfil_reservation(reservation_id):
    g = _guard();
    if g: return g
    from services.reservation_service import ReservationService
    lib_id        = int(get_jwt_identity())
    record, err   = ReservationService.fulfil(reservation_id, lib_id)
    if err:
        return _err(err, 409)
    return _ok({
        'message': 'Reservation fulfilled.',
        'record':  record.to_dict(),
    })


# ── Notifications ──────────────────────────────────────────────────────

@librarian_api.route('/notifications', methods=['GET'])
@jwt_required()
def get_notifications():
    g = _guard();
    if g: return g
    from services.notification_service import NotificationService
    uid    = int(get_jwt_identity())
    limit  = int(request.args.get('limit', 50))
    notifs = NotificationService.get_for_user(uid, limit=limit)
    unread = NotificationService.unread_count(uid)
    return _ok({
        'notifications': [n.to_dict() for n in notifs],
        'unread_count':  unread,
    })


@librarian_api.route('/notifications/<int:notif_id>/read', methods=['POST'])
@jwt_required()
def mark_read(notif_id):
    g = _guard();
    if g: return g
    from services.notification_service import NotificationService
    uid = int(get_jwt_identity())
    ok  = NotificationService.mark_read(notif_id, uid)
    return _ok({'message': 'Marked as read.'}) if ok else _err('Not found.', 404)


@librarian_api.route('/notifications/read-all', methods=['POST'])
@jwt_required()
def mark_all_read():
    g = _guard();
    if g: return g
    from services.notification_service import NotificationService
    uid   = int(get_jwt_identity())
    count = NotificationService.mark_all_read(uid)
    return _ok({'message': f'{count} notification(s) marked as read.'})
