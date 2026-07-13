"""
services/scheduler.py
=====================
APScheduler setup for LibraryMS.

Key design change from the original version
-------------------------------------------
Previously every job function received `app` as a parameter and called
`with app.app_context()` itself.  That meant:
  - Every job function knew about Flask internals
  - The app was passed around as a closure variable
  - Adding a new job meant remembering to wrap it in app_context

Now:
  - `_with_context(app, fn)` is a single factory that wraps any callable
    in an app context.  Job functions receive no arguments — they are
    pure business logic.
  - Hooks (EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, etc.) are registered
    via services/hooks.py — see that file for the full explanation.
  - start_scheduler() wires everything together and is the only function
    app.py needs to call.

Jobs
----
  daily_fine_check    00:05 every night     scan overdue, create/update fines
  hourly_fine_sync    every 60 minutes      update amounts on unpaid fines
  reservation_expiry  every 15 minutes      expire stale reservation holds
"""

import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron         import CronTrigger
from apscheduler.triggers.interval     import IntervalTrigger

from services.hooks import register_hooks

log = logging.getLogger('lms.scheduler')


# ── App-context wrapper ───────────────────────────────────────────────

def _with_context(app, fn):
    """
    Return a zero-argument callable that pushes a Flask app context,
    calls fn(), then pops the context.

    This is the ONLY place app context management lives.
    Job functions themselves stay clean.
    """
    def wrapper():
        with app.app_context():
            fn()
    return wrapper


# ── Fine helpers ─────────────────────────────────────────────────────

def _calc_fine(overdue_days: int, fine_per_day: int,
               grace_days: int, max_amount: int) -> float:
    billable = max(0, overdue_days - grace_days)
    return min(billable * fine_per_day, max_amount)


# ── Job functions (pure — no app, no context) ────────────────────────

def job_daily_fine_check():
    """
    Nightly scan: create or update OverdueRecord for every borrowed
    BorrowRecord that is past its due date.
    Called inside an app context by _with_context.
    """
    from extensions         import db
    from models.transaction import BorrowRecord
    from models.overdue     import OverdueRecord
    from config             import Config

    today            = date.today()
    created = updated = skipped = 0

    overdue_records = BorrowRecord.query.filter(
        BorrowRecord.status   == 'borrowed',
        BorrowRecord.due_date <  today,
    ).all()

    for record in overdue_records:
        overdue_days = (today - record.due_date).days
        fine_amount  = _calc_fine(
            overdue_days,
            Config.FINE_PER_DAY,
            Config.FINE_GRACE_DAYS,
            Config.FINE_MAX_AMOUNT,
        )

        if fine_amount <= 0:
            skipped += 1
            continue

        existing = OverdueRecord.query.filter_by(
            borrow_id   = record.id,
            fine_status = 'unpaid',
        ).first()

        if existing:
            if existing.amount != fine_amount:
                log.debug(
                    'Fine updated BR#%d: ₹%.0f → ₹%.0f (%d days)',
                    record.id, existing.amount, fine_amount, overdue_days,
                )
                existing.amount = fine_amount
                updated += 1
            else:
                skipped += 1
        else:
            db.session.add(OverdueRecord(
                borrow_id   = record.id,
                user_id     = record.user_id,
                amount      = fine_amount,
                fine_status = 'unpaid',
                issued_date = today,
            ))
            log.info(
                'Fine created BR#%d user=%d book="%s" ₹%.0f (%d days)',
                record.id, record.user_id,
                record.book.title if record.book else '?',
                fine_amount, overdue_days,
            )
            created += 1

    db.session.commit()
    log.info(
        '[daily_fine_check %s] created=%d updated=%d skipped=%d',
        today.isoformat(), created, updated, skipped,
    )


def job_hourly_fine_sync():
    """
    Lightweight hourly sync: update amount on existing unpaid fines
    so the dashboard stays accurate between midnight runs.
    Does NOT create new OverdueRecords.
    """
    from extensions     import db
    from models.overdue import OverdueRecord
    from config         import Config

    today   = date.today()
    updated = 0

    for fine in OverdueRecord.query.filter_by(fine_status='unpaid').all():
        br = fine.borrow_record
        if not br or br.status != 'borrowed' or not br.due_date:
            continue
        overdue_days = max(0, (today - br.due_date).days)
        new_amount   = _calc_fine(
            overdue_days,
            Config.FINE_PER_DAY,
            Config.FINE_GRACE_DAYS,
            Config.FINE_MAX_AMOUNT,
        )
        if fine.amount != new_amount:
            fine.amount = new_amount
            updated += 1

    db.session.commit()
    if updated:
        log.info(
            '[hourly_fine_sync %s] updated=%d unpaid fines',
            datetime.now(timezone.utc).isoformat(timespec='seconds'),
            updated,
        )


def job_reservation_expiry():
    """
    Expire 'ready' reservations whose hold window has passed and
    promote the next person in each affected queue.
    """
    from services.reservation_service import ReservationService
    count = ReservationService.expire_stale_holds()
    if count:
        log.info('[reservation_expiry] expired %d hold(s)', count)


