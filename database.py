from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text


db = SQLAlchemy()


# =========================================================
# OTP FIELDS
# =========================================================

class OTPFields:
    otp_hash = db.Column(db.String(256))
    otp_expires_at = db.Column(db.DateTime)
    otp_attempts = db.Column(
        db.Integer,
        default=0,
        nullable=False
    )
    otp_last_sent_at = db.Column(db.DateTime)
    otp_request_count = db.Column(
        db.Integer,
        default=0,
        nullable=False
    )
    otp_request_window_started_at = db.Column(db.DateTime)

    def clear_otp(self):
        self.otp_hash = None
        self.otp_expires_at = None
        self.otp_last_sent_at = None
        self.otp_attempts = 0


# =========================================================
# USER
# =========================================================

class User(db.Model, OTPFields):
    __tablename__ = "users"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    name = db.Column(
        db.String(100),
        nullable=False
    )

    email = db.Column(
        db.String(150),
        unique=True,
        nullable=False,
        index=True
    )

    # Kept for legacy/admin compatibility.
    # Normal users authenticate through email OTP.
    password_hash = db.Column(
        db.String(256),
        nullable=False
    )

    email_verified = db.Column(
        db.Boolean,
        default=True,
        nullable=False
    )

    is_admin = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    is_super_admin = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    daily_mock_limit = db.Column(
        db.Integer,
        default=3
    )

    mocks_taken_today = db.Column(
        db.Integer,
        default=0
    )

    last_reset_date = db.Column(
        db.Date,
        default=date.today
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    # Used by analytics to determine currently active users.
    last_seen_at = db.Column(
        db.DateTime,
        nullable=True,
        index=True
    )

    mocks = db.relationship(
        "Mock",
        backref="user",
        lazy=True
    )

    results = db.relationship(
        "Result",
        backref="user",
        lazy=True
    )

    def reset_daily_count_if_needed(self):
        if self.last_reset_date != date.today():
            self.mocks_taken_today = 0
            self.last_reset_date = date.today()
            db.session.commit()

    def can_take_mock(self):
        self.reset_daily_count_if_needed()

        return (
            self.is_admin
            or self.mocks_taken_today < self.daily_mock_limit
        )

    def to_dict(self):
        self.reset_daily_count_if_needed()

        unlimited = self.is_admin

        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,

            "is_admin": self.is_admin,

            "is_super_admin": self.is_super_admin,

            "email_verified": self.email_verified,

            "daily_mock_limit": self.daily_mock_limit,

            "mocks_taken_today": self.mocks_taken_today,

            "mocks_remaining": (
                None
                if unlimited
                else max(
                    0,
                    self.daily_mock_limit
                    - self.mocks_taken_today
                )
            ),

            "can_take_mock": self.can_take_mock(),

            "last_seen_at": (
                self.last_seen_at.isoformat()
                if self.last_seen_at
                else None
            ),

            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            )
        }


# =========================================================
# PENDING OTP
# =========================================================

class PendingOTP(db.Model, OTPFields):
    __tablename__ = "pending_otps"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    email = db.Column(
        db.String(150),
        unique=True,
        nullable=False,
        index=True
    )

    pending_name = db.Column(
        db.String(100),
        nullable=False
    )


# =========================================================
# OTP EVENT / AUDIT LOG
# =========================================================

class OTPEvent(db.Model):
    """
    Stores OTP delivery/audit metadata.

    IMPORTANT:
    We never store the plaintext OTP here.
    """

    __tablename__ = "otp_events"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    email = db.Column(
        db.String(150),
        nullable=False,
        index=True
    )

    requested_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    status = db.Column(
        db.String(50),
        nullable=False,
        default="requested"
    )

    brevo_message_id = db.Column(
        db.String(500),
        nullable=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True
    )


# =========================================================
# MOCK
# =========================================================

class Mock(db.Model):
    __tablename__ = "mocks"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    topic = db.Column(
        db.String(500),
        nullable=False
    )

    questions = db.Column(
        db.Text,
        nullable=False
    )

    timer_minutes = db.Column(
        db.Integer,
        nullable=False,
        default=15
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    results = db.relationship(
        "Result",
        backref="mock",
        lazy=True
    )

    def to_dict(self):
        import json

        return {
            "id": self.id,
            "user_id": self.user_id,
            "topic": self.topic,
            "questions": json.loads(
                self.questions
            ),
            "timer_minutes": (
                self.timer_minutes
                or 15
            ),
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            )
        }


# =========================================================
# RESULT
# =========================================================

