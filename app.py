import logging
import logging.config
from dotenv import load_dotenv
load_dotenv()  # must run before Config is imported so os.environ is populated

from flask import Flask
from config import Config
from extensions import db, login_manager, jwt, csrf


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # ── Logging ────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    # Silence APScheduler's own verbose logs unless something goes wrong
    logging.getLogger('apscheduler').setLevel(logging.WARNING)

    db.init_app(app)
    login_manager.init_app(app)
    jwt.init_app(app)
    csrf.init_app(app)

    login_manager.login_view = 'auth.landing'

    # AJAX callers (fetch-based buttons like the Razorpay "Pay" flow) need a
    # JSON response when the session isn't logged in — the default redirect
    # to the landing page would make fetch().json() throw on that page's
    # HTML instead of surfacing a clear "please log in again" error.
    @login_manager.unauthorized_handler
    def _unauthorized():
        from flask import request, jsonify, redirect, url_for
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False,
                             'error': 'You have been logged out. Please log in again.'}), 401
        return redirect(url_for(login_manager.login_view))

    with app.app_context():
        from models.user         import User
        from models.book         import Book
        from models.transaction  import BorrowRecord
        from models.damaged      import DamagedBook
        from models.cart         import Cart
        from models.overdue      import OverdueRecord
        from models.reservation  import Reservation
        from models.notification import Notification
        from models.membership   import MembershipPayment
        from models.delivery     import DeliveryAgent, DeliveryOrder
        from models.pickup       import ReturnPickupOrder

        _migrate_schema(db)
        db.create_all()
        _seed_admin(User)
        _grandfather_memberships(User)

    # ── Web blueprints (session-based) ─────────────────────────────
    from routes.auth      import auth
    from routes.user      import user
    from routes.librarian import librarian
    from routes.admin     import admin

    app.register_blueprint(auth)
    app.register_blueprint(user,      url_prefix='/user')
    app.register_blueprint(librarian, url_prefix='/librarian')
    app.register_blueprint(admin,     url_prefix='/admin')

    # ── Template context — inject unread notification count ────────
    @app.context_processor
    def inject_notification_count():
        from flask_login import current_user
        if current_user.is_authenticated and current_user.role in ('user', 'librarian'):
            from services.notification_service import NotificationService
            try:
                return {'unread_notif_count': NotificationService.unread_count(current_user.id)}
            except Exception:
                pass
        return {'unread_notif_count': 0}

    # ── Template context — inject cart item count ───────────────────
    @app.context_processor
    def inject_cart_count():
        from flask_login import current_user
        if current_user.is_authenticated and current_user.role == 'user':
            from models.cart import Cart
            try:
                return {'cart_count': Cart.query.filter_by(user_id=current_user.id).count()}
            except Exception:
                pass
        return {'cart_count': 0}

    # ── REST API blueprints (JWT-based) ────────────────────────────
    from api.auth_api      import auth_api
    from api.books_api     import books_api
    from api.user_api      import user_api
    from api.librarian_api import librarian_api

    app.register_blueprint(auth_api,      url_prefix='/api/auth')
    app.register_blueprint(books_api,     url_prefix='/api/books')
    app.register_blueprint(user_api,      url_prefix='/api/user')
    app.register_blueprint(librarian_api, url_prefix='/api/librarian')

    # These are JWT-authenticated (Authorization header, not cookies),
    # so they aren't vulnerable to CSRF the way the session-based web
    # blueprints above are — exempt them or every JSON POST/PUT/DELETE
    # call would be rejected for missing a csrf_token it was never
    # meant to send.
    csrf.exempt(auth_api)
    csrf.exempt(books_api)
    csrf.exempt(user_api)
    csrf.exempt(librarian_api)

    # ── Payment gateway webhooks (server-to-server, no session) ─────
    from routes.webhooks import webhooks
    app.register_blueprint(webhooks, url_prefix='/webhooks')
    csrf.exempt(webhooks)

    # ── APScheduler — automatic fine calculation ────────────────────
    # Guard: only start in the main process (not in Flask's reloader child)
    import os
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        from services.scheduler import start_scheduler
        start_scheduler(app)

    return app


