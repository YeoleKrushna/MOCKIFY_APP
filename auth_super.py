"""Mockify authentication with Brevo API OTPs and audit logging."""

from datetime import datetime, timedelta
import os
import re
import secrets

import requests
from flask import Blueprint, current_app, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from database import db, PendingOTP, User, OTPEvent


auth_bp = Blueprint("auth", __name__)

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_MAX_PER_HOUR = 5
IP_MAX_REQUESTS_PER_HOUR = 20

_ip_request_times = {}


def normalize_email(value):
    return (value or "").strip().lower()


def valid_email(email):
    return len(email) <= 150 and bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email))


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
    times = [t for t in _ip_request_times.get(ip, []) if t > now - timedelta(hours=1)]
    if len(times) >= IP_MAX_REQUESTS_PER_HOUR:
        _ip_request_times[ip] = times
        return True
    times.append(now)
    _ip_request_times[ip] = times
    return False


def make_otp():
    return f"{secrets.randbelow(900000) + 100000:06d}"


def send_otp_email(to_email, otp):
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM", "otp@mockify.tech").strip()
    sender_name = os.environ.get("MAIL_FROM_NAME", "Mockify").strip() or "Mockify"

    if not api_key or not sender:
        current_app.logger.error("Brevo configuration is incomplete.")
        return False, None

    payload = {
        "sender": {"name": sender_name, "email": sender},
        "to": [{"email": to_email}],
        "subject": "Your Mockify verification code",
        "textContent": (
            "Mockify\n\n"
            f"Your verification code is: {otp}\n\n"
            "This code expires in 10 minutes.\n\n"
            "If you did not request this code, you can safely ignore this email."
        ),
        "htmlContent": f"""
<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;background:#f5f5f7;padding:30px">
<div style="max-width:520px;margin:auto;background:white;padding:32px;border-radius:12px;color:#1c1c27">
<h2>Mockify</h2>
<p>Your verification code is:</p>
<div style="font-size:32px;font-weight:700;letter-spacing:8px;margin:24px 0">{otp}</div>
<p>This code expires in <strong>10 minutes</strong>.</p>
<p style="color:#777">If you did not request this code, you can safely ignore this email.</p>
</div></body></html>
""",
    }

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            json=payload,
            timeout=20,
        )

        if 200 <= response.status_code < 300:
            try:
                message_id = response.json().get("messageId")
            except ValueError:
                message_id = None
            current_app.logger.info("Brevo accepted OTP email for %s", to_email)
            return True, message_id

        current_app.logger.error(
            "Brevo OTP request failed. status=%s response=%s",
            response.status_code,
            response.text[:1000],
        )
        return False, None

    except requests.RequestException as exc:
        current_app.logger.error("Brevo OTP request failed: %s", exc)
        return False, None


def record_otp_event(email, status, message_id=None, user_id=None):
    try:
        db.session.add(
            OTPEvent(
                email=email,
                user_id=user_id,
                event_type="verification",
                status=status,
                brevo_message_id=message_id,
                requested_at=datetime.utcnow(),
                ip_address=client_ip(),
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to save OTP audit event")


def can_send_otp(record):
    if record is None:
        return None
    now = datetime.utcnow()
    if record.otp_last_sent_at and record.otp_last_sent_at > now - timedelta(seconds=OTP_RESEND_SECONDS):
        remaining = max(1, int(OTP_RESEND_SECONDS - (now - record.otp_last_sent_at).total_seconds()))
        return f"Please wait {remaining} seconds before requesting another code."
    if (
        record.otp_request_window_started_at
        and record.otp_request_window_started_at > now - timedelta(hours=1)
        and record.otp_request_count >= OTP_MAX_PER_HOUR
    ):
        return "Too many verification codes requested. Please try again later."
    return None


def save_otp(record, otp):
    now = datetime.utcnow()
    if not record.otp_request_window_started_at or record.otp_request_window_started_at <= now - timedelta(hours=1):
        record.otp_request_window_started_at = now
        record.otp_request_count = 0
    record.otp_hash = generate_password_hash(otp)
    record.otp_expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)
    record.otp_attempts = 0
    record.otp_last_sent_at = now
    record.otp_request_count += 1


def send_for(record, email, name=None):
    rate_error = can_send_otp(record)
    if rate_error:
        return jsonify({"error": rate_error}), 429
    if ip_rate_limited():
        return jsonify({"error": "Too many requests from this network. Please try again later."}), 429

    otp = make_otp()
    sent, message_id = send_otp_email(email, otp)

    if not sent:
        record_otp_event(email, "failed", None, getattr(record, "id", None) if isinstance(record, User) else None)
        return jsonify({"error": "We couldn't send the verification email. Please try again."}), 503

    if record is None:
        record = PendingOTP(email=email, pending_name=name or "")
        db.session.add(record)
    if name is not None:
        record.pending_name = name.strip()

    save_otp(record, otp)
    db.session.flush()

    record_otp_event(
        email,
        "accepted",
        message_id,
        record.id if isinstance(record, User) else None,
    )

    db.session.commit()

    return jsonify({"message": "Verification code sent successfully.", "expires_in": 600}), 200


@auth_bp.route("/register", methods=["POST"])
def register():
    data = request_data()
    name = (data.get("name") or "").strip()
    email = normalize_email(data.get("email"))

    if not name or len(name) > 100:
        return jsonify({"error": "Please provide a name up to 100 characters."}), 400
    if not valid_email(email):
        return jsonify({"error": "Please provide a valid email address."}), 400

    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"error": "Account already exists. Use login instead."}), 409

    pending = PendingOTP.query.filter_by(email=email).first()
    return send_for(pending, email, name=name)


