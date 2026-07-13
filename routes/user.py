from urllib.parse import urlparse
from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, current_app
from flask_login import login_required, current_user
from functools import wraps
from extensions import db
from services.book_service        import BookService
from services.auth_service        import AuthService
from services.openlibrary_service import enrich_books, fetch_book
from models.book        import GENRES
from models.transaction import BorrowRecord
from models.cart        import Cart
from models.overdue      import OverdueRecord
from config               import Config

user = Blueprint('user', __name__)


def _redirect_back(default_endpoint, **default_kwargs):
    """Redirect to wherever the request came from (preserving filters/search/
    sort on the browse page) instead of always bouncing to a fixed page."""
    ref = request.referrer
    if ref and urlparse(ref).netloc == urlparse(request.url).netloc:
        return redirect(ref)
    return redirect(url_for(default_endpoint, **default_kwargs))


def user_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role != 'user':
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                # An AJAX caller (e.g. the Razorpay "Pay" button) expects JSON
                # back — a redirect here would make its fetch().json() throw
                # on the landing page's HTML instead of showing a real error.
                # Most common cause: this browser's session logged into a
                # different role (e.g. librarian) in another tab since this
                # page was loaded — sessions are per-browser, not per-tab.
                return jsonify({'success': False,
                                 'error': 'Your session is no longer logged in as a member. '
                                          'Please refresh and log in again.'}), 401
            flash('Access denied.', 'danger')
            return redirect(url_for('auth.landing'))
        return f(*args, **kwargs)
    return decorated


@user.route('/home')
@login_required
@user_required
def home():
    from models.book import Book
    from models.reservation import Reservation
    active_borrows = BorrowRecord.query.filter_by(
        user_id=current_user.id, status='borrowed').count()
    reserved_books = Reservation.query.filter(
        Reservation.user_id == current_user.id,
        Reservation.status.in_(['queued', 'ready']),
    ).count()
    pending_fine = sum(
        o.amount for o in current_user.overdue_records if o.fine_status == 'unpaid'
    )
    best_books = enrich_books(
        Book.query.filter(Book.lifetime_issued >= 1)
            .order_by(Book.lifetime_issued.desc()).limit(12).all()
    )
    return render_template('user/home.html', title='Home',
                           active_borrows=active_borrows, best_books=best_books,
                           reserved_books=reserved_books, pending_fine=pending_fine)


@user.route('/books')
@login_required
@user_required
def book_list():
    selected_genres = request.args.getlist('genre')[:3]
    search_q  = request.args.get('q', '').strip()
    sort = request.args.get('sort', 'title_asc')
    if sort not in ('title_asc', 'title_desc'):
        sort = 'title_asc'
    availability = request.args.get('availability', 'all')
    if availability not in ('all', 'available', 'unavailable'):
        availability = 'all'
    books      = enrich_books(BookService.get_all(
        genres=selected_genres or None, query=search_q or None,
        sort=sort, availability=availability if availability != 'all' else None,
    ))
    cart_ids   = {c.book_id for c in Cart.query.filter_by(user_id=current_user.id).all()}
    active_ids = {r.book_id for r in BorrowRecord.query.filter(
        BorrowRecord.user_id == current_user.id,
        BorrowRecord.status.in_(['pending', 'borrowed', 'reserved_ready', 'in_delivery']),
    ).all()}
    from models.reservation import Reservation
    reserve_ids = {r.book_id for r in Reservation.query.filter(
        Reservation.user_id == current_user.id,
        Reservation.status.in_(['queued', 'ready']),
    ).all()}
    return render_template('user/books.html', title='Browse Books',
                           books=books, genres=GENRES,
                           selected_genres=selected_genres, search_q=search_q,
                           sort=sort, availability=availability,
                           cart_book_ids=cart_ids,
                           active_book_ids=active_ids,
                           reserve_ids=reserve_ids)


@user.route('/books/<int:book_id>/details')
@login_required
@user_required
def book_details(book_id):
    from models.book import Book
    book = Book.query.get_or_404(book_id)
    enrich_books([book])
    ol = fetch_book(book.isbn) or {}
    return render_template('user/_book_details_fragment.html', book=book, ol=ol)


@user.route('/cart')
@login_required
@user_required
def cart():
    return render_template('user/cart.html', title='Cart',
                           cart_items=BookService.cart_items(current_user.id))


