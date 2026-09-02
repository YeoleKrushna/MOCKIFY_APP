"""Mockify authentication: registration OTP, password login, optional OTP login, and Google OAuth."""

from datetime import datetime, timedelta
import base64
import hashlib
import os
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, current_app, jsonify, request, session, redirect
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from werkzeug.security import check_password_hash, generate_password_hash

from database import db, PendingOTP, User, OTPEvent


auth_bp = Blueprint("auth", __name__)


# =========================================================
# CONFIGURATION
# =========================================================

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60
OTP_MAX_PER_HOUR = 5
IP_MAX_REQUESTS_PER_HOUR = 20
PASSWORD_MIN_LENGTH = 8
PASSWORD_SETUP_TTL_HOURS = 24
REGISTRATION_PASSWORD_SETUP_TTL_MINUTES = 30
PASSWORD_LOGIN_MAX_FAILURES = 10
PASSWORD_LOGIN_WINDOW_MINUTES = 15
GOOGLE_STATE_TTL_MINUTES = 10

_ip_request_times = {}
_password_login_failures = {}


# =========================================================
# HELPERS
# =========================================================

def normalize_email(value):
    return (value or "").strip().lower()


def valid_email(email):
    return (
        len(email) <= 150
        and bool(re.fullmatch(r"[A-Za-z0-9._%+-]+@gmail\.com", email, re.IGNORECASE))
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


def hash_setup_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_password(password):
    return isinstance(password, str) and PASSWORD_MIN_LENGTH <= len(password) <= 256


def password_login_rate_limited(email):
    now = datetime.utcnow()
    key = f"{client_ip()}|{email}"
    entry = _password_login_failures.get(key)
    if not entry:
        return False
    started_at, failures = entry
    if started_at <= now - timedelta(minutes=PASSWORD_LOGIN_WINDOW_MINUTES):
        _password_login_failures.pop(key, None)
        return False
    return failures >= PASSWORD_LOGIN_MAX_FAILURES


def record_password_failure(email):
    now = datetime.utcnow()
    key = f"{client_ip()}|{email}"
    entry = _password_login_failures.get(key)
    if not entry or entry[0] <= now - timedelta(minutes=PASSWORD_LOGIN_WINDOW_MINUTES):
        _password_login_failures[key] = [now, 1]
    else:
        entry[1] += 1


def clear_password_failures(email):
    _password_login_failures.pop(f"{client_ip()}|{email}", None)


def login_user(user):
    now = datetime.utcnow()
    user.last_login_at = now
    user.last_seen_at = now
    db.session.commit()
    session.clear()
    session["user_id"] = user.id
    session.permanent = True


def google_configured():
    return bool(
        os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    )


def google_redirect_uri():
    configured = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    return configured or f"{request.url_root.rstrip('/')}/api/auth/google/callback"


def establish_password_setup_session(user_id, ttl_seconds, token_hash=None):
    session.clear()
    session["password_setup_user_id"] = user_id
    session["password_setup_expires_at"] = int(time.time()) + ttl_seconds
    if token_hash:
        session["password_setup_token_hash"] = token_hash
    session.permanent = True


def password_setup_session_user():
    user_id = session.get("password_setup_user_id")
    expires_at = session.get("password_setup_expires_at")
    if not user_id or not expires_at or int(expires_at) < int(time.time()):
        session.pop("password_setup_user_id", None)
        session.pop("password_setup_expires_at", None)
        return None
    return db.session.get(User, int(user_id))


# =========================================================
# BREVO API
# =========================================================

def _brevo_send(subject, to_email, text_content, html_content):
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender = os.environ.get("MAIL_FROM", "otp@mockify.tech").strip()
    sender_name = os.environ.get("MAIL_FROM_NAME", "Mockify").strip() or "Mockify"

    if not api_key or not sender:
        current_app.logger.error("Brevo email configuration is missing.")
        return False, None

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            json={
                "sender": {"name": sender_name, "email": sender},
                "to": [{"email": to_email}],
                "subject": subject,
                "textContent": text_content,
                "htmlContent": html_content,
            },
            timeout=20,
        )
        if 200 <= response.status_code < 300:
            try:
                return True, response.json().get("messageId")
            except ValueError:
                return True, None

        current_app.logger.error(
            "Brevo rejected transactional email. HTTP status=%s",
            response.status_code,
        )
        return False, None
    except requests.RequestException:
        current_app.logger.exception("Brevo transactional email request failed.")
        return False, None