class Result(db.Model):
    __tablename__ = "results"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    mock_id = db.Column(
        db.Integer,
        db.ForeignKey("mocks.id"),
        nullable=False,
        index=True
    )

    score = db.Column(
        db.Integer,
        nullable=False
    )

    total = db.Column(
        db.Integer,
        default=10
    )

    correct_answers = db.Column(
        db.Integer,
        nullable=False
    )

    wrong_answers = db.Column(
        db.Integer,
        nullable=False
    )

    user_answers = db.Column(
        db.Text,
        nullable=False
    )

    explanations = db.Column(
        db.Text,
        nullable=False,
        default="{}"
    )

    time_taken = db.Column(
        db.Integer,
        default=0
    )

    timestamp = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        index=True
    )

    def to_dict(self):
        import json

        total = self.total or 0

        percentage = (
            round(
                (self.score / total) * 100,
                1
            )
            if total > 0
            else 0
        )

        return {
            "id": self.id,
            "user_id": self.user_id,
            "mock_id": self.mock_id,
            "score": self.score,
            "total": self.total,
            "correct_answers": self.correct_answers,
            "wrong_answers": self.wrong_answers,
            "user_answers": json.loads(
                self.user_answers
            ),
            "explanations": json.loads(
                self.explanations or "{}"
            ),
            "time_taken": self.time_taken,
            "timestamp": (
                self.timestamp.isoformat()
                if self.timestamp
                else None
            ),
            "percentage": percentage
        }


# =========================================================
# USER TABLE MIGRATION
# =========================================================

def _add_missing_user_columns():
    columns = {
        c["name"]
        for c in inspect(db.engine).get_columns("users")
    }

    additions = {
        "email_verified":
            "BOOLEAN NOT NULL DEFAULT 1",

        "otp_hash":
            "VARCHAR(256)",

        "otp_expires_at":
            "DATETIME",

        "otp_attempts":
            "INTEGER NOT NULL DEFAULT 0",

        "otp_last_sent_at":
            "DATETIME",

        "otp_request_count":
            "INTEGER NOT NULL DEFAULT 0",

        "otp_request_window_started_at":
            "DATETIME",

        "is_super_admin":
            "BOOLEAN NOT NULL DEFAULT 0",

        "last_seen_at":
            "DATETIME"
    }

    for name, definition in additions.items():

        if name not in columns:

            db.session.execute(
                text(
                    f"""
                    ALTER TABLE users
                    ADD COLUMN {name} {definition}
                    """
                )
            )

    # Earlier versions used a default limit of 5.
    # Preserve explicitly customized limits but migrate the old
    # untouched default to the current 3/day product rule.
    db.session.execute(
        text(
            """
            UPDATE users
            SET daily_mock_limit = 3
            WHERE is_admin = 0
              AND daily_mock_limit = 5
            """
        )
    )

    db.session.commit()


# =========================================================
# MOCK TABLE MIGRATION
# =========================================================

def _add_missing_mock_columns():

    columns = {
        c["name"]
        for c in inspect(db.engine).get_columns("mocks")
    }

    if "timer_minutes" not in columns:

        db.session.execute(
            text(
                """
                ALTER TABLE mocks
                ADD COLUMN timer_minutes
                INTEGER NOT NULL DEFAULT 15
                """
            )
        )

        db.session.commit()


# =========================================================
# RESULT TABLE MIGRATION
# =========================================================

def _add_missing_result_columns():

    columns = {
        c["name"]
        for c in inspect(db.engine).get_columns("results")
    }

    if "explanations" not in columns:

        db.session.execute(
            text(
                """
                ALTER TABLE results
                ADD COLUMN explanations
                TEXT NOT NULL DEFAULT '{}'
                """
            )
        )

        db.session.commit()


# =========================================================
# OTP EVENTS TABLE MIGRATION
# =========================================================

def _add_missing_otp_event_table():

    inspector = inspect(db.engine)

    existing_tables = inspector.get_table_names()

    if "otp_events" in existing_tables:
        return

    db.session.execute(
        text(
            """
            CREATE TABLE otp_events (
                id INTEGER PRIMARY KEY,
                email VARCHAR(150) NOT NULL,
                requested_at DATETIME NOT NULL,
                status VARCHAR(50) NOT NULL DEFAULT 'requested',
                brevo_message_id VARCHAR(500),
                user_id INTEGER,
                FOREIGN KEY(user_id)
                    REFERENCES users(id)
            )
            """
        )
    )

    db.session.execute(
        text(
            """
            CREATE INDEX
            ix_otp_events_email
            ON otp_events(email)
            """
        )
    )

    db.session.execute(
        text(
            """
            CREATE INDEX
            ix_otp_events_requested_at
            ON otp_events(requested_at)
            """
        )
    )

    db.session.execute(
        text(
            """
            CREATE INDEX
            ix_otp_events_user_id
            ON otp_events(user_id)
            """
        )
    )

    db.session.commit()


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

def init_db():

    # Create all tables that don't exist yet.
    db.create_all()

    # Update existing installations.
    _add_missing_user_columns()

    _add_missing_mock_columns()

    _add_missing_result_columns()

    _add_missing_otp_event_table()

    # -----------------------------------------------------
    # Create legacy admin account only when no admin exists.
    # -----------------------------------------------------

    from werkzeug.security import generate_password_hash

    existing_admin = (
        User.query
        .filter_by(is_admin=True)
        .first()
    )

    if not existing_admin:

        db.session.add(
            User(
                name="Admin",
                email="admin@mockify.com",
                password_hash=generate_password_hash(
                    "admin123"
                ),
                is_admin=True,
                is_super_admin=False,
                email_verified=True,
                daily_mock_limit=999
            )
        )

        db.session.commit()