def _migrate_schema(db):
    """
    Lightweight, idempotent "add missing column" migration.

    This project has no Alembic/Flask-Migrate — db.create_all() only
    creates tables that don't exist yet, it never alters an existing
    table. Any model.py column added after a database file was already
    created (like the membership_type / membership_paid_until columns
    on User, and membership_payment_id on Notification) would otherwise
    make every query touching that column fail with "no such column"
    on anyone's pre-existing library.db.

    Safe to run on every startup: checks each column's presence via the
    engine's own reflected schema before adding it.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    additions = {
        'users': [
            ("membership_type",       "VARCHAR(20) NOT NULL DEFAULT 'basic'"),
            ("membership_paid_until", "DATE"),
            ("address_line1",   "VARCHAR(200)"),
            ("address_line2",   "VARCHAR(200)"),
            ("address_city",    "VARCHAR(80)"),
            ("address_state",   "VARCHAR(80)"),
            ("address_pincode", "VARCHAR(10)"),
            ("address_landmark", "VARCHAR(120)"),
        ],
        'notifications': [
            ("membership_payment_id", "INTEGER"),
            ("delivery_order_id",     "INTEGER"),
            ("pickup_order_id",       "INTEGER"),
        ],
        'borrow_records': [
            ("delivery_order_id", "INTEGER"),
            ("pickup_order_id",   "INTEGER"),
            ("return_requested_at", "DATE"),
        ],
    }

    for table, columns in additions.items():
        if table not in existing_tables:
            continue  # brand-new DB — create_all() will create it with every column already
        existing_cols = {c['name'] for c in inspector.get_columns(table)}
        for col_name, col_def in columns:
            if col_name in existing_cols:
                continue
            db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_def}'))
            print(f'[LibraryMS] Migrated: added {table}.{col_name}')
    db.session.commit()


def _seed_admin(User):
    """Create a default admin account if none exists."""
    from werkzeug.security import generate_password_hash
    from datetime import date

    if User.query.filter_by(role='admin').first():
        return

    admin = User(
        name        = 'Admin',
        email       = 'admin@library.com',
        phone       = '0000000000',
        password    = generate_password_hash('Admin@123'),
        role        = 'admin',
        library_code= 'ADMIN',
        is_active   = True,
        joined_date = date.today(),
    )
    db.session.add(admin)
    db.session.commit()
    print('[LibraryMS] Default admin created → email: admin@library.com  password: Admin@123')


def _grandfather_memberships(User):
    """
    One-time backfill for accounts created before the membership-tier
    feature existed. Existing members never paid a registration fee
    (the feature didn't exist yet), so blocking their borrowing on
    rollout would be a regression, not enforcement. Grandfather them
    in as paid-up Basic members with a fresh one-year renewal clock
    starting today, and log a ₹0 'paid' registration record so their
    payment history stays consistent with everyone else's.

    Idempotent: only touches users with membership_paid_until still
    NULL, so it's a no-op on every run after the first.
    """
    from datetime import date, timedelta
    from models.membership import MembershipPayment

    legacy_users = User.query.filter_by(
        role='user', membership_paid_until=None
    ).all()
    if not legacy_users:
        return

    today = date.today()
    for u in legacy_users:
        u.membership_type       = 'basic'
        u.membership_paid_until = today + timedelta(days=Config.MEMBERSHIP_VALID_DAYS)
        db.session.add(MembershipPayment(
            user_id         = u.id,
            payment_type    = 'registration',
            membership_type = 'basic',
            amount          = 0,
            status          = 'paid',
            notes           = 'Grandfathered in — pre-dates the membership fee feature.',
            issued_date     = today,
            paid_date       = today,
        ))
    db.session.commit()
    print(f'[LibraryMS] Grandfathered {len(legacy_users)} existing member(s) as paid Basic.')


if __name__ == '__main__':
    app = create_app()
    app.run(debug=True)
