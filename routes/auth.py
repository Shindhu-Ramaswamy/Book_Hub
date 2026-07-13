from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required
from services.auth_service import AuthService
from config import Config

auth = Blueprint('auth', __name__)


@auth.route('/')
def landing():
    return render_template('landing.html')


@auth.route('/login/user', methods=['GET', 'POST'])
def login_user_page():
    if request.method == 'POST':
        user, err = AuthService.authenticate(
            request.form['email'], request.form['password'], 'user')
        if err:
            flash(err, 'danger')
            return redirect(url_for('auth.login_user_page'))
        login_user(user)
        return redirect(url_for('user.home'))
    return render_template('auth/login_user.html')


@auth.route('/register/user', methods=['GET', 'POST'])
def register_user_page():
    if request.method == 'POST':
        membership_type = request.form.get('membership_type', 'basic')
        user, err = AuthService.register_user(
            name=request.form['name'], email=request.form['email'],
            phone=request.form['phone'],
            password=request.form['password'], role='user',
            membership_type=membership_type,
        )
        if err:
            flash(err, 'danger')
            return redirect(url_for('auth.register_user_page'))

        if membership_type == 'membership':
            # Skip the "please sign in" round-trip — log them straight in
            # and take them to the payment step for the upgrade fee that
            # register_user() just raised. If they close the tab or the
            # payment fails, nothing here ever set membership_type to
            # 'membership' (that only happens on a verified payment), so
            # the account simply stays on the free Basic tier they were
            # already granted.
            login_user(user)
            flash('Account created — pay the membership fee to activate your upgrade.', 'success')
            return redirect(url_for('user.membership', autopay=1))

        flash('Account created! Please sign in.', 'success')
        return redirect(url_for('auth.login_user_page'))
    return render_template('auth/register_user.html',
                           basic_rules=Config.MEMBERSHIP_RULES['basic'],
                           membership_rules=Config.MEMBERSHIP_RULES['membership'])


@auth.route('/login/librarian', methods=['GET', 'POST'])
def login_librarian_page():
    if request.method == 'POST':
        user, err = AuthService.authenticate(
            request.form['email'], request.form['password'], 'librarian')
        if err:
            flash(err, 'danger')
            return redirect(url_for('auth.login_librarian_page'))
        login_user(user)
        return redirect(url_for('librarian.home'))
    return render_template('auth/login_librarian.html')


@auth.route('/login/admin', methods=['GET', 'POST'])
def login_admin_page():
    if request.method == 'POST':
        user, err = AuthService.authenticate(
            request.form['email'], request.form['password'], 'admin')
        if err:
            flash(err, 'danger')
            return redirect(url_for('auth.login_admin_page'))
        login_user(user)
        return redirect(url_for('admin.home'))
    return render_template('auth/login_admin.html')


@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.landing'))
