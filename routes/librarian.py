from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, current_app
from flask_login import login_required, current_user
from functools import wraps
from services.book_service        import BookService
from services.auth_service        import AuthService
from services.openlibrary_service import enrich_books, search_open_library, fetch_book
from models.book        import Book, GENRES
from models.transaction import BorrowRecord
from models.overdue     import OverdueRecord, PAYMENT_METHODS
from models.damaged     import DamagedBook
from models.user        import User
from extensions         import db
from config              import Config

librarian = Blueprint('librarian', __name__)


def librarian_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role != 'librarian':
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                # See routes/user.py user_required() for why AJAX callers
                # need JSON here instead of a redirect to the landing page.
                return jsonify({'success': False,
                                 'error': 'Your session is no longer logged in as a librarian. '
                                          'Please refresh and log in again.'}), 401
            flash('Access denied.', 'danger')
            return redirect(url_for('auth.landing'))
        return f(*args, **kwargs)
    return decorated


@librarian.route('/home')
@login_required
@librarian_required
def home():
    from models.delivery import DeliveryOrder
    from models.pickup import ReturnPickupOrder
    live_books = Book.query.filter_by(is_deleted=False)
    return render_template('librarian/home.html', title='Dashboard',
        total_books       = live_books.count(),
        available_books   = live_books.filter(Book.total_quantity > Book.issued_count).count(),
        unavailable_books = live_books.filter(Book.total_quantity <= Book.issued_count).count(),
        books_issued      = db.session.query(db.func.sum(Book.issued_count))
                               .filter(Book.is_deleted == False).scalar() or 0,
        total_members    = User.query.filter_by(role='user').count(),
        active_issues    = BorrowRecord.query.filter_by(status='borrowed').count(),
        # Book (in-person pickup) requests only — home-delivery requests
        # are counted separately below and live under their own queue.
        pending_requests  = BorrowRecord.query.filter_by(status='pending')
                               .filter(BorrowRecord.delivery_order_id.is_(None)).count(),
        pending_deliveries = DeliveryOrder.query.filter_by(status='requested').count(),
        pending_pickups   = ReturnPickupOrder.query.filter_by(status='requested').count(),
        unpaid_fines     = OverdueRecord.query.filter_by(fine_status='unpaid').count(),
        damaged_count    = db.session.query(
                               db.func.sum(DamagedBook.quantity)).scalar() or 0,
    )


@librarian.route('/books')
@login_required
@librarian_required
def book_list():
    selected_genres = request.args.getlist('genre')
    search_q = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'title_asc')
    if sort not in ('title_asc', 'title_desc'):
        sort = 'title_asc'
    availability = request.args.get('availability', 'all')
    if availability not in ('all', 'available', 'unavailable'):
        availability = 'all'
    books = enrich_books(BookService.get_all(
        genres=selected_genres or None, query=search_q or None,
        sort=sort, availability=availability if availability != 'all' else None,
    ))
    return render_template('librarian/books.html', title='Book List',
                           books=books, genres=GENRES,
                           selected_genres=selected_genres,
                           search_q=search_q, sort=sort, availability=availability)


@librarian.route('/books/search-ol')
@login_required
@librarian_required
def search_ol():
    """Search Open Library and show results to import."""
    q       = request.args.get('q', '').strip()
    results = search_open_library(q, limit=12) if q else []
    return render_template('librarian/search_ol.html',
                           title='Import from Open Library',
                           results=results, query=q)


@librarian.route('/books/import-ol', methods=['POST'])
@login_required
@librarian_required
def import_ol():
    """Import a book whose metadata came from Open Library search results."""
    isbn   = request.form.get('isbn', '').strip()
    title  = request.form.get('title', '').strip()
    author = request.form.get('author', '').strip()
    genre  = request.form.get('genre', 'Fiction')
    qty    = int(request.form.get('total_quantity', 1))

    if not isbn or not title:
        flash('ISBN and title are required.', 'danger')
        return redirect(url_for('librarian.search_ol'))

    book, err = BookService.create(isbn=isbn, title=title, author=author,
                                   genre=genre, total_quantity=qty)
    if err:
        flash(err, 'danger')
        return redirect(url_for('librarian.search_ol',
                                q=request.form.get('q', '')))
    flash(f'"{book.title}" imported successfully!', 'success')
    return redirect(url_for('librarian.book_list'))


