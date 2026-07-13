"""
services/hooks.py
=================
APScheduler event hooks for the LibraryMS scheduler.

What this file does
-------------------
Registers listeners on the scheduler for every event type that matters:

    EVENT_JOB_SUBMITTED    → job picked up from the queue, about to run
    EVENT_JOB_EXECUTED     → job completed without raising an exception
    EVENT_JOB_ERROR        → job raised an exception during execution
    EVENT_JOB_MISSED       → job's scheduled time passed without running
                             (usually because the server was down)
    EVENT_JOB_MAX_INSTANCES→ job was skipped because max_instances was reached
                             (previous run still going)

Why hooks instead of inline logging inside each job
----------------------------------------------------
1.  Jobs stay pure: _run_daily_fine_check, _run_hourly_fine_sync,
    _run_reservation_expiry contain only business logic.
    They never know about the scheduler, events, or logging format.

2.  Single place to add cross-cutting concerns — email alerts on errors,
    metrics, Slack notifications — without touching job code.

3.  APScheduler already fires these events; we are just listening.
    The events carry the job_id, scheduled_run_time, retval, and
    exception so hooks have full context without any extra plumbing.

4.  Missed-job detection is impossible inside a job function because
    the function never ran. Only a listener on EVENT_JOB_MISSED can
    catch it.

Hook registration
-----------------
Call register_hooks(scheduler) once, right after the scheduler is
created and before scheduler.start().  That's it.

Execution model
---------------
Listeners run in the same thread as the scheduler's event dispatch,
so keep them fast (log, push to a queue, set a flag).
Heavy work (e.g. sending emails) should be offloaded to a thread.
"""

import logging
import threading
from datetime import datetime, timezone

from apscheduler.events import (
    EVENT_JOB_SUBMITTED,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_ERROR,
    EVENT_JOB_MISSED,
    EVENT_JOB_MAX_INSTANCES,
)

log = logging.getLogger('lms.scheduler.hooks')

# ── In-memory job run history (last 50 executions per job) ──────────
# Useful for the admin dashboard "scheduler health" view.
# Keys are job_id strings; values are lists of dicts.
_JOB_HISTORY: dict[str, list] = {}
_HISTORY_LOCK = threading.Lock()
MAX_HISTORY = 50


def _record(job_id: str, entry: dict):
    with _HISTORY_LOCK:
        history = _JOB_HISTORY.setdefault(job_id, [])
        history.append(entry)
        if len(history) > MAX_HISTORY:
            history.pop(0)


def get_job_history(job_id: str | None = None) -> dict:
    """
    Public accessor for the admin view.
    Returns {job_id: [run_entry, ...]} or a single job's list.
    """
    with _HISTORY_LOCK:
        if job_id:
            return {job_id: list(_JOB_HISTORY.get(job_id, []))}
        return {k: list(v) for k, v in _JOB_HISTORY.items()}


# ── Individual hook functions ────────────────────────────────────────

def _on_submitted(event):
    """
    Fired when the scheduler picks up a job and submits it to the
    thread pool, before the job function actually starts.
    Use this if you need to measure queue-wait time.
    """
    log.debug(
        '[SUBMITTED] job_id=%s scheduled=%s',
        event.job_id,
        event.scheduled_run_time.isoformat() if event.scheduled_run_time else '—',
    )


def _on_executed(event):
    """
    Fired when a job function returns normally (no exception).
    event.retval contains whatever the job function returned.
    """
    now = datetime.now(timezone.utc)
    log.info(
        '[EXECUTED] job_id=%-28s  finished=%s',
        event.job_id,
        now.isoformat(timespec='seconds'),
    )
    _record(event.job_id, {
        'status':       'executed',
        'scheduled_at': event.scheduled_run_time.isoformat() if event.scheduled_run_time else None,
        'finished_at':  now.isoformat(),
        'retval':       str(event.retval) if event.retval is not None else None,
    })


def _on_error(event):
    """
    Fired when a job function raises an unhandled exception.
    event.exception is the Exception instance.
    event.traceback is the formatted traceback string.

    This is the hook to extend with email/Slack alerts in production.
    """
    now = datetime.now(timezone.utc)
    log.error(
        '[ERROR] job_id=%s raised %s: %s',
        event.job_id,
        type(event.exception).__name__,
        event.exception,
    )
    # Log full traceback at DEBUG so it doesn't spam production logs
    # but is available when you need it.
    if event.traceback:
        log.debug('[ERROR traceback]\n%s', event.traceback)

    _record(event.job_id, {
        'status':       'error',
        'scheduled_at': event.scheduled_run_time.isoformat() if event.scheduled_run_time else None,
        'finished_at':  now.isoformat(),
        'error':        f'{type(event.exception).__name__}: {event.exception}',
    })

    # ── Extension point ───────────────────────────────────────────
    # Uncomment and configure to send an email / Slack message:
    #
    # _alert_admin(
    #     subject=f'[LibraryMS] Scheduler job {event.job_id} failed',
    #     body=event.traceback or str(event.exception),
    # )


def _on_missed(event):
    """
    Fired when a job's scheduled run time passed without it running —
    typically because the server was down or the process was busy.
    APScheduler will run it as soon as possible if misfire_grace_time
    hasn't elapsed; otherwise it is dropped entirely.

    This event is impossible to catch inside the job function, so
    it MUST be caught here.
    """
    now = datetime.now(timezone.utc)
    log.warning(
        '[MISSED] job_id=%s  was scheduled for %s',
        event.job_id,
        event.scheduled_run_time.isoformat() if event.scheduled_run_time else '—',
    )
    _record(event.job_id, {
        'status':       'missed',
        'scheduled_at': event.scheduled_run_time.isoformat() if event.scheduled_run_time else None,
        'finished_at':  now.isoformat(),
    })


def _on_max_instances(event):
    """
    Fired when a job is skipped because max_instances is already running.
    With max_instances=1 (our setting) this means the previous run
    hasn't finished yet when the next trigger fires.

    If you see this frequently for daily_fine_check it means the DB
    query is taking longer than 24 hours — which should never happen
    but is worth knowing about.
    """
    now = datetime.now(timezone.utc)
    log.warning(
        '[MAX_INSTANCES] job_id=%s skipped — previous run still active',
        event.job_id,
    )
    _record(event.job_id, {
        'status':       'max_instances',
        'scheduled_at': event.scheduled_run_time.isoformat() if event.scheduled_run_time else None,
        'finished_at':  now.isoformat(),
    })


# ── Public registration function ─────────────────────────────────────

def register_hooks(scheduler):
    """
    Attach all listeners to the scheduler.
    Call this once after creating the scheduler, before scheduler.start().

    Each add_listener call maps one or more event mask(s) to a function.
    Multiple masks can be OR-ed together.
    """
    scheduler.add_listener(_on_submitted,     EVENT_JOB_SUBMITTED)
    scheduler.add_listener(_on_executed,      EVENT_JOB_EXECUTED)
    scheduler.add_listener(_on_error,         EVENT_JOB_ERROR)
    scheduler.add_listener(_on_missed,        EVENT_JOB_MISSED)
    scheduler.add_listener(_on_max_instances, EVENT_JOB_MAX_INSTANCES)

    log.info('[LibraryMS] Scheduler hooks registered (submitted/executed/error/missed/max_instances)')