def send_otp_email(to_email, otp):
    text_content = (
        "Mockify\n\n"
        f"Your verification code is: {otp}\n\n"
        "This code expires in 10 minutes.\n\n"
        "If you did not request this code, you can safely ignore this email."
    )
    html_content = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Mockify Verification Code</title></head>
<body style="margin:0;padding:30px;background:#f5f5f7;font-family:Arial,sans-serif;">
<div style="max-width:520px;margin:0 auto;background:#ffffff;padding:32px;border-radius:12px;color:#1c1c27;">
<h2 style="margin-top:0;">Mockify</h2>
<p>Your verification code is:</p>
<div style="font-size:32px;font-weight:700;letter-spacing:8px;margin:24px 0;">{otp}</div>
<p>This code expires in <strong>10 minutes</strong>.</p>
<p style="color:#777;">If you did not request this code, you can safely ignore this email.</p>
</div></body></html>
"""
    return _brevo_send("Your Mockify verification code", to_email, text_content, html_content)


def send_password_setup_email(to_email, name, setup_url):
    safe_name = (name or "there").strip() or "there"
    text_content = (
        f"Hi {safe_name},\n\n"
        "Set a password for your Mockify account using the link below:\n\n"
        f"{setup_url}\n\n"
        "This link expires in 24 hours and can be used only once.\n\n"
        "If you did not expect this email, you can safely ignore it."
    )
    html_content = f"""
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Set your Mockify password</title></head>
<body style="margin:0;padding:30px;background:#f5f5f7;font-family:Arial,sans-serif;">
<div style="max-width:520px;margin:0 auto;background:#fff;padding:32px;border-radius:12px;color:#1c1c27;">
<h2>Mockify</h2>
<p>Hi {safe_name},</p>
<p>Your Mockify account is ready for password sign-in.</p>
<p style="margin:28px 0"><a href="{setup_url}" style="display:inline-block;background:#6c63ff;color:#fff;text-decoration:none;padding:13px 20px;border-radius:8px;font-weight:600">Set your password</a></p>
<p style="color:#666">This link expires in 24 hours and can be used only once.</p>
<p style="color:#777">If you did not expect this email, you can safely ignore it.</p>
</div></body></html>
"""
    return _brevo_send("Set your Mockify password", to_email, text_content, html_content)


# =========================================================
# OTP RATE LIMITING
# =========================================================

def can_send_otp(record):
    if record is None:
        return None
    now = datetime.utcnow()
    if record.otp_last_sent_at and record.otp_last_sent_at > now - timedelta(seconds=OTP_RESEND_SECONDS):
        elapsed = (now - record.otp_last_sent_at).total_seconds()
        remaining = max(1, int(OTP_RESEND_SECONDS - elapsed))
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
        db.session.add(OTPEvent(email=email, user_id=record.id if isinstance(record, User) else None, status="failed", ip_address=client_ip()))
        db.session.commit()
        return jsonify({"error": "We couldn't send the verification email. Please try again."}), 503

    if record is None:
        record = PendingOTP(email=email, pending_name=name or "")
        db.session.add(record)
    if name is not None:
        record.pending_name = name.strip()

    save_otp(record, otp)
    db.session.flush()
    db.session.add(OTPEvent(email=email, user_id=record.id if isinstance(record, User) else None, status="accepted", brevo_message_id=message_id, ip_address=client_ip()))
    db.session.commit()
    return jsonify({"message": "Verification code sent successfully.", "expires_in": OTP_TTL_MINUTES * 60}), 200


# =========================================================
# REGISTER
# =========================================================

@auth_bp.route("/register", methods=["POST"])
def register():
    data = request_data()
    name = (data.get("name") or "").strip()
    email = normalize_email(data.get("email"))

    if not name:
        return jsonify({"error": "Please enter your name."}), 400
    if len(name) > 100:
        return jsonify({"error": "Name must be 100 characters or fewer."}), 400
    if not valid_email(email):
        return jsonify({"error": "Please use a valid Gmail address ending with @gmail.com."}), 400

    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        if existing_user.email_verified and not existing_user.password_set:
            return jsonify({"error": "This account already exists. Use Sign In → Sign in with OTP to finish setting your password."}), 409
        return jsonify({"error": "Account already exists. Use Sign In instead."}), 409

    pending = PendingOTP.query.filter_by(email=email).first()
    return send_for(pending, email, name)


# =========================================================
# REQUEST LOGIN OTP
# =========================================================

@auth_bp.route("/request-otp", methods=["POST"])
def request_otp():
    data = request_data()
    email = normalize_email(data.get("email"))
    if not valid_email(email):
        return jsonify({"error": "Please use a valid Gmail address ending with @gmail.com."}), 400
    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "Account not found. Please register first."}), 404
    if not user.email_verified:
        return jsonify({"error": "Your email is not verified."}), 403
    return send_for(user, email)


# =========================================================
# VERIFY OTP
# =========================================================

@auth_bp.route("/verify-otp", methods=["POST"])
def verify_otp():
    data = request_data()
    email = normalize_email(data.get("email"))
    otp = (data.get("otp") or "").strip()

    if not valid_email(email):
        return jsonify({"error": "Please use a valid Gmail address ending with @gmail.com."}), 400
    if not re.fullmatch(r"\d{6}", otp):
        return jsonify({"error": "Enter a valid six-digit verification code."}), 400

    user = User.query.filter_by(email=email).first()
    record = user if user and user.email_verified else PendingOTP.query.filter_by(email=email).first()
    if not record or not record.otp_hash or not record.otp_expires_at:
        return jsonify({"error": "Verification code is invalid or has expired."}), 401

    now = datetime.utcnow()
    if record.otp_expires_at < now:
        record.clear_otp(); db.session.commit()
        return jsonify({"error": "Verification code has expired. Request a new code."}), 401

    if record.otp_attempts >= OTP_MAX_ATTEMPTS:
        record.clear_otp(); db.session.commit()
        return jsonify({"error": "Too many incorrect attempts. Request a new code."}), 429

    if not check_password_hash(record.otp_hash, otp):
        record.otp_attempts += 1
        remaining = OTP_MAX_ATTEMPTS - record.otp_attempts
        if remaining <= 0:
            record.clear_otp(); db.session.commit()
            return jsonify({"error": "Too many incorrect attempts. Request a new code."}), 429
        db.session.commit()
        return jsonify({"error": f"Incorrect code. {remaining} attempt(s) remaining."}), 401

    if isinstance(record, PendingOTP):
        user = User(
            name=(record.pending_name or "Mockify User").strip(),
            email=email,
            password_hash=generate_password_hash(secrets.token_urlsafe(32)),
            password_set=False,
            auth_provider="email",
            email_verified=True,
            is_admin=False,
            is_super_admin=False,
            daily_mock_limit=3,
            mocks_taken_today=0,
            last_reset_date=now.date(),
            last_seen_at=now,
        )
        db.session.add(user)
        db.session.delete(record)
        db.session.flush()
        db.session.commit()
        establish_password_setup_session(user.id, REGISTRATION_PASSWORD_SETUP_TTL_MINUTES * 60)
        return jsonify({"message":"Email verified. Set your password to finish registration.","needs_password_setup":True,"user":user.to_dict()}),200

    user = record
    user.email_verified = True
    user.clear_otp()
    user.last_seen_at = now
    if not user.password_set:
        db.session.commit()
        establish_password_setup_session(user.id, PASSWORD_SETUP_TTL_HOURS * 60 * 60)
        return jsonify({"message":"Email verified. Set your password to finish setup.","needs_password_setup":True,"user":user.to_dict()}),200

    user.last_login_at = now
    db.session.commit()
    session.clear(); session["user_id"] = user.id; session.permanent = True
    return jsonify({"message":"Verification successful.","user":user.to_dict()}),200


# =========================================================
# PASSWORD LOGIN
# =========================================================

@auth_bp.route("/login", methods=["POST"])
def password_login():
    data = request_data()
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""
    if not valid_email(email) or not password:
        return jsonify({"error":"Invalid email or password."}),401
    if password_login_rate_limited(email):
        return jsonify({"error":"Too many password attempts. Please try again later or use OTP sign-in."}),429
    user = User.query.filter_by(email=email).first()
    if not user or not user.email_verified or not user.password_set or not check_password_hash(user.password_hash,password):
        record_password_failure(email)
        return jsonify({"error":"Invalid email or password."}),401
    clear_password_failures(email)
    login_user(user)
    return jsonify({"message":"Login successful.","user":user.to_dict()}),200


# =========================================================
# PASSWORD SETUP
# =========================================================

@auth_bp.route("/prepare-password-setup", methods=["POST"])
def prepare_password_setup():
    token=(request_data().get("token") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,200}",token):
        return jsonify({"error":"This password setup link is invalid or expired."}),400
    token_hash = hash_setup_token(token)
    user = User.query.filter_by(password_setup_token_hash=token_hash).first()
    if (
        not user
        or not user.password_setup_expires_at
        or user.password_setup_expires_at < datetime.utcnow()
        or user.password_set
    ):
        return jsonify({"error": "This password setup link is invalid or expired."}), 400
    establish_password_setup_session(
        user.id,
        PASSWORD_SETUP_TTL_HOURS * 60 * 60,
        token_hash=token_hash,
    )
    return jsonify({"message": "Password setup link verified.", "name": user.name}), 200


@auth_bp.route("/set-password", methods=["POST"])
def set_password():
    data=request_data(); password=data.get("password") or ""; confirm=data.get("confirm_password") or ""
    if not valid_password(password): return jsonify({"error":f"Password must be between {PASSWORD_MIN_LENGTH} and 256 characters."}),400
    if password!=confirm: return jsonify({"error":"Passwords do not match."}),400
    user = password_setup_session_user()
    if not user:
        return jsonify({"error": "Your password setup session has expired. Start again from the setup link."}), 401

    setup_token_hash = session.get("password_setup_token_hash")
    if setup_token_hash:
        if (
            not user.password_setup_token_hash
            or not secrets.compare_digest(user.password_setup_token_hash, setup_token_hash)
            or not user.password_setup_expires_at
            or user.password_setup_expires_at < datetime.utcnow()
        ):
            session.clear()
            return jsonify({"error": "This password setup link is invalid or expired."}), 401

    user.password_hash=generate_password_hash(password)
    user.password_set=True
    user.password_setup_token_hash=None
    user.password_setup_expires_at=None
    user.email_verified=True
    if user.auth_provider=="google": user.auth_provider="mixed"
    login_user(user)
    return jsonify({"message":"Password set successfully.","user":user.to_dict()}),200


# =========================================================
# GOOGLE OAUTH
# =========================================================

@auth_bp.route("/google/start", methods=["GET"])
def google_start():
    if not google_configured(): return redirect("/?google_error=not_configured")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    session["google_oauth_state"] = state
    session["google_oauth_state_expires_at"] = int(time.time()) + GOOGLE_STATE_TTL_MINUTES * 60
    session["google_oauth_code_verifier"] = verifier
    session["google_oauth_nonce"] = nonce
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?"+urlencode(params))


@auth_bp.route("/google/callback", methods=["GET"])
def google_callback():
    if request.args.get("error"):
        session.clear(); return redirect("/?google_error=cancelled")
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected = session.get("google_oauth_state")
    expires = session.get("google_oauth_state_expires_at", 0)
    verifier = session.get("google_oauth_code_verifier")
    nonce = session.get("google_oauth_nonce")
    if (
        not code
        or not state
        or not expected
        or not verifier
        or not nonce
        or not secrets.compare_digest(state, expected)
        or int(expires) < int(time.time())
    ):
        session.clear()
        return redirect("/?google_error=invalid_state")

    try:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
                "redirect_uri": google_redirect_uri(),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=20,
        )
    except requests.RequestException:
        current_app.logger.exception("Google token exchange request failed.")
        session.clear()
        return redirect("/?google_error=token_exchange")

    if token_response.status_code != 200:
        current_app.logger.error("Google token exchange failed: HTTP %s", token_response.status_code)
        session.clear()
        return redirect("/?google_error=token_exchange")

    try:
        token_data = token_response.json()
    except ValueError:
        token_data = {}

    raw_id_token = token_data.get("id_token")
    if not raw_id_token:
        session.clear()
        return redirect("/?google_error=no_id_token")

    try:
        profile = id_token.verify_oauth2_token(
            raw_id_token,
            google_requests.Request(),
            os.environ["GOOGLE_CLIENT_ID"].strip(),
        )
    except Exception:
        current_app.logger.exception("Google ID token validation failed.")
        session.clear()
        return redirect("/?google_error=invalid_id_token")

    if (
        profile.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
        or profile.get("nonce") != nonce
    ):
        session.clear()
        return redirect("/?google_error=invalid_id_token")

    google_sub = str(profile.get("sub") or "").strip()
    email = normalize_email(profile.get("email"))
    verified = bool(profile.get("email_verified"))
    name = (profile.get("name") or profile.get("given_name") or "Mockify User").strip()

    if not google_sub or not verified or not valid_email(email):
        session.clear()
        return redirect("/?google_error=unsupported_account")
    user=User.query.filter_by(google_sub=google_sub).first()
    if user and user.email!=email: session.clear(); return redirect("/?google_error=account_mismatch")
    if not user: user=User.query.filter_by(email=email).first()
    if user:
        if user.google_sub and user.google_sub!=google_sub: session.clear(); return redirect("/?google_error=account_mismatch")
        user.google_sub=google_sub; user.auth_provider="mixed" if user.password_set else "google"; user.email_verified=True
        if not user.name.strip(): user.name=name[:100] or "Mockify User"
    else:
        user=User(name=name[:100] or "Mockify User",email=email,password_hash=generate_password_hash(secrets.token_urlsafe(32)),password_set=False,auth_provider="google",google_sub=google_sub,email_verified=True,is_admin=False,is_super_admin=False,daily_mock_limit=3,mocks_taken_today=0,last_reset_date=datetime.utcnow().date())
        db.session.add(user)
    # Google identity itself is sufficient authentication; do not force a password setup.
    for key in (
        "google_oauth_state",
        "google_oauth_state_expires_at",
        "google_oauth_code_verifier",
        "google_oauth_nonce",
    ):
        session.pop(key, None)
    db.session.flush()
    login_user(user)
    return redirect("/?google=success")


# =========================================================
# ADMIN: SEND PASSWORD SETUP LINKS
# =========================================================

@auth_bp.route("/admin/send-password-setup-links", methods=["POST"])
def admin_send_password_setup_links():
    requester=db.session.get(User,session.get("user_id")) if session.get("user_id") else None
    if not requester: return jsonify({"error":"Not authenticated."}),401
    if not requester.is_admin: return jsonify({"error":"Admin access required."}),403
    users=User.query.filter(User.email_verified.is_(True),User.password_set.is_(False),User.google_sub.is_(None)).all()
    sent_count=failed_count=0; now=datetime.utcnow(); base_url=request.url_root.rstrip("/")
    for user in users:
        raw=secrets.token_urlsafe(48)
        user.password_setup_token_hash=hash_setup_token(raw)
        user.password_setup_expires_at=now+timedelta(hours=PASSWORD_SETUP_TTL_HOURS)
        ok,_=send_password_setup_email(user.email,user.name,f"{base_url}/?password_setup={raw}")
        if ok: sent_count+=1
        else:
            failed_count+=1
            user.password_setup_token_hash=None
            user.password_setup_expires_at=None
    db.session.commit()
    return jsonify({"message":"Password setup emails processed.","sent":sent_count,"failed":failed_count}),200


# =========================================================
# LOGOUT
# =========================================================

@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message":"Logged out successfully."}),200


# =========================================================
# CURRENT USER
# =========================================================

@auth_bp.route("/me", methods=["GET"])
def me():
    user_id=session.get("user_id")
    if not user_id: return jsonify({"error":"Not authenticated."}),401
    user=db.session.get(User,user_id)
    if not user:
        session.clear(); return jsonify({"error":"User not found."}),404
    user.reset_daily_count_if_needed()
    return jsonify({"user":user.to_dict()}),200
