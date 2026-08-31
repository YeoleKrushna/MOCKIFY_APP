"""Mockify email OTP authentication using Brevo HTTP API."""

from datetime import datetime, timedelta
import os
import re
import secrets

import requests
from flask import Blueprint, current_app, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from database import db, PendingOTP, User


auth_bp = Blueprint("auth", __name__)


# =========================================================
# CONFIGURATION
# =========================================================

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_MAX_PER_HOUR = 5
IP_MAX_REQUESTS_PER_HOUR = 20

_ip_request_times = {}


# =========================================================
# HELPERS
# =========================================================

def normalize_email(value):
    return (value or "").strip().lower()


def valid_email(email):
    return (
        len(email) <= 150
        and bool(
            re.fullmatch(
                r"[^\s@]+@[^\s@]+\.[^\s@]+",
                email
            )
        )
    )


def request_data():
    return request.get_json(silent=True) or {}


def client_ip():
    if current_app.config.get("TRUST_PROXY_HEADERS"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()

    return request.remote_addr or "unknown"


def ip_rate_limited():
    now = datetime.utcnow()
    ip = client_ip()

    times = [
        timestamp
        for timestamp in _ip_request_times.get(ip, [])
        if timestamp > now - timedelta(hours=1)
    ]

    if len(times) >= IP_MAX_REQUESTS_PER_HOUR:
        _ip_request_times[ip] = times
        return True

    times.append(now)
    _ip_request_times[ip] = times

    return False


def make_otp():
    return f"{secrets.randbelow(900000) + 100000:06d}"


# =========================================================
# BREVO API
# =========================================================

def send_otp_email(to_email, otp):
    """
    Send OTP using Brevo HTTP API.

    Only BREVO_API_KEY is used.
    No SMTP is used.
    """

    api_key = os.environ.get(
        "BREVO_API_KEY",
        ""
    ).strip()

    sender = os.environ.get(
        "MAIL_FROM",
        "otp@mockify.tech"
    ).strip()

    sender_name = (
        os.environ.get(
            "MAIL_FROM_NAME",
            "Mockify"
        ).strip()
        or "Mockify"
    )

    # -----------------------------------------------------
    # DEBUG
    # -----------------------------------------------------

    print(
        "\n========== MOCKIFY OTP EMAIL ==========",
        flush=True
    )

    print(
        "send_otp_email() CALLED",
        flush=True
    )

    print(
        "Recipient:",
        to_email,
        flush=True
    )

    print(
        "BREVO_API_KEY configured:",
        bool(api_key),
        flush=True
    )

    print(
        "Sender:",
        sender,
        flush=True
    )

    # Never print the API key or OTP.
    print(
        "========================================",
        flush=True
    )

    # -----------------------------------------------------
    # CONFIG VALIDATION
    # -----------------------------------------------------

    if not api_key:
        print(
            "ERROR: BREVO_API_KEY is missing.",
            flush=True
        )

        current_app.logger.error(
            "BREVO_API_KEY is missing."
        )

        return False

    if not sender:
        print(
            "ERROR: MAIL_FROM is missing.",
            flush=True
        )

        current_app.logger.error(
            "MAIL_FROM is missing."
        )

        return False

    # -----------------------------------------------------
    # PAYLOAD
    # -----------------------------------------------------

    payload = {
        "sender": {
            "name": sender_name,
            "email": sender,
        },
        "to": [
            {
                "email": to_email,
            }
        ],
        "subject": "Your Mockify verification code",
        "textContent": (
            "Mockify\n\n"
            f"Your verification code is: {otp}\n\n"
            "This code expires in 10 minutes.\n\n"
            "If you did not request this code, "
            "you can safely ignore this email."
        ),
        "htmlContent": f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Mockify Verification Code</title>
</head>

<body style="
    margin:0;
    padding:30px;
    background:#f5f5f7;
    font-family:Arial,sans-serif;
">

<div style="
    max-width:520px;
    margin:0 auto;
    background:#ffffff;
    padding:32px;
    border-radius:12px;
    color:#1c1c27;
">

    <h2 style="margin-top:0;">
        Mockify
    </h2>

    <p>
        Your verification code is:
    </p>

    <div style="
        font-size:32px;
        font-weight:700;
        letter-spacing:8px;
        margin:24px 0;
    ">
        {otp}
    </div>

    <p>
        This code expires in
        <strong>10 minutes</strong>.
    </p>

    <p style="color:#777;">
        If you did not request this code,
        you can safely ignore this email.
    </p>

</div>

</body>
</html>
""",
    }

    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }

    # -----------------------------------------------------
    # BREVO REQUEST
    # -----------------------------------------------------

    try:

        print(
            "Calling Brevo API...",
            flush=True
        )

        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers=headers,
            json=payload,
            timeout=20,
        )

        print(
            "Brevo HTTP status:",
            response.status_code,
            flush=True
        )

        print(
            "Brevo response:",
            response.text[:2000],
            flush=True
        )

        # -------------------------------------------------
        # SUCCESS
        # -------------------------------------------------

        if 200 <= response.status_code < 300:

            print(
                "SUCCESS: Brevo accepted OTP email.",
                flush=True
            )

            print(
                "========================================\n",
                flush=True
            )

            return True

        # -------------------------------------------------
        # FAILURE
        # -------------------------------------------------

        print(
            "ERROR: Brevo rejected OTP email.",
            flush=True
        )

        print(
            "========================================\n",
            flush=True
        )

        current_app.logger.error(
            "Brevo rejected OTP email. HTTP %s. Response: %s",
            response.status_code,
            response.text[:2000],
        )

        return False

    except requests.Timeout as exc:

        print(
            "ERROR: Brevo request timed out:",
            repr(exc),
            flush=True
        )

        return False

    except requests.ConnectionError as exc:

        print(
            "ERROR: Could not connect to Brevo:",
            repr(exc),
            flush=True
        )

        return False

    except requests.RequestException as exc:

        print(
            "ERROR: Brevo request failed:",
            repr(exc),
            flush=True
        )

        return False

    except Exception as exc:

        print(
            "UNEXPECTED ERROR in send_otp_email:",
            repr(exc),
            flush=True
        )

        current_app.logger.exception(
            "Unexpected OTP email error."
        )

        return False


# =========================================================
# OTP RATE LIMITING
# =========================================================

def can_send_otp(record):

    if record is None:
        return None

    now = datetime.utcnow()

    # 60 second resend cooldown
    if (
        record.otp_last_sent_at
        and record.otp_last_sent_at
        > now - timedelta(
            seconds=OTP_RESEND_SECONDS
        )
    ):

        elapsed = (
            now - record.otp_last_sent_at
        ).total_seconds()

        remaining = max(
            1,
            int(
                OTP_RESEND_SECONDS - elapsed
            )
        )

        return (
            f"Please wait {remaining} seconds "
            "before requesting another code."
        )

    # Maximum 5 requests per hour
    if (
        record.otp_request_window_started_at
        and record.otp_request_window_started_at
        > now - timedelta(hours=1)
        and record.otp_request_count
        >= OTP_MAX_PER_HOUR
    ):

        return (
            "Too many verification codes requested. "
            "Please try again later."
        )

    return None


# =========================================================
# SAVE OTP
# =========================================================

def save_otp(record, otp):

    now = datetime.utcnow()

    if (
        not record.otp_request_window_started_at
        or record.otp_request_window_started_at
        <= now - timedelta(hours=1)
    ):

        record.otp_request_window_started_at = now
        record.otp_request_count = 0

    # Store only hash
    record.otp_hash = generate_password_hash(
        otp
    )

    record.otp_expires_at = (
        now
        + timedelta(
            minutes=OTP_TTL_MINUTES
        )
    )

    record.otp_attempts = 0
    record.otp_last_sent_at = now
    record.otp_request_count += 1


# =========================================================
# SEND OTP WORKFLOW
# =========================================================

def send_for(record, email, name=None):

    print(
        "\n========== OTP REQUEST ==========",
        flush=True
    )

    print(
        "send_for() called",
        flush=True
    )

    print(
        "Email:",
        email,
        flush=True
    )

    print(
        "Existing OTP record:",
        record is not None,
        flush=True
    )

    print(
        "=================================",
        flush=True
    )

    rate_error = can_send_otp(record)

    if rate_error:

        print(
            "OTP RATE LIMITED:",
            rate_error,
            flush=True
        )

        return jsonify({
            "error": rate_error
        }), 429

    if ip_rate_limited():

        print(
            "OTP IP RATE LIMITED",
            flush=True
        )

        return jsonify({
            "error": (
                "Too many requests from this network. "
                "Please try again later."
            )
        }), 429

    otp = make_otp()

    print(
        "OTP generated.",
        flush=True
    )

    print(
        "Calling send_otp_email()...",
        flush=True
    )

    if not send_otp_email(
        email,
        otp
    ):

        print(
            "OTP EMAIL FAILED.",
            flush=True
        )

        return jsonify({
            "error": (
                "We couldn't send the verification email. "
                "Please try again."
            )
        }), 503

    print(
        "OTP EMAIL SUCCESS.",
        flush=True
    )

    # Create pending record only after Brevo accepted email.
    if record is None:

        record = PendingOTP(
            email=email,
            pending_name=name or "",
        )

        db.session.add(record)

    if name is not None:
        record.pending_name = name.strip()

    save_otp(
        record,
        otp
    )

    db.session.commit()

    print(
        "OTP saved successfully.",
        flush=True
    )

    return jsonify({
        "message": (
            "Verification code sent successfully."
        ),
        "expires_in": OTP_TTL_MINUTES * 60,
    }), 200


# =========================================================
# REGISTER
# =========================================================

@auth_bp.route(
    "/register",
    methods=["POST"]
)
def register():

    print(
        "\n******** /api/auth/register HIT ********",
        flush=True
    )

    data = request_data()

    name = (
        data.get("name") or ""
    ).strip()

    email = normalize_email(
        data.get("email")
    )

    print(
        "Registration email:",
        email,
        flush=True
    )

    if not name:

        return jsonify({
            "error": "Please enter your name."
        }), 400

    if len(name) > 100:

        return jsonify({
            "error": (
                "Name must be 100 characters "
                "or fewer."
            )
        }), 400

    if not valid_email(email):

        return jsonify({
            "error": (
                "Please provide a valid email address."
            )
        }), 400

    existing_user = User.query.filter_by(
        email=email
    ).first()

    if existing_user:

        print(
            "Registration rejected: account exists.",
            flush=True
        )

        return jsonify({
            "error": (
                "Account already exists. "
                "Use login instead."
            )
        }), 409

    pending = PendingOTP.query.filter_by(
        email=email
    ).first()

    return send_for(
        pending,
        email,
        name
    )


# =========================================================
# REQUEST LOGIN OTP
# =========================================================

@auth_bp.route(
    "/request-otp",
    methods=["POST"]
)
def request_otp():

    print(
        "\n******** /api/auth/request-otp HIT ********",
        flush=True
    )

    data = request_data()

    email = normalize_email(
        data.get("email")
    )

    if not valid_email(email):

        return jsonify({
            "error": (
                "Please provide a valid email address."
            )
        }), 400

    user = User.query.filter_by(
        email=email
    ).first()

    if not user:

        return jsonify({
            "error": (
                "Account not found. "
                "Please register first."
            )
        }), 404

    if not user.email_verified:

        return jsonify({
            "error": (
                "Your email is not verified. "
                "Please register again."
            )
        }), 403

    return send_for(
        user,
        email
    )


# =========================================================
# VERIFY OTP
# =========================================================

@auth_bp.route(
    "/verify-otp",
    methods=["POST"]
)
def verify_otp():

    print(
        "\n******** /api/auth/verify-otp HIT ********",
        flush=True
    )

    data = request_data()

    email = normalize_email(
        data.get("email")
    )

    otp = (
        data.get("otp") or ""
    ).strip()

    if not valid_email(email):

        return jsonify({
            "error": (
                "Please provide a valid email address."
            )
        }), 400

    if not re.fullmatch(
        r"\d{6}",
        otp
    ):

        return jsonify({
            "error": (
                "Enter a valid six-digit "
                "verification code."
            )
        }), 400

    user = User.query.filter_by(
        email=email
    ).first()

    if user and user.email_verified:

        record = user

    else:

        record = PendingOTP.query.filter_by(
            email=email
        ).first()

    if not record:

        return jsonify({
            "error": (
                "Verification code is invalid "
                "or has expired."
            )
        }), 401

    if (
        not record.otp_hash
        or not record.otp_expires_at
    ):

        return jsonify({
            "error": (
                "Verification code is invalid "
                "or has expired."
            )
        }), 401

    now = datetime.utcnow()

    if record.otp_expires_at < now:

        record.clear_otp()

        db.session.commit()

        return jsonify({
            "error": (
                "Verification code has expired. "
                "Request a new code."
            )
        }), 401

    if (
        record.otp_attempts
        >= OTP_MAX_ATTEMPTS
    ):

        record.clear_otp()

        db.session.commit()

        return jsonify({
            "error": (
                "Too many incorrect attempts. "
                "Request a new code."
            )
        }), 429

    if not check_password_hash(
        record.otp_hash,
        otp
    ):

        record.otp_attempts += 1

        remaining = (
            OTP_MAX_ATTEMPTS
            - record.otp_attempts
        )

        if remaining <= 0:

            record.clear_otp()

            db.session.commit()

            return jsonify({
                "error": (
                    "Too many incorrect attempts. "
                    "Request a new code."
                )
            }), 429

        db.session.commit()

        return jsonify({
            "error": (
                f"Incorrect code. "
                f"{remaining} attempt(s) remaining."
            )
        }), 401

    # =====================================================
    # NEW USER
    # =====================================================

    if isinstance(
        record,
        PendingOTP
    ):

        random_password = secrets.token_urlsafe(
            32
        )

        user = User(
            name=(
                record.pending_name
                or "Mockify User"
            ).strip(),

            email=email,

            password_hash=generate_password_hash(
                random_password
            ),

            email_verified=True,

            is_admin=False,

            daily_mock_limit=3,

            mocks_taken_today=0,

            last_reset_date=now.date(),
        )

        db.session.add(user)

        db.session.delete(record)

    # =====================================================
    # EXISTING USER LOGIN
    # =====================================================

    else:

        user = record

        user.email_verified = True

        user.clear_otp()

        user.reset_daily_count_if_needed()

    db.session.commit()

    # =====================================================
    # SESSION
    # =====================================================

    session.clear()

    session["user_id"] = user.id

    session.permanent = True

    print(
        "OTP verification successful for:",
        email,
        flush=True
    )

    return jsonify({
        "message": "Verification successful.",
        "user": user.to_dict(),
    }), 200


# =========================================================
# ADMIN PASSWORD LOGIN
# =========================================================

@auth_bp.route(
    "/login",
    methods=["POST"]
)
def legacy_admin_login():

    data = request_data()

    email = normalize_email(
        data.get("email")
    )

    password = (
        data.get("password")
        or ""
    )

    user = User.query.filter_by(
        email=email,
        is_admin=True
    ).first()

    if (
        not user
        or not password
        or not check_password_hash(
            user.password_hash,
            password
        )
    ):

        return jsonify({
            "error": "Invalid admin credentials."
        }), 401

    session.clear()

    session["user_id"] = user.id

    session.permanent = True

    return jsonify({
        "message": "Login successful.",
        "user": user.to_dict(),
    }), 200


# =========================================================
# LOGOUT
# =========================================================

@auth_bp.route(
    "/logout",
    methods=["POST"]
)
def logout():

    session.clear()

    return jsonify({
        "message": "Logged out successfully."
    }), 200


# =========================================================
# CURRENT USER
# =========================================================

@auth_bp.route(
    "/me",
    methods=["GET"]
)
def me():

    user_id = session.get(
        "user_id"
    )

    if not user_id:

        return jsonify({
            "error": "Not authenticated."
        }), 401

    user = db.session.get(
        User,
        user_id
    )

    if not user:

        session.clear()

        return jsonify({
            "error": "User not found."
        }), 404

    user.reset_daily_count_if_needed()

    return jsonify({
        "user": user.to_dict()
    }), 200