@auth_bp.route("/request-otp", methods=["POST"])
def request_otp():
    data = request_data()
    email = normalize_email(data.get("email"))

    if not valid_email(email):
        return jsonify({"error": "Please provide a valid email address."}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "Account not found. Please register first."}), 404
    if not user.email_verified:
        return jsonify({"error": "Your email is not verified. Please register again."}), 403

    return send_for(user, email)


@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request_data()
    email = normalize_email(data.get("email"))
    otp = (data.get("otp") or "").strip()

    if not valid_email(email):
        return jsonify({"error": "Please provide a valid email address."}), 400
    if not re.fullmatch(r"\d{6}", otp):
        return jsonify({"error": "Enter a valid six-digit verification code."}), 400

    user = User.query.filter_by(email=email).first()
    record = user if user and user.email_verified else PendingOTP.query.filter_by(email=email).first()

    if not record or not record.otp_hash or not record.otp_expires_at:
        return jsonify({"error": "Verification code is invalid or has expired."}), 401

    now = datetime.utcnow()
    if record.otp_expires_at < now:
        record.clear_otp()
        db.session.commit()
        return jsonify({"error": "Verification code has expired. Request a new code."}), 401

    if record.otp_attempts >= OTP_MAX_ATTEMPTS:
        record.clear_otp()
        db.session.commit()
        return jsonify({"error": "Too many incorrect attempts. Request a new code."}), 429

    if not check_password_hash(record.otp_hash, otp):
        record.otp_attempts += 1
        remaining = OTP_MAX_ATTEMPTS - record.otp_attempts
        if remaining <= 0:
            record.clear_otp()
            db.session.commit()
            return jsonify({"error": "Too many incorrect attempts. Request a new code."}), 429
        db.session.commit()
        return jsonify({"error": f"Incorrect code. {remaining} attempt(s) remaining."}), 401

    if isinstance(record, PendingOTP):
        user = User(
            name=(record.pending_name or "Mockify User").strip(),
            email=email,
            password_hash=generate_password_hash(secrets.token_urlsafe(32)),
            email_verified=True,
            is_admin=False,
            is_super_admin=False,
            daily_mock_limit=3,
            mocks_taken_today=0,
            last_reset_date=now.date(),
            last_login_at=now,
            last_seen_at=now,
        )
        db.session.add(user)
        db.session.delete(record)
    else:
        user = record
        user.email_verified = True
        user.clear_otp()
        user.last_login_at = now
        user.last_seen_at = now
        user.reset_daily_count_if_needed()

    db.session.commit()

    session.clear()
    session["user_id"] = user.id
    session.permanent = True

    return jsonify({"message": "Verification successful.", "user": user.to_dict()}), 200


@auth_bp.route("/login", methods=["POST"])
def legacy_admin_login():
    data = request_data()
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""

    user = User.query.filter_by(email=email, is_admin=True).first()

    if not user or not password or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid admin credentials."}), 401

    now = datetime.utcnow()
    user.last_login_at = now
    user.last_seen_at = now
    db.session.commit()

    session.clear()
    session["user_id"] = user.id
    session.permanent = True

    return jsonify({"message": "Login successful.", "user": user.to_dict()}), 200


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully."}), 200


@auth_bp.route("/me", methods=["GET"])
def me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not authenticated."}), 401

    user = db.session.get(User, user_id)
    if not user:
        session.clear()
        return jsonify({"error": "User not found."}), 404

    user.last_seen_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"user": user.to_dict()}), 200
