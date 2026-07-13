from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_jwt_extended import JWTManager
from flask_wtf import CSRFProtect

db            = SQLAlchemy()
login_manager = LoginManager()
jwt           = JWTManager()
csrf          = CSRFProtect()