@user.route('/cart/add/<int:book_id>', methods=['POST'])
@login_required
@user_required
def add_to_cart(book_id):
    _, err = BookService.add_to_cart(current_user.id, book_id)
    flash(err if err else 'Added to cart!', 'danger' if err else 'success')
    return _redirect_back('user.book_list')


@user.route('/cart/remove/<int:book_id>', methods=['POST'])
@login_required
@user_required
def remove_from_cart(book_id):
    BookService.remove_from_cart(current_user.id, book_id)
    flash('Removed from cart.', 'success')
    return _redirect_back('user.cart')


@user.route('/cart/request', methods=['POST'])
@login_required
@user_required
def request_books():
    records, err = BookService.request_books(current_user.id)
    if err:
        flash(err, 'danger')
        return redirect(url_for('user.cart'))
    flash(f'{len(records)} request(s) sent to librarian!', 'success')
    return redirect(url_for('user.my_books'))


@user.route('/my-books')
@login_required
@user_required
def my_books():
    requests = BorrowRecord.query.filter(
        BorrowRecord.user_id == current_user.id,
        BorrowRecord.status == 'pending',
    ).order_by(BorrowRecord.request_date.desc()).all()
    issued = BookService.active_borrows(user_id=current_user.id)
    return render_template('user/my_books.html', title='Books',
                           records=requests, issued=issued)


@user.route('/history')
@login_required
@user_required
def history():
    page = request.args.get('page', 1, type=int)
    pagination = BorrowRecord.query.filter_by(user_id=current_user.id)\
                .order_by(BorrowRecord.request_date.desc(), BorrowRecord.id.desc())\
                .paginate(page=page, per_page=25, error_out=False)
    return render_template('user/history.html', title='History',
                           records=pagination.items, pagination=pagination)


@user.route('/search')
@login_required
@user_required
def search():
    q     = request.args.get('q', '').strip()
    books = enrich_books(BookService.get_all(query=q)) if q else []
    cart_ids   = {c.book_id for c in Cart.query.filter_by(user_id=current_user.id).all()}
    active_ids = {r.book_id for r in BorrowRecord.query.filter(
        BorrowRecord.user_id == current_user.id,
        BorrowRecord.status.in_(['pending', 'borrowed', 'reserved_ready', 'in_delivery']),
    ).all()}
    return render_template('user/search.html', title='Search',
                           books=books, query=q,
                           cart_book_ids=cart_ids,
                           active_book_ids=active_ids)


@user.route('/reservations')
@login_required
@user_required
def reservations():
    from services.reservation_service import ReservationService
    active = ReservationService.user_reservations(current_user.id)
    history = ReservationService.user_history(current_user.id)
    return render_template('user/reservations.html', title='My Reservations',
                           active=active, history=history)


@user.route('/reserve/<int:book_id>', methods=['POST'])
@login_required
@user_required
def reserve_book(book_id):
    from services.reservation_service import ReservationService
    res, err = ReservationService.reserve(current_user.id, book_id)
    if err:
        flash(err, 'danger')
    elif res.status == 'ready':
        flash(
            f'"{res.book.title}" is ready for you to collect! '
            f'Please pick it up at the library soon.',
            'success'
        )
    else:
        flash(
            f'Reserved "{res.book.title}"! You are #'
            f'{res.queue_position} in the queue.',
            'success'
        )
    return _redirect_back('user.book_list')


@user.route('/reservations/cancel/<int:reservation_id>', methods=['POST'])
@login_required
@user_required
def cancel_reservation(reservation_id):
    from services.reservation_service import ReservationService
    _, err = ReservationService.cancel(reservation_id, current_user.id)
    flash(err if err else 'Reservation cancelled.', 'danger' if err else 'success')
    return redirect(url_for('user.reservations'))


# ── Notifications ─────────────────────────────────────────────────────
@user.route('/notifications')
@login_required
@user_required
def notifications():
    from services.notification_service import NotificationService
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    all_notifs = NotificationService.get_for_user(current_user.id, limit=60)
    # Remember which were unread *before* this view, so the page can still
    # highlight them once, then mark everything read automatically —
    # no manual "mark as read" step needed.
    unread_ids = {n.id for n in all_notifs if not n.is_read}
    if unread_ids:
        NotificationService.mark_all_read(current_user.id)
    if is_ajax:
        return render_template('user/_notifications_fragment.html',
                               notifs=all_notifs[:5], unread_ids=unread_ids,
                               total_count=len(all_notifs))
    return render_template('user/notifications.html',
                           title='Notifications', notifs=all_notifs, unread_ids=unread_ids)


