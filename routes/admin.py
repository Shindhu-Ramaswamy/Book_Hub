from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user
from functools import wraps
from services.admin_service import AdminService

admin = Blueprint('admin', __name__)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role != 'admin':
            flash('Access denied.', 'danger')
            return redirect(url_for('auth.landing'))
        return f(*args, **kwargs)
    return decorated


@admin.route('/home')
@login_required
@admin_required
def home():
    stats = AdminService.dashboard_stats()
    return render_template('admin/home.html', title='Admin Dashboard', **stats)


@admin.route('/users')
@login_required
@admin_required
def users():
    return render_template('admin/users.html', title='All Members',
                           users=AdminService.all_users())


@admin.route('/librarians')
@login_required
@admin_required
def librarians():
    return render_template('admin/librarians.html', title='Librarians',
                           librarians=AdminService.all_librarians())


@admin.route('/librarians/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_librarian():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    if request.method == 'POST':
        user, err = AdminService.create_librarian(
            name=request.form['name'], email=request.form['email'],
            phone=request.form['phone'],
            password=request.form['password'],
            library_code=request.form['library_code'],
        )
        if is_ajax:
            if not err:
                flash(f'Librarian {user.name} created.', 'success')
            return jsonify({
                'success': not bool(err),
                'message': err if err else f'Librarian {user.name} created.',
            })
        if err:
            flash(err, 'danger')
            return redirect(url_for('admin.add_librarian'))
        flash(f'Librarian {user.name} created.', 'success')
        return redirect(url_for('admin.librarians'))
    if is_ajax:
        return render_template('admin/_add_librarian_fragment.html')
    return render_template('admin/add_librarian.html', title='Add Librarian')


@admin.route('/librarians/toggle/<int:lib_id>', methods=['POST'])
@login_required
@admin_required
def toggle_librarian(lib_id):
    lib = AdminService.toggle_librarian(lib_id)
    status = 'enabled' if lib.is_active else 'disabled'
    flash(f'{lib.name} has been {status}.', 'success')
    return redirect(url_for('admin.librarians'))


@admin.route('/books')
@login_required
@admin_required
def books():
    return render_template('admin/books.html', title='All Books',
                           books=AdminService.all_books())


# ── Fine / Scheduler management ────────────────────────────────────────
@admin.route('/fines')
@login_required
@admin_required
def fines():
    from models.overdue     import OverdueRecord
    from models.transaction import BorrowRecord
    from services.hooks     import get_job_history
    from datetime import date

    all_fines    = OverdueRecord.query.order_by(
        OverdueRecord.fine_status.asc(),
        OverdueRecord.issued_date.desc()
    ).all()
    live_overdue = [r for r in BorrowRecord.query.filter_by(status='borrowed').all()
                    if r.is_overdue]
    total_unpaid = sum(f.amount for f in all_fines if f.fine_status == 'unpaid')
    total_paid   = sum(f.amount for f in all_fines if f.fine_status == 'paid')
    job_history  = get_job_history()

    return render_template(
        'admin/fines.html',
        title        = 'Fine Management',
        all_fines    = all_fines,
        live_overdue = live_overdue,
        total_unpaid = total_unpaid,
        total_paid   = total_paid,
        today        = date.today(),
        job_history  = job_history,
    )


@admin.route('/fines/run-now', methods=['POST'])
@login_required
@admin_required
def run_fine_check_now():
    """Manually trigger the daily fine check right now."""
    try:
        from services.scheduler import job_daily_fine_check
        job_daily_fine_check()
        flash('Fine check completed successfully.', 'success')
    except Exception as e:
        flash(f'Fine check failed: {e}', 'danger')
    return redirect(url_for('admin.fines'))

