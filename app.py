"""Mockify Flask application entry point."""

import logging
import os
import secrets
import time
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
if os.environ.get("ENVIRONMENT", "development").strip().lower() != "production":
    load_dotenv(BASE_DIR / ".env", override=False)

from admin import admin_bp  # noqa: E402
from analytics import analytics_bp  # noqa: E402
from auth import auth_bp  # noqa: E402
from database import db, init_db  # noqa: E402
from feedback import feedback_bp  # noqa: E402
from mock import mock_bp  # noqa: E402
from results import results_bp  # noqa: E402


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


environment = os.environ.get("ENVIRONMENT", "development").strip().lower()
is_production = environment == "production"
secret_key = os.environ.get("SECRET_KEY", "").strip()
if is_production and not secret_key:
    raise RuntimeError("SECRET_KEY must be set when ENVIRONMENT=production.")

database_url = os.environ.get("DATABASE_URL", "").strip()
if database_url.startswith("postgres://"):
    database_url = "postgresql://" + database_url[len("postgres://"):]
if not database_url:
    database_url = f"sqlite:///{BASE_DIR / 'instance' / 'mockify.db'}"


app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret_key or secrets.token_urlsafe(32),
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.environ.get("SESSION_COOKIE_SAMESITE", "Lax"),
    SESSION_COOKIE_SECURE=env_bool("SESSION_COOKIE_SECURE", is_production),
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=int(os.environ.get("SESSION_LIFETIME_SECONDS", "28800"))),
    TRUST_PROXY_HEADERS=env_bool("TRUST_PROXY_HEADERS", is_production),
)

if app.config["TRUST_PROXY_HEADERS"]:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
default_origins = "https://mockify.tech" if is_production else "http://127.0.0.1:5000,http://localhost:5000,http://127.0.0.1:5500,http://localhost:5500"
cors_origins = [item.strip() for item in os.environ.get("CORS_ORIGINS", default_origins).split(",") if item.strip()]
CORS(app, resources={r"/api/*": {"origins": cors_origins}}, supports_credentials=True)

db.init_app(app)
for blueprint, prefix in ((auth_bp, "/api/auth"), (mock_bp, "/api/mock"), (admin_bp, "/api/admin"), (results_bp, "/api/results"), (analytics_bp, "/api/analytics"), (feedback_bp, "/api/feedback")):
    app.register_blueprint(blueprint, url_prefix=prefix)


@app.before_request
def request_started():
    request._mockify_started_at = time.monotonic()


@app.after_request
def secure_response(response):
    elapsed = time.monotonic() - getattr(request, "_mockify_started_at", time.monotonic())
    app.logger.info("%s %s -> %s (%.3fs)", request.method, request.path, response.status_code, elapsed)
    response.headers.update({
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "default-src 'self'; connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com https://openidconnect.googleapis.com; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com",
    })
    if is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/") and ("auth" in request.path or "admin" in request.path or "results" in request.path or "feedback" in request.path):
        response.headers["Cache-Control"] = "no-store, private"
    return response


def static_file(filename, mimetype=None, cache_control=None):
    response = send_from_directory(BASE_DIR, filename, mimetype=mimetype)
    # Explicit UTF-8 is essential for Hindi/Marathi text in static HTML.
    # It also prevents a proxy/browser from falling back to a legacy charset.
    if response.mimetype == "text/html":
        response.headers["Content-Type"] = "text/html; charset=utf-8"
    if cache_control:
        response.headers["Cache-Control"] = cache_control
    return response


@app.route("/")
@app.route("/index.html")
def serve_index():
    return static_file("index.html", cache_control="no-cache")


@app.route("/result.html")
def serve_result():
    return static_file("result.html", cache_control="no-store, private")


@app.route("/exam.html")
def serve_exam():
    return static_file("exam.html", cache_control="no-store, private")


@app.route("/super-admin.html")
def serve_super_admin():
    return static_file("super_admin.html", cache_control="no-store, private")


@app.route("/privacy.html")
def serve_privacy():
    return static_file("privacy.html", cache_control="no-store, private")


@app.route("/terms.html")
def serve_terms():
    return static_file("terms.html", cache_control="no-store, private")


@app.route("/public_stats.js")
def serve_public_stats():
    return static_file("public_stats.js", mimetype="application/javascript", cache_control="public, max-age=3600")


@app.route("/robots.txt")
def serve_robots():
    return static_file("robots.txt", mimetype="text/plain", cache_control="public, max-age=3600")


@app.route("/sitemap.xml")
def serve_sitemap():
    return static_file("sitemap.xml", mimetype="application/xml", cache_control="public, max-age=3600")


@app.route("/api/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        db.session.rollback()
        app.logger.exception("Health check database query failed")
        return jsonify({"status": "unhealthy"}), 503
    return jsonify({"status": "ok", "environment": environment}), 200


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=not is_production, use_reloader=not is_production)