@librarian.route('/books/add', methods=['GET', 'POST'])
@login_required
@librarian_required
def add_book():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        book, err = BookService.create(
            isbn=request.form['isbn'], title=request.form['title'],
            author=request.form['author'], genre=request.form['genre'],
            total_quantity=request.form.get('total_quantity', 1),
        )
        if is_ajax:
            if not err:
                flash(f'"{book.title}" added!', 'success')
            return jsonify({'success': not bool(err), 'message': err if err else f'"{book.title}" added!'})
        if err:
            flash(err, 'danger')
            return redirect(url_for('librarian.add_book'))
        flash(f'"{book.title}" added!', 'success')
        return redirect(url_for('librarian.book_list'))
    if is_ajax:
        return render_template('librarian/_add_book_fragment.html', genres=GENRES)
    return render_template('librarian/add_book.html', title='Add Book', genres=GENRES)


@librarian.route('/books/<int:book_id>/details')
@login_required
@librarian_required
def book_details(book_id):
    book = BookService.get_or_404(book_id)
    enrich_books([book])
    ol = fetch_book(book.isbn) or {}
    return render_template('user/_book_details_fragment.html', book=book, ol=ol)


@librarian.route('/books/edit/<int:book_id>', methods=['GET', 'POST'])
@login_required
@librarian_required
def edit_book(book_id):
    book = BookService.get_or_404(book_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        BookService.update(book,
            title=request.form['title'], author=request.form['author'],
            genre=request.form['genre'],
            total_quantity=request.form.get('total_quantity', 1),
        )
        if is_ajax:
            flash('Book updated!', 'success')
            return jsonify({'success': True, 'message': 'Book updated!'})
        flash('Book updated!', 'success')
        return redirect(url_for('librarian.book_list'))
    if is_ajax:
        return render_template('librarian/_edit_book_fragment.html', book=book, genres=GENRES)
    return render_template('librarian/edit_book.html', title='Edit Book',
                           book=book, genres=GENRES)


@librarian.route('/books/delete/<int:book_id>', methods=['POST'])
@login_required
@librarian_required
def delete_book(book_id):
    book = BookService.get_or_404(book_id)
    err  = BookService.delete(book)
    flash(err if err else 'Book deleted.', 'danger' if err else 'success')
    return redirect(url_for('librarian.book_list'))


@librarian.route('/reservations')
@login_required
@librarian_required
def reservations():
    from services.reservation_service import ReservationService
    from models.reservation import Reservation
    ready   = ReservationService.all_ready()
    queued  = Reservation.query.filter_by(status='queued')\
                .order_by(Reservation.queue_position.asc()).all()
    return render_template('librarian/reservations.html',
                           title='Reservations', ready=ready, queued=queued)


@librarian.route('/reservations/fulfil/<int:reservation_id>', methods=['POST'])
@login_required
@librarian_required
def fulfil_reservation(reservation_id):
    from services.reservation_service import ReservationService
    record, err = ReservationService.fulfil(reservation_id, current_user.id)
    if err:
        flash(err, 'danger')
    else:
        flash(
            f'Reservation fulfilled — "{record.book.title}" '
            f'issued to {record.user.name}.', 'success'
        )
    return redirect(url_for('librarian.reservations'))


@librarian.route('/requests')
@login_required
@librarian_required
def requests():
    # Home-delivery requests also create 'pending' BorrowRecords (see
    # DeliveryService.request_delivery), but they're managed through
    # their own accept/pack/ship pipeline under Deliveries, not here —
    # excluded so the two request queues never overlap.
    pending = BorrowRecord.query.filter_by(status='pending')\
                .filter(BorrowRecord.delivery_order_id.is_(None))\
                .order_by(BorrowRecord.request_date.asc()).all()
    return render_template('librarian/requests.html', title='Requests', pending=pending)


@librarian.route('/requests/approve/<int:record_id>', methods=['POST'])
@login_required
@librarian_required
def approve_request(record_id):
    record, err = BookService.approve_request(record_id, current_user.id)
    if err:
        flash(err, 'danger')
    else:
        flash(f'"{record.book.title}" approved and issued to {record.user.name}.', 'success')
    return redirect(url_for('librarian.requests'))


@librarian.route('/requests/reject/<int:record_id>', methods=['POST'])
@login_required
@librarian_required
def reject_request(record_id):
    record, err = BookService.reject_request(record_id)
    if err:
        flash(err, 'danger')
    else:
        flash(f'Request for "{record.book.title}" rejected.', 'success')
    return redirect(url_for('librarian.requests'))


@librarian.route('/issued')
@login_required
@librarian_required
def issued_books():
    f       = request.args.get('filter')
    records = BookService.all_transactions(filter_by=f)
    return render_template('librarian/issued.html', title='Transactions',
                           records=records, active_filter=f or 'all')


@librarian.route('/issued/return/<int:record_id>')
@login_required
@librarian_required
def return_inspect(record_id):
    """Return-inspection screen — librarian picks the book's condition."""
    record = BorrowRecord.query.get_or_404(record_id)
    if record.status != 'borrowed':
        flash('This book has already been returned.', 'danger')
        return redirect(url_for('librarian.issued_books'))
    return render_template(
        'librarian/return_inspect.html', title='Return Inspection',
        record=record, damage_default=Config.DAMAGE_CHARGE,
        lost_default=Config.LOST_BOOK_CHARGE,
    )


@librarian.route('/issued/return/<int:record_id>', methods=['POST'])
@login_required
@librarian_required
def return_book(record_id):
    condition     = request.form.get('condition', 'good')
    notes         = request.form.get('notes') or None
    charge_amount = request.form.get('charge_amount')

    record, overdue_charge, condition_charge = BookService.return_book(
        record_id, librarian_id=current_user.id, condition=condition,
        notes=notes, charge_amount=charge_amount,
    )

    parts = [f'"{record.book.title}" returned.']
    if overdue_charge:
        parts.append(f'Fine ₹{overdue_charge.amount:.0f} for '
                     f'{record.overdue_days} overdue day(s).')
    if condition_charge:
        label = 'Lost book charge' if condition == 'lost' else 'Damage charge'
        parts.append(f'{label} ₹{condition_charge.amount:.0f}.')
    if not overdue_charge and not condition_charge:
        parts.append('No charges.')

    flash(' '.join(parts), 'warning' if (overdue_charge or condition_charge) else 'success')
    return redirect(url_for('librarian.issued_books'))


@librarian.route('/overdue')
@login_required
@librarian_required
def overdue():
    charge_type = request.args.get('type')
    q = OverdueRecord.query
    if charge_type in ('overdue', 'damaged', 'lost'):
        q = q.filter_by(charge_type=charge_type)
    records = q.order_by(OverdueRecord.fine_status.asc(),
                         OverdueRecord.issued_date.desc()).all()
    return render_template('librarian/overdue.html',
                           title='Overdue & Fines', records=records,
                           active_filter=charge_type or 'all')


@librarian.route('/overdue/pay/<int:overdue_id>', methods=['GET', 'POST'])
@login_required
@librarian_required
def mark_paid(overdue_id):
    record = OverdueRecord.query.get_or_404(overdue_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        BookService.mark_fine_paid(overdue_id, current_user.id,
                                   request.form['payment_method'])
        if is_ajax:
            flash(f'Fine ₹{record.amount:.0f} marked as paid.', 'success')
            return jsonify({'success': True, 'message': 'Marked as paid.'})
        flash(f'Fine ₹{record.amount:.0f} marked as paid.', 'success')
        return redirect(url_for('librarian.overdue'))
    if is_ajax:
        return render_template('librarian/_mark_paid_fragment.html',
                               record=record, payment_methods=PAYMENT_METHODS)
    return render_template('librarian/mark_paid.html', title='Mark Paid',
                           record=record, payment_methods=PAYMENT_METHODS)


@librarian.route('/users')
@login_required
@librarian_required
def users_list():
    users = User.query.filter_by(role='user').order_by(User.id).all()
    return render_template('librarian/users.html', title='Users', users=users)


@librarian.route('/users/<int:user_id>')
@login_required
@librarian_required
def user_detail(user_id):
    member = User.query.filter_by(id=user_id, role='user').first_or_404()
    borrowed = [t for t in member.transactions if t.status == 'borrowed']
    pending_fine = sum(
        o.amount for o in member.overdue_records if o.fine_status == 'unpaid'
    )
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('librarian/_user_detail_fragment.html',
                               member=member, borrowed=borrowed, pending_fine=pending_fine)
    return redirect(url_for('librarian.users_list'))


@librarian.route('/history')
@login_required
@librarian_required
def history():
    records = BorrowRecord.query.order_by(BorrowRecord.request_date.desc()).all()
    return render_template('librarian/history.html', title='History', records=records)


@librarian.route('/damaged')
@login_required
@librarian_required
def damaged():
    from models.lost_book import LostBook
    view_type = request.args.get('type', 'damaged')
    if view_type == 'lost':
        logs = LostBook.query.order_by(LostBook.reported_date.desc()).all()
    else:
        view_type = 'damaged'
        logs = DamagedBook.query.order_by(DamagedBook.reported_date.desc()).all()
    return render_template('librarian/damaged.html', title='Damaged & Lost Books',
                           logs=logs, view_type=view_type)


@librarian.route('/damaged/add', methods=['GET', 'POST'])
@login_required
@librarian_required
def add_damaged():
    if request.method == 'POST':
        log, err = BookService.log_damaged(
            book_id=request.form['book_id'],
            librarian_id=current_user.id,
            quantity=request.form.get('quantity', 1),
            notes=request.form.get('notes'),
        )
        flash(err if err else 'Damage logged.', 'danger' if err else 'success')
        return redirect(url_for('librarian.damaged'))
    books = Book.query.order_by(Book.title).all()
    return render_template('librarian/add_damaged.html',
                           title='Log Damage', books=books, book=None)


@librarian.route('/damaged/add/<int:book_id>', methods=['GET', 'POST'])
@login_required
@librarian_required
def add_damaged_for(book_id):
    book = Book.query.get_or_404(book_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        log, err = BookService.log_damaged(
            book_id=book_id, librarian_id=current_user.id,
            quantity=request.form.get('quantity', 1),
            notes=request.form.get('notes'),
        )
        if is_ajax:
            if not err:
                flash('Damage logged.', 'success')
            return jsonify({'success': not bool(err), 'message': err if err else 'Damage logged.'})
        flash(err if err else 'Damage logged.', 'danger' if err else 'success')
        return redirect(url_for('librarian.book_list'))
    if is_ajax:
        return render_template('librarian/_add_damaged_fragment.html', book=book)
    return render_template('librarian/add_damaged.html',
                           title='Log Damage', book=book, books=None)


@librarian.route('/profile', methods=['GET', 'POST'])
@login_required
@librarian_required
def profile():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        err = AuthService.update_profile(
            current_user,
            name=request.form['name'], email=request.form['email'],
            phone=request.form['phone'],
        )
        if is_ajax:
            return jsonify({
                'success':  not bool(err),
                'message':  err if err else 'Profile updated!',
                'name':     current_user.name,
                'initials': current_user.name[:2].upper(),
            })
        flash(err if err else 'Profile updated!', 'danger' if err else 'success')
    return render_template('librarian/profile.html', title='Profile')


# ── Notifications ─────────────────────────────────────────────────────
@librarian.route('/notifications')
@login_required
@librarian_required
def notifications():
    from services.notification_service import NotificationService
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    all_notifs = NotificationService.get_for_user(current_user.id, limit=60)
    unread_ids = {n.id for n in all_notifs if not n.is_read}
    if unread_ids:
        NotificationService.mark_all_read(current_user.id)
    if is_ajax:
        return render_template('librarian/_notifications_fragment.html',
                               notifs=all_notifs[:5], unread_ids=unread_ids,
                               total_count=len(all_notifs))
    return render_template('librarian/notifications.html',
                           title='Notifications', notifs=all_notifs, unread_ids=unread_ids)


@librarian.route('/notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
@librarian_required
def mark_notification_read(notif_id):
    from services.notification_service import NotificationService
    NotificationService.mark_read(notif_id, current_user.id)
    return redirect(url_for('librarian.notifications'))


@librarian.route('/notifications/mark-all-read', methods=['POST'])
@login_required
@librarian_required
def mark_all_notifications_read():
    from services.notification_service import NotificationService
    NotificationService.mark_all_read(current_user.id)
    flash('All notifications marked as read.', 'success')
    return redirect(url_for('librarian.notifications'))


# ── Membership payments ──────────────────────────────────────────────

@librarian.route('/membership-payments')
@login_required
@librarian_required
def membership_payments():
    from services.membership_service import MembershipService
    from models.membership import MembershipPayment
    pending = MembershipService.all_pending()
    paid = MembershipPayment.query.filter_by(status='paid')\
        .order_by(MembershipPayment.paid_date.desc()).limit(30).all()
    return render_template('librarian/membership_payments.html',
                           title='Membership Payments', pending=pending, paid=paid)


@librarian.route('/membership-payments/pay/<int:payment_id>', methods=['GET', 'POST'])
@login_required
@librarian_required
def mark_membership_paid(payment_id):
    from services.membership_service import MembershipService
    from models.membership import MembershipPayment
    record = MembershipPayment.query.get_or_404(payment_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        MembershipService.mark_paid(payment_id, current_user.id,
                                    request.form['payment_method'])
        if is_ajax:
            flash(f'₹{record.amount:.0f} marked as paid.', 'success')
            return jsonify({'success': True, 'message': 'Marked as paid.'})
        flash(f'₹{record.amount:.0f} marked as paid.', 'success')
        return redirect(url_for('librarian.membership_payments'))
    if is_ajax:
        return render_template('librarian/_mark_membership_paid_fragment.html',
                               record=record, payment_methods=PAYMENT_METHODS)
    return render_template('librarian/mark_membership_paid.html', title='Mark Paid',
                           record=record, payment_methods=PAYMENT_METHODS)


# ── Home delivery ─────────────────────────────────────────────────────

@librarian.route('/deliveries')
@login_required
@librarian_required
def deliveries():
    from services.delivery_service import DeliveryService
    status = request.args.get('status', 'requested')
    orders = DeliveryService.orders_by_status(status)
    return render_template('librarian/deliveries.html', title='Deliveries',
                           orders=orders, active_status=status)


@librarian.route('/deliveries/<int:order_id>')
@login_required
@librarian_required
def delivery_detail(order_id):
    from services.delivery_service import DeliveryService
    order  = DeliveryService.get_or_404(order_id)
    agents = DeliveryService.list_agents(active_only=True)
    return render_template('librarian/delivery_detail.html', title='Delivery Order',
                           order=order, agents=agents)


@librarian.route('/deliveries/<int:order_id>/accept', methods=['POST'])
@login_required
@librarian_required
def accept_delivery(order_id):
    from services.delivery_service import DeliveryService
    order, err = DeliveryService.accept_order(order_id, current_user.id)
    if err:
        flash(err, 'danger')
    else:
        flash(f'Delivery order #{order.id} accepted.', 'success')
    return redirect(url_for('librarian.delivery_detail', order_id=order_id))


@librarian.route('/deliveries/<int:order_id>/reject', methods=['POST'])
@login_required
@librarian_required
def reject_delivery(order_id):
    from services.delivery_service import DeliveryService
    order, err = DeliveryService.reject_order(order_id, reason=request.form.get('reason'))
    flash(err if err else f'Delivery order #{order_id} rejected.', 'danger' if err else 'success')
    return redirect(url_for('librarian.deliveries'))


@librarian.route('/deliveries/<int:order_id>/cancel', methods=['POST'])
@login_required
@librarian_required
def cancel_delivery(order_id):
    from services.delivery_service import DeliveryService
    order, err = DeliveryService.cancel_order(order_id, reason=request.form.get('reason'))
    flash(err if err else f'Delivery order #{order_id} cancelled.', 'danger' if err else 'success')
    return redirect(url_for('librarian.deliveries'))


@librarian.route('/deliveries/<int:order_id>/assign-agent', methods=['POST'])
@login_required
@librarian_required
def assign_delivery_agent(order_id):
    from services.delivery_service import DeliveryService
    agent_id = request.form.get('agent_id')
    if not agent_id:
        flash('Select a delivery agent.', 'danger')
    else:
        DeliveryService.assign_agent(order_id, agent_id)
        flash('Delivery agent assigned.', 'success')
    return redirect(url_for('librarian.delivery_detail', order_id=order_id))


@librarian.route('/deliveries/<int:order_id>/pack', methods=['POST'])
@login_required
@librarian_required
def pack_delivery(order_id):
    from services.delivery_service import DeliveryService
    order, err = DeliveryService.mark_packed(order_id)
    flash(err if err else 'Order marked as packed.', 'danger' if err else 'success')
    return redirect(url_for('librarian.delivery_detail', order_id=order_id))


@librarian.route('/deliveries/<int:order_id>/ship', methods=['POST'])
@login_required
@librarian_required
def ship_delivery(order_id):
    from services.delivery_service import DeliveryService
    order, err = DeliveryService.mark_shipped(order_id)
    flash(err if err else 'Order marked as shipped.', 'danger' if err else 'success')
    return redirect(url_for('librarian.delivery_detail', order_id=order_id))


@librarian.route('/deliveries/<int:order_id>/out-for-delivery', methods=['POST'])
@login_required
@librarian_required
def out_for_delivery(order_id):
    from services.delivery_service import DeliveryService
    order, err = DeliveryService.mark_out_for_delivery(order_id)
    flash(err if err else 'Order marked out for delivery.', 'danger' if err else 'success')
    return redirect(url_for('librarian.delivery_detail', order_id=order_id))


@librarian.route('/deliveries/<int:order_id>/deliver', methods=['POST'])
@login_required
@librarian_required
def deliver_delivery(order_id):
    from services.delivery_service import DeliveryService
    order, err = DeliveryService.mark_delivered(order_id)
    flash(err if err else 'Order marked as delivered!', 'danger' if err else 'success')
    return redirect(url_for('librarian.delivery_detail', order_id=order_id))


@librarian.route('/deliveries/<int:order_id>/pay', methods=['GET', 'POST'])
@login_required
@librarian_required
def mark_delivery_fee_paid(order_id):
    from services.delivery_service import DeliveryService
    order = DeliveryService.get_or_404(order_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        DeliveryService.mark_fee_paid(order_id, current_user.id,
                                      request.form['payment_method'])
        if is_ajax:
            flash(f'Delivery fee ₹{order.delivery_fee:.0f} marked as paid.', 'success')
            return jsonify({'success': True, 'message': 'Marked as paid.'})
        flash(f'Delivery fee ₹{order.delivery_fee:.0f} marked as paid.', 'success')
        return redirect(url_for('librarian.delivery_detail', order_id=order_id))
    if is_ajax:
        return render_template('librarian/_mark_delivery_paid_fragment.html',
                               order=order, payment_methods=PAYMENT_METHODS)
    return render_template('librarian/mark_delivery_paid.html', title='Mark Paid',
                           order=order, payment_methods=PAYMENT_METHODS)


@librarian.route('/deliveries/<int:order_id>/pay/create-order', methods=['POST'])
@login_required
@librarian_required
def create_delivery_payment_order_lib(order_id):
    """
    Librarian-initiated Razorpay checkout — for collecting the delivery
    fee online (QR/link shown on the librarian's screen) instead of
    cash. Same create-order/verify pattern as the user-side routes in
    routes/user.py, just without an ownership check since any librarian
    may collect payment for any order.
    """
    from services.delivery_service import DeliveryService
    from services.payment_service import create_order, PaymentConfigError

    order = DeliveryService.get_or_404(order_id)
    if order.fee_status == 'paid':
        return jsonify({'success': False, 'error': 'This delivery fee is already paid.'}), 400

    try:
        gw_order = create_order(order.delivery_fee, receipt=f'delivery-{order.id}')
    except PaymentConfigError as e:
        current_app.logger.error('Razorpay not configured: %s', e)
        return jsonify({'success': False, 'error': 'Online payment is not available right now.'}), 503
    except Exception as e:
        current_app.logger.error('Razorpay order creation failed: %s', e)
        return jsonify({'success': False, 'error': 'Could not start payment. Please try again.'}), 502

    order.gateway_order_id = gw_order['id']
    db.session.commit()

    return jsonify({
        'success':  True,
        'order_id': gw_order['id'],
        'amount':   gw_order['amount'],
        'key_id':   current_app.config.get('RAZORPAY_KEY_ID'),
        'name':     'LibraryMS',
        'description': f'Delivery fee — Order #{order.id}',
    })


@librarian.route('/deliveries/<int:order_id>/pay/verify', methods=['POST'])
@login_required
@librarian_required
def verify_delivery_payment_lib(order_id):
    from services.delivery_service import DeliveryService
    from services.payment_service import verify_signature

    order = DeliveryService.get_or_404(order_id)

    body        = request.get_json(silent=True) or {}
    gw_order_id = body.get('razorpay_order_id')
    payment_id  = body.get('razorpay_payment_id')
    signature   = body.get('razorpay_signature')

    if not all([gw_order_id, payment_id, signature]):
        return jsonify({'success': False, 'error': 'Missing payment details.'}), 400

    if gw_order_id != order.gateway_order_id:
        return jsonify({'success': False, 'error': 'Payment does not match this order.'}), 400

    if not verify_signature(gw_order_id, payment_id, signature):
        return jsonify({'success': False, 'error': 'Payment verification failed.'}), 400

    DeliveryService.record_online_payment(
        order.id, gateway='razorpay', order_id_gw=gw_order_id, payment_id=payment_id
    )
    return jsonify({'success': True, 'message': 'Payment verified — delivery fee cleared!'})


# ── Delivery agent roster ────────────────────────────────────────────

@librarian.route('/delivery-agents')
@login_required
@librarian_required
def delivery_agents():
    from services.delivery_service import DeliveryService
    agents = DeliveryService.list_agents()
    return render_template('librarian/delivery_agents.html', title='Delivery Agents',
                           agents=agents)


@librarian.route('/delivery-agents/add', methods=['GET', 'POST'])
@login_required
@librarian_required
def add_delivery_agent():
    from services.delivery_service import DeliveryService
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        agent, err = DeliveryService.create_agent(
            name=request.form.get('name', '').strip(),
            phone=request.form.get('phone', '').strip(),
        )
        if is_ajax:
            if not err:
                flash(f'"{agent.name}" added as a delivery agent!', 'success')
            return jsonify({'success': not bool(err),
                            'message': err if err else f'"{agent.name}" added!'})
        flash(err if err else f'"{agent.name}" added as a delivery agent!',
              'danger' if err else 'success')
        return redirect(url_for('librarian.delivery_agents'))
    if is_ajax:
        return render_template('librarian/_add_delivery_agent_fragment.html')
    return redirect(url_for('librarian.delivery_agents'))


@librarian.route('/delivery-agents/edit/<int:agent_id>', methods=['GET', 'POST'])
@login_required
@librarian_required
def edit_delivery_agent(agent_id):
    from services.delivery_service import DeliveryService
    from models.delivery import DeliveryAgent
    agent = DeliveryAgent.query.get_or_404(agent_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        DeliveryService.update_agent(
            agent_id,
            name=request.form.get('name', '').strip(),
            phone=request.form.get('phone', '').strip(),
        )
        if is_ajax:
            flash('Delivery agent updated!', 'success')
            return jsonify({'success': True, 'message': 'Delivery agent updated!'})
        flash('Delivery agent updated!', 'success')
        return redirect(url_for('librarian.delivery_agents'))
    if is_ajax:
        return render_template('librarian/_edit_delivery_agent_fragment.html', agent=agent)
    return redirect(url_for('librarian.delivery_agents'))


@librarian.route('/delivery-agents/<int:agent_id>/toggle-active', methods=['POST'])
@login_required
@librarian_required
def toggle_delivery_agent(agent_id):
    from services.delivery_service import DeliveryService
    agent = DeliveryService.toggle_agent_active(agent_id)
    flash(f'"{agent.name}" is now {"active" if agent.is_active else "inactive"}.', 'success')
    return redirect(url_for('librarian.delivery_agents'))


# ── Return pickup ─────────────────────────────────────────────────────
# Reuses the same DeliveryAgent roster as home delivery — no separate
# agent management here. There is no "mark returned" action: a pickup
# order auto-closes once its book is inspected via the ordinary
# return_inspect/return_book flow below (see PickupService._maybe_close_order).

@librarian.route('/pickups')
@login_required
@librarian_required
def pickups():
    from services.pickup_service import PickupService
    status = request.args.get('status', 'requested')
    orders = PickupService.orders_by_status(status)
    return render_template('librarian/pickups.html', title='Return Pickups',
                           orders=orders, active_status=status)


@librarian.route('/pickups/<int:order_id>')
@login_required
@librarian_required
def pickup_detail(order_id):
    from services.pickup_service import PickupService
    from services.delivery_service import DeliveryService
    order  = PickupService.get_or_404(order_id)
    agents = DeliveryService.list_agents(active_only=True)
    return render_template('librarian/pickup_detail.html', title='Return Pickup',
                           order=order, agents=agents)


@librarian.route('/pickups/<int:order_id>/accept', methods=['POST'])
@login_required
@librarian_required
def accept_pickup(order_id):
    from services.pickup_service import PickupService
    order, err = PickupService.accept_order(order_id, current_user.id)
    if err:
        flash(err, 'danger')
    else:
        flash(f'Return pickup #{order.id} accepted.', 'success')
    return redirect(url_for('librarian.pickup_detail', order_id=order_id))


@librarian.route('/pickups/<int:order_id>/reject', methods=['POST'])
@login_required
@librarian_required
def reject_pickup(order_id):
    from services.pickup_service import PickupService
    order, err = PickupService.reject_order(order_id, reason=request.form.get('reason'))
    flash(err if err else f'Return pickup #{order_id} rejected.', 'danger' if err else 'success')
    return redirect(url_for('librarian.pickups'))


@librarian.route('/pickups/<int:order_id>/cancel', methods=['POST'])
@login_required
@librarian_required
def cancel_pickup(order_id):
    from services.pickup_service import PickupService
    order, err = PickupService.cancel_order(order_id, reason=request.form.get('reason'))
    flash(err if err else f'Return pickup #{order_id} cancelled.', 'danger' if err else 'success')
    return redirect(url_for('librarian.pickups'))


@librarian.route('/pickups/<int:order_id>/assign-agent', methods=['POST'])
@login_required
@librarian_required
def assign_pickup_agent(order_id):
    from services.pickup_service import PickupService
    agent_id = request.form.get('agent_id')
    if not agent_id:
        flash('Select a delivery agent.', 'danger')
    else:
        PickupService.assign_agent(order_id, agent_id)
        flash('Delivery agent assigned.', 'success')
    return redirect(url_for('librarian.pickup_detail', order_id=order_id))


@librarian.route('/pickups/<int:order_id>/out-for-pickup', methods=['POST'])
@login_required
@librarian_required
def out_for_pickup(order_id):
    from services.pickup_service import PickupService
    order, err = PickupService.mark_out_for_pickup(order_id)
    flash(err if err else 'Order marked out for pickup.', 'danger' if err else 'success')
    return redirect(url_for('librarian.pickup_detail', order_id=order_id))


@librarian.route('/pickups/<int:order_id>/picked-up', methods=['POST'])
@login_required
@librarian_required
def picked_up(order_id):
    from services.pickup_service import PickupService
    order, err = PickupService.mark_picked_up(order_id)
    if err:
        flash(err, 'danger')
    else:
        flash('Book marked picked up — return it via Transactions once it arrives.', 'success')
    return redirect(url_for('librarian.pickup_detail', order_id=order_id))


@librarian.route('/pickups/<int:order_id>/pay', methods=['GET', 'POST'])
@login_required
@librarian_required
def mark_pickup_fee_paid(order_id):
    from services.pickup_service import PickupService
    order = PickupService.get_or_404(order_id)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        PickupService.mark_fee_paid(order_id, current_user.id,
                                    request.form['payment_method'])
        if is_ajax:
            flash(f'Pickup fee ₹{order.pickup_fee:.0f} marked as paid.', 'success')
            return jsonify({'success': True, 'message': 'Marked as paid.'})
        flash(f'Pickup fee ₹{order.pickup_fee:.0f} marked as paid.', 'success')
        return redirect(url_for('librarian.pickup_detail', order_id=order_id))
    if is_ajax:
        return render_template('librarian/_mark_pickup_paid_fragment.html',
                               order=order, payment_methods=PAYMENT_METHODS)
    return render_template('librarian/mark_pickup_paid.html', title='Mark Paid',
                           order=order, payment_methods=PAYMENT_METHODS)


@librarian.route('/pickups/<int:order_id>/pay/create-order', methods=['POST'])
@login_required
@librarian_required
def create_pickup_payment_order_lib(order_id):
    """Librarian-initiated Razorpay checkout — mirrors
    create_delivery_payment_order_lib."""
    from services.pickup_service import PickupService
    from services.payment_service import create_order, PaymentConfigError

    order = PickupService.get_or_404(order_id)
    if order.fee_status == 'paid':
        return jsonify({'success': False, 'error': 'This pickup fee is already paid.'}), 400

    try:
        gw_order = create_order(order.pickup_fee, receipt=f'pickup-{order.id}')
    except PaymentConfigError as e:
        current_app.logger.error('Razorpay not configured: %s', e)
        return jsonify({'success': False, 'error': 'Online payment is not available right now.'}), 503
    except Exception as e:
        current_app.logger.error('Razorpay order creation failed: %s', e)
        return jsonify({'success': False, 'error': 'Could not start payment. Please try again.'}), 502

    order.gateway_order_id = gw_order['id']
    db.session.commit()

    return jsonify({
        'success':  True,
        'order_id': gw_order['id'],
        'amount':   gw_order['amount'],
        'key_id':   current_app.config.get('RAZORPAY_KEY_ID'),
        'name':     'LibraryMS',
        'description': f'Return pickup fee — Order #{order.id}',
    })


@librarian.route('/pickups/<int:order_id>/pay/verify', methods=['POST'])
@login_required
@librarian_required
def verify_pickup_payment_lib(order_id):
    from services.pickup_service import PickupService
    from services.payment_service import verify_signature

    order = PickupService.get_or_404(order_id)

    body        = request.get_json(silent=True) or {}
    gw_order_id = body.get('razorpay_order_id')
    payment_id  = body.get('razorpay_payment_id')
    signature   = body.get('razorpay_signature')

    if not all([gw_order_id, payment_id, signature]):
        return jsonify({'success': False, 'error': 'Missing payment details.'}), 400

    if gw_order_id != order.gateway_order_id:
        return jsonify({'success': False, 'error': 'Payment does not match this order.'}), 400

    if not verify_signature(gw_order_id, payment_id, signature):
        return jsonify({'success': False, 'error': 'Payment verification failed.'}), 400

    PickupService.record_online_payment(
        order.id, gateway='razorpay', order_id_gw=gw_order_id, payment_id=payment_id
    )
    return jsonify({'success': True, 'message': 'Payment verified — pickup fee cleared!'})
