"""Canonical SQLAlchemy models and conservative schema upgrades for Mockify."""

import os
from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()


class OTPFields:
    otp_hash = db.Column(db.String(256))
    otp_expires_at = db.Column(db.DateTime)
    otp_attempts = db.Column(db.Integer, default=0, nullable=False)
    otp_last_sent_at = db.Column(db.DateTime)
    otp_request_count = db.Column(db.Integer, default=0, nullable=False)
    otp_request_window_started_at = db.Column(db.DateTime)

    def clear_otp(self):
        self.otp_hash = self.otp_expires_at = self.otp_last_sent_at = None
        self.otp_attempts = 0


class User(db.Model, OTPFields):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    email_verified = db.Column(db.Boolean, default=True, nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_super_admin = db.Column(db.Boolean, default=False, nullable=False)
    daily_mock_limit = db.Column(db.Integer, default=3, nullable=False)
    mocks_taken_today = db.Column(db.Integer, default=0, nullable=False)
    last_reset_date = db.Column(db.Date, default=date.today, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = db.Column(db.DateTime, index=True)
    last_login_at = db.Column(db.DateTime, index=True)
    mocks = db.relationship("Mock", backref="user", lazy=True, cascade="all, delete-orphan", passive_deletes=True)
    results = db.relationship("Result", backref="user", lazy=True, cascade="all, delete-orphan", passive_deletes=True)
    otp_events = db.relationship("OTPEvent", backref="user", lazy=True, cascade="all, delete-orphan", passive_deletes=True)
    feedback = db.relationship("Feedback", backref="user", lazy=True, cascade="all, delete-orphan", passive_deletes=True)

    def reset_daily_count_if_needed(self):
        if self.last_reset_date != date.today():
            self.mocks_taken_today, self.last_reset_date = 0, date.today()
            db.session.commit()

    def can_take_mock(self):
        self.reset_daily_count_if_needed()
        return self.is_admin or self.mocks_taken_today < self.daily_mock_limit

    def to_dict(self):
        self.reset_daily_count_if_needed()
        return {
            "id": self.id, "name": self.name, "email": self.email,
            "is_admin": self.is_admin, "is_super_admin": self.is_super_admin,
            "email_verified": self.email_verified, "daily_mock_limit": self.daily_mock_limit,
            "mocks_taken_today": self.mocks_taken_today,
            "mocks_remaining": None if self.is_admin else max(0, self.daily_mock_limit - self.mocks_taken_today),
            "can_take_mock": self.can_take_mock(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class PendingOTP(db.Model, OTPFields):
    __tablename__ = "pending_otps"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    pending_name = db.Column(db.String(100), nullable=False)


class OTPEvent(db.Model):
    __tablename__ = "otp_events"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email = db.Column(db.String(150), nullable=False, index=True)
    event_type = db.Column(db.String(50), nullable=False, default="verification")
    status = db.Column(db.String(30), nullable=False, default="requested")
    brevo_message_id = db.Column(db.String(500))
    requested_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    ip_address = db.Column(db.String(64))


class Mock(db.Model):
    __tablename__ = "mocks"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    topic = db.Column(db.String(500), nullable=False)
    questions = db.Column(db.Text, nullable=False)
    timer_minutes = db.Column(db.Integer, nullable=False, default=15)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    results = db.relationship("Result", backref="mock", lazy=True, cascade="all, delete-orphan", passive_deletes=True)


class Result(db.Model):
    __tablename__ = "results"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    mock_id = db.Column(db.Integer, db.ForeignKey("mocks.id", ondelete="CASCADE"), nullable=False, index=True)
    score = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, default=10, nullable=False)
    correct_answers = db.Column(db.Integer, nullable=False)
    wrong_answers = db.Column(db.Integer, nullable=False)
    user_answers = db.Column(db.Text, nullable=False)
    explanations = db.Column(db.Text, nullable=False, default="{}")
    time_taken = db.Column(db.Integer, default=0, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class Feedback(db.Model):
    __tablename__ = "feedback"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), nullable=False, index=True)
    category = db.Column(db.String(30), nullable=False, default="general")
    message = db.Column(db.Text, nullable=False)
    image_name = db.Column(db.String(255))
    image_mime = db.Column(db.String(100))
    image_size = db.Column(db.Integer)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


def _column_additions():
    pg = db.engine.dialect.name == "postgresql"
    boolean = "BOOLEAN NOT NULL DEFAULT " + ("TRUE" if pg else "1")
    datetime_type = "TIMESTAMP" if pg else "DATETIME"
    return {
        "users": {
            "email_verified": boolean, "is_super_admin": boolean, "otp_hash": "VARCHAR(256)",
            "otp_expires_at": datetime_type, "otp_attempts": "INTEGER NOT NULL DEFAULT 0",
            "otp_last_sent_at": datetime_type, "otp_request_count": "INTEGER NOT NULL DEFAULT 0",
            "otp_request_window_started_at": datetime_type, "last_seen_at": datetime_type,
            "last_login_at": datetime_type,
        },
        "otp_events": {
            "event_type": "VARCHAR(50) NOT NULL DEFAULT 'verification'",
            "ip_address": "VARCHAR(64)",
        },
        "mocks": {"timer_minutes": "INTEGER NOT NULL DEFAULT 15"},
        "results": {"explanations": "TEXT NOT NULL DEFAULT '{}'"},
    }


def _upgrade_existing_tables():
    inspector = inspect(db.engine)
    for table, columns in _column_additions().items():
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, definition in columns.items():
            if name not in existing:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
    false = "FALSE" if db.engine.dialect.name == "postgresql" else "0"
    db.session.execute(text(f"UPDATE users SET daily_mock_limit = 3 WHERE is_admin = {false} AND daily_mock_limit = 5"))
    db.session.commit()


def init_db():
    db.create_all()
    _upgrade_existing_tables()
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if email and password and not User.query.filter_by(email=email).first():
        from werkzeug.security import generate_password_hash
        db.session.add(User(
            name=os.environ.get("BOOTSTRAP_ADMIN_NAME", "Administrator").strip() or "Administrator",
            email=email,
            password_hash=generate_password_hash(password),
            email_verified=True,
            is_admin=True,
            is_super_admin=os.environ.get("BOOTSTRAP_SUPER_ADMIN", "false").lower() == "true",
            daily_mock_limit=999,
        ))
        db.session.commit()
