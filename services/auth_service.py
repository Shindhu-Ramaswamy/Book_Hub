from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db
from models.user import User
from datetime import date


class AuthService:

    @staticmethod
    def register_user(name, email, phone, password,
                      role='user', library_code=None, membership_type='basic'):
        if User.query.filter_by(email=email).first():
            return None, 'Email already registered.'
        if role == 'librarian' and not library_code:
            return None, 'Library code is required for librarians.'
        if membership_type not in ('basic', 'membership'):
            return None, 'Invalid membership type.'

        user = User(
            name=name, email=email, phone=phone,
            password=generate_password_hash(password),
            role=role, library_code=library_code,
            joined_date=date.today(),
        )
        db.session.add(user)
        db.session.commit()

        # Members owe a registration fee before they can borrow.
        # Librarians/admins never pay membership fees.
        if role == 'user':
            from services.membership_service import MembershipService
            MembershipService.create_registration_charge(user)

            # Chose Membership at signup: the free basic registration
            # above already activated the account, so request_upgrade's
            # preconditions (active, no pending charge) are satisfied —
            # reuse it rather than duplicating the upgrade-charge logic.
            # Same ₹100 fee whether requested now or later from the
            # Membership page.
            if membership_type == 'membership':
                MembershipService.request_upgrade(user.id)

        return user, None

    @staticmethod
    def authenticate(email, password, role):
        user = User.query.filter_by(email=email, role=role).first()
        if not user:
            return None, 'No account found with that email.'
        if not user.is_active:
            return None, 'This account has been disabled by admin.'
        if not check_password_hash(user.password, password):
            return None, 'Incorrect password.'
        return user, None

    @staticmethod
    def update_profile(user, name, email, phone):
        dup_email = User.query.filter_by(email=email).first()
        if dup_email and dup_email.id != user.id:
            return 'Email already in use.'
        user.name = name; user.email = email
        user.phone = phone
        db.session.commit()
        return None

    @staticmethod
    def update_address(user, address_line1, address_line2, city, state, pincode, landmark):
        """
        Save/update the user's single default address — used to pre-fill
        delivery and return-pickup request forms. Kept as its own method
        (not folded into update_profile) so saving one never risks
        clobbering the other from a form that doesn't submit both.
        """
        user.address_line1  = address_line1 or None
        user.address_line2  = address_line2 or None
        user.address_city   = city or None
        user.address_state  = state or None
        user.address_pincode = pincode or None
        user.address_landmark = landmark or None
        db.session.commit()
        return None
