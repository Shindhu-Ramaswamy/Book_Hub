"""
POST /api/auth/register      — register a new member
POST /api/auth/login         — get access + refresh tokens
POST /api/auth/refresh       — get new access token using refresh token
GET  /api/auth/me            — get current user info (requires access token)
POST /api/auth/logout        — client-side: just discard tokens (stateless JWT)
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token, create_refresh_token,
    jwt_required, get_jwt_identity, get_jwt,
)
from services.auth_service import AuthService
from models.user import User

auth_api = Blueprint('auth_api', __name__)


def _json_error(message, status=400):
    return jsonify({'success': False, 'error': message}), status


def _json_ok(data, status=200):
    return jsonify({'success': True, **data}), status


# ── Register ──────────────────────────────────────────────────────────
@auth_api.route('/register', methods=['POST'])
def register():
    body = request.get_json(silent=True) or {}
    required = ['name', 'email', 'phone', 'password']
    missing  = [f for f in required if not body.get(f)]
    if missing:
        return _json_error(f'Missing fields: {", ".join(missing)}')

    user, err = AuthService.register_user(
        name     = body['name'],
        email    = body['email'],
        phone    = body['phone'],
        password = body['password'],
        role     = 'user',
        membership_type = body.get('membership_type', 'basic'),
    )
    if err:
        return _json_error(err)
    return _json_ok({'message': 'Registration successful.', 'user': user.to_dict()}, 201)


# ── Login ─────────────────────────────────────────────────────────────
@auth_api.route('/login', methods=['POST'])
def login():
    body = request.get_json(silent=True) or {}
    email    = body.get('email',    '').strip()
    password = body.get('password', '')
    role     = body.get('role',     'user')   # user | librarian | admin

    if not email or not password:
        return _json_error('email and password are required.')
    if role not in ('user', 'librarian', 'admin'):
        return _json_error('role must be user, librarian, or admin.')

    user, err = AuthService.authenticate(email, password, role)
    if err:
        return _json_error(err, 401)

    # Identity stored in token = user id (string)
    access_token  = create_access_token(identity=str(user.id),
                                        additional_claims={'role': user.role})
    refresh_token = create_refresh_token(identity=str(user.id),
                                         additional_claims={'role': user.role})

    return _json_ok({
        'access_token':  access_token,
        'refresh_token': refresh_token,
        'user':          user.to_dict(),
    })


# ── Refresh ───────────────────────────────────────────────────────────
@auth_api.route('/refresh', methods=['POST'])
@jwt_required(refresh=True)
def refresh():
    identity   = get_jwt_identity()
    claims     = get_jwt()
    new_access = create_access_token(identity=identity,
                                     additional_claims={'role': claims.get('role')})
    return _json_ok({'access_token': new_access})


# ── Me ────────────────────────────────────────────────────────────────
@auth_api.route('/me', methods=['GET'])
@jwt_required()
def me():
    user_id = get_jwt_identity()
    user    = User.query.get_or_404(int(user_id))
    return _json_ok({'user': user.to_dict()})


# ── Logout ────────────────────────────────────────────────────────────
@auth_api.route('/logout', methods=['POST'])
@jwt_required()
def logout():
    # JWT is stateless — client must delete tokens on their side.
    # For server-side revocation you would add token jti to a blocklist here.
    return _json_ok({'message': 'Logged out. Please discard your tokens.'})