@user.route('/notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
@user_required
def mark_notification_read(notif_id):
    from services.notification_service import NotificationService
    NotificationService.mark_read(notif_id, current_user.id)
    return redirect(url_for('user.notifications'))


@user.route('/notifications/mark-all-read', methods=['POST'])
@login_required
@user_required
def mark_all_notifications_read():
    from services.notification_service import NotificationService
    NotificationService.mark_all_read(current_user.id)
    flash('All notifications marked as read.', 'success')
    return redirect(url_for('user.notifications'))


@user.route('/profile', methods=['GET', 'POST'])
@login_required
@user_required
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
    return render_template('user/profile.html', title='Profile')


@user.route('/profile/address', methods=['POST'])
@login_required
@user_required
def update_address():
    AuthService.update_address(
        current_user,
        address_line1 = request.form.get('address_line1', '').strip(),
        address_line2 = request.form.get('address_line2', '').strip(),
        city          = request.form.get('city', '').strip(),
        state         = request.form.get('state', '').strip(),
        pincode       = request.form.get('pincode', '').strip(),
        landmark      = request.form.get('landmark', '').strip(),
    )
    flash('Saved address updated!', 'success')
    return redirect(url_for('user.profile'))


# ── Fines & online payment (Razorpay) ─────────────────────────────────

@user.route('/fines')
@login_required
@user_required
def fines():
    unpaid = OverdueRecord.query.filter_by(
        user_id=current_user.id, fine_status='unpaid'
    ).order_by(OverdueRecord.issued_date.desc()).all()
    paid = OverdueRecord.query.filter_by(
        user_id=current_user.id, fine_status='paid'
    ).order_by(OverdueRecord.paid_date.desc()).limit(20).all()
    return render_template(
        'user/fines.html', title='Fines',
        unpaid=unpaid, paid=paid,
        razorpay_key_id=current_app.config.get('RAZORPAY_KEY_ID'),
    )


@user.route('/fines/<int:overdue_id>/pay/create-order', methods=['POST'])
@login_required
@user_required
def create_payment_order(overdue_id):
    """
    Step 1 of the payment flow: ask Razorpay for an Order before
    showing checkout. The amount charged is always read from OUR
    database record, never from anything the client sends — otherwise
    a modified request could ask us to charge ₹1 for a ₹500 fine.
    """
    from services.payment_service import create_order, PaymentConfigError

    record = OverdueRecord.query.filter_by(
        id=overdue_id, user_id=current_user.id
    ).first_or_404()

    if record.fine_status == 'paid':
        return jsonify({'success': False, 'error': 'This fine is already paid.'}), 400

    try:
        order = create_order(record.amount, receipt=f'fine-{record.id}')
    except PaymentConfigError as e:
        current_app.logger.error('Razorpay not configured: %s', e)
        return jsonify({'success': False, 'error': 'Online payment is not available right now.'}), 503
    except Exception as e:
        current_app.logger.error('Razorpay order creation failed: %s', e)
        return jsonify({'success': False, 'error': 'Could not start payment. Please try again.'}), 502

    record.gateway_order_id = order['id']
    db.session.commit()

    return jsonify({
        'success':  True,
        'order_id': order['id'],
        'amount':   order['amount'],   # in paise — Razorpay Checkout expects this
        'key_id':   current_app.config.get('RAZORPAY_KEY_ID'),
        'name':     'LibraryMS',
        'description': f'Fine payment — Fine #{record.id}',
    })


@user.route('/fines/<int:overdue_id>/pay/verify', methods=['POST'])
@login_required
@user_required
def verify_payment(overdue_id):
    """
    Step 2: the browser calls this after Razorpay's checkout popup
    reports success, handing us back the payment_id + signature.
    We verify the signature ourselves before trusting any of it —
    a client-side "success" on its own proves nothing.
    """
    from services.payment_service import verify_signature

    record = OverdueRecord.query.filter_by(
        id=overdue_id, user_id=current_user.id
    ).first_or_404()

    body       = request.get_json(silent=True) or {}
    order_id   = body.get('razorpay_order_id')
    payment_id = body.get('razorpay_payment_id')
    signature  = body.get('razorpay_signature')

    if not all([order_id, payment_id, signature]):
        return jsonify({'success': False, 'error': 'Missing payment details.'}), 400

    # The order we're verifying against must be the one WE created for
    # THIS fine — stops someone reusing a signature from a different order.
    if order_id != record.gateway_order_id:
        return jsonify({'success': False, 'error': 'Payment does not match this fine.'}), 400

    if not verify_signature(order_id, payment_id, signature):
        return jsonify({'success': False, 'error': 'Payment verification failed.'}), 400

    BookService.record_online_payment(
        record.id, gateway='razorpay', order_id=order_id, payment_id=payment_id
    )
    return jsonify({'success': True, 'message': 'Payment verified — fine cleared!'})


# ── Membership tier & fees ──────────────────────────────────────────────

@user.route('/membership')
@login_required
@user_required
def membership():
    from services.membership_service import MembershipService
    pending = MembershipService.pending_payment(current_user.id)
    history = MembershipService.history(current_user.id)
    return render_template(
        'user/membership.html', title='Membership',
        pending=pending, history=history,
        upgrade_rules=Config.MEMBERSHIP_RULES['membership'],
        razorpay_key_id=current_app.config.get('RAZORPAY_KEY_ID'),
        autopay=request.args.get('autopay') == '1',
    )


@user.route('/membership/upgrade', methods=['POST'])
@login_required
@user_required
def upgrade_membership():
    from services.membership_service import MembershipService
    _, err = MembershipService.request_upgrade(current_user.id)
    flash(err if err else 'Upgrade fee raised — pay it below to activate Membership.',
          'danger' if err else 'success')
    return redirect(url_for('user.membership'))


@user.route('/membership/pay/<int:payment_id>/create-order', methods=['POST'])
@login_required
@user_required
def create_membership_payment_order(payment_id):
    """Same pattern as create_payment_order for fines — amount always
    comes from OUR record, never from the client."""
    from services.membership_service import MembershipService
    from services.payment_service import create_order, PaymentConfigError
    from models.membership import MembershipPayment

    record = MembershipPayment.query.filter_by(
        id=payment_id, user_id=current_user.id
    ).first_or_404()

    if record.status == 'paid':
        return jsonify({'success': False, 'error': 'This payment is already made.'}), 400

    try:
        order = create_order(record.amount, receipt=f'membership-{record.id}')
    except PaymentConfigError as e:
        current_app.logger.error('Razorpay not configured: %s', e)
        return jsonify({'success': False, 'error': 'Online payment is not available right now.'}), 503
    except Exception as e:
        current_app.logger.error('Razorpay order creation failed: %s', e)
        return jsonify({'success': False, 'error': 'Could not start payment. Please try again.'}), 502

    record.gateway_order_id = order['id']
    db.session.commit()

    return jsonify({
        'success':  True,
        'order_id': order['id'],
        'amount':   order['amount'],
        'key_id':   current_app.config.get('RAZORPAY_KEY_ID'),
        'name':     'LibraryMS',
        'description': f'Membership {record.payment_type} — #{record.id}',
    })


@user.route('/membership/pay/<int:payment_id>/verify', methods=['POST'])
@login_required
@user_required
def verify_membership_payment(payment_id):
    from services.membership_service import MembershipService
    from services.payment_service import verify_signature
    from models.membership import MembershipPayment

    record = MembershipPayment.query.filter_by(
        id=payment_id, user_id=current_user.id
    ).first_or_404()

    body       = request.get_json(silent=True) or {}
    order_id   = body.get('razorpay_order_id')
    payment_id_gw = body.get('razorpay_payment_id')
    signature  = body.get('razorpay_signature')

    if not all([order_id, payment_id_gw, signature]):
        return jsonify({'success': False, 'error': 'Missing payment details.'}), 400

    if order_id != record.gateway_order_id:
        return jsonify({'success': False, 'error': 'Payment does not match this charge.'}), 400

    if not verify_signature(order_id, payment_id_gw, signature):
        return jsonify({'success': False, 'error': 'Payment verification failed.'}), 400

    MembershipService.record_online_payment(
        record.id, gateway='razorpay', order_id=order_id, gw_payment_id=payment_id_gw
    )
    return jsonify({'success': True, 'message': 'Payment verified — membership active!'})


# ── Home delivery ──────────────────────────────────────────────────────

@user.route('/cart/delivery', methods=['GET', 'POST'])
@login_required
@user_required
def delivery_address_form():
    from services.delivery_service import DeliveryService

    if current_user.membership_type != 'membership':
        flash('Home delivery is a Membership perk — upgrade your membership to use it.', 'danger')
        return redirect(url_for('user.membership'))

    items = BookService.cart_items(current_user.id)
    if request.method == 'POST':
        order, err = DeliveryService.request_delivery(
            current_user.id,
            recipient_name = request.form.get('recipient_name', '').strip(),
            phone          = request.form.get('phone', '').strip(),
            address_line1  = request.form.get('address_line1', '').strip(),
            address_line2  = request.form.get('address_line2', '').strip(),
            city           = request.form.get('city', '').strip(),
            state          = request.form.get('state', '').strip(),
            pincode        = request.form.get('pincode', '').strip(),
            landmark       = request.form.get('landmark', '').strip(),
        )
        if err:
            flash(err, 'danger')
            return redirect(url_for('user.delivery_address_form'))
        # The address just used becomes the new saved default, so next
        # time this form (or the pickup one) starts pre-filled with it.
        AuthService.update_address(
            current_user, order.address_line1, order.address_line2,
            order.city, order.state, order.pincode, order.landmark,
        )
        flash(f'Delivery request submitted! Fee: ₹{order.delivery_fee:.0f}', 'success')
        return redirect(url_for('user.deliveries'))

    if not items:
        flash('Cart is empty.', 'danger')
        return redirect(url_for('user.cart'))
    fee_preview = DeliveryService.calc_fee(len(items))
    return render_template('user/delivery_address.html', title='Home Delivery',
                           cart_items=items, fee_preview=fee_preview)


@user.route('/deliveries')
@login_required
@user_required
def deliveries():
    from services.delivery_service import DeliveryService
    orders = DeliveryService.user_orders(current_user.id)
    active  = [o for o in orders if not o.is_terminal]
    history = [o for o in orders if o.is_terminal]
    return render_template('user/deliveries.html', title='My Deliveries',
                           active=active, history=history)


@user.route('/deliveries/<int:order_id>')
@login_required
@user_required
def delivery_tracking(order_id):
    from services.delivery_service import DeliveryService
    order = DeliveryService.user_order_or_404(order_id, current_user.id)
    return render_template('user/delivery_tracking.html', title='Track Delivery',
                           order=order,
                           razorpay_key_id=current_app.config.get('RAZORPAY_KEY_ID'))


@user.route('/deliveries/<int:order_id>/status')
@login_required
@user_required
def delivery_status(order_id):
    from services.delivery_service import DeliveryService
    order = DeliveryService.user_order_or_404(order_id, current_user.id)
    return jsonify(DeliveryService.status_snapshot(order))


@user.route('/deliveries/<int:order_id>/cancel', methods=['POST'])
@login_required
@user_required
def cancel_delivery(order_id):
    from services.delivery_service import DeliveryService
    DeliveryService.user_order_or_404(order_id, current_user.id)   # ownership check
    _, err = DeliveryService.cancel_order(order_id)
    flash(err if err else 'Delivery order cancelled.', 'danger' if err else 'success')
    return redirect(url_for('user.deliveries'))


@user.route('/deliveries/<int:order_id>/pay/create-order', methods=['POST'])
@login_required
@user_required
def create_delivery_payment_order(order_id):
    """Same pattern as create_payment_order/create_membership_payment_order —
    amount always comes from OUR record, never from the client."""
    from services.delivery_service import DeliveryService
    from services.payment_service import create_order, PaymentConfigError

    order = DeliveryService.user_order_or_404(order_id, current_user.id)

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


@user.route('/deliveries/<int:order_id>/pay/verify', methods=['POST'])
@login_required
@user_required
def verify_delivery_payment(order_id):
    from services.delivery_service import DeliveryService
    from services.payment_service import verify_signature

    order = DeliveryService.user_order_or_404(order_id, current_user.id)

    body       = request.get_json(silent=True) or {}
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


# ── Return at library (advance heads-up, no pickup involved) ───────────

@user.route('/books/<int:record_id>/return-request', methods=['POST'])
@login_required
@user_required
def request_return(record_id):
    record, err = BookService.request_return(record_id, current_user.id)
    flash(err if err else f'Thanks — we\'ve let the librarian know you\'re returning "{record.book.title}".',
          'danger' if err else 'success')
    return redirect(url_for('user.my_books'))


# ── Return pickup ──────────────────────────────────────────────────────

@user.route('/books/<int:record_id>/pickup', methods=['GET', 'POST'])
@login_required
@user_required
def pickup_request_form(record_id):
    from services.pickup_service import PickupService

    if current_user.membership_type != 'membership':
        flash('Return pickup is a Membership perk — upgrade your membership to use it.', 'danger')
        return redirect(url_for('user.membership'))

    record = BorrowRecord.query.filter_by(id=record_id, user_id=current_user.id).first_or_404()
    if request.method == 'POST':
        order, err = PickupService.request_pickup(
            current_user.id, record_id,
            recipient_name = request.form.get('recipient_name', '').strip(),
            phone          = request.form.get('phone', '').strip(),
            address_line1  = request.form.get('address_line1', '').strip(),
            address_line2  = request.form.get('address_line2', '').strip(),
            city           = request.form.get('city', '').strip(),
            state          = request.form.get('state', '').strip(),
            pincode        = request.form.get('pincode', '').strip(),
            landmark       = request.form.get('landmark', '').strip(),
        )
        if err:
            flash(err, 'danger')
            return redirect(url_for('user.pickup_request_form', record_id=record_id))
        AuthService.update_address(
            current_user, order.address_line1, order.address_line2,
            order.city, order.state, order.pincode, order.landmark,
        )
        if order.pickup_fee > 0:
            flash(f'Return pickup requested! Fee: ₹{order.pickup_fee:.0f}', 'success')
        else:
            flash('Return pickup requested! No fee — already covered by your delivery.', 'success')
        return redirect(url_for('user.pickups'))

    if record.status != 'borrowed':
        flash('This book is not currently borrowed.', 'danger')
        return redirect(url_for('user.my_books'))
    if record.pickup_order_id and not record.pickup_order.is_terminal:
        flash('A return pickup is already in progress for this book.', 'danger')
        return redirect(url_for('user.pickups'))
    fee_preview = 0 if record.delivery_order_id else PickupService.calc_fee(1)
    return render_template('user/pickup_address.html', title='Return Pickup',
                           record=record, fee_preview=fee_preview,
                           waived=bool(record.delivery_order_id))


@user.route('/pickups')
@login_required
@user_required
def pickups():
    from services.pickup_service import PickupService
    orders = PickupService.user_orders(current_user.id)
    active  = [o for o in orders if not o.is_terminal]
    history = [o for o in orders if o.is_terminal]
    return render_template('user/pickups.html', title='My Return Pickups',
                           active=active, history=history)


@user.route('/pickups/<int:order_id>')
@login_required
@user_required
def pickup_tracking(order_id):
    from services.pickup_service import PickupService
    order = PickupService.user_order_or_404(order_id, current_user.id)
    return render_template('user/pickup_tracking.html', title='Track Pickup',
                           order=order,
                           razorpay_key_id=current_app.config.get('RAZORPAY_KEY_ID'))


@user.route('/pickups/<int:order_id>/status')
@login_required
@user_required
def pickup_status(order_id):
    from services.pickup_service import PickupService
    order = PickupService.user_order_or_404(order_id, current_user.id)
    return jsonify(PickupService.status_snapshot(order))


@user.route('/pickups/<int:order_id>/cancel', methods=['POST'])
@login_required
@user_required
def cancel_pickup(order_id):
    from services.pickup_service import PickupService
    PickupService.user_order_or_404(order_id, current_user.id)   # ownership check
    _, err = PickupService.cancel_order(order_id)
    flash(err if err else 'Return pickup cancelled.', 'danger' if err else 'success')
    return redirect(url_for('user.pickups'))


@user.route('/pickups/<int:order_id>/pay/create-order', methods=['POST'])
@login_required
@user_required
def create_pickup_payment_order(order_id):
    """Same pattern as create_delivery_payment_order — amount always
    comes from OUR record, never from the client."""
    from services.pickup_service import PickupService
    from services.payment_service import create_order, PaymentConfigError

    order = PickupService.user_order_or_404(order_id, current_user.id)

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


@user.route('/pickups/<int:order_id>/pay/verify', methods=['POST'])
@login_required
@user_required
def verify_pickup_payment(order_id):
    from services.pickup_service import PickupService
    from services.payment_service import verify_signature

    order = PickupService.user_order_or_404(order_id, current_user.id)

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
