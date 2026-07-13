"""
GET  /api/user/cart             — view cart
POST /api/user/cart/<book_id>   — add to cart
DEL  /api/user/cart/<book_id>   — remove from cart
POST /api/user/cart/request     — submit borrow requests
GET  /api/user/my-books         — active borrows
GET  /api/user/history          — full history
GET  /api/user/reservations     — active reservations
POST /api/user/reservations/<book_id>             — reserve a book
DEL  /api/user/reservations/<reservation_id>      — cancel reservation
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt
from services.book_service import BookService
from services.reservation_service import ReservationService
from models.transaction import BorrowRecord

user_api = Blueprint('user_api', __name__)


def _guard():
    if get_jwt().get('role') != 'user':
        return jsonify({'success': False, 'error': 'Forbidden.'}), 403
    return None

def _ok(data, status=200):  return jsonify({'success': True,  **data}), status
def _err(msg, status=400):  return jsonify({'success': False, 'error': msg}), status


@user_api.route('/cart', methods=['GET'])
@jwt_required()
def view_cart():
    g = _guard();
    if g: return g
    items = BookService.cart_items(int(get_jwt_identity()))
    return _ok({'cart': [i.to_dict() for i in items], 'count': len(items)})


@user_api.route('/cart/<int:book_id>', methods=['POST'])
@jwt_required()
def add_to_cart(book_id):
    g = _guard();
    if g: return g
    item, err = BookService.add_to_cart(int(get_jwt_identity()), book_id)
    return _err(err) if err else _ok({'message': 'Added.', 'item': item.to_dict()}, 201)


@user_api.route('/cart/<int:book_id>', methods=['DELETE'])
@jwt_required()
def remove_from_cart(book_id):
    g = _guard();
    if g: return g
    BookService.remove_from_cart(int(get_jwt_identity()), book_id)
    return _ok({'message': 'Removed.'})


@user_api.route('/cart/request', methods=['POST'])
@jwt_required()
def request_books():
    g = _guard();
    if g: return g
    records, err = BookService.request_books(int(get_jwt_identity()))
    return _err(err) if err else _ok({
        'message': f'{len(records)} request(s) submitted.',
        'records': [r.to_dict() for r in records]
    }, 201)


@user_api.route('/my-books', methods=['GET'])
@jwt_required()
def my_books():
    g = _guard();
    if g: return g
    uid = int(get_jwt_identity())
    pending = BorrowRecord.query.filter(
        BorrowRecord.user_id == uid,
        BorrowRecord.status.in_(['pending', 'borrowed']),
    ).order_by(BorrowRecord.request_date.desc()).all()
    issued = BookService.active_borrows(user_id=uid)
    return _ok({'requests': [r.to_dict() for r in pending],
                'issued':   [r.to_dict() for r in issued]})


@user_api.route('/history', methods=['GET'])
@jwt_required()
def history():
    g = _guard();
    if g: return g
    uid     = int(get_jwt_identity())
    records = BorrowRecord.query.filter_by(user_id=uid)\
                .order_by(BorrowRecord.request_date.desc()).all()
    return _ok({'history': [r.to_dict() for r in records]})


# ── Reservations ──────────────────────────────────────────────────────

@user_api.route('/reservations', methods=['GET'])
@jwt_required()
def get_reservations():
    g = _guard();
    if g: return g
    uid    = int(get_jwt_identity())
    active = ReservationService.user_reservations(uid)
    hist   = ReservationService.user_history(uid)
    return _ok({
        'active':  [r.to_dict() for r in active],
        'history': [r.to_dict() for r in hist],
    })


@user_api.route('/reservations/<int:book_id>', methods=['POST'])
@jwt_required()
def reserve(book_id):
    g = _guard();
    if g: return g
    uid       = int(get_jwt_identity())
    res, err  = ReservationService.reserve(uid, book_id)
    if err:
        return _err(err)
    message = ('Ready for pickup — a copy was already available.'
               if res.status == 'ready'
               else f'Reserved. You are #{res.queue_position} in the queue.')
    return _ok({
        'message':        message,
        'reservation':    res.to_dict(),
    }, 201)


@user_api.route('/reservations/<int:reservation_id>', methods=['DELETE'])
@jwt_required()
def cancel_reservation(reservation_id):
    g = _guard();
    if g: return g
    uid      = int(get_jwt_identity())
    res, err = ReservationService.cancel(reservation_id, uid)
    if err:
        return _err(err, 404)
    return _ok({'message': 'Reservation cancelled.', 'reservation': res.to_dict()})


# ── Notifications ─────────────────────────────────────────────────────

@user_api.route('/notifications', methods=['GET'])
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


@user_api.route('/notifications/<int:notif_id>/read', methods=['POST'])
@jwt_required()
def mark_read(notif_id):
    g = _guard();
    if g: return g
    from services.notification_service import NotificationService
    uid = int(get_jwt_identity())
    ok  = NotificationService.mark_read(notif_id, uid)
    return _ok({'message': 'Marked as read.'}) if ok else _err('Not found.', 404)


@user_api.route('/notifications/read-all', methods=['POST'])
@jwt_required()
def mark_all_read():
    g = _guard();
    if g: return g
    from services.notification_service import NotificationService
    uid   = int(get_jwt_identity())
    count = NotificationService.mark_all_read(uid)
    return _ok({'message': f'{count} notification(s) marked as read.'})