def job_due_reminders():
    """
    Every morning at 07:00 — send due-date reminders.
      3 days left  → due_reminder_3
      1 day left   → due_reminder_1
    Skips users who already got the same reminder today (idempotent).
    """
    from models.transaction  import BorrowRecord
    from models.notification import Notification
    from services.notification_service import NotificationService
    from datetime import timedelta

    today       = date.today()
    in_3_days   = today + timedelta(days=3)
    tomorrow    = today + timedelta(days=1)
    sent3 = sent1 = 0

    # 3-day reminder
    for r in BorrowRecord.query.filter_by(status='borrowed', due_date=in_3_days).all():
        already = Notification.query.filter(
            Notification.user_id    == r.user_id,
            Notification.borrow_id  == r.id,
            Notification.notif_type == 'due_reminder_3',
        ).first()
        if not already:
            NotificationService.due_reminder_3(r)
            sent3 += 1

    # 1-day reminder
    for r in BorrowRecord.query.filter_by(status='borrowed', due_date=tomorrow).all():
        already = Notification.query.filter(
            Notification.user_id    == r.user_id,
            Notification.borrow_id  == r.id,
            Notification.notif_type == 'due_reminder_1',
        ).first()
        if not already:
            NotificationService.due_reminder_1(r)
            sent1 += 1

    log.info('[due_reminders %s] 3-day=%d 1-day=%d', today.isoformat(), sent3, sent1)


def job_overdue_alerts():
    """
    Every morning at 08:00 — send one overdue alert per overdue book.
    Only sends if no overdue_alert was sent TODAY for that record
    (prevents duplicate alerts on re-run).
    """
    from models.transaction  import BorrowRecord
    from models.notification import Notification
    from services.notification_service import NotificationService

    today = date.today()
    sent  = 0

    for r in BorrowRecord.query.filter(
        BorrowRecord.status   == 'borrowed',
        BorrowRecord.due_date <  today,
    ).all():
        # Check if we already sent an overdue_alert for this record today
        already = Notification.query.filter(
            Notification.user_id    == r.user_id,
            Notification.borrow_id  == r.id,
            Notification.notif_type == 'overdue_alert',
            # created_at >= today midnight
            Notification.created_at >= datetime.combine(today, datetime.min.time())
                                                .replace(tzinfo=timezone.utc),
        ).first()
        if not already:
            NotificationService.overdue_alert(r)
            sent += 1

    log.info('[overdue_alerts %s] sent=%d', today.isoformat(), sent)


def job_membership_renewal_check():
    """
    Nightly scan: raise a 'renewal' MembershipPayment for every member
    whose membership_paid_until has passed and who doesn't already
    have an unpaid charge outstanding. Borrowing stays blocked
    (User.membership_active) until it's paid — this job only makes the
    charge visible; it doesn't touch is_active or anything else.
    """
    from models.user import User
    from services.membership_service import MembershipService

    today   = date.today()
    expired = User.query.filter(
        User.role == 'user',
        User.membership_paid_until.isnot(None),
        User.membership_paid_until < today,
    ).all()

    raised = 0
    for user in expired:
        payment = MembershipService.create_renewal_charge(user)
        if payment:
            raised += 1

    log.info('[membership_renewal_check %s] raised=%d', today.isoformat(), raised)


# ── Public: start scheduler ──────────────────────────────────────────

def start_scheduler(app):
    """
    Create the BackgroundScheduler, register hooks, add jobs, start.
    Returns the running scheduler instance.

    Call once from create_app() — guarded by the reloader check in
    app.py so it only runs in the main process.
    """
    scheduler = BackgroundScheduler(
        timezone     = 'Asia/Kolkata',
        job_defaults = {
            'coalesce':           True,
            'max_instances':      1,
            'misfire_grace_time': 3600,
        },
    )

    # ── Register event hooks BEFORE start() ──────────────────────
    register_hooks(scheduler)

    # ── Add jobs — functions are wrapped in app context ──────────
    scheduler.add_job(
        func             = _with_context(app, job_daily_fine_check),
        trigger          = CronTrigger(hour=0, minute=5),
        id               = 'daily_fine_check',
        name             = 'Daily fine check',
        replace_existing = True,
    )

    scheduler.add_job(
        func             = _with_context(app, job_hourly_fine_sync),
        trigger          = IntervalTrigger(hours=1),
        id               = 'hourly_fine_sync',
        name             = 'Hourly fine sync',
        replace_existing = True,
    )

    scheduler.add_job(
        func             = _with_context(app, job_reservation_expiry),
        trigger          = IntervalTrigger(minutes=15),
        id               = 'reservation_expiry',
        name             = 'Reservation hold expiry',
        replace_existing = True,
    )

    scheduler.add_job(
        func             = _with_context(app, job_due_reminders),
        trigger          = CronTrigger(hour=7, minute=0),
        id               = 'due_reminders',
        name             = 'Due date reminders (3d & 1d)',
        replace_existing = True,
    )

    scheduler.add_job(
        func             = _with_context(app, job_overdue_alerts),
        trigger          = CronTrigger(hour=8, minute=0),
        id               = 'overdue_alerts',
        name             = 'Daily overdue alerts',
        replace_existing = True,
    )

    scheduler.add_job(
        func             = _with_context(app, job_membership_renewal_check),
        trigger          = CronTrigger(hour=0, minute=10),
        id               = 'membership_renewal_check',
        name             = 'Membership renewal check',
        replace_existing = True,
    )

    scheduler.start()
    log.info(
        '[LibraryMS] Scheduler started | '
        'daily_fine_check 00:05 | '
        'hourly_fine_sync every 1h | '
        'reservation_expiry every 15min | '
        'due_reminders 07:00 | '
        'overdue_alerts 08:00 | '
        'membership_renewal_check 00:10'
    )

    # Run fine check immediately on startup so fines are visible
    # before the first midnight run
    try:
        with app.app_context():
            job_daily_fine_check()
        log.info('[LibraryMS] Startup fine check completed')
    except Exception as exc:
        log.warning('[LibraryMS] Startup fine check failed: %s', exc)

    return scheduler
