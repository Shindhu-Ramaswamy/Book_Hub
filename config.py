import os
from datetime import timedelta

class Config:
    # ── Core ──────────────────────────────────────────────────
    SECRET_KEY = os.environ.get('c0e06fa8ef79281eb4562e8dfc62bb0d5ef65a4da353c26391ff65a4433dd53c') or 'library-secret-key-2024'
    BASE_DIR   = os.path.abspath(os.path.dirname(__file__))

    # ── Database ───────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI     = 'sqlite:///' + os.path.join(BASE_DIR, 'library.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── JWT ────────────────────────────────────────────────────
    JWT_SECRET_KEY              = os.environ.get('aa0c04e4d77c507b91ba4f2f32c203348c9076970fb239263f617d28e1effc5b') or 'jwt-library-secret-2024'
    JWT_ACCESS_TOKEN_EXPIRES    = timedelta(hours=1)
    JWT_REFRESH_TOKEN_EXPIRES   = timedelta(days=30)
    JWT_TOKEN_LOCATION          = ['headers']
    JWT_HEADER_NAME             = 'Authorization'
    JWT_HEADER_TYPE             = 'Bearer'

    # ── Business rules ─────────────────────────────────────────
    FINE_PER_DAY      = 10    # ₹ per overdue day
    FINE_GRACE_DAYS   = 0     # free days after due date before fine starts (0 = strict)
    FINE_MAX_AMOUNT   = 500   # ₹ cap — fine never exceeds this per book

    # ── Membership tiers ──────────────────────────────────────────
    # 'basic' is the default tier every new member registers into.
    # 'membership' is a paid upgrade on top of basic (upgrade_fee is
    # charged in addition to whatever basic already cost — it is not
    # a replacement price). Both tiers must be renewed annually;
    # max_books/borrow_days are read per-user from whichever tier
    # they're currently in via User.max_books / User.borrow_days.
    MEMBERSHIP_VALID_DAYS = 365   # how long one payment (registration or renewal) lasts
    MEMBERSHIP_RULES = {
        'basic': {
            'label':            'Basic',
            'registration_fee': 0,     # fresh registration is free
            'renewal_fee':      80,
            'max_books':        3,
            'borrow_days':      30,
        },
        'membership': {
            'label':            'Membership',
            'upgrade_fee':      100,   # basic → membership, charged on top of basic
            'renewal_fee':      200,
            'max_books':        5,
            'borrow_days':      60,
        },
    }

    # ── Return-condition charges ─────────────────────────────────
    DAMAGE_CHARGE      = 200  # ₹ default charge when a returned book is damaged
    LOST_BOOK_CHARGE   = 500  # ₹ default charge when a returned book is lost

    # ── Reservation ────────────────────────────────────────────
    RESERVATION_HOLD_HOURS    = 48   # hours user has to collect after being promoted
    MAX_RESERVATIONS_PER_USER = 3    # max active reservations per user at once

    # ── Home delivery ─────────────────────────────────────────
    DELIVERY_BASE_FEE     = 30   # ₹ flat fee per delivery order
    DELIVERY_FEE_PER_BOOK = 10   # ₹ additional per book in the order

    # ── Return pickup ─────────────────────────────────────────
    # Waived entirely when the book being returned was originally
    # home-delivered — that delivery fee already covers the round trip.
    # Only charged fresh when the book was borrowed at the counter.
    PICKUP_BASE_FEE     = 30   # ₹ flat fee per pickup order
    PICKUP_FEE_PER_BOOK = 10   # ₹ additional per book in the order

    # ── Payments (Razorpay) ──────────────────────────────────────
    # Get these from the Razorpay Dashboard → Settings → API Keys.
    # Test-mode keys start with 'rzp_test_'; live keys with 'rzp_live_'.
    # No hardcoded fallback on purpose — unlike SECRET_KEY, these are
    # real payment credentials, so we want a loud, obvious failure if
    # they're missing rather than something that silently half-works.
    RAZORPAY_KEY_ID         = os.environ.get('RAZORPAY_KEY_ID')
    RAZORPAY_KEY_SECRET     = os.environ.get('RAZORPAY_KEY_SECRET')
    RAZORPAY_WEBHOOK_SECRET = os.environ.get('RAZORPAY_WEBHOOK_SECRET')
