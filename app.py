import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, send_from_directory
from flask_cors import CORS


# =========================================================
# PATHS / ENV
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(
    BASE_DIR / ".env",
    override=True
)


# =========================================================
# IMPORT ROOT MODULES ONLY
# =========================================================

from auth import auth_bp
from mock import mock_bp
from admin import admin_bp
from results import results_bp
from analytics import analytics_bp
from database import db, init_db


# =========================================================
# APP
# =========================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = (
    os.environ.get("SECRET_KEY", "").strip()
    or secrets.token_urlsafe(32)
)

app.config["SQLALCHEMY_DATABASE_URI"] = (
    os.environ.get("DATABASE_URL", "").strip()
    or "sqlite:///mockify.db"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["SESSION_COOKIE_HTTPONLY"] = True

app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get(
        "SESSION_COOKIE_SECURE",
        "false"
    ).lower() == "true"
)

app.config["PERMANENT_SESSION_LIFETIME"] = int(
    os.environ.get(
        "SESSION_LIFETIME_SECONDS",
        "28800"
    )
)

app.config["TRUST_PROXY_HEADERS"] = (
    os.environ.get(
        "TRUST_PROXY_HEADERS",
        "false"
    ).lower() == "true"
)


# =========================================================
# CORS
# =========================================================

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "http://127.0.0.1:5000",
                "http://localhost:5000",
                "http://127.0.0.1:5500",
                "http://localhost:5500",
            ]
        }
    },
    supports_credentials=True,
)


# =========================================================
# DATABASE
# =========================================================

db.init_app(app)


# =========================================================
# BLUEPRINTS
# =========================================================

app.register_blueprint(
    auth_bp,
    url_prefix="/api/auth"
)

app.register_blueprint(
    mock_bp,
    url_prefix="/api/mock"
)

app.register_blueprint(
    admin_bp,
    url_prefix="/api/admin"
)

app.register_blueprint(
    results_bp,
    url_prefix="/api/results"
)

app.register_blueprint(
    analytics_bp,
    url_prefix="/api/analytics"
)


# =========================================================
# REQUEST DEBUG LOGGING
# =========================================================

@app.before_request
def log_request():
    print(
        f"\n>>> REQUEST {request.method} {request.path}",
        flush=True
    )

    if request.method in [
        "POST",
        "PUT",
        "PATCH"
    ]:
        print(
            ">>> CONTENT TYPE:",
            request.content_type,
            flush=True
        )


@app.after_request
def log_response(response):
    print(
        f"<<< RESPONSE {request.method} "
        f"{request.path} -> {response.status_code}",
        flush=True
    )

    return response


# =========================================================
# FRONTEND
# =========================================================

@app.route("/")
def serve_index():

    response = send_from_directory(
        BASE_DIR,
        "index.html"
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )

    response.headers["Pragma"] = "no-cache"

    return response


@app.route("/index.html")
def serve_index_html():

    response = send_from_directory(
        BASE_DIR,
        "index.html"
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )

    response.headers["Pragma"] = "no-cache"

    return response


@app.route("/result.html")
def serve_result_html():

    return send_from_directory(
        BASE_DIR,
        "result.html"
    )


@app.route("/exam.html")
def serve_exam_html():

    return send_from_directory(
        BASE_DIR,
        "exam.html"
    )


@app.route("/super-admin.html")
def serve_super_admin_html():

    return send_from_directory(
        BASE_DIR,
        "super_admin.html"
    )


# =========================================================
# PUBLIC STATS JAVASCRIPT
# =========================================================

@app.route("/public_stats.js")
def serve_public_stats_js():

    return send_from_directory(
        BASE_DIR,
        "public_stats.js"
    )


# =========================================================
# HEALTH
# =========================================================

@app.route("/api/health")
def health():

    return {
        "status": "ok",

        "auth_module": auth_bp.name,

        "analytics_module": analytics_bp.name,

        "brevo_configured": bool(
            os.environ.get(
                "BREVO_API_KEY",
                ""
            ).strip()
        ),

        "mail_from": os.environ.get(
            "MAIL_FROM",
            "otp@mockify.tech"
        ),
    }


# =========================================================
# DATABASE INIT
# =========================================================

with app.app_context():
    init_db()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    print(
        "=" * 60,
        flush=True
    )

    print(
        "MOCKIFY STARTUP",
        flush=True
    )

    print(
        "=" * 60,
        flush=True
    )

    print(
        "App file:",
        os.path.abspath(__file__),
        flush=True
    )

    print(
        "Working directory:",
        os.getcwd(),
        flush=True
    )

    print(
        "Auth module:",
        auth_bp,
        flush=True
    )

    print(
        "Analytics module:",
        analytics_bp,
        flush=True
    )

    print(
        "Brevo API key configured:",
        bool(
            os.environ.get(
                "BREVO_API_KEY",
                ""
            ).strip()
        ),
        flush=True
    )

    print(
        "Mail sender:",
        os.environ.get(
            "MAIL_FROM",
            "otp@mockify.tech"
        ),
        flush=True
    )

    print(
        "=" * 60,
        flush=True
    )

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_reloader=True
    )